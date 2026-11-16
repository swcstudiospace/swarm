#!/usr/bin/env python3
"""Vectors for the pure Rust transition decision (`crates/gsd-route/src/transition_plan.rs`), generated from the REAL `TaskStore`.

Every case seeds a task row in a temporary store, runs the real Python write path (`check_manual_transition` for a manual request, then
`TaskStore.transition`) and records the task row before, the columns that changed, the audit rows and the error (code, message,
details) or the exception type and text. The Rust test replays the same inputs through `decide_transition`, applies the plan to the same
row and compares row and audit rows exactly; a case Rust cannot answer byte for byte must come back `not_started`.

Determinism: the clock `taskstore.time` is replaced by a fixed one, so `updated_at`, every `ts` and `notes.verdicts_since` (`now + 0.001`)
are reproducible; the `live.json` observer is switched off; ids and rows are fixed; keys are sorted. APPROVED depends on the host's
signing keys and on verdict rows: those inputs are not seeded but injected as the `approval` facts (the real key, gate and issuer helpers
of the store are replaced for the one call), so no key material or envelope appears in the file. Exception texts are those of
CPython 3.12.

`python3 tests/parity/gen_transition_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from swarm import taskstore  # noqa: E402
from swarm.errors import SwarmError  # noqa: E402
from swarm.results import check_manual_transition  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "transition.json"
SCHEMA = "gsd-route.transition-vectors.v1"
CREATE_AT = 1_700_000_000.0
NOW = 1_700_000_123.456789
I64_MAX = 9_223_372_036_854_775_807
STATES = [state.value for state in taskstore.TaskState]
INSECURE_PLACEHOLDER = "placeholder"  # insecure_dev_key() is only compared with None; the value is never used

Row = dict[str, Any]
Approval = dict[str, Any]


@dataclass(frozen=True)
class Spec:
    """One case: how the task is seeded, what is requested and what Rust must do."""

    case_id: str
    group: str
    to: str
    state: str | None = "IN_PROGRESS"  # None: the task does not exist
    manual: bool = True
    attempt: object = 0
    max_attempts: object = 3
    rework_loops: object = 0
    notes: dict[str, Any] | None = field(default=None)
    notes_text: str | None = None  # raw `notes` column text, wins over `notes`
    columns: tuple[tuple[str, object], ...] = ()  # other raw column overrides
    actor: str = "A01"
    reason: str = "manual"
    now: float = NOW
    approval: Approval | None = None
    rust_not_started: bool = False  # Python answers; Rust cannot match it byte for byte, so it must refuse


class _Clock:
    """Stand-in for the `time` module of `swarm.taskstore`: `time()` returns the value last set."""

    def __init__(self) -> None:
        self.now = CREATE_AT

    def time(self) -> float:
        return self.now


def approval(*, require_key: bool = False, real_key: bool = True, dev_key: bool = False,
             missing: tuple[str, ...] = (), unaccepted: tuple[str, ...] = ()) -> Approval:
    """Host-supplied APPROVED facts, in the wire form the Rust test reads."""
    return {"require_key": require_key, "real_key": real_key, "dev_key": dev_key,
            "missing_gates": list(missing), "unaccepted_gates": list(unaccepted)}


def _specs() -> list[Spec]:
    specs: list[Spec] = []
    happy = approval()
    for src in STATES:
        for dst in STATES:
            gate_open = (src, dst) == ("IN_REVIEW", "APPROVED")
            specs.append(Spec(f"matrix-{src}-{dst}", "matrix", dst, state=src, reason="matrix",
                              approval=happy if gate_open else None))
    specs += _rework_specs() + _claim_specs() + _gate_specs() + _state_name_specs() + _approval_specs() + _refusal_specs()
    return specs


def _rework_specs() -> list[Spec]:
    def rework(case_id: str, notes: dict[str, Any] | None = None, loops: object = 0, **kw: Any) -> Spec:
        return Spec(f"rework-{case_id}", "rework", "CHANGES_REQUESTED", state="IN_REVIEW", notes=notes, rework_loops=loops,
                    reason=kw.pop("reason", "needs work"), **kw)

    evidence = [{"result": {"round": 1}}]
    both = {"result": {"summary": "fail", "score": 0.1}, "meta": {"tokens": 5}, "keep": [1, 2]}
    return [
        rework("plain", {}),
        rework("null-notes", None),
        rework("result-meta", both),
        rework("meta-first-order", {"meta": {"a": 1}, "z": 1, "result": {"b": 2}}),
        rework("only-meta", {"meta": {"tokens": 3}}),
        rework("falsy-result-empty-dict", {"result": {}}),
        rework("falsy-result-meta", {"result": 0, "meta": ""}),
        rework("falsy-null-false", {"result": None, "meta": False}),
        rework("falsy-list-float", {"result": [], "meta": 0.0}),
        rework("truthy-str-int", {"result": "text", "meta": 7}),
        rework("truthy-true", {"result": True}),
        rework("truthy-float", {"meta": 0.5}),
        rework("evidence-list", {"result": {"round": 2}, "rework_evidence": evidence, "verdicts_since": 5.5}, loops=1),
        rework("evidence-str", {"result": {"round": 2}, "rework_evidence": "oops", "z": 1}),
        rework("evidence-null", {"result": {"round": 2}, "rework_evidence": None}),
        rework("evidence-str-no-prior", {"rework_evidence": "oops"}),
        rework("verdicts-since-keeps-position", {"verdicts_since": 1.5, "a": 1}),
        rework("verdicts-since-zero-int", {"verdicts_since": 0, "a": 1}),
        rework("loops-1", both, loops=1),
        rework("loops-negative", both, loops=-5),
        rework("unicode-notes", {"ünï": "✓", "emoji": "\U0001F600", "result": {"k": "é"}}),
        rework("reason-empty", both, reason=""),
        rework("reason-unicode", both, reason="ünï ✓ \"q\" \\ end", actor="A09"),
        rework("reason-newline", both, reason="line\nbreak\ttab"),
        rework("time-zero", both, now=0.0),
        rework("time-fraction", both, now=1761600000.987654321),
        rework("time-large", both, now=1e12 + 0.1),
        rework("cap-no-prior", {"a": 1}, loops=2),
        rework("cap-prior", both, loops=2),
        rework("cap-prior-evidence", {"result": {"round": 3}, "rework_evidence": evidence, "verdicts_since": 9.5}, loops=2),
        rework("cap-evidence-str", {"result": {"round": 3}, "rework_evidence": "oops"}, loops=2),
        rework("cap-loops-3", both, loops=3),
        rework("cap-loops-7-empty-reason", both, loops=7, reason=""),
        rework("cap-loops-i64-max", both, loops=I64_MAX),
        rework("cap-null-notes", None, loops=2),
        rework("cap-time", both, loops=2, now=1761600000.987654321),
        Spec("rework-null-loops", "rework", "CHANGES_REQUESTED", state="IN_REVIEW", rework_loops=None, notes={}),
        Spec("rework-text-loops", "rework", "CHANGES_REQUESTED", state="IN_REVIEW", rework_loops="many", notes={}),
        Spec("rework-null-loops-other-target", "rework", "FAILED", state="IN_REVIEW", rework_loops=None, notes={}),
        Spec("rework-non-manual", "rework", "CHANGES_REQUESTED", state="IN_REVIEW", manual=False, notes=both),
        Spec("rework-non-manual-cap", "rework", "CHANGES_REQUESTED", state="IN_REVIEW", manual=False, notes=both, rework_loops=2),
    ]


def _claim_specs() -> list[Spec]:
    specs: list[Spec] = []
    for src in ("PLANNED", "RETRY", "BLOCKED"):
        for attempt, cap in ((0, 3), (2, 3), (3, 3), (5, 3), (0, 0), (0, 1), (-1, 0), (I64_MAX, I64_MAX), (I64_MAX - 1, I64_MAX)):
            specs.append(Spec(f"claim-{src}-{attempt}-of-{cap}", "claim", "CLAIMED", state=src, attempt=attempt, max_attempts=cap,
                              reason="lease"))
    specs += [
        Spec("claim-null-attempt", "claim", "CLAIMED", state="PLANNED", attempt=None),
        Spec("claim-text-attempt", "claim", "CLAIMED", state="PLANNED", attempt="abc"),
        Spec("claim-null-max", "claim", "CLAIMED", state="PLANNED", max_attempts=None),
        Spec("claim-null-attempt-other-target", "claim", "FAILED", state="PLANNED", attempt=None, max_attempts=None),
        Spec("claim-exhausted-other-target", "claim", "FAILED", state="PLANNED", attempt=9, max_attempts=3),
        Spec("claim-non-manual", "claim", "CLAIMED", state="PLANNED", manual=False, attempt=1),
        Spec("claim-attempt-untouched-in-progress", "claim", "IN_PROGRESS", state="CLAIMED", attempt=2),
    ]
    return specs


def _gate_specs() -> list[Spec]:
    gate = {"gate": "review", "gate_for": ["T1"]}
    specs = [
        Spec("gate-to-in-review-manual", "gate", "IN_REVIEW", notes=gate),
        Spec("gate-to-in-review-store", "gate", "IN_REVIEW", notes=gate, manual=False),
        Spec("gate-to-failed-manual", "gate", "FAILED", notes=gate),
        Spec("gate-illegal-in-review-manual", "gate", "IN_REVIEW", state="CLAIMED", notes=gate),
        Spec("gate-illegal-in-review-store", "gate", "IN_REVIEW", state="CLAIMED", notes=gate, manual=False),
        Spec("gate-unknown-state-name", "gate", "BOGUS", notes=gate),
        Spec("gate-lowercase-in-review", "gate", "in_review", notes=gate),
        Spec("gate-weird-row-state", "gate", "IN_REVIEW", state="WEIRD", notes=gate),
        Spec("gate-to-cancelled-manual", "gate", "CANCELLED", state="IN_REVIEW", notes=gate),
    ]
    for label, value in (("empty-str", ""), ("null", None), ("zero", 0), ("false", False), ("empty-list", [])):
        specs.append(Spec(f"gate-falsy-{label}", "gate", "IN_REVIEW", notes={"gate": value}))
    for label, value in (("int", 1), ("list", ["x"]), ("dict", {"a": 1}), ("true", True), ("space", " ")):
        specs.append(Spec(f"gate-truthy-{label}", "gate", "IN_REVIEW", notes={"gate": value}))
    return specs


def _state_name_specs() -> list[Spec]:
    specs: list[Spec] = []
    for label, name in (("bogus", "BOGUS"), ("empty", ""), ("lower", "in_progress"), ("lead-space", " DONE"),
                        ("trail-newline", "IN_REVIEW\n"), ("title-case", "Cancelled")):
        for manual in (True, False):
            specs.append(Spec(f"name-{label}-{'manual' if manual else 'store'}", "state-name", name, manual=manual))
    specs += [
        Spec("unknown-task-manual", "unknown-task", "IN_PROGRESS", state=None),
        Spec("unknown-task-manual-bad-name", "unknown-task", "BOGUS", state=None),
        Spec("unknown-task-store", "unknown-task", "IN_PROGRESS", state=None, manual=False),
        Spec("unknown-task-store-bad-name", "unknown-task", "BOGUS", state=None, manual=False),
        Spec("unknown-task-manual-in-review", "unknown-task", "IN_REVIEW", state=None),
    ]
    return specs


def _approval_specs() -> list[Spec]:
    def approved(case_id: str, facts: Approval | None, **kw: Any) -> Spec:
        return Spec(f"approved-{case_id}", "approval", "APPROVED", state=kw.pop("state", "IN_REVIEW"), approval=facts,
                    reason=kw.pop("reason", "gates pass"), **kw)

    return [
        approved("ok", approval()),
        approved("ok-require-key", approval(require_key=True)),
        approved("ok-dev-key", approval(real_key=False, dev_key=True)),
        approved("ok-store", approval(), manual=False),
        approved("ok-other-actor", approval(), actor="A10"),
        approved("require-key-no-key", approval(require_key=True, real_key=False)),
        approved("require-key-no-key-dev-ignored", approval(require_key=True, real_key=False, dev_key=True)),
        approved("no-key", approval(real_key=False, dev_key=False)),
        approved("no-key-beats-missing", approval(real_key=False, dev_key=False, missing=("review",))),
        approved("require-key-beats-missing", approval(require_key=True, real_key=False, missing=("review",))),
        approved("missing-one", approval(missing=("review",))),
        approved("missing-three", approval(missing=("review", "quality", "security"))),
        approved("missing-punctuation", approval(missing=("gate_a", "gate-b", "gate c", "a.b"))),
        approved("missing-beats-unaccepted", approval(missing=("quality",), unaccepted=("review",))),
        approved("unaccepted-one", approval(unaccepted=("review",))),
        approved("unaccepted-sorted", approval(unaccepted=("zeta", "alpha", "Mid", "beta"))),
        approved("illegal-from-changes-requested", approval(), state="CHANGES_REQUESTED"),
        approved("illegal-from-in-progress-no-facts", None, state="IN_PROGRESS"),
        approved("illegal-no-facts-store", None, state="DONE", manual=False),
        approved("time-ignored", approval(), now=0.0),
        Spec("approved-facts-ignored-for-other-target", "approval", "FAILED", state="IN_REVIEW",
             approval=approval(real_key=False, dev_key=False, missing=("review",))),
        approved("missing-quote-rust-refuses", approval(missing=("it's",)), rust_not_started=True),
        approved("missing-double-quote", approval(missing=('say "x"',))),
        approved("missing-backslash-rust-refuses", approval(missing=("a\\b",)), rust_not_started=True),
        approved("missing-non-ascii-rust-refuses", approval(missing=("gate-é",)), rust_not_started=True),
        approved("unaccepted-quote-rust-refuses", approval(unaccepted=("a'b",)), rust_not_started=True),
        approved("missing-tab-rust-refuses", approval(missing=("a\tb",)), rust_not_started=True),
    ]


def _refusal_specs() -> list[Spec]:
    deep = '{"result": ' + "[" * 150 + "]" * 150 + "}"
    return [
        Spec("notes-corrupt-text", "rust-refuses", "CHANGES_REQUESTED", state="IN_REVIEW", notes_text="{not json",
             rust_not_started=True),
        Spec("notes-big-int", "rust-refuses", "CHANGES_REQUESTED", state="IN_REVIEW",
             notes_text='{"big": 12345678901234567890, "result": {"a": 1}}', rust_not_started=True),
        Spec("notes-too-deep", "rust-refuses", "CHANGES_REQUESTED", state="IN_REVIEW", notes_text=deep, rust_not_started=True),
        Spec("notes-corrupt-other-target", "rust-refuses", "FAILED", state="IN_REVIEW", notes_text="{not json",
             rust_not_started=True),
        Spec("notes-array-text", "notes", "CHANGES_REQUESTED", state="IN_REVIEW", notes_text="[1, 2]"),
        Spec("notes-string-text", "notes", "CHANGES_REQUESTED", state="IN_REVIEW", notes_text='"just text"'),
        Spec("notes-number-text", "notes", "CHANGES_REQUESTED", state="IN_REVIEW", notes_text="5"),
        Spec("notes-empty-text", "notes", "CHANGES_REQUESTED", state="IN_REVIEW", notes_text=""),
        Spec("notes-array-other-target", "notes", "FAILED", state="IN_REVIEW", notes_text="[1, 2]"),
        Spec("notes-duplicate-keys", "notes", "CHANGES_REQUESTED", state="IN_REVIEW",
             notes_text='{"result": {"a": 1}, "x": 1, "result": {"b": 2}}'),
        Spec("row-corrupt-inputs", "rust-refuses", "FAILED", state="IN_REVIEW", columns=(("inputs", "not json"),)),
        Spec("row-corrupt-budget", "rust-refuses", "FAILED", state="IN_REVIEW", columns=(("budget", "{"),)),
        Spec("row-weird-state", "row", "FAILED", state="WEIRD"),
        Spec("row-weird-state-store", "row", "CANCELLED", state="WEIRD", manual=False),
        Spec("row-lowercase-state", "row", "FAILED", state="in_review"),
        Spec("row-null-depends-on", "row", "FAILED", state="IN_REVIEW", columns=(("depends_on", None),)),
        Spec("row-empty-json-columns", "row", "FAILED", state="IN_REVIEW",
             columns=(("inputs", ""), ("outputs", None), ("acceptance", ""), ("budget", ""))),
    ]


class _Runner:
    """An in-memory store (no fsync on a loaded host) plus the fixed clock; one task per case."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.store = taskstore.TaskStore(":memory:")
        self.store._notify_committed = lambda: None  # type: ignore[method-assign]  # no live.json refresh in a vector run

    def row(self, task_id: str) -> Row | None:
        found = self.store.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return None if found is None else dict(found)

    def history(self, task_id: str) -> list[Row]:
        return [{"from_state": h["from_state"], "to_state": h["to_state"], "actor": h["actor"], "reason": h["reason"], "ts": h["ts"]}
                for h in self.store.history(task_id)]

    def seed(self, task_id: str, spec: Spec) -> None:
        self.clock.now = CREATE_AT
        self.store.create(task_id=task_id, correlation_id="C-VECTORS", capability="code", title=f"vector {spec.case_id}")
        notes_text = spec.notes_text if spec.notes_text is not None else (None if spec.notes is None else json.dumps(spec.notes))
        raw: dict[str, object] = {"state": spec.state, "attempt": spec.attempt, "max_attempts": spec.max_attempts,
                                  "rework_loops": spec.rework_loops, "notes": notes_text, **dict(spec.columns)}
        sets = ", ".join(f"{name}=?" for name in raw)
        self.store.conn.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", [*raw.values(), task_id])  # noqa: S608 - fixed column names
        self.store.conn.commit()

    @contextmanager
    def approval_facts(self, facts: Approval | None) -> Iterator[None]:
        """Replace the key policy and the gate helpers by the injected facts for one call; restore everything afterwards."""
        if facts is None:
            yield
            return
        saved_env = os.environ.get("SWARM_REQUIRE_KEY")
        saved_fns = (taskstore.real_key_configured, taskstore.insecure_dev_key)
        missing, unaccepted = list(facts["missing_gates"]), list(facts["unaccepted_gates"])
        if facts["require_key"]:
            os.environ["SWARM_REQUIRE_KEY"] = "1"
        else:
            os.environ.pop("SWARM_REQUIRE_KEY", None)
        taskstore.real_key_configured = lambda: bool(facts["real_key"])  # type: ignore[assignment]
        taskstore.insecure_dev_key = lambda: INSECURE_PLACEHOLDER if facts["dev_key"] else None  # type: ignore[assignment]
        self.store.missing_gates = lambda task_id: list(missing)  # type: ignore[method-assign]
        self.store.unaccepted_gates = lambda task_id, reasons=None: dict.fromkeys(unaccepted, "unaccepted")  # type: ignore[method-assign]
        try:
            yield
        finally:
            taskstore.real_key_configured, taskstore.insecure_dev_key = saved_fns
            del self.store.missing_gates, self.store.unaccepted_gates
            if saved_env is None:
                os.environ.pop("SWARM_REQUIRE_KEY", None)
            else:
                os.environ["SWARM_REQUIRE_KEY"] = saved_env

    def run(self, spec: Spec) -> Row:
        task_id = f"T-{spec.case_id}"
        if spec.state is not None:
            self.seed(task_id, spec)
        before, before_hist = self.row(task_id), self.history(task_id)
        self.clock.now = spec.now
        outcome: Row
        try:
            with self.approval_facts(spec.approval):
                if spec.manual:
                    check_manual_transition(self.store, task_id, spec.to)
                self.store.transition(task_id, spec.to, actor=spec.actor, reason=spec.reason)
            outcome = {"outcome": "ok"}
        except SwarmError as err:
            outcome = {"outcome": "error", "error": {"code": err.code.value, "message": str(err), "details": err.details}}
        except Exception as err:  # noqa: BLE001 - the generic handler of script_base turns any of these into a traceback reply
            outcome = {"outcome": "exception", "exception": type(err).__name__, "text": str(err)}
        after, after_hist = self.row(task_id), self.history(task_id)
        if outcome["outcome"] == "ok":
            assert before is not None and after is not None
            outcome["row_diff"] = {name: value for name, value in after.items() if before[name] != value}
            outcome["audit"] = after_hist[len(before_hist):]
        else:
            assert (after, after_hist) == (before, before_hist), f"{spec.case_id}: a refused transition wrote something"
        return {"id": spec.case_id, "group": spec.group, "now": spec.now, "approval": spec.approval,
                "request": {"task_id": task_id, "to": spec.to, "actor": spec.actor, "reason": spec.reason, "manual": spec.manual},
                "rust_not_started": spec.rust_not_started, "task": before, "expect": outcome}


def constants() -> Row:
    """The Python constants the decision depends on, so a drift of any of them changes the committed file."""
    return {"states": STATES,
            "legal": {state.value: sorted(t.value for t in targets) for state, targets in taskstore.LEGAL_TRANSITIONS.items()},
            "max_rework_loops": taskstore.MAX_REWORK_LOOPS}


def build_cases() -> list[Row]:
    """Run every spec against the real `TaskStore` and return the recorded cases, in the fixed spec order."""
    clock = _Clock()
    saved = taskstore.time
    taskstore.time = clock  # type: ignore[assignment]
    try:
        runner = _Runner(clock)
        try:
            return [runner.run(spec) for spec in _specs()]
        finally:
            runner.store.conn.close()
    finally:
        taskstore.time = saved


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
