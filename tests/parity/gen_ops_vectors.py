#!/usr/bin/env python3
"""Vectors for the Rust writer ops (`crates/gsd-taskwriter/src/ops.rs`), generated from the REAL `TaskStore`.

Every case seeds tasks in an in-memory store through the real `TaskStore.create` (plus raw column overrides for the odd rows), runs an ordered
list of the store's own write calls inside ONE `store.transaction()` (what `apply_result` and `reconcile` do), and records the whole content of
`tasks`, `transitions` and `artifacts` before and after, or the error. The Rust test `crates/gsd-taskwriter/tests/ops.rs` replays the same op
list (in its wire form) through `TaskStoreWriter::apply_ops` against a store seeded with the `initial` rows and compares the database content
exactly; a refusal must carry Python's code, message and details and leave the store file byte-identical.

Case fields: `ops` (wire form, see `WriterOp::from_json`), `python` (`outcome` ok, error or exception, the error as `SwarmError` prints it,
the exception type, and `after`, the rows after the transaction: a failed list rolls back, so `after` equals `initial`) and `rust`: `same`
(Rust must produce Python's outcome) or `not_started` (Python accepts the list, or fails with an exception that is not a `SwarmError`, and
Rust refuses it before the first write so that Python answers; `why` says which rule).

A `transition` op carries a plan, not a request: the generator runs the real `TaskStore.transition` and writes the plan Python's run amounts to
(the final state, the columns it changed among `state`, `attempt`, `rework_loops` and `notes`, and its audit rows), so the Rust test needs no
decision code. Object key order is meaningful here (the order of a notes object is the order of the stored text), so keys are NOT sorted; the
generator builds every object in a fixed order, and the file is deterministic.

Determinism: `taskstore.time` is replaced by a fixed clock (`CREATE_AT` while seeding, `NOW` for the op list), the `live.json` observer is off,
ids and rows are fixed. Exception types are those of CPython 3.12; their texts are not recorded.

`python3 tests/parity/gen_ops_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from swarm import taskstore  # noqa: E402
from swarm.errors import SwarmError  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-taskwriter" / "tests" / "vectors" / "ops.json"
SCHEMA = "gsd-taskwriter.ops-vectors.v1"
CREATE_AT = 1_700_000_000.0
NOW = 1_700_000_123.456789
I64_MAX = 9_223_372_036_854_775_807

Row = dict[str, Any]
Op = tuple[Any, ...]


@dataclass(frozen=True)
class Seed:
    """A task as it exists before the op list: created at `CREATE_AT`, then raw column overrides (state first)."""

    task_id: str
    state: str = "IN_PROGRESS"
    columns: tuple[tuple[str, object], ...] = ()
    artifacts: int = 0  # real `add_artifact` calls, so the `artifacts` rows and `outputs` agree


@dataclass(frozen=True)
class Case:
    """One op list: the seeds, the Python calls, and what Rust must do."""

    case_id: str
    group: str
    seeds: tuple[Seed, ...]
    ops: tuple[Op, ...]
    rust: str = "same"  # "same" or "not_started"
    why: str = ""  # the rule that makes Rust refuse, for "not_started"


class _Clock:
    """Stand-in for the `time` module of `swarm.taskstore`: `time()` returns the value last set."""

    def __init__(self) -> None:
        self.now = CREATE_AT

    def time(self) -> float:
        return self.now


def _rows(store: taskstore.TaskStore, table: str) -> list[Row]:
    return [dict(row) for row in store.conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]  # noqa: S608 - fixed table names


def _dump(store: taskstore.TaskStore) -> Row:
    return {table: _rows(store, table) for table in ("tasks", "transitions", "artifacts")}


def _seed(store: taskstore.TaskStore, clock: _Clock, seed: Seed) -> None:
    clock.now = CREATE_AT
    store.create(task_id=seed.task_id, correlation_id="C-OPS", capability="code", title=f"ops {seed.task_id}")
    raw: dict[str, object] = {"state": seed.state, **dict(seed.columns)}
    sets = ", ".join(f"{name}=?" for name in raw)
    store.conn.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", [*raw.values(), seed.task_id])  # noqa: S608 - fixed column names
    store.conn.commit()
    for index in range(seed.artifacts):
        store.add_artifact(seed.task_id, kind="doc", uri=f"file:///seed/{seed.task_id}/{index}", version="1", digest=f"d{index}", producer="seed")


def _transition_wire(store: taskstore.TaskStore, task_id: str, to: str, actor: str, reason: str) -> Row:
    """Runs the real `TaskStore.transition` and returns the plan its effect amounts to, in the wire form of `WriterOp::from_json`."""
    before = store.get(task_id)
    seen = len(store.history(task_id))
    store.transition(task_id, to, actor=actor, reason=reason)
    after = store.get(task_id)
    columns: Row = {"state": after["state"]}
    for name in ("attempt", "rework_loops"):
        if after[name] != before[name]:
            columns[name] = after[name]
    if after["notes"] != before["notes"]:
        columns["notes"] = json.loads(after["notes"])
    audit = [{"from": h["from_state"], "to": h["to_state"], "actor": h["actor"], "reason": h["reason"]} for h in store.history(task_id)[seen:]]
    plan = {"from": before["state"], "to": after["state"], "columns": columns, "audit": audit, "effects": [{"kind": "live_refresh"}]}
    return {"op": "transition", "task_id": task_id, "actor": actor, "plan": plan}


def _wire(op: Op) -> Row | None:
    """The wire form of an op, or None for a transition (its plan exists only after the real call)."""
    kind = op[0]
    if kind == "notes":
        return {"op": "set_notes", "task_id": op[1], "set": op[2]}
    if kind == "artifact":
        _, tid, kind_, uri, version, digest, producer = op
        return {"op": "add_artifact", "task_id": tid, "kind": kind_, "uri": uri, "version": version, "digest": digest, "producer": producer}
    if kind == "feedback":
        return {"op": "append_feedback", "task_id": op[1], "entry": op[2]}
    if kind == "create":
        return {"op": "create_task", "task": op[1]}
    if kind == "deps":
        return {"op": "set_depends_on", "task_id": op[1], "depends_on": op[2]}
    return None


def _call(store: taskstore.TaskStore, op: Op) -> Row | None:
    """The real Python call of one op; a transition returns its derived wire op."""
    kind = op[0]
    if kind == "notes":
        store.set_notes(op[1], **op[2])
    elif kind == "artifact":
        _, tid, kind_, uri, version, digest, producer = op
        store.add_artifact(tid, kind=kind_, uri=uri, version=version, digest=digest, producer=producer)
    elif kind == "feedback":
        store.append_feedback(op[1], op[2])
    elif kind == "create":
        store.create(**op[1])
    elif kind == "deps":
        store.update(op[1], depends_on=op[2])
    else:
        return _transition_wire(store, op[1], op[2], op[3], op[4])
    return None


def run_case(case: Case, clock: _Clock) -> Row:
    """Seeds a fresh in-memory store, runs the op list in one transaction and records everything."""
    store = taskstore.TaskStore(":memory:")
    store._notify_committed = lambda: None  # type: ignore[method-assign]  # no live.json refresh in a vector run
    for seed in case.seeds:
        _seed(store, clock, seed)
    initial = _dump(store)
    clock.now = NOW
    wire: list[Row | None] = [_wire(op) for op in case.ops]
    outcome: Row
    try:
        with store.transaction():
            for index, op in enumerate(case.ops):
                derived = _call(store, op)
                if derived is not None:
                    wire[index] = derived
        outcome = {"outcome": "ok"}
    except SwarmError as error:
        outcome = {"outcome": "error", "error": {"code": error.code.value, "message": str(error), "details": error.details}}
    except Exception as error:  # noqa: BLE001 - the oracle records any exception type
        outcome = {"outcome": "exception", "exception": type(error).__name__}
    if any(item is None for item in wire):
        raise AssertionError(f"{case.case_id}: a transition after a failed op has no plan; reorder the list")
    if outcome["outcome"] == "exception" and case.rust != "not_started":
        raise AssertionError(f"{case.case_id}: Python raises a non-SwarmError exception, so Rust must refuse not_started")
    if case.rust == "not_started" and not case.why:
        raise AssertionError(f"{case.case_id}: a not_started case names its rule")
    return {
        "id": case.case_id,
        "group": case.group,
        "now": NOW,
        "initial": initial,
        "ops": wire,
        "python": {**outcome, "after": _dump(store)},
        "rust": {"outcome": case.rust, **({"why": case.why} if case.why else {})},
    }


# ---- cases -------------------------------------------------------------------------------------------------------------------------


def _t(task_id: str = "T-1", **kw: Any) -> Seed:
    return Seed(task_id, **kw)


def _notes(text: str | None) -> tuple[tuple[str, object], ...]:
    return (("notes", text),)


def _set_notes_cases() -> list[Case]:
    def case(cid: str, seed: Seed | None, kv: dict[str, Any], rust: str = "same", why: str = "", tid: str = "T-1") -> Case:
        return Case(f"notes-{cid}", "set_notes", (seed,) if seed is not None else (), (("notes", tid, kv),), rust, why)

    nested = {"result": {"summary": "ok", "items": [1, 2.5, None, True, {"deep": ["x", {"y": []}]}]}, "meta": {"tokens": 12, "cost": 0.0125}}
    return [
        case("on-empty-dict", _t(), {"a": 1}),
        case("on-null-notes", _t(columns=_notes(None)), {"a": 1}),
        case("on-empty-text", _t(columns=_notes("")), {"a": 1}),
        case("on-dict", _t(columns=_notes('{"keep": 1, "b": "two"}')), {"a": 1}),
        case("existing-key-keeps-position", _t(columns=_notes('{"a": 1, "b": 2, "c": 3}')), {"b": 20}),
        case("new-keys-append-in-order", _t(columns=_notes('{"a": 1}')), {"z": 1, "m": 2, "b": 3}),
        case("mixed-existing-and-new", _t(columns=_notes('{"a": 1, "b": 2}')), {"c": 3, "a": 10}),
        case("noncanonical-stored-text-is-normalised", _t(columns=_notes('{"a":1,"b":[1,2],"c":{"d":null}}')), {"e": 5}),
        case("empty-kv-rewrites-the-notes", _t(columns=_notes('{"a":1,  "b":2}')), {}),
        case("nested-values", _t(), nested),
        case("value-types", _t(), {"n": None, "t": True, "f": False, "i": -7, "big": I64_MAX, "s": "text", "l": [], "d": {}}),
        case("floats", _t(), {"a": 1.5, "b": 1e-07, "c": 1e22, "d": -0.0, "e": 100.0, "f": 0.1, "g": 1e16, "h": 123456789.123456789}),
        case("unicode-values-and-keys", _t(), {"ünï": "✓ \"q\" \\ \n\t", "emoji": "\U0001f600", "k\u00e9y": "\u2028"}),
        case("existing-unicode-text", _t(columns=_notes('{"é": "\\u00e9", "z": "\U0001f600"}')), {"a": 1}),
        case("duplicate-keys-in-stored-text-last-wins", _t(columns=_notes('{"a": 1, "b": 2, "a": 3}')), {"c": 4}),
        case("existing-null-value-is-replaced", _t(columns=_notes('{"running": {"x": 1}}')), {"running": None}),
        case("non-dict-list-notes-count-as-empty", _t(columns=_notes("[1, 2]")), {"a": 1}),
        case("non-dict-string-notes-count-as-empty", _t(columns=_notes('"text"')), {"a": 1}),
        case("non-dict-null-notes-count-as-empty", _t(columns=_notes("null")), {"a": 1}),
        case("non-dict-number-notes-count-as-empty", _t(columns=_notes("5")), {"a": 1}),
        case("unknown-task", None, {"a": 1}, tid="T-missing"),
        case("unknown-task-among-seeds", _t(), {"a": 1}, tid="T-missing"),
        case("invalid-json-notes-count-as-empty", _t(columns=_notes('{"oops')), {"a": 1}, "not_started", "notes text that is not JSON: Python reads {}, Rust refuses the row"),
        case("existing-infinity-in-notes", _t(columns=_notes('{"x": 1e400}')), {"a": 1}, "not_started", "a non-finite number in the notes"),
        case("key-task_id-collides-with-the-parameter", _t(), {"task_id": 1}, "not_started", "set_notes(task_id, **{'task_id': ..}) is a TypeError in Python"),
        case("key-self-collides-with-the-parameter", _t(), {"self": 1}, "not_started", "set_notes(task_id, **{'self': ..}) is a TypeError in Python"),
        case("big-integer-in-stored-notes", _t(columns=_notes('{"n": 99999999999999999999}')), {"a": 1}, "not_started", "an integer outside i64 in the stored notes"),
    ]


def _artifact_cases() -> list[Case]:
    def case(cid: str, seed: Seed | tuple[Seed, ...] | None, art: Op | None = None, rust: str = "same", why: str = "", extra: tuple[Op, ...] = ()) -> Case:
        ops = ((art,) if art is not None else ()) + extra
        seeds = seed if isinstance(seed, tuple) else (seed,) if seed is not None else ()
        return Case(f"artifact-{cid}", "add_artifact", seeds, ops, rust, why)

    def art(tid: str = "T-1", kind: str = "report", uri: str = "file:///out/report.md", version: str = "1", digest: str = "abc123", producer: str = "A05") -> Op:
        return ("artifact", tid, kind, uri, version, digest, producer)

    return [
        case("no-existing-outputs", _t(), art()),
        case("null-outputs-column", _t(columns=(("outputs", None),)), art()),
        case("empty-text-outputs-column", _t(columns=(("outputs", ""),)), art()),
        case("existing-outputs-list", _t(columns=(("outputs", '[{"kind": "x", "uri": "u", "extra": [1, 2]}]'),)), art()),
        case("existing-artifact-rows", _t(artifacts=2), art(kind="build", uri="img:1", version="2", digest="")),
        case("two-in-one-list", _t(), art(), extra=(art(kind="log", uri="file:///out/log.txt", digest="d2"),)),
        case("empty-strings", _t(), art(kind="", uri="", version="", digest="", producer="")),
        case("unicode-and-quotes", _t(), art(kind="ünï", uri='file:///a b/"c"\\d', digest="\U0001f600")),
        case("noncanonical-existing-outputs-text", _t(columns=(("outputs", '[{"kind":"x","uri":"u"}]'),)), art()),
        case("two-tasks", (_t("T-1"), _t("T-2")), art("T-1"), extra=(art("T-2", kind="k2"),)),
        case("unknown-task-rolls-back-the-row", None, art("T-missing")),
        case("unknown-task-after-a-good-artifact", _t(), art(), extra=(art("T-missing"),)),
        case("outputs-object-is-a-type-error", _t(columns=(("outputs", '{"a": 1}'),)), art(), "not_started", "outputs that is not a JSON list: Python raises TypeError"),
        case("outputs-null-text-is-a-type-error", _t(columns=(("outputs", "null"),)), art(), "not_started", "outputs that is not a JSON list: Python raises TypeError"),
        case("outputs-string-is-a-type-error", _t(columns=(("outputs", '"abc"'),)), art(), "not_started", "outputs that is not a JSON list: Python raises TypeError"),
    ]


def _feedback_cases() -> list[Case]:
    entry = {"gate": "review", "source": "agent", "findings": ["a", {"sev": "major", "msg": "x"}]}

    def case(cid: str, seed: Seed | None, ent: object = entry, rust: str = "same", why: str = "", extra: tuple[Op, ...] = (), tid: str = "T-1") -> Case:
        return Case(f"feedback-{cid}", "append_feedback", (seed,) if seed is not None else (), (("feedback", tid, ent),) + extra, rust, why)

    return [
        case("on-empty-notes", _t()),
        case("on-null-notes", _t(columns=_notes(None))),
        case("appended-after-other-keys", _t(columns=_notes('{"result": {"a": 1}, "meta": {}}'))),
        case("existing-list-is-extended", _t(columns=_notes('{"feedback": [{"gate": "qa"}], "z": 1}'))),
        case("existing-empty-list", _t(columns=_notes('{"a": 1, "feedback": [], "z": 1}'))),
        case("two-entries-in-one-list", _t(), extra=(("feedback", "T-1", {"gate": "sec", "source": "script", "findings": []}),)),
        case("non-dict-notes-count-as-empty", _t(columns=_notes("[1]"))),
        case("string-entry", _t(), "plain text"),
        case("list-entry", _t(), [1, 2]),
        case("unicode-entry", _t(), {"findings": ["ünï ✓", "\U0001f600"]}),
        case("float-entry", _t(), {"score": 0.5, "n": 1e22, "z": -0.0}),
        case("unknown-task", None, tid="T-missing"),
        case("feedback-string-is-an-attribute-error", _t(columns=_notes('{"feedback": "oops"}')), rust="not_started", why="notes.feedback that is not a list: Python raises AttributeError"),
        case("feedback-dict-is-an-attribute-error", _t(columns=_notes('{"feedback": {"a": 1}}')), rust="not_started", why="notes.feedback that is not a list: Python raises AttributeError"),
        case("feedback-null-is-an-attribute-error", _t(columns=_notes('{"feedback": null}')), rust="not_started", why="notes.feedback that is not a list: Python raises AttributeError"),
        case("feedback-number-is-an-attribute-error", _t(columns=_notes('{"feedback": 3}')), rust="not_started", why="notes.feedback that is not a list: Python raises AttributeError"),
    ]


def _create_cases() -> list[Case]:
    def case(cid: str, task: dict[str, Any], *seeds: Seed, rust: str = "same", why: str = "", extra: tuple[Op, ...] = (), before: tuple[Op, ...] = ()) -> Case:
        return Case(f"create-{cid}", "create_task", seeds, (*before, ("create", task), *extra), rust, why)

    def base(task_id: str = "T-new", **kw: Any) -> dict[str, Any]:
        return {"task_id": task_id, "correlation_id": "C-OPS", "capability": "code", **kw}

    every = base(
        title="Everything", parent_id="T-1", dag_depth=2, inputs=[{"kind": "doc", "uri": "u"}], acceptance=["done", "tested"],
        budget={"max_tokens": 10, "max_wall_s": 20.5, "max_cost_usd": 1}, risk_class="high", priority="P0", depends_on=["T-1", "T-2"],
        agent_id="A05", max_attempts=5, notes={"gate": "review", "gate_for": ["T-1"], "gates": []},
    )
    return [
        case("minimal-defaults", base()),
        case("every-optional-field", every),
        case("with-dependencies", base(depends_on=["T-1", "T-2"], dag_depth=1), _t()),
        case("unknown-dependencies-are-accepted", base(depends_on=["T-nope", "T-also-nope"])),
        case("falsy-inputs-null", base(inputs=None, acceptance=None, budget=None, notes=None, depends_on=None)),
        case("falsy-empty-containers", base(inputs={}, acceptance={}, budget=[], notes={}, depends_on=[])),
        case("falsy-scalars", base(inputs="", acceptance=0, budget=False)),
        case("truthy-odd-types", base(inputs={"a": 1}, acceptance="text", budget=[1, 2])),
        case("budget-given", base(budget={"max_tokens": 1})),
        case("dag-depth-and-attempts-extremes", base(dag_depth=-1, max_attempts=0)),
        case("i64-max", base(dag_depth=I64_MAX, max_attempts=I64_MAX)),
        case("unicode-title-and-notes", base(title="ünï ✓ \"q\" \\", notes={"ünï": "\U0001f600", "list": [1, 2.5]})),
        case("empty-title", base(title="")),
        case("agent-and-parent-null", base(agent_id=None, parent_id=None)),
        case("odd-risk-class-and-priority-are-stored", base(risk_class="weird", priority="P9")),
        case("safe-id-forms", base("A.b-c_d.9")),
        case("id-128-characters", base("a" * 128)),
        case("then-set-notes-on-the-new-task", base(), extra=(("notes", "T-new", {"x": 1}),)),
        case("then-transition-of-the-new-task", base(), extra=(("transition", "T-new", "VALIDATED", "A01", "validated"),)),
        case("two-creates", base("T-a"), extra=(("create", base("T-b", depends_on=["T-a"])),)),
        case("create-then-set-depends-on-of-an-old-task", base(), _t(), extra=(("deps", "T-1", ["T-new"]),)),
        case("duplicate-id-with-a-seed", base("T-1"), _t(), rust="not_started", why="a task id that exists already: Python raises sqlite3.IntegrityError"),
        case("duplicate-id-inside-the-list", base("T-a"), extra=(("create", base("T-a")),), rust="not_started", why="a task id that exists already: Python raises sqlite3.IntegrityError"),
        case("duplicate-after-good-ops-rolls-back-everything", base("T-a"), _t(), before=(("notes", "T-1", {"a": 1}),), extra=(("create", base("T-1")),), rust="not_started", why="a task id that exists already: Python raises sqlite3.IntegrityError"),
        case("empty-id", base(""), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("id-with-space", base("has space"), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("id-leading-dot", base(".hidden"), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("id-double-dot", base("a..b"), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("id-slash", base("a/b"), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("id-129-characters", base("a" * 129), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("id-non-ascii", base("t-é"), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("title-null", base(title=None), rust="not_started", why="a title that is not text: Python stores NULL"),
        case("dag-depth-text", base(dag_depth="2"), rust="not_started", why="a dag_depth that is not an integer"),
        case("max-attempts-float", base(max_attempts=2.5), rust="not_started", why="a max_attempts that is not an integer"),
        case("notes-list", base(notes=[1, 2]), rust="not_started", why="initial notes that are not an object"),
        case("depends-on-not-text", base(depends_on=[1, 2]), rust="not_started", why="a dependency that is not text"),
        case("correlation-null-is-a-constraint-error", base(correlation_id=None), rust="not_started", why="a correlation_id that is not text: NOT NULL"),
    ]


def _deps_cases() -> list[Case]:
    def case(cid: str, seed: Seed | None, deps: list[Any], rust: str = "same", why: str = "", tid: str = "T-1") -> Case:
        return Case(f"deps-{cid}", "set_depends_on", (seed,) if seed is not None else (), (("deps", tid, deps),), rust, why)

    return [
        case("empty-to-list", _t(), ["T-a", "T-b"]),
        case("replaces-the-list", _t(columns=(("depends_on", '["T-old"]'),)), ["T-new"]),
        case("to-empty", _t(columns=(("depends_on", '["T-old"]'),)), []),
        case("unknown-dependency-is-accepted", _t(), ["T-does-not-exist"]),
        case("duplicates-are-kept", _t(), ["T-a", "T-a"]),
        case("unicode-id", _t(), ["T-é", "\U0001f600"]),
        case("unknown-task", None, ["T-a"], tid="T-missing"),
        case("non-text-dependency", _t(), [1], "not_started", "a dependency that is not text"),
    ]


def _transition_cases() -> list[Case]:
    def case(cid: str, seeds: tuple[Seed, ...], *ops: Op, rust: str = "same", why: str = "") -> Case:
        return Case(f"transition-{cid}", "transition", seeds, ops, rust, why)

    both = '{"result": {"summary": "fail"}, "meta": {"tokens": 5}, "keep": [1, 2]}'
    return [
        case("claim-planned", (_t(state="PLANNED"),), ("transition", "T-1", "CLAIMED", "agent-1", "auto-claim")),
        case("auto-claim-then-start", (_t(state="PLANNED"),), ("transition", "T-1", "CLAIMED", "agent-1", "auto-claim"), ("transition", "T-1", "IN_PROGRESS", "agent-1", "start")),
        case("to-review", (_t(),), ("transition", "T-1", "IN_REVIEW", "agent-1", "result accepted")),
        case("rework-stashes-the-notes", (_t(state="IN_REVIEW", columns=_notes(both)),), ("transition", "T-1", "CHANGES_REQUESTED", "A09", "needs work")),
        case("rework-cap-escalates-with-two-audit-rows", (_t(state="IN_REVIEW", columns=(("rework_loops", 2), ("notes", both))),), ("transition", "T-1", "CHANGES_REQUESTED", "A09", "needs work")),
        case("transition-then-notes-artifact-feedback-and-keys", (_t(),),
             ("transition", "T-1", "IN_REVIEW", "agent-1", "result accepted"),
             ("notes", "T-1", {"result": {"summary": "ok"}, "meta": {"tokens": 3}}),
             ("artifact", "T-1", "report", "file:///out/r.md", "1", "d1", "agent-1"),
             ("artifact", "T-1", "log", "file:///out/l.txt", "1", "d2", "agent-1"),
             ("feedback", "T-1", {"gate": "review", "source": "agent", "findings": []}),
             ("notes", "T-1", {"ingest_keys": [{"task_id": "T-1", "attempt": 0, "digest": "abc", "reply": "IN_REVIEW"}]})),
        case("heartbeat-notes-and-artifact", (_t(),), ("notes", "T-1", {"result": {"progress": 0.5}, "meta": {}}), ("artifact", "T-1", "doc", "u", "1", "", "agent-1")),
        case("transition-on-two-tasks", (_t("T-1"), _t("T-2", state="PLANNED")), ("transition", "T-1", "IN_REVIEW", "agent-1", "r1"), ("transition", "T-2", "CLAIMED", "agent-2", "r2")),
        case("rerun-chain-create-validate-plan-and-dependency-edit", (_t("T-1", state="IN_REVIEW"), _t("T-2", state="DONE", columns=(("depends_on", '["T-1"]'),))),
             ("create", {"task_id": "T-1.r1", "correlation_id": "C-OPS", "capability": "code", "title": "ops T-1 (rerun)", "depends_on": ["T-1"], "notes": {"gate": "review"}}),
             ("transition", "T-1.r1", "VALIDATED", "A01", ""),
             ("transition", "T-1.r1", "PLANNED", "A01", "gate rerun"),
             ("deps", "T-2", ["T-1", "T-1.r1"]),
             ("notes", "T-2", {"gate_stall": []})),
        case("failure-after-a-transition-rolls-the-transition-back", (_t(state="PLANNED"),), ("transition", "T-1", "CLAIMED", "agent-1", "claim"), ("notes", "T-missing", {"a": 1})),
        case("failure-last-rolls-everything-back", (_t(),), ("notes", "T-1", {"a": 1}), ("artifact", "T-1", "k", "u", "1", "d", "p"), ("feedback", "T-1", {"x": 1}), ("deps", "T-1", ["T-a"]), ("notes", "T-missing", {"a": 1})),
        case("python-accepts-but-rust-refuses-a-later-op", (_t(),), ("notes", "T-1", {"a": 1}), ("create", {"task_id": "bad id", "correlation_id": "C-OPS", "capability": "code"}), rust="not_started", why="a task id outside the safe id rule: Python accepts any text"),
        case("two-notes-ops-merge-in-sequence", (_t(columns=_notes('{"a": 1}')),), ("notes", "T-1", {"b": 2, "a": 10}), ("notes", "T-1", {"c": 3, "b": 20})),
        case("notes-and-feedback-share-the-notes-text", (_t(),), ("notes", "T-1", {"x": 1}), ("feedback", "T-1", {"f": 1}), ("notes", "T-1", {"y": 2}), ("feedback", "T-1", {"f": 2})),
        case("two-tasks-interleaved", (_t("T-1"), _t("T-2")), ("notes", "T-1", {"a": 1}), ("notes", "T-2", {"b": 2}), ("artifact", "T-2", "k", "u", "1", "d", "p"), ("feedback", "T-1", {"c": 3})),
    ]


def build_cases() -> list[Case]:
    cases = [*_set_notes_cases(), *_artifact_cases(), *_feedback_cases(), *_create_cases(), *_deps_cases(), *_transition_cases()]
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)):
        raise AssertionError("duplicate case ids")
    return cases


def constants() -> Row:
    """What a change of the store's defaults must show up in the vectors."""
    return {"now": NOW, "create_at": CREATE_AT, "default_max_attempts": taskstore.DEFAULT_MAX_ATTEMPTS}


def render() -> str:
    """The whole vector file as text, regenerated from the real `TaskStore`."""
    clock = _Clock()
    saved = taskstore.time
    taskstore.time = clock  # type: ignore[assignment]
    try:
        lines = ",\n".join("  " + json.dumps(run_case(case, clock)) for case in build_cases())
    finally:
        taskstore.time = saved
    head = json.dumps({"schema": SCHEMA, "constants": constants()})
    return f'{head[:-1]}, "cases": [\n{lines}\n]}}\n'


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
