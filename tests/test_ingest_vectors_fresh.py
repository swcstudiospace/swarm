"""The committed ingest vectors still match the Python oracle.

`tests/parity/gen_ingest_vectors.py` seeds real in-memory `TaskStore`s, runs the real ingest route (`orch_status.ingest`: `validate_result`,
`apply_result`, `reconcile`) or the headless `apply_result`, and records the initial facts, the store after the call, the emitted events, the
reply and the refusals. This test regenerates that file in memory and compares it with the committed one
(`crates/gsd-route/tests/vectors/ingest.json`, replayed by the Rust tests of `ingest_plan.rs`), so a change to the claim rule, a refusal text,
the gate checks, the feedback shape, the replay key or its bound fails here until the vectors, and then the Rust planner, are regenerated:
`python3 tests/parity/gen_ingest_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_ingest_vectors.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_ingest_vectors", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _load()


@pytest.fixture(scope="module")
def regenerated(gen: ModuleType) -> str:
    return str(gen.render())


@pytest.fixture(scope="module")
def doc(regenerated: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(regenerated)
    return parsed


def test_committed_vectors_are_what_the_oracle_produces(gen: ModuleType, regenerated: str) -> None:
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    if committed != regenerated:
        old, new = json.loads(committed), json.loads(regenerated)
        changed = [a["name"] for a, b in zip(old["cases"], new["cases"], strict=False) if a != b]
        pytest.fail(f"ingest.json is stale: constants changed={old['constants'] != new['constants']}, "
                    f"cases {len(old['cases'])} -> {len(new['cases'])}, first changed cases {changed[:5]}; "
                    "run `python3 tests/parity/gen_ingest_vectors.py`")


def test_regeneration_is_deterministic(gen: ModuleType, regenerated: str) -> None:
    assert gen.render() == regenerated


def test_vectors_hold_no_key_material(gen: ModuleType, regenerated: str) -> None:
    assert gen.base.derive_key(gen.base.KEY_SEED) not in regenerated
    assert gen.base.KEY_SEED not in regenerated
    assert "SWARM_SIGNING_KEY" not in regenerated


def test_case_names_are_unique_and_every_scenario_group_is_present(doc: dict[str, Any]) -> None:
    names = [case["name"] for case in doc["cases"]]
    assert len(names) == len(set(names))
    groups = {case["group"] for case in doc["cases"]}
    assert groups >= {"matrix", "claim", "blocked", "agent", "reason", "outputs", "notes", "reconcile", "gate", "feedback", "replay",
                      "replay-bound", "replay-stored", "invalid"}
    for mode in ("ingest", "headless"):
        for state in doc["constants"]["states"]:
            for reported in ("IN_PROGRESS", "IN_REVIEW", "FAILED", "BLOCKED"):
                assert f"matrix-{mode}-{state}-{reported}" in names


def test_every_outcome_the_rust_planner_must_reproduce_is_recorded(doc: dict[str, Any]) -> None:
    cases = doc["cases"]
    events = {event["type"] for case in cases for event in case["expect"].get("events", [])}
    assert events == {"task.claimed", "task.result.rejected", "task.result.deduplicated", "task.approval.skipped", "escalation.request"}
    messages = [case["expect"]["error"]["message"] for case in cases if "error" in case["expect"]]
    for fragment in ("is BLOCKED; A01 releases it", "has unmet dependencies", "gate script not run for", "review verdict mismatch",
                     "illegal transition", "max_attempts exceeded", "task.result invalid", "no JSON result block", "task_id mismatch"):
        assert any(fragment in message for message in messages), fragment
    replies = {case["expect"]["reply"] for case in cases if "reply" in case["expect"]}
    assert replies >= {"IN_PROGRESS", "IN_REVIEW", "FAILED", "BLOCKED", "CLAIMED"}
    errors = {case["expect"]["python_error"] for case in cases if "python_error" in case["expect"]}
    assert {"TypeError", "AttributeError"} <= errors
    assert any(case["rust"] == "not_started" and "python_error" not in case["expect"] for case in cases)


def test_a_replay_changes_nothing_and_a_refusal_changes_nothing(doc: dict[str, Any]) -> None:
    deduplicated = [case for case in doc["cases"] if any(e["type"] == "task.result.deduplicated" for e in case["expect"].get("events", []))]
    assert len(deduplicated) >= 10
    for case in deduplicated:
        assert case["expect"]["artifacts"] == [] or case["expect"]["transitions"] == [] or case["group"] != "replay-stored"
    for case in doc["cases"]:
        if "error" in case["expect"] and case["rust"] == "plan":
            assert case["expect"]["tasks"] == case["tasks_before"], case["name"]
            assert case["expect"]["transitions"] == [] and case["expect"]["artifacts"] == [], case["name"]


def test_the_bound_is_32_and_the_key_is_the_last_write(doc: dict[str, Any]) -> None:
    assert doc["constants"]["ingest_keys_bound"] == 32
    for name in ("replay-bound-newest-replays", "replay-bound-new-result-drops-the-oldest", "replay-bound-evicted-result-applies-again"):
        case = next(c for c in doc["cases"] if c["name"] == name)
        row = next(r for r in case["expect"]["tasks"] if r["task_id"] == "T-1")
        assert len(json.loads(row["notes"])["ingest_keys"]) <= 32
    evicted = next(c for c in doc["cases"] if c["name"] == "replay-bound-33-keys-oldest-evicted")
    assert not any(e["type"] == "task.result.deduplicated" for e in evicted["expect"]["events"])
    kept = next(c for c in doc["cases"] if c["name"] == "replay-bound-32-keys-oldest-replays")
    assert any(e["type"] == "task.result.deduplicated" for e in kept["expect"]["events"])
    for case in doc["cases"]:
        replayed = any(e["type"] == "task.result.deduplicated" for e in case["expect"].get("events", []))
        if case["mode"] == "headless" or "reply" not in case["expect"] or replayed:
            continue
        try:
            before = json.loads(next(r for r in case["tasks_before"] if r["task_id"] == case["task_id"])["notes"] or "{}")
        except ValueError:
            continue
        if not isinstance(before, dict) or "ingest_keys" in before or "result" in before:
            continue
        row = next(r for r in case["expect"]["tasks"] if r["task_id"] == case["task_id"])
        notes = list(json.loads(row["notes"]))
        assert "ingest_keys" in notes and "result" in notes and notes.index("ingest_keys") > notes.index("result"), case["name"]


@pytest.mark.parametrize(("patch", "what"), [
    (("results", "INGEST_KEYS_BOUND", 31), "the key bound"),
    (("taskstore", "SATISFIED_STATES", {"DONE"}), "the dependency rule"),
    (("results", "PASS_VERDICTS", frozenset({"pass"})), "the agent pass verdicts"),
    (("taskstore", "MAX_REWORK_LOOPS", 3), "the rework cap that reconcile applies after an ingest"),
])
def test_a_change_of_the_python_rules_changes_the_vectors(gen: ModuleType, regenerated: str, monkeypatch: pytest.MonkeyPatch,
                                                          patch: tuple[str, str, object], what: str) -> None:
    module, attribute, value = patch
    monkeypatch.setattr(getattr(gen, module), attribute, value)
    assert gen.render() != regenerated, f"{what} is not recorded in the vectors"
