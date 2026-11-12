#!/usr/bin/env python3
"""Vectors for the pure Rust ingest planner (`crates/gsd-route/src/ingest_plan.rs`), generated from the REAL ingest route.

Every case seeds an in-memory `TaskStore` through the real API (the helpers of `gen_reconcile_vectors.py`: tasks are driven through the state
machine, verdicts are signed with `swarm.gates.make_verdict` and stored with `TaskStore.record_verdict`), records the facts the Rust planner
is given, runs the real code, and records what Python did:

* mode `ingest`: `scripts/orch_status.py::ingest`, i.e. `parse_result`, `validate_result`, `apply_result(mode="ingest")` and `reconcile`, with
  `apply_result` wrapped to record the `agent_id` and `meta` the route passes and `emit_inline_transition` replaced by a recorder (it writes
  run-log files; its arguments are the `before` and `after` states the plan reports);
* mode `headless`: `apply_result(mode="headless")`, or `reject(mode="headless")` for a result that does not validate (what `swarm_run.py` does);
* `tasks_before` / `expect.tasks`: every `tasks` row (`SELECT *`, rowid order, JSON columns still text); `verdicts`: every verdict row with
  `sig_ok`, the host's answer to "does the stored envelope verify"; `claims`: the `ts` of the task's CLAIMED transitions; `expect.transitions` and
  `expect.artifacts`: the rows the call wrote; `expect.events`: every `emit(event_type, payload, **kw)` call in order;
* `result`: `{"value": <the validated result>}` or `{"contract": <the text validate_result raised>}`; `digest`: `result_digest`;
* `expect.reply` and `notes_log`, `before` and `after` for a call that returned; `expect.error` (code, message, details) with the events emitted
  before it for a `SwarmError` Python raised, which leaves the store unchanged; `expect.python_error` for an exception that is not a `SwarmError`
  or a failure after a commit, which Rust must refuse (`not_started`);
* `rust: "not_started"`: Python succeeded (or raised a `SwarmError`) but the planner must still refuse: an input whose Python meaning it does
  not reproduce.

Determinism: the clocks of `swarm.taskstore`, `swarm.gates` and `swarm.envelope` are one fake clock; during the recorded call it stands still
at `NOW`, so every `updated_at`, `ts` and `created_at` is exact and the Rust replay compares the whole store without masks. Envelope ids come
from a counter, the `live.json` observer is off. Signing uses a throwaway HMAC key derived at run time from a seed phrase; the file holds
envelopes and signatures, never a key.

`python3 tests/parity/gen_ingest_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SWARM_PURE_PYTHON", "1")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_reconcile_vectors as base  # noqa: E402  (the seeding helpers: World, the fake clock, signing, key policy)

orch_status = _load("_orch_status_for_ingest", REPO / "scripts" / "orch_status.py")

from swarm import gates, results, taskstore  # noqa: E402
from swarm.errors import SwarmError  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "ingest.json"
SCHEMA = "gsd-route.ingest-vectors.v1"

Row = dict[str, Any]
NOW = base.NOW
EARLIER = 2500.0
STATES = base.STATES
CORR = base.CORR
REPORTED = ("IN_PROGRESS", "IN_REVIEW", "FAILED", "BLOCKED")
AGENT = "A05@seed"
ISSUED_A = 2100.0
ISSUED_B = 2200.0


def out(uri: str, **extra: str) -> dict[str, str]:
    """One `outputs` entry."""
    return {"kind": "code.backend", "uri": uri, "version": "1", "digest": "", **extra}


def res(task_id: str, state: str, **extra: Any) -> dict[str, Any]:
    """A `task.result` object with the members the cases need."""
    return {"task_id": task_id, "state": state, **extra}


def reported(task_id: str, state: str) -> dict[str, Any]:
    """A plain result for `state`, with the member Python reads for it."""
    extra: dict[str, Any] = {"outputs": [out(f"file://{task_id}")]}
    if state == "FAILED":
        extra["error"] = {"code": "E-BOOM", "message": "it broke"}
    if state == "BLOCKED":
        extra["needs"] = "human approval"
    return res(task_id, state, **extra)


class FakePath:
    """What `ingest()` uses of a `Path`: `read_text`, `name` and `str()`; the path text lands in `notes.meta`, so it is fixed."""

    def __init__(self, name: str, text: str) -> None:
        self.name, self._text = name, text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self._text

    def __str__(self) -> str:
        return f"/ingest/{self.name}"


class IngestWorld(base.World):
    """A seeded store plus the recorded call."""

    def __init__(self, name: str, group: str, clock: base._Clock) -> None:
        super().__init__(name, group, clock)
        self.expect_rust = "plan"

    # ---- recording --------------------------------------------------------------------------------------------------------
    def _facts(self, tid: str) -> Row:
        store = self.store
        return {
            "tasks_before": self.rows(),
            "verdicts": [{"id": r["id"], "task_id": r["task_id"], "gate": r["gate"], "verdict": r["verdict"],
                          "envelope_json": r["envelope_json"], "ts": r["ts"], "sig_ok": base.verifies(r["envelope_json"])}
                         for r in store.conn.execute("SELECT * FROM verdicts ORDER BY id")],
            "claims": [h["ts"] for h in store.history(tid) if h["to_state"] == "CLAIMED"],
            "transitions_before_max": store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM transitions").fetchone()[0],
            "artifacts_before_max": store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM artifacts").fetchone()[0],
        }

    def call(self, tid: str, source: dict[str, Any] | str, *, mode: str = "ingest", advisory: bool = False, agent: str = AGENT,
             at: float = NOW, keys: bool = True, record: bool = True) -> Row:
        """Runs the real route once at `at`; with `record` returns the case, without it only drives the store (a first application)."""
        store = self.store
        text = source if isinstance(source, str) else json.dumps(source)
        parsed = results.parse_result(text)
        invalid: str | None = None
        result: dict[str, Any] | None = None
        try:
            result = results.validate_result(parsed, task_id=tid)
        except SwarmError as exc:
            invalid = str(exc)
        facts = self._facts(tid)
        events: list[Row] = []
        inline: list[Row] = []
        seen: list[Row] = []

        def emit(event_type: str, payload: dict[str, Any], **kw: Any) -> None:
            assert set(kw) <= {"task_id"}, kw
            events.append({"type": event_type, "payload": payload, "task_id": kw.get("task_id")})

        def spy(st: taskstore.TaskStore, task: Row, **kw: Any) -> str:
            entry: Row = {"agent_id": kw["agent_id"], "meta": kw["meta"]}
            seen.append(entry)
            reply: str = results.apply_result(st, task, **kw)
            entry["reply"] = reply
            return reply

        def recorder(st: taskstore.TaskStore, task: Row, before: str, after: str, **kw: Any) -> list[Any]:
            inline.append({"task_id": task["task_id"], "before": before, "after": after, "reason": kw.get("reason")})
            return []

        self.clock.now = at
        expect: Row
        before_state = store.get(tid)["state"]
        env_patch = {"SWARM_SIGNING_KEY": ""} if not keys else {}
        with mock.patch.dict(os.environ, env_patch):
            if not keys:
                os.environ.pop("HOME", None)
                os.environ.pop("XDG_CONFIG_HOME", None)
            policy = base.key_policy()
            try:
                if mode == "ingest":
                    ctx = SimpleNamespace(task_id=tid, emit=emit, root=None)
                    with (mock.patch.object(orch_status, "apply_result", spy),
                          mock.patch.object(orch_status, "emit_inline_transition", recorder)):
                        reply_doc = orch_status.ingest(store, FakePath("result.json", text), ctx, approve=not advisory)
                    expect = {"reply": seen[0]["reply"], "notes_log": reply_doc["reconcile"]}
                    expect["before"], expect["after"] = inline[0]["before"], inline[0]["after"]
                    expect["inline_reason"] = inline[0]["reason"]
                elif invalid is not None:
                    task = store.get(tid)
                    reply = results.reject(store, tid, attempt=task["attempt"], reason=invalid, mode="headless", emit=emit)
                    expect = {"reply": reply, "notes_log": [], "before": before_state, "after": store.get(tid)["state"]}
                else:
                    assert result is not None
                    task = store.get(tid)
                    meta = {"runner": "headless"}
                    seen.append({"agent_id": agent, "meta": meta})
                    reply = results.apply_result(store, task, agent_id=agent, result=result, meta=meta, emit=emit, mode="headless")
                    expect = {"reply": reply, "notes_log": [], "before": before_state, "after": store.get(tid)["state"]}
            except SwarmError as exc:
                expect = {"error": {"code": exc.code.value, "message": str(exc), "details": exc.details}}
            except Exception as exc:  # noqa: BLE001 - the oracle's crash is the recorded answer
                expect = {"python_error": type(exc).__name__}
        if not record:
            return {}
        expect["events"] = events
        expect["tasks"] = self.rows()
        expect["transitions"] = [dict(r) for r in store.conn.execute("SELECT * FROM transitions WHERE id > ? ORDER BY id",
                                                                    (facts["transitions_before_max"],))]
        expect["artifacts"] = [dict(r) for r in store.conn.execute("SELECT * FROM artifacts WHERE id > ? ORDER BY id",
                                                                  (facts["artifacts_before_max"],))]
        if "error" in expect and self.expect_rust == "plan" and expect["tasks"] != facts["tasks_before"]:
            raise AssertionError(f"{self.name}: a refusal changed the store")
        case: Row = {
            "name": self.name, "group": self.group, "mode": mode, "advisory": advisory, "now": at, "task_id": tid,
            "correlation_id": CORR, "key_policy": policy, "rust": self.expect_rust,
            "agent_id": seen[0]["agent_id"] if seen else None, "meta": seen[0]["meta"] if seen else None,
            "result": {"contract": invalid} if invalid is not None else {"value": result},
            "digest": None if result is None else results.result_digest(result), **facts, "expect": expect}
        store.conn.close()
        return case

    # ---- first applications -----------------------------------------------------------------------------------------------
    def first(self, tid: str, source: dict[str, Any] | str, **kw: Any) -> None:
        """A real earlier call (ingest, advisory by default so the task stays where the result put it), not recorded."""
        kw.setdefault("advisory", True)
        self.call(tid, source, at=EARLIER, record=False, **kw)

    def cycle_back(self, tid: str) -> None:
        """IN_REVIEW → CHANGES_REQUESTED → IN_PROGRESS by another writer."""
        self.clock.now = EARLIER + 1
        for step in ("CHANGES_REQUESTED", "IN_PROGRESS"):
            self.store.transition(tid, step, actor="A01", reason="cycle")


Scenario = Callable[[base._Clock], Iterator[Row]]


def _w(clock: base._Clock, name: str, group: str) -> IngestWorld:
    return IngestWorld(name, group, clock)


# ---- every state against every reported state, in both modes --------------------------------------------------------------
def _matrix(clock: base._Clock) -> Iterator[Row]:
    for mode in ("ingest", "headless"):
        for state in STATES:
            for state_reported in REPORTED:
                w = _w(clock, f"matrix-{mode}-{state}-{state_reported}", "matrix")
                w.task("T-1", state, notes={"gates": []})
                yield w.call("T-1", reported("T-1", state_reported), mode=mode)
    for state_reported in REPORTED:  # the same with the gates of the default risk class, so reconcile stalls instead of approving
        w = _w(clock, f"matrix-ingest-default-gates-{state_reported}", "matrix")
        w.task("T-1", "IN_PROGRESS")
        yield w.call("T-1", reported("T-1", state_reported))


# ---- auto-claim, BLOCKED and unmet dependencies ---------------------------------------------------------------------------
def _claim(clock: base._Clock) -> Iterator[Row]:
    for start in ("PLANNED", "RETRY"):
        for dep in ("IN_REVIEW", "APPROVED", "DONE", "PLANNED", "IN_PROGRESS", "CHANGES_REQUESTED", "BLOCKED", "FAILED"):
            for state_reported in ("IN_REVIEW", "IN_PROGRESS"):
                w = _w(clock, f"claim-{start}-dependency-{dep}-{state_reported}", "claim")
                w.task("D-1", dep, depth=0, notes={"gates": []})
                w.task("T-1", start, depth=1, deps=["D-1"], notes={"gates": []})
                yield w.call("T-1", reported("T-1", state_reported))
    w = _w(clock, "claim-two-dependencies-one-unsatisfied", "claim")
    w.task("D-1", "DONE", depth=0)
    w.task("D-2", "PLANNED", depth=0)
    w.task("T-1", "PLANNED", depth=1, deps=["D-1", "D-2"], notes={"gates": []})
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    w = _w(clock, "claim-dependency-unknown", "claim")
    w.task("T-1", "PLANNED", depth=1, deps=["D-none"], notes={"gates": []})
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    w = _w(clock, "claim-dependency-in-another-correlation", "claim")
    w.task("D-1", "DONE", depth=0, corr=base.OTHER_CORR)
    w.task("T-1", "PLANNED", depth=1, deps=["D-1"], notes={"gates": []})
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    w = _w(clock, "claim-dependency-is-a-number", "claim")
    w.task("T-1", "PLANNED", depth=1, notes={"gates": []})
    w.set_column("T-1", "depends_on", json.dumps([7]))
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    w = _w(clock, "claim-dependency-is-a-list", "claim")
    w.task("T-1", "PLANNED", depth=1, notes={"gates": []})
    w.set_column("T-1", "depends_on", json.dumps([["D-1"]]))
    w.expect_rust = "not_started"
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    for attempts in (0, 1, 2, 3):
        w = _w(clock, f"claim-retry-with-{attempts}-attempts", "claim")
        w.task("T-1", "RETRY", notes={"gates": []})
        w.set_column("T-1", "attempt", attempts)
        yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    w = _w(clock, "claim-small-max-attempts", "claim")
    w.task("T-1", "PLANNED", notes={"gates": []})
    w.set_column("T-1", "max_attempts", 1)
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    for state_reported in REPORTED:
        w = _w(clock, f"blocked-task-{state_reported}", "blocked")
        w.task("T-1", "BLOCKED", notes={"gates": []})
        yield w.call("T-1", reported("T-1", state_reported))

    for label, given, row_agent in (("result-names-an-agent", "A99@result", "A05@seed"), ("task-agent-only", None, "A05@seed"),
                                    ("no-agent-at-all", None, None), ("empty-result-agent", "", "A05@seed")):
        w = _w(clock, f"agent-{label}", "agent")
        w.task("T-1", "PLANNED", agent=row_agent, notes={"gates": []})
        extra = {} if given is None else {"agent_id": given}
        yield w.call("T-1", reported("T-1", "IN_REVIEW") | extra)

    w = _w(clock, "agent-result-agent-is-a-number", "agent")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.expect_rust = "not_started"
    yield w.call("T-1", reported("T-1", "IN_REVIEW") | {"agent_id": 5})

    w = _w(clock, "agent-headless-agent", "agent")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    yield w.call("T-1", reported("T-1", "IN_REVIEW"), mode="headless", agent="A77@runner")


# ---- reasons, outputs and notes -------------------------------------------------------------------------------------------
def _provenance(clock: base._Clock) -> Iterator[Row]:
    cut = "n" * 700
    for label, result in {
        "needs-text": res("T-1", "BLOCKED", needs="a human must approve"),
        "needs-absent": res("T-1", "BLOCKED"),
        "needs-long": res("T-1", "BLOCKED", needs=cut),
        "needs-non-ascii": res("T-1", "BLOCKED", needs="besoin d'accord \u00e9\u00e8 \U0001f600"),
        "error-absent": res("T-1", "FAILED"),
        "error-object": res("T-1", "FAILED", error={"code": "E-X", "message": "bad \u00e9 \"quoted\" \U0001f600", "n": 1.5, "ok": True, "z": None}),
        "error-long": res("T-1", "FAILED", error={"message": cut}),
        "error-nested": res("T-1", "FAILED", error={"code": "E-X", "details": {"a": [1, 2, {"b": 1e-7}], "big": 1e22}}),
        "error-empty": res("T-1", "FAILED", error={}),
    }.items():
        for mode in ("ingest", "headless"):
            w = _w(clock, f"reason-{label}-{mode}", "reason")
            w.task("T-1", "IN_PROGRESS", notes={"gates": []})
            yield w.call("T-1", result, mode=mode)

    for label, result in {"needs-list": res("T-1", "BLOCKED", needs=["a", "b"]), "needs-object": res("T-1", "BLOCKED", needs={"k": "v"})}.items():
        w = _w(clock, f"reason-{label}", "reason")
        w.task("T-1", "IN_PROGRESS", notes={"gates": []})
        w.expect_rust = "not_started"
        yield w.call("T-1", result)

    outputs: dict[str, list[dict[str, str]] | None] = {
        "none": None,
        "empty": [],
        "one": [out("file://a", digest="d1")],
        "several": [out("file://a"), out("file://b", kind="doc.md", version="2"), out("file://c", digest="sha:c")],
        "defaults": [{}],
        "partial": [{"uri": "file://x"}, {"kind": "k"}, {"version": "9"}, {"digest": "dd"}],
        "non-ascii": [out("file://caf\u00e9/\U0001f600.txt")],
        "extra-member": [out("file://a", note="ignored")],
    }
    for label, listed in outputs.items():
        for state_reported in ("IN_PROGRESS", "IN_REVIEW", "FAILED"):
            w = _w(clock, f"outputs-{label}-{state_reported}", "outputs")
            w.task("T-1", "IN_PROGRESS", notes={"gates": []})
            result = res("T-1", state_reported) | ({} if listed is None else {"outputs": listed})
            yield w.call("T-1", result)
    w = _w(clock, "outputs-appended-to-existing", "outputs")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.set_column("T-1", "outputs", json.dumps([out("file://old")]))
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))
    w = _w(clock, "outputs-column-is-an-object", "outputs")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.set_column("T-1", "outputs", json.dumps({"a": 1}))
    w.expect_rust = "not_started"
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))
    w = _w(clock, "outputs-column-null-and-no-outputs", "outputs")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.set_column("T-1", "outputs", None)
    yield w.call("T-1", res("T-1", "IN_REVIEW"))

    notes_cases: dict[str, dict[str, Any]] = {
        "empty": {"gates": []},
        "other-keys": {"gates": [], "zeta": 1, "alpha": {"deep": [1, 2]}},
        "result-already-there": {"gates": [], "zeta": 1, "result": {"old": True}, "alpha": 2},
        "meta-before-result": {"gates": [], "meta": {"old": True}, "mid": 1, "result": {"old": True}},
        "unicode": {"gates": [], "note": "caf\u00e9 \U0001f600", "float": 0.1, "big": 12345678901},
        "rework-evidence": {"gates": [], "rework_evidence": [{"result": {"a": 1}}], "verdicts_since": 1000.5},
    }
    for label, notes in notes_cases.items():
        for state_reported in ("IN_PROGRESS", "IN_REVIEW", "FAILED", "BLOCKED"):
            w = _w(clock, f"notes-{label}-{state_reported}", "notes")
            w.task("T-1", "IN_PROGRESS", notes=notes)
            yield w.call("T-1", reported("T-1", state_reported) | {"metrics": {"tokens": 12, "cost": 0.25}, "summary_md": "done \u00e9"})
    for label, text in {"empty-text": "", "list": "[1, 2]", "text": "\"x\"", "null": "null"}.items():
        w = _w(clock, f"notes-column-{label}", "notes")
        w.task("T-1", "IN_PROGRESS", notes={"gates": []})
        w.set_column("T-1", "notes", text)
        yield w.call("T-1", reported("T-1", "IN_REVIEW"))
    w = _w(clock, "notes-column-is-sql-null", "notes")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.set_column("T-1", "notes", None)
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))
    w = _w(clock, "notes-column-garbage", "notes")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.set_column("T-1", "notes", "{not json")
    w.expect_rust = "not_started"
    yield w.call("T-1", reported("T-1", "IN_REVIEW"))

    for label, advisory in (("approve", False), ("advisory", True)):
        for state_reported in ("IN_REVIEW", "IN_PROGRESS"):
            w = _w(clock, f"reconcile-{label}-{state_reported}", "reconcile")
            w.task("T-1", "IN_PROGRESS", notes={"gates": []})
            w.task("T-2", "IN_REVIEW", depth=2, notes={"gates": []})
            yield w.call("T-1", reported("T-1", state_reported), advisory=advisory)
    w = _w(clock, "reconcile-without-a-key", "reconcile")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.expect_rust = "not_started"  # reconcile raises E-POLICY after the result is committed: a partial result one transaction cannot give
    yield w.call("T-1", reported("T-1", "IN_REVIEW"), keys=False)
    w = _w(clock, "reconcile-without-a-key-advisory", "reconcile")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    yield w.call("T-1", reported("T-1", "IN_REVIEW"), keys=False, advisory=True)


# ---- gate tasks -----------------------------------------------------------------------------------------------------------
def _gate_world(clock: base._Clock, name: str, group: str, *, gate: str = "review", targets: tuple[str, ...] = ("T-1",),
                state: str = "IN_PROGRESS", producers: str = "IN_REVIEW", gate_notes: dict[str, Any] | None = None,
                risk: str = "low", rework: int = 0) -> IngestWorld:
    w = _w(clock, name, group)
    for target in targets:
        w.task(target, producers, depth=1, risk=risk, rework=rework)
    w.gate_task("G-1", gate, list(targets), state, depth=2, notes=gate_notes)
    return w


def _verdict(w: IngestWorld, target: str, gate: str = "review", kind: str = "pass", **kw: Any) -> None:
    kw.setdefault("issuer", "G-1")
    w.verdict(target, gate, kind, **kw)


def _gates(clock: base._Clock) -> Iterator[Row]:
    def verdicts(**entries: str) -> dict[str, Any]:
        return {"verdicts": {key: {"verdict": value, "findings": []} for key, value in entries.items()}}

    for mode in ("ingest", "headless"):
        w = _gate_world(clock, f"gate-script-ran-{mode}", "gate")
        _verdict(w, "T-1")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "pass"}), mode=mode)

        w = _gate_world(clock, f"gate-script-not-run-{mode}", "gate")
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

        w = _gate_world(clock, f"gate-row-older-than-the-lease-{mode}", "gate")
        _verdict(w, "T-1", at=100.0)
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

        w = _gate_world(clock, f"gate-row-issued-by-another-gate-task-{mode}", "gate")
        _verdict(w, "T-1", issuer="G-other")
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

        w = _gate_world(clock, f"gate-row-without-an-issuer-{mode}", "gate")
        _verdict(w, "T-1", issuer=None)
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

        w = _gate_world(clock, f"gate-row-of-another-gate-{mode}", "gate")
        _verdict(w, "T-1", gate="quality")
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

        w = _gate_world(clock, f"gate-two-targets-one-missing-{mode}", "gate", targets=("T-1", "T-2"))
        _verdict(w, "T-1")
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

        w = _gate_world(clock, f"gate-two-targets-both-ran-{mode}", "gate", targets=("T-1", "T-2"))
        _verdict(w, "T-1")
        _verdict(w, "T-2", kind="fail")
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

        w = _gate_world(clock, f"gate-review-mismatch-row-pass-agent-fails-{mode}", "gate")
        _verdict(w, "T-1")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "request_changes"}), mode=mode)

        w = _gate_world(clock, f"gate-review-mismatch-row-waive-agent-fails-{mode}", "gate")
        _verdict(w, "T-1", kind="waive")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "block"}), mode=mode)

        w = _gate_world(clock, f"gate-review-mismatch-names-two-targets-{mode}", "gate", targets=("T-2", "T-1"))
        _verdict(w, "T-1")
        _verdict(w, "T-2")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "fail", "T-2": "nope"}), mode=mode)

        w = _gate_world(clock, f"gate-review-mismatch-key-outside-gate-for-accuses-all-{mode}", "gate", targets=("T-1", "T-2"))
        _verdict(w, "T-1")
        _verdict(w, "T-2", kind="fail")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"everything": "fail"}), mode=mode)

        w = _gate_world(clock, f"gate-review-agent-fails-and-row-fails-{mode}", "gate")
        _verdict(w, "T-1", kind="fail")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "request_changes"}), mode=mode)

        for loops in (1, 2):
            w = _gate_world(clock, f"gate-review-fails-after-{loops}-rework-loops-{mode}", "gate", rework=loops)
            _verdict(w, "T-1", kind="fail")
            yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "request_changes"}), mode=mode)

        w = _gate_world(clock, f"gate-review-agent-passes-{mode}", "gate")
        _verdict(w, "T-1")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": " APPROVE "}), mode=mode)

        w = _gate_world(clock, f"gate-quality-agent-fails-no-mismatch-check-{mode}", "gate", gate="quality", risk="medium")
        _verdict(w, "T-1", gate="quality")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "fail"}), mode=mode)

        w = _gate_world(clock, f"gate-no-targets-{mode}", "gate", targets=())
        yield w.call("G-1", reported("G-1", "IN_REVIEW"), mode=mode)

    for start in ("PLANNED", "RETRY"):
        w = _gate_world(clock, f"gate-{start}-with-a-row-from-the-previous-lease", "gate", state=start)
        _verdict(w, "T-1", at=900.0)
        yield w.call("G-1", reported("G-1", "IN_REVIEW"))
        w = _gate_world(clock, f"gate-{start}-no-targets", "gate", state=start, targets=())
        yield w.call("G-1", reported("G-1", "IN_REVIEW"))
        w = _gate_world(clock, f"gate-{start}-heartbeat-is-not-gate-checked", "gate", state=start)
        yield w.call("G-1", reported("G-1", "IN_PROGRESS"))

    for state_reported in ("FAILED", "BLOCKED", "IN_PROGRESS"):
        w = _gate_world(clock, f"gate-not-checked-for-{state_reported}", "gate")
        yield w.call("G-1", reported("G-1", state_reported))

    w = _gate_world(clock, "gate-claimed-has-a-lease", "gate", state="CLAIMED")
    _verdict(w, "T-1")
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-no-claim-row", "gate")
    _verdict(w, "T-1")
    w.store.conn.execute("DELETE FROM transitions WHERE task_id='G-1' AND to_state='CLAIMED'")
    w.store.conn.commit()
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-lease-start-is-the-latest-claim", "gate")
    _verdict(w, "T-1", at=1500.0)
    w.clock.now = 1800.0
    for step in ("RETRY", "CLAIMED", "IN_PROGRESS"):
        w.store.transition("G-1", step, actor="A01", reason="second lease")
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))

    w = _gate_world(clock, "gate-forged-row-skipped", "gate")
    env = w.sign("T-1", "review", issuer="G-1")
    env["payload"]["verdict"] = "fail"
    w.raw("T-1", "review", "fail", json.dumps(env))
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-unsigned-row-skipped", "gate")
    w.raw("T-1", "review", "pass", None)
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-garbage-envelope-skipped", "gate")
    w.raw("T-1", "review", "pass", "{not json")
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-newest-row-wins-over-older-pass", "gate")
    _verdict(w, "T-1", kind="pass", at=ISSUED_A)
    _verdict(w, "T-1", kind="fail", at=ISSUED_B)
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | verdicts(**{"T-1": "fail"}))
    w = _gate_world(clock, "gate-newest-row-is-too-old-older-one-counts", "gate")
    _verdict(w, "T-1", kind="pass", at=ISSUED_B)
    _verdict(w, "T-1", kind="fail", at=100.0)
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-row-issued-exactly-at-the-lease-start", "gate")
    _verdict(w, "T-1", at=w.rows()[-1]["created_at"] + 0.5)
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))

    w = _gate_world(clock, "gate-name-is-text-of-a-list", "gate", gate_notes={"gate": ["review"]})
    w.expect_rust = "not_started"
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-for-is-text", "gate", gate_notes={"gate_for": "T-1"})
    w.expect_rust = "not_started"
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-target-name-needs-an-escaped-repr", "gate", targets=("T-'1",))
    w.expect_rust = "not_started"
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))
    w = _gate_world(clock, "gate-name-is-empty-so-not-a-gate-task", "gate", gate_notes={"gate": ""})
    yield w.call("G-1", reported("G-1", "IN_REVIEW"))


# ---- feedback on the gate targets -----------------------------------------------------------------------------------------
def _feedback(clock: base._Clock) -> Iterator[Row]:
    major = {"severity": "major", "kind": "bug", "summary": "broken"}
    minor = {"severity": "minor", "kind": "style", "summary": "meh"}
    entries: dict[str, dict[str, Any]] = {
        "major-finding": {"verdict": "request_changes", "findings": [major]},
        "minor-only-synthesizes": {"verdict": "request_changes", "findings": [minor]},
        "no-findings-synthesizes": {"verdict": "request_changes", "findings": []},
        "findings-absent": {"verdict": "block"},
        "verdict-absent": {"findings": [minor]},
        "verdict-empty": {"verdict": "", "findings": []},
        "verdict-padded-and-capitalised": {"verdict": "  Request_Changes\t", "findings": []},
        "uppercase-major": {"verdict": "fail", "findings": [{"severity": "MAJOR"}]},
        "critical": {"verdict": "fail", "findings": [{"severity": "Critical"}, minor]},
        "unknown-severity-counts-as-major": {"verdict": "fail", "findings": [{"severity": "weird"}]},
        "severity-missing-counts-as-major": {"verdict": "fail", "findings": [{"kind": "k"}]},
        "severity-not-text-counts-as-major": {"verdict": "fail", "findings": [{"severity": 3}]},
        "finding-not-an-object-counts-as-major": {"verdict": "fail", "findings": ["text"]},
        "info-and-minor-synthesize": {"verdict": "fail", "findings": [{"severity": "info"}, {"severity": "MINOR"}]},
        "blocker": {"verdict": "fail", "findings": [{"severity": "blocker"}]},
        "non-ascii-verdict-needs-a-repr": {"verdict": "d\u00e9j\u00e0", "findings": []},
        "quote-in-verdict-needs-a-repr": {"verdict": "it's", "findings": []},
        "findings-extra-members": {"verdict": "fail", "findings": [dict(major, extra={"deep": [1, 2.5]})], "note": "x"},
    }
    for label, entry in entries.items():
        w = _gate_world(clock, f"feedback-{label}", "feedback")
        _verdict(w, "T-1", kind="fail")
        if label in ("non-ascii-verdict-needs-a-repr", "quote-in-verdict-needs-a-repr"):
            w.expect_rust = "not_started"
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": entry}})
    for label, verdict in {"pass": "pass", "approve": "approve", "padded-pass": " PASS \n"}.items():
        w = _gate_world(clock, f"feedback-passing-entry-{label}", "feedback")
        _verdict(w, "T-1")
        yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": verdict}}})

    w = _gate_world(clock, "feedback-two-targets-in-result-order", "feedback", targets=("T-1", "T-2"))
    _verdict(w, "T-1", kind="fail")
    _verdict(w, "T-2", kind="fail")
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-2": {"verdict": "fail"}, "T-1": {"verdict": "fail", "findings": [major]}}})

    w = _gate_world(clock, "feedback-only-the-failing-target", "feedback", targets=("T-1", "T-2"))
    _verdict(w, "T-1")
    _verdict(w, "T-2", kind="fail")
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": "pass"}, "T-2": {"verdict": "fail"}}})

    w = _gate_world(clock, "feedback-key-outside-gate-for-is-skipped", "feedback", targets=("T-1", "T-2"))
    _verdict(w, "T-1", kind="fail")
    _verdict(w, "T-2", kind="fail")
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": "fail"}, "stranger": {"verdict": "fail"}}})

    w = _gate_world(clock, "feedback-appends-to-existing-feedback", "feedback")
    _verdict(w, "T-1", kind="fail")
    w.store.set_notes("T-1", feedback=[{"gate": "quality", "source": "script", "findings": []}])
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": "fail"}}})

    w = _gate_world(clock, "feedback-existing-feedback-is-not-a-list", "feedback")
    _verdict(w, "T-1", kind="fail")
    w.store.set_notes("T-1", feedback="text")
    w.expect_rust = "not_started"
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": "fail"}}})

    w = _gate_world(clock, "feedback-target-is-another-correlation", "feedback", gate_notes={"gate_for": ["X-1"]})
    w.task("X-1", "IN_REVIEW", corr=base.OTHER_CORR)
    _verdict(w, "X-1", kind="fail")
    w.expect_rust = "not_started"
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"X-1": {"verdict": "fail"}}})

    w = _gate_world(clock, "feedback-target-does-not-exist", "feedback", targets=(), gate_notes={"gate_for": ["T-gone"]})
    w.raw("T-gone", "review", "fail", json.dumps(w.sign("T-gone", "review", issuer="G-1", findings=base.fail_findings())))
    w.expect_rust = "not_started"  # Python raises E-INPUT (unknown task) inside the transaction; Rust cannot see a row outside the correlation
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-gone": {"verdict": "fail"}}})

    w = _gate_world(clock, "feedback-not-written-for-a-heartbeat", "feedback")
    _verdict(w, "T-1", kind="fail")
    yield w.call("G-1", reported("G-1", "IN_PROGRESS") | {"verdicts": {"T-1": {"verdict": "fail"}}})

    w = _gate_world(clock, "feedback-quality-gate-too", "feedback", gate="quality", risk="medium")
    _verdict(w, "T-1", gate="quality", kind="fail")
    yield w.call("G-1", reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": "fail", "findings": [minor]}}})

    w = _gate_world(clock, "feedback-not-a-gate-task-ignores-verdicts", "feedback")
    w.task("T-2", "IN_PROGRESS", depth=3, notes={"gates": []})
    yield w.call("T-2", reported("T-2", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": "fail"}}})


# ---- replay (D-P24) -------------------------------------------------------------------------------------------------------
def _replay(clock: base._Clock) -> Iterator[Row]:
    heartbeat = reported("T-1", "IN_PROGRESS")
    review = reported("T-1", "IN_REVIEW")

    w = _w(clock, "replay-heartbeat", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", heartbeat)
    yield w.call("T-1", heartbeat)

    w = _w(clock, "replay-heartbeat-with-another-meta-path", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", heartbeat)
    yield w.call("T-1", heartbeat, advisory=False)

    w = _w(clock, "replay-heartbeat-different-result-applies", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", heartbeat)
    yield w.call("T-1", heartbeat | {"metrics": {"n": 2}})

    w = _w(clock, "replay-in-review-after-another-writer-cycled-the-task-back", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", review)
    w.cycle_back("T-1")
    yield w.call("T-1", review)

    w = _w(clock, "replay-in-review-after-the-first-reconcile-approved", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", review, advisory=False)
    yield w.call("T-1", review)

    w = _w(clock, "replay-in-review-while-still-in-review-is-refused", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", review)
    yield w.call("T-1", review)

    w = _w(clock, "replay-failed-while-failed-is-refused", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    failed = reported("T-1", "FAILED")
    w.first("T-1", failed)
    yield w.call("T-1", failed)

    w = _w(clock, "replay-failed-after-a-retry-cycle", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", failed)
    w.clock.now = EARLIER + 1
    for step in ("RETRY", "CLAIMED", "IN_PROGRESS"):
        w.store.transition("T-1", step, actor="A01", reason="again")
    yield w.call("T-1", failed)

    w = _w(clock, "replay-blocked-after-unblock", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    blocked = reported("T-1", "BLOCKED")
    w.first("T-1", blocked)
    w.clock.now = EARLIER + 1
    for step in ("CLAIMED", "IN_PROGRESS"):
        w.store.transition("T-1", step, actor="A01", reason="released")
    yield w.call("T-1", blocked)

    w = _w(clock, "replay-new-attempt-is-a-new-key", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", failed)
    w.clock.now = EARLIER + 1
    for step in ("RETRY", "CLAIMED", "IN_PROGRESS"):
        w.store.transition("T-1", step, actor="A01", reason="again")
    w.first("T-1", heartbeat)
    yield w.call("T-1", heartbeat)

    w = _w(clock, "replay-after-a-rework-same-attempt", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.first("T-1", review)
    w.cycle_back("T-1")
    yield w.call("T-1", review, advisory=False)

    w = _w(clock, "replay-headless-is-never-keyed", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.call("T-1", review, mode="headless", at=EARLIER, record=False)
    w.cycle_back("T-1")
    yield w.call("T-1", review, mode="headless")

    w = _w(clock, "replay-ingest-after-headless-key-absent", "replay")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    w.call("T-1", heartbeat, mode="headless", at=EARLIER, record=False)
    yield w.call("T-1", heartbeat)

    w = _w(clock, "replay-with-auto-claim-first-then-heartbeat", "replay")
    w.task("T-1", "PLANNED", notes={"gates": []})
    w.first("T-1", heartbeat)
    yield w.call("T-1", heartbeat)

    w = _gate_world(clock, "replay-gate-task-skips-the-gate-check", "replay")
    _verdict(w, "T-1")
    gate_result = reported("G-1", "IN_REVIEW") | {"verdicts": {"T-1": {"verdict": "pass"}}}
    w.first("G-1", gate_result)
    w.cycle_back("G-1")
    yield w.call("G-1", gate_result)

    # the bound: 32 keys are kept, the oldest falls out at the 33rd
    for count, label in ((31, "31-keys-oldest-replays"), (32, "32-keys-oldest-replays"), (33, "33-keys-oldest-evicted"),
                         (34, "34-keys-oldest-evicted")):
        w = _w(clock, f"replay-bound-{label}", "replay-bound")
        w.task("T-1", "IN_PROGRESS", notes={"gates": []})
        for n in range(count):
            w.first("T-1", heartbeat | {"metrics": {"n": n}})
        yield w.call("T-1", heartbeat | {"metrics": {"n": 0}})
    w = _w(clock, "replay-bound-newest-replays", "replay-bound")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    for n in range(40):
        w.first("T-1", heartbeat | {"metrics": {"n": n}})
    yield w.call("T-1", heartbeat | {"metrics": {"n": 39}})
    w = _w(clock, "replay-bound-new-result-drops-the-oldest", "replay-bound")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    for n in range(32):
        w.first("T-1", heartbeat | {"metrics": {"n": n}})
    yield w.call("T-1", heartbeat | {"metrics": {"n": 99}})
    w = _w(clock, "replay-bound-evicted-result-applies-again", "replay-bound")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    for n in range(33):
        w.first("T-1", heartbeat | {"metrics": {"n": n}})
    yield w.call("T-1", heartbeat | {"metrics": {"n": 0}}, advisory=False)

    stored = {
        "foreign-task-entry-does-not-match": [{"task_id": "T-other", "attempt": 1, "digest": "{digest}", "reply": "IN_PROGRESS"}],
        "reply-not-text-is-no-hit": [{"task_id": "T-1", "attempt": 1, "digest": "{digest}", "reply": 5}],
        "reply-missing-is-no-hit": [{"task_id": "T-1", "attempt": 1, "digest": "{digest}"}],
        "attempt-as-float-matches": [{"task_id": "T-1", "attempt": 1.0, "digest": "{digest}", "reply": "IN_PROGRESS"}],
        "attempt-as-true-matches": [{"task_id": "T-1", "attempt": True, "digest": "{digest}", "reply": "IN_PROGRESS"}],
        "attempt-as-text-does-not": [{"task_id": "T-1", "attempt": "1", "digest": "{digest}", "reply": "IN_PROGRESS"}],
        "first-match-wins": [{"task_id": "T-1", "attempt": 1, "digest": "{digest}", "reply": "FIRST"},
                             {"task_id": "T-1", "attempt": 1, "digest": "{digest}", "reply": "SECOND"}],
        "first-match-with-bad-reply-is-no-hit": [{"task_id": "T-1", "attempt": 1, "digest": "{digest}", "reply": None},
                                                 {"task_id": "T-1", "attempt": 1, "digest": "{digest}", "reply": "SECOND"}],
        "non-dict-entries-dropped-on-write": ["junk", 7, None, {"task_id": "T-1", "attempt": 9, "digest": "other", "reply": "X"}],
        "same-attempt-and-digest-of-another-task-dropped-on-write": [{"task_id": "T-other", "attempt": 1, "digest": "{digest}", "reply": "X"}],
        "entry-with-extra-members-kept": [{"task_id": "T-1", "attempt": 9, "digest": "other", "reply": "X", "extra": [1, {"a": 2}]}],
    }
    for label, keys in stored.items():
        w = _w(clock, f"replay-stored-{label}", "replay-stored")
        w.task("T-1", "IN_PROGRESS", notes={"gates": []})
        digest = results.result_digest(heartbeat)
        listed = json.loads(json.dumps(keys).replace("{digest}", digest))
        w.store.set_notes("T-1", ingest_keys=listed)
        yield w.call("T-1", heartbeat)
    for label, value in {"object": {"a": 1}, "text": "keys", "null": None, "number": 4}.items():
        w = _w(clock, f"replay-stored-keys-are-{label}", "replay-stored")
        w.task("T-1", "IN_PROGRESS", notes={"gates": []})
        w.store.set_notes("T-1", ingest_keys=value)
        yield w.call("T-1", heartbeat)


# ---- results that do not validate -----------------------------------------------------------------------------------------
def _invalid(clock: base._Clock) -> Iterator[Row]:
    texts: dict[str, Any] = {
        "no-block": "I finished, thanks.",
        "empty-text": "",
        "missing-state": {"task_id": "T-1"},
        "unknown-state": {"task_id": "T-1", "state": "DONE"},
        "state-not-text": {"task_id": "T-1", "state": 3},
        "outputs-not-a-list": {"task_id": "T-1", "state": "IN_REVIEW", "outputs": "x"},
        "output-member-wrong-type": {"task_id": "T-1", "state": "IN_REVIEW", "outputs": [{"uri": 1}]},
        "verdicts-entry-wrong-type": {"task_id": "T-1", "state": "IN_REVIEW", "verdicts": {"T-1": {"verdict": 1}}},
        "other-task": {"task_id": "T-2", "state": "IN_REVIEW"},
        "several-errors": {"task_id": "T-1", "state": "nope", "outputs": 3, "error": []},
        "fenced-block-last-wins": 'text\n```json\n{"task_id":"T-1","state":"IN_REVIEW"}\n```\nmore\n```json\n{"task_id":"T-1","state":"BAD"}\n```\n',
        "fenced-block-valid": 'prose\n```json\n{"task_id":"T-1","state":"IN_REVIEW","outputs":[{"uri":"file://f"}]}\n```\n',
        "task-id-mismatch-quote": {"task_id": "T-'1", "state": "IN_REVIEW"},
    }
    for label, source in texts.items():
        for mode in ("ingest", "headless"):
            for state in ("IN_PROGRESS", "CLAIMED", "IN_REVIEW"):
                w = _w(clock, f"invalid-{label}-{mode}-{state}", "invalid")
                w.task("T-1", state, notes={"gates": []})
                yield w.call("T-1", source, mode=mode)
    w = _w(clock, "invalid-on-a-planned-task-claims-nothing", "invalid")
    w.task("T-1", "PLANNED", notes={"gates": []})
    yield w.call("T-1", {"task_id": "T-1"})
    w = _w(clock, "invalid-headless-on-an-old-attempt", "invalid")
    w.task("T-1", "IN_PROGRESS", notes={"gates": []})
    yield w.call("T-1", {"task_id": "T-1", "state": "NOPE"}, mode="headless")


def constants() -> Row:
    """Python's constants the Rust module must match."""
    return {"ingest_keys_bound": results.INGEST_KEYS_BOUND, "satisfied_states": sorted(taskstore.SATISFIED_STATES),
            "pass_verdicts": sorted(results.PASS_VERDICTS), "severities": list(gates.SEVERITIES), "blocking_severity": gates.BLOCKING_SEVERITY,
            "now": NOW, "states": STATES}


def build_cases() -> list[Row]:
    """Run every case against the real route and return the recorded cases, in the fixed order."""
    clock = base._Clock()
    groups: tuple[Scenario, ...] = (_matrix, _claim, _provenance, _gates, _feedback, _replay, _invalid)
    cases: list[Row] = []
    with base.seeded(clock):
        for group in groups:
            cases.extend(group(clock))
    names = [case["name"] for case in cases]
    repeated = sorted({name for name in names if names.count(name) > 1})
    assert not repeated, f"duplicate case names: {repeated}"
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
