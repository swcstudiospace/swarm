//! `TaskStoreWriter::ingest` (U3.5e part 1) against the REAL Python oracle, and the writer-level guarantees of the unit.
//!
//! # The oracle corpus
//!
//! A small Python child (`DRIVER`, run once per key family) builds every scenario's Task Store with the real `TaskStore` API, copies the
//! file twice, runs the real `scripts/orch_status.py::ingest` on one copy and writes what it did (reply, refusal, events, inline
//! transition). The test then runs `ingest` on the other copy and compares, scenario by scenario: the four tables of the store
//! (`tasks`, `transitions`, `verdicts`, `artifacts`) and `sqlite_sequence`, cell by cell with the storage class and the exact bits of a
//! REAL; the ordered events; the `reconcile` log; the summary of the reply; the inline transition; the returned task row; and, for a
//! refusal, the code, message, details and the events emitted before it. The swarm clocks of the child stand still at `NOW` during the
//! recorded call, so nothing is masked: every `updated_at`, `ts`, `created_at` and `verdicts_since` must be equal.
//!
//! A scenario marked `not_started` is one Python answers (a result it cannot reproduce, a key shape, an approval whose key policy refuses
//! mid-run): the writer must refuse it BEFORE any write, as `E-DEP` `not_started`, and the store file must equal the one it was given.
//!
//! The keys of the families are random per run (`/dev/urandom`), live only in the environment of the Python child and in the
//! [`FixedApprovalSource`] of the test, and are written to no file.
//!
//! # The guard of F-7
//!
//! `an_approved_plan_is_never_applied_without_the_hosts_facts` and its siblings drive `apply_ops` and `apply_ops_approving` with
//! hand-built plans to APPROVED. They are the tests that fail when the guard of `ops.rs` is removed.

use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::LazyLock;

use gsd_codes::ErrorCode;
use gsd_envelope::Declined;
use gsd_route::pydumps::to_python_dumps_sorted;
use gsd_route::reconcile_plan::EmitEvent;
use gsd_route::result_parse::{parse_result, validate_result};
use gsd_route::transition_plan::{AuditRow, PlanEffect, TransitionPlan};
use gsd_route::{jstr, obj_get, parse_json, Json};
use gsd_taskwriter::{
    open_for_write, ApprovalKeys, ApprovalSource, FixedApprovalSource, IngestApplied, IngestCause,
    IngestError, IngestRequest, OpsCause, TaskStoreWriter, WriteState, WriterConfig, WriterOp,
};
use sqlx::sqlite::SqliteConnectOptions;
use sqlx::{Column, ConnectOptions, Connection, Row, SqliteConnection, TypeInfo, ValueRef};

/// The clock the oracle child stands still at for the recorded call.
const NOW: f64 = 3000.0;

const DRIVER: &str = r##""""The Python oracle of the ingest corpus (crates/gsd-taskwriter/tests/ingest.rs).

`python3 -c <this file> <family> <outdir>`, run from the repository root. For every scenario of the family it builds a REAL Task Store
file with the real `TaskStore` API (tasks driven through the state machine, verdicts signed with `swarm.gates.make_verdict` under the
key of the child's environment and stored with `TaskStore.record_verdict`), copies it to `python.db` and `rust.db`, runs the REAL
`scripts/orch_status.py::ingest` on `python.db` and writes what it did to `python.json`. The Rust test then runs `TaskStoreWriter::ingest`
on `rust.db` and compares the stores table by table and the events, the reply and the refusal one by one.

The clocks of `swarm.taskstore`, `swarm.gates` and `swarm.envelope` are one fake clock that stands still at NOW during the recorded
call, so every `updated_at`, `ts`, `created_at` and `verdicts_since` is exact and nothing needs a mask. No key is ever written: the
child's environment holds them, the files hold envelopes and signatures.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time as real_time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

REPO = Path(os.getcwd())
sys.path.insert(0, str(REPO))
os.environ["SWARM_PURE_PYTHON"] = "1"

from swarm import envelope, gates, taskstore  # noqa: E402
from swarm.errors import SwarmError  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("orch_status_oracle", REPO / "scripts" / "orch_status.py")
assert _SPEC is not None and _SPEC.loader is not None
orch = importlib.util.module_from_spec(_SPEC)
sys.modules["orch_status_oracle"] = orch
_SPEC.loader.exec_module(orch)

NOW = 3000.0
EARLIER = 2500.0
CREATED_AT = 500.0
REWORK_AT = 1000.0
BEFORE_REWORK = 900.25
ISSUED = 2000.25
CORR = "corr-a"
OTHER = "corr-b"

PATHS = {
    "CREATED": [],
    "VALIDATED": ["VALIDATED"],
    "PLANNED": ["VALIDATED", "PLANNED"],
    "CLAIMED": ["VALIDATED", "PLANNED", "CLAIMED"],
    "IN_PROGRESS": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"],
    "IN_REVIEW": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW"],
    "APPROVED": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW", "APPROVED"],
    "DONE": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW", "APPROVED", "DONE"],
    "BLOCKED": ["VALIDATED", "PLANNED", "CLAIMED", "BLOCKED"],
    "FAILED": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "FAILED"],
    "RETRY": ["VALIDATED", "PLANNED", "CLAIMED", "RETRY"],
    "ESCALATED": ["VALIDATED", "PLANNED", "CLAIMED", "BLOCKED", "ESCALATED"],
}
NO_GATES = {"gates": []}


class Clock:
    """Stand-in for the `time` module of the swarm modules: `time()` is the value last set."""

    def __init__(self) -> None:
        self.now = CREATED_AT

    def time(self) -> float:
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


class Ids:
    def __init__(self) -> None:
        self.count = 0

    def uuid4(self) -> uuid.UUID:
        self.count += 1
        digest = hashlib.sha256(f"ingest-oracle-uuid-{self.count}".encode("ascii")).digest()
        return uuid.UUID(int=int.from_bytes(digest[:16], "big"))


def patched(clock: Clock) -> contextlib.ExitStack:
    stack = contextlib.ExitStack()
    ids = Ids()
    for module in (taskstore, gates, envelope):
        stack.enter_context(mock.patch.object(module, "time", clock))
    stack.enter_context(mock.patch.object(envelope, "_now_iso", lambda: "2026-01-01T00:00:00.000Z"))
    stack.enter_context(mock.patch.object(uuid, "uuid4", ids.uuid4))
    stack.enter_context(mock.patch.object(TaskStore, "_notify_committed", lambda self: None))
    return stack


class FakePath:
    """What `ingest()` uses of a `Path`: `read_text`, `name` and `str()`; the text lands in `notes.meta`, so it is fixed."""

    name = "result.json"

    def __init__(self, text: str) -> None:
        self._text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text

    def __str__(self) -> str:
        return "/ingest/result.json"


def run_ingest(store: TaskStore, call: dict[str, Any], clock: Clock, at: float, scratch: Path) -> dict[str, Any]:
    """The real `orch_status.ingest` once at `at`, with its emits and its inline transition recorded."""
    events: list[dict[str, Any]] = []
    inline: list[list[Any]] = []

    def emit(event_type: str, payload: dict[str, Any], **kw: Any) -> None:
        assert set(kw) <= {"task_id"}, kw
        events.append({"type": event_type, "payload": payload, "task_id": kw.get("task_id")})

    def spy(_store: TaskStore, _task: dict[str, Any], from_state: str, to_state: str, **kw: Any) -> list[Any]:
        inline.append([from_state, to_state, kw.get("reason")])
        return []

    ctx = SimpleNamespace(task_id=call.get("ctx_task"), emit=emit, root=scratch)
    clock.now = at
    try:
        with mock.patch.object(orch, "emit_inline_transition", spy):
            done = orch.ingest(store, FakePath(call["text"]), ctx, approve=not call["advisory"])
        out: dict[str, Any] = {"kind": "ok", "summary": done["summary"], "reconcile": done["reconcile"], "task": done["task"],
                               "inline": inline[0]}
    except SwarmError as exc:
        out = {"kind": "error", "error": {"code": exc.code.value, "message": str(exc), "details": exc.details}}
    except Exception as exc:  # noqa: BLE001 - the oracle's crash is the recorded answer
        out = {"kind": "crash", "crash": type(exc).__name__}
    out["events"] = events
    return out


def res(task_id: str, state: str, **extra: Any) -> dict[str, Any]:
    return {"task_id": task_id, "state": state, **extra}


def out(uri: str, **extra: str) -> dict[str, str]:
    return {"kind": "code.backend", "uri": uri, "version": "1", "digest": "", **extra}


def call(result: dict[str, Any] | None = None, *, advisory: bool = False, ctx_task: str | None = None, text: str | None = None) -> dict[str, Any]:
    return {"text": json.dumps(result) if text is None else text, "advisory": advisory, "ctx_task": ctx_task}


class World:
    """One seeded Task Store file and the fake clock that drives it."""

    def __init__(self, path: Path, clock: Clock, scratch: Path) -> None:
        self.store = TaskStore(str(path))
        self.clock, self.scratch, self.seq = clock, scratch, 0
        clock.now = CREATED_AT

    def task(self, task_id: str, state: str = "IN_REVIEW", *, corr: str = CORR, depth: int = 1, deps: list[str] | None = None,
             notes: dict[str, Any] | None = None, risk: str = "low", capability: str = "code.backend", agent: str | None = "A05@seed") -> None:
        self.seq += 1
        self.clock.now = CREATED_AT + self.seq
        if state in ("APPROVED", "DONE") and "gates" not in (notes or {}):
            notes = {**(notes or {}), "gates": []}
        self.store.create(task_id=task_id, correlation_id=corr, capability=capability, title=task_id, dag_depth=depth, depends_on=deps or [],
                          notes=notes, risk_class=risk, agent_id=agent)
        self.clock.now = CREATED_AT + self.seq + 0.5
        for step in PATHS[state]:
            self.store.transition(task_id, step, actor="A01", reason="seed")

    def gate_task(self, task_id: str, gate: str, targets: list[str], state: str = "DONE", *, deps: list[str] | None = None, depth: int = 2,
                  corr: str = CORR) -> None:
        notes = {"gate": gate, "gate_for": targets, "gates": []}
        self.task(task_id, state, corr=corr, depth=depth, deps=deps if deps is not None else list(targets), notes=notes,
                  capability=f"gate.{gate}")

    def sign(self, target: str, gate: str, *, at: float = ISSUED, kind: str = "pass", issuer: str | None = None, expires_s: int = 86400,
             corr: str = CORR) -> dict[str, Any]:
        self.clock.now = at
        findings = [gates.make_finding("F-1", "major", "functional", "broken")] if kind == "fail" else None
        extra: dict[str, Any] = {}
        if issuer is not None:
            extra["gate_task"] = issuer
        if kind == "waive":
            extra["waived_by"] = "human"
        return gates.make_verdict(gate=gate, task_id=target, agent_id="A08@seed", findings=findings,
                                  verdict="waive" if kind == "waive" else None, runs={}, expires_s=expires_s, correlation_id=corr,
                                  extra=extra or None, audit=False)

    def verdict(self, target: str, gate: str, kind: str = "pass", *, at: float = ISSUED, stored_at: float | None = None,
                issuer: str | None = None, expires_s: int = 86400) -> None:
        env = self.sign(target, gate, at=at, kind=kind, issuer=issuer, expires_s=expires_s)
        self.clock.now = at if stored_at is None else stored_at
        self.store.record_verdict(target, env)

    def forged(self, target: str, gate: str, *, issuer: str | None = None) -> None:
        """A row the real flow would refuse to store: a signed envelope whose signature was tampered with."""
        env = self.sign(target, gate, issuer=issuer)
        env["sig"] = "0" * len(env["sig"])
        self.store.conn.execute(
            "INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig) VALUES (?,?,?,?,?,?,?,?,?)",
            (target, gate, "pass", "A08@seed", "[]", ISSUED + 86400, ISSUED, json.dumps(env), env["sig"]))
        self.store.conn.commit()

    def set_column(self, task_id: str, column: str, value: object) -> None:
        self.store.conn.execute(f"UPDATE tasks SET {column}=? WHERE task_id=?", (value, task_id))
        self.store.conn.commit()

    def cycle(self, task_id: str) -> None:
        """IN_PROGRESS -> IN_REVIEW -> CHANGES_REQUESTED -> IN_PROGRESS: a real rework, so `verdicts_since` is the production one."""
        self.clock.now = REWORK_AT
        for step in ("IN_REVIEW", "CHANGES_REQUESTED", "IN_PROGRESS"):
            self.store.transition(task_id, step, actor="A01", reason="seed rework")

    def first(self, request: dict[str, Any], **overrides: Any) -> None:
        """A real earlier ingest (advisory by default, so the task stays where the result put it), not recorded."""
        earlier = {**request, "advisory": True, **overrides}
        run_ingest(self.store, earlier, self.clock, EARLIER, self.scratch)

    def back(self, task_id: str) -> None:
        """IN_REVIEW -> CHANGES_REQUESTED -> IN_PROGRESS by another writer."""
        self.clock.now = EARLIER + 1
        for step in ("CHANGES_REQUESTED", "IN_PROGRESS"):
            self.store.transition(task_id, step, actor="A01", reason="cycle")


SCENARIOS: dict[str, tuple[str, str, Any]] = {}


def scenario(name: str, family: str = "hmac", rust: str = "plan") -> Any:
    def register(fn: Any) -> Any:
        assert name not in SCENARIOS, name
        SCENARIOS[name] = (family, rust, fn)
        return fn

    return register


# ---- heartbeats, notes and artifacts ---------------------------------------------------------------------------------------
@scenario("heartbeat-with-outputs")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS", outputs=[out("file://a"), out("file://b", kind="doc")], summary_md="working"))


@scenario("heartbeat-without-outputs")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS"))


@scenario("notes-merge-keeps-key-order")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes={"gates": [], "keep": 1, "result": "old", "z": [1, {"a": None}], "meta": {"old": True}})
    return call(res("T-1", "IN_PROGRESS", metrics={"cost": 0.1, "tiny": 1e-07, "big": 123456789.125, "n": 10**15}))


@scenario("artifact-defaults-and-unicode")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS", summary_md="naïve — ☃ \U0001f600", outputs=[{"uri": "é/файл.txt"}, {}, out("file://c", digest="abc")]))


@scenario("agent-id-of-the-result-claims-and-produces")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "PLANNED", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW", agent_id="A99@result", outputs=[out("file://p")]))


@scenario("gate-stall-already-recorded-is-quiet")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes={"gate_stall": ["review:absent"]})
    return call(res("T-1", "IN_REVIEW"))


@scenario("heartbeat-twice-with-different-results-keeps-both-keys")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    w.first(call(res("T-1", "IN_PROGRESS", summary_md="one")))
    return call(res("T-1", "IN_PROGRESS", summary_md="two"))


@scenario("ingest-keys-bound-drops-the-oldest")
def _(w: World) -> dict[str, Any]:
    keys = [{"task_id": "T-1", "attempt": 1, "digest": f"{n:064x}", "reply": "IN_PROGRESS"} for n in range(32)]
    w.task("T-1", "IN_PROGRESS", notes={"gates": [], "ingest_keys": keys})
    return call(res("T-1", "IN_PROGRESS", summary_md="the 33rd"))


# ---- the reported state ----------------------------------------------------------------------------------------------------
@scenario("in-review-without-gates-approves")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW", outputs=[out("file://done")]))


@scenario("in-review-default-gate-without-rows-stalls")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    return call(res("T-1", "IN_REVIEW"))


@scenario("in-review-advisory-skips-approval")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"), advisory=True)


@scenario("failed-result-records-the-error")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "FAILED", error={"code": "E-BOOM", "message": "it broke \u00e9"}))


@scenario("blocked-result-records-the-need")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "BLOCKED", needs="human approval"))


@scenario("blocked-needs-a-list-is-left-to-python", rust="not_started")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "BLOCKED", needs=["a", "b"]))


# ---- auto-claim and refusals before the first write ------------------------------------------------------------------------
@scenario("claim-planned-then-review-approves")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "PLANNED", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"))


@scenario("claim-retry-then-heartbeat")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "RETRY", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS"))


@scenario("claim-refused-for-unmet-dependencies")
def _(w: World) -> dict[str, Any]:
    w.task("D-1", "PLANNED", depth=0, notes=NO_GATES)
    w.task("T-1", "PLANNED", deps=["D-1"], notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"))


@scenario("claim-with-satisfied-dependency")
def _(w: World) -> dict[str, Any]:
    w.task("D-1", "DONE", depth=0)
    w.task("T-1", "PLANNED", deps=["D-1"], notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS"))


@scenario("blocked-task-refuses-a-result")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "BLOCKED", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS"))


@scenario("heartbeat-on-a-claimed-task-is-refused")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "CLAIMED", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS"))


@scenario("in-review-again-is-refused-by-the-state-machine")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_REVIEW", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"))


@scenario("invalid-result-missing-state")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(text=json.dumps({"task_id": "T-1"}))


@scenario("invalid-result-unknown-state")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "DONE"))


@scenario("result-names-another-task")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-9", "IN_PROGRESS"), ctx_task="T-1")


@scenario("no-json-block")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(text="the agent wrote prose only", ctx_task="T-1")


@scenario("unknown-task")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-404", "IN_PROGRESS"), ctx_task="T-404")


# ---- replays ---------------------------------------------------------------------------------------------------------------
@scenario("replay-heartbeat-is-deduplicated")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    request = call(res("T-1", "IN_PROGRESS", outputs=[out("file://once")]))
    w.first(request)
    return request


@scenario("replay-in-review-after-a-rework-cycle-is-deduplicated")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    request = call(res("T-1", "IN_REVIEW", outputs=[out("file://once")]))
    w.first(request)
    w.back("T-1")
    return request


@scenario("replay-of-a-unicode-and-float-result-is-deduplicated")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    metrics = {"b": [1, 2.5, 1e-07, 123456789.125, 10**15, -3, 0.1, True, None, -0.0], "a": {"z": 1, "y": {"k": 1e22}}}
    request = call(res("T-1", "IN_PROGRESS", summary_md="na\u00efve \u2014 \u2603 \U0001f600 \u2028 \"q\" \\ \t\x01 \x7f", metrics=metrics,
                       outputs=[{"uri": "\u00e9/\u0444.txt", "kind": "k"}], zebra=1, alpha=2))
    w.first(request)
    return request


@scenario("replay-after-approval-is-refused-by-the-state-machine")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    request = call(res("T-1", "IN_REVIEW", outputs=[out("file://once")]))
    w.first(request, advisory=False)
    return request


@scenario("replay-failed-is-refused-by-the-state-machine")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    request = call(res("T-1", "FAILED", error={"code": "E-X"}))
    w.first(request)
    return request


# ---- gate tasks ------------------------------------------------------------------------------------------------------------
@scenario("gate-task-script-not-run")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_REVIEW")
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    return call(res("G-review", "IN_REVIEW", verdicts={"T-1": {"verdict": "pass"}}))


@scenario("gate-task-review-verdict-mismatch")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_REVIEW")
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    w.verdict("T-1", "review", "pass", issuer="G-review")
    return call(res("G-review", "IN_REVIEW", verdicts={"T-1": {"verdict": "fail", "findings": []}}))


@scenario("gate-task-accepted-then-the-target-is-approved")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_REVIEW")
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    w.verdict("T-1", "review", "pass", issuer="G-review")
    return call(res("G-review", "IN_REVIEW", verdicts={"T-1": {"verdict": "pass"}}))


@scenario("gate-task-failing-rows-send-feedback-and-a-rework")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_REVIEW")
    w.task("T-next", "PLANNED", depth=3, deps=["G-review"], notes=NO_GATES)
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    w.verdict("T-1", "review", "fail", issuer="G-review")
    finding = {"id": "F-1", "severity": "major", "kind": "functional", "summary": "broken"}
    return call(res("G-review", "IN_REVIEW", verdicts={"T-1": {"verdict": "fail", "findings": [finding]}}))


@scenario("gate-task-with-two-targets-approves-both")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_REVIEW")
    w.task("T-2", "IN_REVIEW")
    w.gate_task("G-review", "review", ["T-1", "T-2"], "IN_PROGRESS")
    w.verdict("T-1", "review", "pass", issuer="G-review")
    w.verdict("T-2", "review", "pass", issuer="G-review")
    return call(res("G-review", "IN_REVIEW", verdicts={"T-1": {"verdict": "pass"}, "T-2": {"verdict": "approve"}}))


@scenario("gate-task-agent-failing-entry-without-a-major-finding-synthesizes-one")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_REVIEW")
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    w.verdict("T-1", "review", "fail", issuer="G-review")
    return call(res("G-review", "IN_REVIEW", verdicts={"T-1": {"verdict": "reject", "findings": [{"severity": "minor"}]}}))


# ---- reconcile -------------------------------------------------------------------------------------------------------------
@scenario("in-review-with-a-passing-row-approves")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.verdict("T-1", "review", "pass")
    return call(res("T-1", "IN_REVIEW"))


@scenario("in-review-held-by-a-leased-gate-task")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
    w.verdict("T-1", "review", "pass", issuer="G-review")
    return call(res("T-1", "IN_REVIEW"))


@scenario("in-review-whose-issuer-failed-is-not-approved")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.gate_task("G-review", "review", ["T-1"], "FAILED")
    w.verdict("T-1", "review", "pass", issuer="G-review")
    return call(res("T-1", "IN_REVIEW"))


@scenario("failing-gate-sends-a-rework-and-a-rerun")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.gate_task("G-review", "review", ["T-1"], "DONE")
    w.task("T-next", "PLANNED", depth=3, deps=["G-review"], notes=NO_GATES)
    w.verdict("T-1", "review", "fail", issuer="G-review")
    return call(res("T-1", "IN_REVIEW", outputs=[out("file://work")]))


@scenario("rework-cap-escalates")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.set_column("T-1", "rework_loops", 2)
    w.verdict("T-1", "review", "fail")
    return call(res("T-1", "IN_REVIEW"))


@scenario("medium-risk-with-both-gates-approves")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", risk="medium")
    w.verdict("T-1", "review", "pass")
    w.verdict("T-1", "quality", "pass")
    return call(res("T-1", "IN_REVIEW"))


@scenario("medium-risk-with-one-gate-missing-stalls")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", risk="medium")
    w.verdict("T-1", "review", "pass")
    return call(res("T-1", "IN_REVIEW"))


@scenario("expired-verdict-stalls")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.verdict("T-1", "review", "pass", expires_s=10)
    return call(res("T-1", "IN_REVIEW"))


@scenario("stale-verdict-stalls")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.cycle("T-1")
    w.verdict("T-1", "review", "pass", at=BEFORE_REWORK, stored_at=ISSUED)
    return call(res("T-1", "IN_REVIEW"))


@scenario("waive-verdict-approves")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.verdict("T-1", "review", "waive")
    return call(res("T-1", "IN_REVIEW"))


@scenario("forged-signature-stalls")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.forged("T-1", "review")
    return call(res("T-1", "IN_REVIEW"))


@scenario("a-heartbeat-approves-another-task-of-the-correlation")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    w.task("U-1", "IN_REVIEW")
    w.verdict("U-1", "review", "pass")
    return call(res("T-1", "IN_PROGRESS"))


@scenario("another-correlation-is-left-alone")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    w.task("X-1", "IN_REVIEW", corr=OTHER, notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"))


@scenario("agent-id-defaults-to-agent")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "PLANNED", notes=NO_GATES, agent=None)
    return call(res("T-1", "IN_PROGRESS", outputs=[out("file://x")]))


@scenario("null-notes-and-outputs-columns")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    w.set_column("T-1", "notes", None)
    w.set_column("T-1", "outputs", None)
    return call(res("T-1", "IN_PROGRESS", outputs=[out("file://n")]))


@scenario("rerun-id-taken-in-another-correlation-is-left-to-python", rust="not_started")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS")
    w.gate_task("G-review", "review", ["T-1"], "DONE")
    w.task("G-review.r1", "DONE", corr=OTHER, notes=NO_GATES)
    w.verdict("T-1", "review", "fail", issuer="G-review")
    return call(res("T-1", "IN_REVIEW"))


@scenario("unknown-risk-class-is-left-to-python", rust="not_started")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", risk="critical")
    return call(res("T-1", "IN_REVIEW"))


# ---- the other key sources -------------------------------------------------------------------------------------------------
for _family in ("ed25519", "dev"):

    @scenario(f"{_family}-signed-row-approves", family=_family)
    def _(w: World) -> dict[str, Any]:
        w.task("T-1", "IN_PROGRESS")
        w.verdict("T-1", "review", "pass")
        return call(res("T-1", "IN_REVIEW"))

    @scenario(f"{_family}-gate-task-accepted-then-the-target-is-approved", family=_family)
    def _(w: World) -> dict[str, Any]:
        w.task("T-1", "IN_REVIEW")
        w.gate_task("G-review", "review", ["T-1"], "IN_PROGRESS")
        w.verdict("T-1", "review", "pass", issuer="G-review")
        return call(res("G-review", "IN_REVIEW", verdicts={"T-1": {"verdict": "pass"}}))

    @scenario(f"{_family}-failing-row-reworks", family=_family)
    def _(w: World) -> dict[str, Any]:
        w.task("T-1", "IN_PROGRESS")
        w.gate_task("G-review", "review", ["T-1"], "DONE")
        w.verdict("T-1", "review", "fail", issuer="G-review")
        return call(res("T-1", "IN_REVIEW"))


@scenario("without-a-key-approval-is-left-to-python", family="nokey", rust="not_started")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"))


@scenario("without-a-key-advisory-is-served", family="nokey")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"), advisory=True)


@scenario("without-a-key-a-heartbeat-is-served", family="nokey")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS", outputs=[out("file://h")]))


@scenario("require-key-without-a-key-leaves-approval-to-python", family="require", rust="not_started")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_REVIEW"))


@scenario("a-key-file-reference-is-left-to-python", family="keyfile", rust="not_started")
def _(w: World) -> dict[str, Any]:
    w.task("T-1", "IN_PROGRESS", notes=NO_GATES)
    return call(res("T-1", "IN_PROGRESS"))


def main() -> None:
    family, outdir = sys.argv[1], Path(sys.argv[2])
    scratch = outdir / "_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    with patched(clock):
        for name, (scenario_family, rust, build) in SCENARIOS.items():
            if scenario_family != family:
                continue
            folder = outdir / name
            folder.mkdir()
            world = World(folder / "before.db", clock, scratch)
            request = build(world)
            world.store.conn.close()
            for copy in ("python.db", "rust.db"):
                shutil.copy(folder / "before.db", folder / copy)
            store = TaskStore(str(folder / "python.db"))
            outcome = run_ingest(store, request, clock, NOW, scratch)
            store.conn.close()
            (folder / "python.json").write_text(json.dumps(outcome))
            (folder / "spec.json").write_text(json.dumps({"name": name, "family": family, "rust": rust, **request}))


if __name__ == "__main__":
    main()
"##;

fn repo_root() -> PathBuf {
    match std::env::var_os("SWARM_REPO_ROOT") {
        Some(root) => PathBuf::from(root),
        None => Path::new(env!("CARGO_MANIFEST_DIR")).join("../.."),
    }
}

/// A random 64-hex value from the kernel, made at run time. It is never written to a file or printed.
fn random_hex() -> String {
    use std::io::Read;
    let mut bytes = [0u8; 32];
    std::fs::File::open("/dev/urandom")
        .expect("open /dev/urandom")
        .read_exact(&mut bytes)
        .expect("read /dev/urandom");
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// A Python child with a clean signing environment plus `vars`, run from the repository root.
fn python(script: &str, args: &[&Path], vars: &[(String, String)], home: &Path) -> String {
    let root = repo_root();
    assert!(
        root.join("swarm/taskstore.py").is_file(),
        "no swarm/taskstore.py under {}: set SWARM_REPO_ROOT",
        root.display()
    );
    let mut command = Command::new("python3");
    command
        .arg("-c")
        .arg(script)
        .args(args)
        .current_dir(&root)
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env("SWARM_GSD_STATE_FILE", "")
        .env("SWARM_CLAUDE_STATE_DIR", home.join("claude-state"))
        .env("SWARM_DIR", home.join(".swarm"))
        .env("HOME", home)
        .env_remove("XDG_CONFIG_HOME")
        .env_remove("SWARM_TASKSTORE_WAL")
        .env_remove("SWARM_SIGNING_KEY")
        .env_remove("SWARM_ED25519_KEY")
        .env_remove("SWARM_ED25519_KEY_FILE")
        .env_remove("SWARM_ALLOW_INSECURE_DEV_KEY")
        .env_remove("SWARM_REQUIRE_KEY")
        .env_remove("SWARM_AGENT_SESSION")
        .env_remove("SUBSTRATE_TOKEN")
        .env_remove("SUBSTRATE_URL");
    for (name, value) in vars {
        command.env(name, value);
    }
    let output = command.output().expect("run python3");
    assert!(
        output.status.success(),
        "python failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8_lossy(&output.stdout).into_owned()
}

/// The signing environment of one family, as variables for both the Python child and the writer's source.
fn family_vars(family: &str, home: &Path) -> Vec<(String, String)> {
    let mut vars = vec![("HOME".to_string(), home.to_string_lossy().into_owned())];
    match family {
        "hmac" => vars.push(("SWARM_SIGNING_KEY".to_string(), random_hex())),
        "ed25519" => vars.push(("SWARM_ED25519_KEY".to_string(), random_hex())),
        "dev" => vars.push(("SWARM_ALLOW_INSECURE_DEV_KEY".to_string(), "1".to_string())),
        "require" => vars.push(("SWARM_REQUIRE_KEY".to_string(), "1".to_string())),
        "keyfile" => vars.push((
            "SWARM_ED25519_KEY_FILE".to_string(),
            home.join("no-such-key-file").to_string_lossy().into_owned(),
        )),
        "nokey" => {}
        other => panic!("unknown family {other}"),
    }
    vars
}

// ---- raw SQL access, independent of the writer under test -----------------------------------------------------------------

async fn connect(path: &Path) -> SqliteConnection {
    SqliteConnectOptions::new()
        .filename(path)
        .connect()
        .await
        .expect("raw connection")
}

/// One SQLite value with its storage class; a REAL compares by bits.
#[derive(Debug, Clone, PartialEq)]
enum Cell {
    Null,
    Int(i64),
    Real(u64),
    Text(String),
    Blob(Vec<u8>),
}

fn show(cell: &Cell) -> String {
    let text = match cell {
        Cell::Null => "NULL".to_string(),
        Cell::Int(number) => number.to_string(),
        Cell::Real(bits) => format!("{}", f64::from_bits(*bits)),
        Cell::Text(text) => format!("{text:?}"),
        Cell::Blob(bytes) => format!("<{} bytes>", bytes.len()),
    };
    text.chars().take(240).collect()
}

struct Table {
    name: &'static str,
    columns: Vec<String>,
    rows: Vec<Vec<Cell>>,
}

type Content = Vec<Table>;

/// Every row of every table of the store (`SELECT *` in rowid order), values typed as stored: the whole content.
async fn content(path: &Path) -> Content {
    let mut conn = connect(path).await;
    let mut out = Vec::new();
    for name in [
        "tasks",
        "transitions",
        "verdicts",
        "artifacts",
        "sqlite_sequence",
    ] {
        let rows = sqlx::query(&format!("SELECT * FROM {name} ORDER BY rowid"))
            .fetch_all(&mut conn)
            .await
            .expect(name);
        let mut cells = Vec::new();
        let mut names: Vec<String> = Vec::new();
        for row in &rows {
            let mut line = Vec::new();
            names = row
                .columns()
                .iter()
                .map(|column| column.name().to_string())
                .collect();
            for index in 0..row.columns().len() {
                let raw = row.try_get_raw(index).expect("raw value");
                line.push(if raw.is_null() {
                    Cell::Null
                } else {
                    match raw.type_info().name() {
                        "INTEGER" => Cell::Int(row.get(index)),
                        "REAL" => Cell::Real(row.get::<f64, _>(index).to_bits()),
                        "TEXT" => Cell::Text(row.get(index)),
                        "BLOB" => Cell::Blob(row.get(index)),
                        other => panic!("{name}: unexpected storage class {other}"),
                    }
                });
            }
            cells.push(line);
        }
        out.push(Table {
            name,
            columns: names,
            rows: cells,
        });
    }
    conn.close().await.expect("close");
    out
}

/// The first difference between two store contents, named by table, row and column; `None` when they are equal.
fn diff(left: &Content, right: &Content) -> Option<String> {
    for (one, other) in left.iter().zip(right) {
        if one.rows.len() != other.rows.len() {
            return Some(format!(
                "{}: {} rows against {}",
                one.name,
                one.rows.len(),
                other.rows.len()
            ));
        }
        for (index, (row, other_row)) in one.rows.iter().zip(&other.rows).enumerate() {
            for (column, (cell, other_cell)) in row.iter().zip(other_row).enumerate() {
                if cell != other_cell {
                    let name = one.columns.get(column).map_or("?", String::as_str);
                    return Some(format!(
                        "{} row {index} column {name}: {} against {}",
                        one.name,
                        show(cell),
                        show(other_cell)
                    ));
                }
            }
        }
    }
    None
}

// ---- the oracle corpus -----------------------------------------------------------------------------------------------------

fn member<'a>(value: &'a Json, key: &str) -> &'a Json {
    match value {
        Json::Obj(fields) => obj_get(fields, key).unwrap_or_else(|| panic!("no key {key}")),
        other => panic!("not an object looking for {key}: {other:?}"),
    }
}

fn text(value: &Json) -> &str {
    match value {
        Json::Str(text) => text,
        other => panic!("not text: {other:?}"),
    }
}

fn flag(value: &Json) -> bool {
    matches!(value, Json::Bool(true))
}

fn read_json(path: &Path) -> Json {
    let raw = std::fs::read_to_string(path).expect("read json");
    parse_json(&raw).unwrap_or_else(|error| panic!("{}: {}", path.display(), error.message()))
}

fn sorted(value: &Json) -> String {
    to_python_dumps_sorted(value)
}

fn events_json(events: &[EmitEvent]) -> Json {
    Json::Arr(
        events
            .iter()
            .map(|event| {
                Json::Obj(vec![
                    ("type".to_string(), jstr(&event.event_type)),
                    ("payload".to_string(), Json::Obj(event.payload.clone())),
                    (
                        "task_id".to_string(),
                        event.task_id.as_deref().map_or(Json::Null, jstr),
                    ),
                ])
            })
            .collect(),
    )
}

/// What the route glue does before it calls the writer: `parse_result`, `tid`, `validate_result`. `None` when the glue itself would
/// leave the call to Python.
fn glue(spec: &Json) -> Option<IngestRequest> {
    let parsed = parse_result(text(member(spec, "text"))).ok()?;
    let task_id = match member(spec, "ctx_task") {
        Json::Str(task) => task.clone(),
        _ => match parsed.as_ref() {
            Some(Json::Obj(fields)) => match obj_get(fields, "task_id") {
                Some(Json::Str(task)) => task.clone(),
                _ => return None,
            },
            _ => return None,
        },
    };
    let result = validate_result(parsed.as_ref(), &task_id).cloned();
    Some(IngestRequest {
        task_id,
        result,
        meta: Json::Obj(vec![("ingest".to_string(), jstr("/ingest/result.json"))]),
        advisory: flag(member(spec, "advisory")),
    })
}

#[derive(Default, Debug)]
struct Tally {
    applied: usize,
    refused: usize,
    left_to_python: usize,
    replayed: usize,
}

/// Runs one scenario on `rust.db` and compares it with what Python did on `python.db`.
async fn replay(dir: &Path, vars: &[(String, String)], tally: &mut Tally) -> Result<(), String> {
    let spec = read_json(&dir.join("spec.json"));
    let name = text(member(&spec, "name")).to_string();
    let fail = |what: String| Err(format!("{name}: {what}"));
    let python = read_json(&dir.join("python.json"));
    let kind = text(member(&python, "kind")).to_string();
    let expected_not_started = text(member(&spec, "rust")) == "not_started";
    let before = content(&dir.join("before.db")).await;
    let Some(request) = glue(&spec) else {
        return fail("the glue left the call to Python".to_string());
    };
    let store = open_for_write(&WriterConfig::new(dir.join("rust.db")))
        .await
        .expect("open the store");
    let writer = TaskStoreWriter::new(store);
    let source = FixedApprovalSource::new(vars.to_vec());
    let outcome = writer.ingest(&request, NOW, &source).await;
    writer.close().await;
    let after = content(&dir.join("rust.db")).await;

    if expected_not_started {
        let Err(error) = outcome else {
            return fail("expected not_started, the writer applied it".to_string());
        };
        if error.write_state() != WriteState::NotStarted || error.code() != ErrorCode::Dep {
            return fail(format!("not a not_started refusal: {error}"));
        }
        if let Some(changed) = diff(&before, &after) {
            return fail(format!(
                "a not_started refusal changed the store: {changed}"
            ));
        }
        tally.left_to_python += 1;
        return Ok(());
    }

    let python_after = content(&dir.join("python.db")).await;
    match (kind.as_str(), outcome) {
        ("ok", Ok(applied)) => {
            if let Some(changed) = diff(&python_after, &after) {
                return fail(format!("the stores differ: {changed}"));
            }
            if !applied.replayed() && diff(&before, &after).is_none() {
                return fail("the oracle changed nothing: the scenario proves nothing".to_string());
            }
            let task_id = &request.task_id;
            let summary = format!(
                "ingested task.result for {task_id} \u{2192} {} (now {})",
                applied.reply,
                text(member(&Json::Obj(applied.task.clone()), "state"))
            );
            if summary != text(member(&python, "summary")) {
                return fail(format!(
                    "summary {summary:?} against {:?}",
                    member(&python, "summary")
                ));
            }
            let log = Json::Arr(applied.notes_log.iter().map(|line| jstr(line)).collect());
            if sorted(&log) != sorted(member(&python, "reconcile")) {
                return fail("the reconcile log differs".to_string());
            }
            let inline = Json::Arr(vec![
                jstr(&applied.before),
                jstr(&applied.after),
                jstr("ingest result.json"),
            ]);
            if sorted(&inline) != sorted(member(&python, "inline")) {
                return fail(format!(
                    "inline transition {} against {}",
                    sorted(&inline),
                    sorted(member(&python, "inline"))
                ));
            }
            if sorted(&Json::Obj(applied.task.clone())) != sorted(member(&python, "task")) {
                return fail("the returned task row differs".to_string());
            }
            if sorted(&events_json(&applied.events)) != sorted(member(&python, "events")) {
                return fail(format!(
                    "events {} against {}",
                    sorted(&events_json(&applied.events)),
                    sorted(member(&python, "events"))
                ));
            }
            tally.applied += 1;
            tally.replayed += usize::from(applied.replayed());
            Ok(())
        }
        ("error", Err(error)) => {
            if *error.cause() != IngestCause::Refused || error.write_state() != WriteState::Final {
                return fail(format!(
                    "not Python's own refusal: {:?} {error}",
                    error.cause()
                ));
            }
            let expected = member(&python, "error");
            if error.code().as_str() != text(member(expected, "code"))
                || error.message() != text(member(expected, "message"))
            {
                return fail(format!(
                    "refusal {error} against {}",
                    text(member(expected, "message"))
                ));
            }
            if sorted(&Json::Obj(error.details().to_vec())) != sorted(member(expected, "details")) {
                return fail("the refusal's details differ".to_string());
            }
            if sorted(&events_json(error.events())) != sorted(member(&python, "events")) {
                return fail("the events emitted before the refusal differ".to_string());
            }
            if let Some(changed) = diff(&before, &after) {
                return fail(format!("a refusal changed the store: {changed}"));
            }
            if let Some(changed) = diff(&before, &python_after) {
                return fail(format!("the oracle's refusal changed its store: {changed}"));
            }
            tally.refused += 1;
            Ok(())
        }
        (python_kind, outcome) => fail(format!(
            "the oracle says {python_kind}, the writer says {}",
            outcome.map_or_else(|error| format!("Err {error}"), |_| "Ok".to_string())
        )),
    }
}

/// Builds the family's scenarios with the Python oracle, replays each one and returns what went wrong.
async fn run_family(family: &str) -> (Vec<String>, Tally, usize) {
    let work = tempfile::tempdir().expect("tempdir");
    let home = work.path().join("home");
    std::fs::create_dir_all(&home).expect("home");
    let vars = family_vars(family, &home);
    let out = work.path().join("scenarios");
    std::fs::create_dir_all(&out).expect("out");
    python(DRIVER, &[Path::new(family), &out], &vars, &home);
    let mut dirs: Vec<PathBuf> = std::fs::read_dir(&out)
        .expect("scenarios")
        .map(|entry| entry.expect("entry").path())
        .filter(|path| path.join("spec.json").is_file())
        .collect();
    dirs.sort();
    let mut failures = Vec::new();
    let mut tally = Tally::default();
    for dir in &dirs {
        if let Err(why) = replay(dir, &vars, &mut tally).await {
            failures.push(why);
        }
    }
    (failures, tally, dirs.len())
}

#[tokio::test(flavor = "multi_thread")]
async fn hmac_corpus_matches_the_python_oracle() {
    let (failures, tally, count) = run_family("hmac").await;
    assert!(
        failures.is_empty(),
        "{} of {count} scenarios differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert!(count >= 40, "only {count} scenarios");
    assert!(
        tally.applied >= 28
            && tally.refused >= 8
            && tally.left_to_python >= 1
            && tally.replayed >= 2,
        "{tally:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn ed25519_corpus_matches_the_python_oracle() {
    let (failures, tally, count) = run_family("ed25519").await;
    assert!(
        failures.is_empty(),
        "{} of {count} scenarios differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_eq!((count, tally.applied), (3, 3), "{tally:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn dev_key_corpus_matches_the_python_oracle() {
    let (failures, tally, count) = run_family("dev").await;
    assert!(
        failures.is_empty(),
        "{} of {count} scenarios differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_eq!((count, tally.applied), (3, 3), "{tally:?}");
}

#[tokio::test(flavor = "multi_thread")]
async fn keyless_corpus_matches_the_python_oracle() {
    let (failures, tally, count) = run_family("nokey").await;
    assert!(
        failures.is_empty(),
        "{} of {count} scenarios differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
    assert_eq!(
        (count, tally.applied, tally.left_to_python),
        (3, 2, 1),
        "{tally:?}"
    );
}

#[tokio::test(flavor = "multi_thread")]
async fn require_key_and_key_file_references_are_left_to_python() {
    for family in ["require", "keyfile"] {
        let (failures, tally, count) = run_family(family).await;
        assert!(
            failures.is_empty(),
            "{family}: {} of {count} scenarios differ:\n{}",
            failures.len(),
            failures.join("\n")
        );
        assert_eq!((count, tally.left_to_python), (1, 1), "{family}: {tally:?}");
    }
}

// ---- the writer-level guarantees -------------------------------------------------------------------------------------------

/// The key of the writer-level fixtures: random per run, in the seeding child's environment and in the sources of the tests.
static KEY: LazyLock<String> = LazyLock::new(random_hex);

/// Seeds, through the real `TaskStore` and the real clock:
///
/// * correlation `C-1`: `H-1` (IN_PROGRESS, no gates), `R-1` (IN_PROGRESS, no gates), `F-1` (IN_PROGRESS, no gates), `P-1` (PLANNED, no gates);
/// * correlation `C-7`: `T-1` (IN_REVIEW, default gate `review`, a passing row issued by the gate task `G-review`), `G-review` (IN_PROGRESS,
///   `gate_for` `T-1`), `T-2` (IN_REVIEW, no gates), `T-3` (IN_REVIEW, default gate, no rows), `T-4` (IN_REVIEW, no gates).
const WRITER_SEED: &str = r#"
import sys
from swarm import gates
from swarm.taskstore import TaskStore
s = TaskStore(sys.argv[1])
PATH = {"PLANNED": ["VALIDATED", "PLANNED"], "IN_PROGRESS": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"],
        "IN_REVIEW": ["VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW"]}
def task(tid, state, corr, notes=None, **kw):
    s.create(task_id=tid, correlation_id=corr, capability=kw.pop("capability", "code.backend"), title=tid, notes=notes, agent_id="A05@seed", **kw)
    for step in PATH[state]:
        s.transition(tid, step, actor="A01", reason="seed")
for tid, state in (("H-1", "IN_PROGRESS"), ("R-1", "IN_PROGRESS"), ("F-1", "IN_PROGRESS"), ("P-1", "PLANNED")):
    task(tid, state, "C-1", notes={"gates": []})
task("T-1", "IN_REVIEW", "C-7")
task("G-review", "IN_PROGRESS", "C-7", notes={"gate": "review", "gate_for": ["T-1"], "gates": []}, capability="gate.review", depends_on=["T-1"])
task("T-2", "IN_REVIEW", "C-7", notes={"gates": []})
task("T-3", "IN_REVIEW", "C-7")
task("T-4", "IN_REVIEW", "C-7", notes={"gates": []})
env = gates.make_verdict(gate="review", task_id="T-1", agent_id="A08@seed", runs={}, correlation_id="C-7", extra={"gate_task": "G-review"}, audit=False)
s.record_verdict("T-1", env)
s.conn.close()
"#;

static TEMPLATE: LazyLock<Vec<u8>> = LazyLock::new(|| {
    let dir = tempfile::tempdir().expect("tempdir");
    let db = dir.path().join("tasks.db");
    let vars = vec![("SWARM_SIGNING_KEY".to_string(), KEY.clone())];
    python(WRITER_SEED, &[&db], &vars, dir.path());
    std::fs::read(&db).expect("read the seeded store")
});

/// A private copy of the seeded store.
struct Fixture {
    _dir: tempfile::TempDir,
    path: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        let path = dir.path().join("tasks.db");
        std::fs::write(&path, &*TEMPLATE).expect("copy the template");
        Self { _dir: dir, path }
    }

    async fn writer(&self) -> TaskStoreWriter {
        TaskStoreWriter::new(
            open_for_write(&WriterConfig::new(&self.path))
                .await
                .expect("open_for_write"),
        )
    }

    async fn content(&self) -> Content {
        content(&self.path).await
    }
}

/// The source of the writer-level tests: the run's key, with the real clock of the call.
fn keyed() -> FixedApprovalSource {
    FixedApprovalSource::new(vec![
        ("SWARM_SIGNING_KEY".to_string(), KEY.clone()),
        (
            "HOME".to_string(),
            "/nonexistent-home-for-ingest-tests".to_string(),
        ),
    ])
}

fn keyless() -> FixedApprovalSource {
    FixedApprovalSource::new(vec![(
        "HOME".to_string(),
        "/nonexistent-home-for-ingest-tests".to_string(),
    )])
}

/// The clock the fixtures were written with: verdicts issued by the seed are alive for a day from it.
fn clock() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_secs_f64()
}

/// The request of a result text, as the glue builds it.
fn request_of(task_id: &str, body: &str, advisory: bool) -> IngestRequest {
    let parsed = parse_result(body).expect("parse_result");
    IngestRequest {
        task_id: task_id.to_string(),
        result: validate_result(parsed.as_ref(), task_id).cloned(),
        meta: Json::Obj(vec![("ingest".to_string(), jstr("/ingest/result.json"))]),
        advisory,
    }
}

fn plan_to(from: &str, to: &str, reason: &str) -> TransitionPlan {
    TransitionPlan {
        from: from.to_string(),
        to: to.to_string(),
        columns: vec![("state".to_string(), jstr(to))],
        audit: vec![AuditRow {
            from: from.to_string(),
            to: to.to_string(),
            actor: "A01".to_string(),
            reason: reason.to_string(),
        }],
        effects: vec![PlanEffect::LiveRefresh],
    }
}

fn transition(task_id: &str, from: &str, to: &str) -> WriterOp {
    WriterOp::Transition {
        task_id: task_id.to_string(),
        actor: "A01".to_string(),
        plan: plan_to(from, to, "all required gates pass"),
    }
}

async fn state_of(path: &Path, task_id: &str) -> String {
    let mut conn = connect(path).await;
    let state: String = sqlx::query_scalar("SELECT state FROM tasks WHERE task_id=?")
        .bind(task_id)
        .fetch_one(&mut conn)
        .await
        .expect("state");
    conn.close().await.expect("close");
    state
}

async fn fixture_text(path: &Path, sql: &str) -> String {
    let mut conn = connect(path).await;
    let value: String = sqlx::query_scalar(sql)
        .fetch_one(&mut conn)
        .await
        .expect(sql);
    conn.close().await.expect("close");
    value
}

async fn count(path: &Path, sql: &str) -> i64 {
    let mut conn = connect(path).await;
    let value: i64 = sqlx::query_scalar(sql)
        .fetch_one(&mut conn)
        .await
        .expect(sql);
    conn.close().await.expect("close");
    value
}

fn assert_unchanged(before: &Content, after: &Content, what: &str) {
    assert_eq!(diff(before, after), None, "{what}: the store changed");
}

// ---- F-7: an APPROVED plan is never applied on the caller's word ------------------------------------------------------------

#[tokio::test]
async fn an_approved_plan_is_never_applied_without_the_hosts_facts() {
    let fixture = Fixture::new();
    let before = fixture.content().await;
    let writer = fixture.writer().await;

    // No approval context at all: refused in phase one, before the first write, whatever the rows say (T-2 needs no gate).
    let error = writer
        .apply_ops(&[transition("T-2", "IN_REVIEW", "APPROVED")], clock())
        .await
        .expect_err("apply_ops has no approval facts");
    assert_eq!(error.cause(), &OpsCause::ApprovalRefused);
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(error.code(), ErrorCode::Dep);
    assert_eq!(error.op_index(), Some(0));

    // The host's facts say the task lacks its gate (T-3 has no verdict row): the plan is refused although it is a valid plan.
    let error = writer
        .apply_ops_approving(
            &[transition("T-3", "IN_REVIEW", "APPROVED")],
            clock(),
            &keyed(),
        )
        .await
        .expect_err("T-3 lacks its review row");
    assert_eq!(error.cause(), &OpsCause::ApprovalRefused);
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert!(error.message().contains("lacks passing gates"), "{error}");

    // The key policy refuses it too: no key, no dev key.
    let error = writer
        .apply_ops_approving(
            &[transition("T-2", "IN_REVIEW", "APPROVED")],
            clock(),
            &keyless(),
        )
        .await
        .expect_err("no key");
    assert_eq!(error.cause(), &OpsCause::ApprovalRefused);
    assert!(
        error.message().contains("no signing key configured"),
        "{error}"
    );

    // A plan that passes through APPROVED to DONE in one step is not the host's decision either, whatever the gates say.
    let two_steps = WriterOp::Transition {
        task_id: "T-2".to_string(),
        actor: "A01".to_string(),
        plan: TransitionPlan {
            from: "IN_REVIEW".to_string(),
            to: "DONE".to_string(),
            columns: vec![("state".to_string(), jstr("DONE"))],
            audit: vec![
                AuditRow {
                    from: "IN_REVIEW".to_string(),
                    to: "APPROVED".to_string(),
                    actor: "A01".to_string(),
                    reason: "x".to_string(),
                },
                AuditRow {
                    from: "APPROVED".to_string(),
                    to: "DONE".to_string(),
                    actor: "A01".to_string(),
                    reason: "y".to_string(),
                },
            ],
            effects: vec![PlanEffect::LiveRefresh],
        },
    };
    let error = writer
        .apply_ops_approving(std::slice::from_ref(&two_steps), clock(), &keyed())
        .await
        .expect_err("a two-step plan through APPROVED");
    assert_eq!(error.cause(), &OpsCause::ApprovalRefused);
    let error = writer
        .apply_ops(&[two_steps], clock())
        .await
        .expect_err("a two-step plan through APPROVED, without facts");
    assert_eq!(error.cause(), &OpsCause::ApprovalRefused);
    writer.close().await;
    assert_unchanged(&before, &fixture.content().await, "every refusal");
}

#[tokio::test]
async fn an_approved_plan_the_hosts_facts_allow_is_applied() {
    let fixture = Fixture::new();
    let writer = fixture.writer().await;
    let applied = writer
        .apply_ops_approving(
            &[transition("T-2", "IN_REVIEW", "APPROVED")],
            clock(),
            &keyed(),
        )
        .await
        .expect("T-2 needs no gate and a key is configured");
    writer.close().await;
    assert_eq!(applied.audit.len(), 1);
    assert_eq!(applied.effects, vec![PlanEffect::LiveRefresh]);
    assert_eq!(state_of(&fixture.path, "T-2").await, "APPROVED");
}

#[tokio::test]
async fn an_approval_sees_the_rows_the_earlier_ops_of_its_own_list_left() {
    // T-1's passing row was issued by G-review, which is IN_PROGRESS (leased): the row is not accepted yet.
    let fixture = Fixture::new();
    let before = fixture.content().await;
    let writer = fixture.writer().await;
    let alone = writer
        .apply_ops_approving(
            &[transition("T-1", "IN_REVIEW", "APPROVED")],
            clock(),
            &keyed(),
        )
        .await
        .expect_err("the issuer's result was not accepted");
    assert_eq!(alone.cause(), &OpsCause::ApprovalRefused);
    assert!(alone.message().contains("unaccepted gates"), "{alone}");

    // The same list in the wrong order: the approval comes first, the issuer is accepted afterwards: refused, and nothing stays.
    let wrong_order = [
        transition("T-1", "IN_REVIEW", "APPROVED"),
        transition("G-review", "IN_PROGRESS", "IN_REVIEW"),
    ];
    let error = writer
        .apply_ops_approving(&wrong_order, clock(), &keyed())
        .await
        .expect_err("the issuer is still leased when T-1 is approved");
    assert_eq!(error.cause(), &OpsCause::ApprovalRefused);
    assert_eq!(error.op_index(), Some(0));
    assert_unchanged(&before, &fixture.content().await, "the refused lists");

    // In the right order the list itself makes the row count: the gate task is IN_REVIEW when T-1 is attested.
    let right_order = [
        transition("G-review", "IN_PROGRESS", "IN_REVIEW"),
        transition("T-1", "IN_REVIEW", "APPROVED"),
    ];
    writer
        .apply_ops_approving(&right_order, clock(), &keyed())
        .await
        .expect("the earlier op of the list accepts the issuer");
    writer.close().await;
    assert_eq!(state_of(&fixture.path, "G-review").await, "IN_REVIEW");
    assert_eq!(state_of(&fixture.path, "T-1").await, "APPROVED");
}

#[tokio::test]
async fn a_list_without_an_approval_never_reads_the_keys() {
    struct Spy(AtomicUsize);
    impl std::fmt::Debug for Spy {
        fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            write!(f, "Spy")
        }
    }
    impl ApprovalSource for Spy {
        fn keys(&self) -> Result<ApprovalKeys, Declined> {
            self.0.fetch_add(1, Ordering::SeqCst);
            Err(Declined::KeyFileReference)
        }
        fn clock(&self, requested_now: f64) -> f64 {
            requested_now
        }
    }
    let fixture = Fixture::new();
    let writer = fixture.writer().await;
    let spy = Spy(AtomicUsize::new(0));
    let ops = [WriterOp::SetNotes {
        task_id: "H-1".to_string(),
        set: vec![("a".to_string(), Json::Int(1))],
    }];
    writer
        .apply_ops_approving(&ops, clock(), &spy)
        .await
        .expect("a list with no APPROVED step reads no key");
    assert_eq!(spy.0.load(Ordering::SeqCst), 0);
    let error = writer
        .apply_ops_approving(&[transition("T-2", "IN_REVIEW", "APPROVED")], clock(), &spy)
        .await
        .expect_err("a declined key shape");
    writer.close().await;
    assert_eq!(spy.0.load(Ordering::SeqCst), 1, "the keys are read once");
    assert_eq!(error.write_state(), WriteState::NotStarted);
}

/// A source whose clock is `skew` seconds ahead of the call's own reading: the host's clock after the lock.
#[derive(Debug)]
struct Skewed {
    inner: FixedApprovalSource,
    skew: f64,
}

impl ApprovalSource for Skewed {
    fn keys(&self) -> Result<ApprovalKeys, Declined> {
        self.inner.keys()
    }
    fn clock(&self, requested_now: f64) -> f64 {
        requested_now + self.skew
    }
}

#[tokio::test]
async fn an_ingest_applies_nothing_when_the_host_cannot_attest_an_approval_the_planner_made() {
    // G-review's result is accepted by this very list and T-1 (default gate `review`, passing row issued by G-review) is then approved.
    // The planner decides it at the call's clock; the host's own clock, read inside the lock, is two days later and the verdict
    // (a day to live) has expired: the guard refuses the approval, and with it the whole list.
    let body =
        r#"{"task_id":"G-review","state":"IN_REVIEW","verdicts":{"T-1":{"verdict":"pass"}}}"#;
    let fixture = Fixture::new();
    let before = fixture.content().await;
    let writer = fixture.writer().await;
    let late = Skewed {
        inner: keyed(),
        skew: 2.0 * 86_400.0,
    };
    let error = writer
        .ingest(&request_of("G-review", body, false), clock(), &late)
        .await
        .expect_err("the host's clock finds the verdict expired");
    assert_eq!(*error.cause(), IngestCause::Ops(OpsCause::ApprovalRefused));
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(error.code(), ErrorCode::Dep);
    assert!(error.message().contains("lacks passing gates"), "{error}");
    assert_unchanged(&before, &fixture.content().await, "a refused approval list");

    // The same call with the host's clock at the call's own reading applies the whole list.
    // Python's oracle for this shape (`gate-task-accepted-then-the-target-is-approved`): the reply is the
    // result state `IN_REVIEW`, then reconcile finishes both tasks (`T-1: DONE`, `G-review: DONE`), so `after`
    // is `DONE`. A gate task with `gates: []` is approved once its own result is accepted.
    let applied = writer
        .ingest(&request_of("G-review", body, false), clock(), &keyed())
        .await
        .expect("the control applies");
    writer.close().await;
    assert_eq!(applied.reply, "IN_REVIEW");
    assert_eq!(applied.after, "DONE");
    assert_eq!(state_of(&fixture.path, "T-1").await, "DONE");
    assert_eq!(state_of(&fixture.path, "G-review").await, "DONE");
}

// ---- the keys of an ingest: once, before the lock --------------------------------------------------------------------------

/// Counts the calls of the writer and hands out the run's key.
struct Counting {
    inner: FixedApprovalSource,
    keys: AtomicUsize,
    clocks: AtomicUsize,
}

impl Counting {
    fn new(inner: FixedApprovalSource) -> Self {
        Self {
            inner,
            keys: AtomicUsize::new(0),
            clocks: AtomicUsize::new(0),
        }
    }
}

impl std::fmt::Debug for Counting {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Counting")
    }
}

impl ApprovalSource for Counting {
    fn keys(&self) -> Result<ApprovalKeys, Declined> {
        self.keys.fetch_add(1, Ordering::SeqCst);
        self.inner.keys()
    }
    fn clock(&self, requested_now: f64) -> f64 {
        self.clocks.fetch_add(1, Ordering::SeqCst);
        self.inner.clock(requested_now)
    }
}

#[tokio::test]
async fn an_ingest_reads_the_keys_once_and_the_clock_once_per_approval() {
    let fixture = Fixture::new();
    let writer = fixture.writer().await;
    let source = Counting::new(keyed());
    let applied = writer
        .ingest(
            &request_of("R-1", r#"{"task_id":"R-1","state":"IN_REVIEW"}"#, false),
            clock(),
            &source,
        )
        .await
        .expect("R-1 needs no gate");
    writer.close().await;
    assert_eq!(applied.after, "DONE");
    assert_eq!(source.keys.load(Ordering::SeqCst), 1);
    assert_eq!(
        source.clocks.load(Ordering::SeqCst),
        1,
        "one clock reading for the one APPROVED step"
    );
}

#[tokio::test]
async fn a_declined_key_shape_refuses_before_the_lock_is_asked_for() {
    let fixture = Fixture::new();
    let before = fixture.content().await;
    let mut holder = connect(&fixture.path).await;
    sqlx::query("BEGIN IMMEDIATE")
        .execute(&mut holder)
        .await
        .expect("hold the write lock");
    let mut config = WriterConfig::new(&fixture.path);
    config.busy_timeout = std::time::Duration::from_secs(60);
    let writer = TaskStoreWriter::new(open_for_write(&config).await.expect("open"));
    let declining = FixedApprovalSource::new(vec![
        (
            "SWARM_ED25519_KEY_FILE".to_string(),
            "/nonexistent/key-file".to_string(),
        ),
        (
            "HOME".to_string(),
            "/nonexistent-home-for-ingest-tests".to_string(),
        ),
    ]);
    // The lock is held and the busy timeout is a minute: only a refusal that comes before BEGIN IMMEDIATE returns at once.
    let outcome = tokio::time::timeout(
        std::time::Duration::from_secs(20),
        writer.ingest(
            &request_of("H-1", r#"{"task_id":"H-1","state":"IN_PROGRESS"}"#, false),
            clock(),
            &declining,
        ),
    )
    .await
    .expect("the refusal did not wait for the write lock");
    let error = outcome.expect_err("a key-file reference is Python's");
    assert_eq!(error.write_state(), WriteState::NotStarted);
    assert_eq!(error.code(), ErrorCode::Dep);
    sqlx::query("ROLLBACK")
        .execute(&mut holder)
        .await
        .expect("release");
    holder.close().await.expect("close");
    writer.close().await;
    assert_unchanged(&before, &fixture.content().await, "a declined key shape");
}

// ---- idempotency (design item 11, D-P24 (3)) --------------------------------------------------------------------------------

#[tokio::test]
async fn a_replayed_heartbeat_is_answered_with_the_stored_reply_and_writes_nothing() {
    let fixture = Fixture::new();
    let writer = fixture.writer().await;
    let body = r#"{"task_id":"H-1","state":"IN_PROGRESS","outputs":[{"kind":"code","uri":"file://once","version":"1","digest":"d"}]}"#;
    let first = writer
        .ingest(&request_of("H-1", body, false), clock(), &keyed())
        .await
        .expect("first application");
    assert!(!first.replayed());
    assert_eq!(first.reply, "IN_PROGRESS");
    assert_eq!(
        count(
            &fixture.path,
            "SELECT COUNT(*) FROM artifacts WHERE task_id='H-1'"
        )
        .await,
        1
    );
    let applied_once = fixture.content().await;

    let second = writer
        .ingest(&request_of("H-1", body, false), clock() + 5.0, &keyed())
        .await
        .expect("the replay is answered");
    writer.close().await;
    assert!(second.replayed());
    assert_eq!(second.reply, first.reply);
    let kinds: Vec<&str> = second
        .events
        .iter()
        .map(|event| event.event_type.as_str())
        .collect();
    assert_eq!(kinds, ["task.result.deduplicated"]);
    assert_eq!(second.applied.audit.len(), 0);
    // The replay key is the LAST write of the list: it follows `result` and `meta` in the notes the first application wrote.
    let notes_text =
        fixture_text(&fixture.path, "SELECT notes FROM tasks WHERE task_id='H-1'").await;
    let keys_of_notes: Vec<String> = match parse_json(&notes_text).expect("notes") {
        Json::Obj(fields) => fields.into_iter().map(|(key, _)| key).collect(),
        other => panic!("notes is not an object: {other:?}"),
    };
    assert_eq!(keys_of_notes, ["gates", "result", "meta", "ingest_keys"]);
    assert_eq!(
        count(
            &fixture.path,
            "SELECT COUNT(*) FROM artifacts WHERE task_id='H-1'"
        )
        .await,
        1,
        "artifact rows are not doubled"
    );
    assert_unchanged(
        &applied_once,
        &fixture.content().await,
        "a replay writes nothing (not even updated_at)",
    );
}

#[tokio::test]
async fn a_replay_the_state_machine_accepts_is_deduplicated_after_the_task_moved_on() {
    let fixture = Fixture::new();
    let writer = fixture.writer().await;
    let body = r#"{"task_id":"R-1","state":"IN_REVIEW","outputs":[{"uri":"file://r"}]}"#;
    let first = writer
        .ingest(&request_of("R-1", body, true), clock(), &keyed())
        .await
        .expect("advisory: the task stays IN_REVIEW");
    assert_eq!(
        (first.reply.as_str(), first.after.as_str()),
        ("IN_REVIEW", "IN_REVIEW")
    );
    // Another writer cycles the task back to IN_PROGRESS: IN_REVIEW is legal again, so the replay is answered, not applied.
    writer
        .apply_ops(
            &[
                transition("R-1", "IN_REVIEW", "CHANGES_REQUESTED"),
                transition("R-1", "CHANGES_REQUESTED", "IN_PROGRESS"),
            ],
            clock(),
        )
        .await
        .expect("the cycle");
    let before = fixture.content().await;
    let second = writer
        .ingest(&request_of("R-1", body, false), clock(), &keyed())
        .await
        .expect("the replay is answered");
    writer.close().await;
    assert!(second.replayed());
    assert_eq!(
        second.reply, "IN_REVIEW",
        "the stored reply, not the state the task is in now"
    );
    assert_eq!(second.after, "IN_PROGRESS");
    assert_unchanged(&before, &fixture.content().await, "the replay");
}

#[tokio::test]
async fn a_replay_after_a_transition_the_state_machine_refuses_is_python_s_refusal() {
    let fixture = Fixture::new();
    let writer = fixture.writer().await;
    let body = r#"{"task_id":"F-1","state":"FAILED","error":{"code":"E-X"},"outputs":[{"uri":"file://f"}]}"#;
    let first = writer
        .ingest(&request_of("F-1", body, false), clock(), &keyed())
        .await
        .expect("first application");
    assert_eq!(first.reply, "FAILED");
    let before = fixture.content().await;
    let error = writer
        .ingest(&request_of("F-1", body, false), clock() + 1.0, &keyed())
        .await
        .expect_err("FAILED -> FAILED is illegal, so the key does not answer it");
    writer.close().await;
    assert_eq!(*error.cause(), IngestCause::Refused);
    assert_eq!(error.write_state(), WriteState::Final);
    assert_eq!(error.code(), ErrorCode::Contract);
    assert_eq!(
        error.message(),
        "E-CONTRACT: illegal transition FAILED \u{2192} FAILED for F-1"
    );
    let kinds: Vec<&str> = error
        .events()
        .iter()
        .map(|event| event.event_type.as_str())
        .collect();
    assert_eq!(kinds, ["task.result.rejected"]);
    assert_unchanged(&before, &fixture.content().await, "the refused replay");
    assert_eq!(
        count(
            &fixture.path,
            "SELECT COUNT(*) FROM artifacts WHERE task_id='F-1'"
        )
        .await,
        1
    );
}

#[tokio::test]
async fn a_refusal_writes_nothing_and_carries_the_events_python_emitted() {
    let fixture = Fixture::new();
    let before = fixture.content().await;
    let writer = fixture.writer().await;
    let source = keyed();
    let unknown = writer
        .ingest(
            &request_of(
                "T-404",
                r#"{"task_id":"T-404","state":"IN_PROGRESS"}"#,
                false,
            ),
            clock(),
            &source,
        )
        .await
        .expect_err("unknown task");
    assert_eq!(unknown.message(), "E-INPUT: unknown task T-404");
    assert_eq!(unknown.write_state(), WriteState::Final);
    assert!(unknown.events().is_empty());
    assert_eq!(
        sorted(&Json::Obj(unknown.details().to_vec())),
        r#"{"task_id": "T-404"}"#
    );

    let invalid = writer
        .ingest(
            &request_of("H-1", r#"{"task_id":"H-1"}"#, false),
            clock(),
            &source,
        )
        .await
        .expect_err("a result without a state");
    assert_eq!(*invalid.cause(), IngestCause::Refused);
    assert_eq!(invalid.events().len(), 1);
    assert_eq!(invalid.events()[0].event_type, "task.result.rejected");
    assert_eq!(
        sorted(&Json::Obj(invalid.events()[0].payload.clone())),
        format!(
            r#"{{"mode": "ingest", "reason": {}, "task_id": "H-1"}}"#,
            sorted(&jstr(invalid.message()))
        )
    );
    writer.close().await;
    assert_unchanged(&before, &fixture.content().await, "the refusals");
}

#[test]
fn ingest_errors_are_plain_data_with_no_secret_in_their_text() {
    // The type is data only: `Debug` and `Display` print the refusal, never a value of the call.
    fn assert_traits<
        T: std::fmt::Debug + std::fmt::Display + std::error::Error + Clone + PartialEq,
    >() {
    }
    assert_traits::<IngestError>();
    fn assert_applied<T: std::fmt::Debug + Clone + PartialEq>() {}
    assert_applied::<IngestApplied>();
    let _: fn(&IngestError) -> &[EmitEvent] = IngestError::events;
}
