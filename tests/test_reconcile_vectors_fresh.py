"""The committed reconcile vectors still match the Python oracle.

`tests/parity/gen_reconcile_vectors.py` seeds real in-memory `TaskStore`s, runs `swarm.results.reconcile` over them and records the initial
facts, the store after the call, the emitted events and the returned log. This test regenerates that file in memory and compares it with the
committed one (`crates/gsd-route/tests/vectors/reconcile.json`, replayed by the Rust tests of `reconcile_plan.rs`), so a change to the
rework cap, the rerun numbering, the approval rule, the stall rule or a message text fails here until the vectors, and then the Rust
planner, are regenerated: `python3 tests/parity/gen_reconcile_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_reconcile_vectors.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_reconcile_vectors", GENERATOR)
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
        pytest.fail(f"reconcile.json is stale: constants changed={old['constants'] != new['constants']}, "
                    f"cases {len(old['cases'])} -> {len(new['cases'])}, first changed cases {changed[:5]}; "
                    "run `python3 tests/parity/gen_reconcile_vectors.py`")


def test_regeneration_is_deterministic(gen: ModuleType, regenerated: str) -> None:
    assert gen.render() == regenerated


def test_vectors_hold_no_key_material(gen: ModuleType, regenerated: str) -> None:
    assert gen.derive_key(gen.KEY_SEED) not in regenerated
    assert gen.KEY_SEED not in regenerated
    assert "SWARM_SIGNING_KEY" not in regenerated


def test_case_names_are_unique_and_every_scenario_group_is_present(doc: dict[str, Any]) -> None:
    names = [case["name"] for case in doc["cases"]]
    assert len(names) == len(set(names))
    groups = {case["group"] for case in doc["cases"]}
    assert groups >= {"rework", "rework-state", "cap", "cap-stash", "lineage", "scan", "release", "dependants", "clone", "approve", "issuer",
                      "leased", "stall", "stall-order", "stall-reason", "stall-recorded", "waive", "correlations", "policy", "refused"}
    for state in doc["constants"]["states"]:
        assert f"rework-latest-member-{state}" in names
        assert f"issuer-{state}-covering" in names and f"issuer-{state}-not-covering" in names


def test_every_outcome_the_rust_planner_must_reproduce_is_recorded(doc: dict[str, Any]) -> None:
    planned = [case for case in doc["cases"] if "python_error" not in case["expect"] and case["rust"] == "plan"]
    log = "\n".join(line for case in planned for line in case["expect"]["notes_log"])
    for fragment in ("→ rework #1", "→ rework #2", "ESCALATED after 2 rework loops", ": DONE", "advisory, approval not attempted",
                     "gate stall ['review:absent']", "gate stall ['review:unaccepted']", "gate stall ['quality:absent', 'release:absent', 'review:expired', 'security:unsigned']"):
        assert fragment in log, fragment
    events = {event["type"] for case in planned for event in case["expect"]["events"]}
    assert events == {"escalation.request", "task.approval.skipped"}
    created = {row["task_id"] for case in planned for row in case["expect"]["tasks"][len(case["tasks_before"]):]}
    assert {"G-review.r1", "G-review.r3", "G-review.r11"} <= created
    errors = {case["expect"]["python_error"] for case in doc["cases"] if "python_error" in case["expect"]}
    assert "SwarmError: E-POLICY: fail-closed: no signing key configured" in errors
    assert "SwarmError: E-POLICY: fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured" in errors
    assert {"IntegrityError", "AttributeError", "KeyError"} <= errors
    assert any(case["rust"] == "not_started" and "python_error" not in case["expect"] for case in doc["cases"])


def test_reconcile_never_touches_another_correlation(doc: dict[str, Any]) -> None:
    for case in doc["cases"]:
        if "python_error" in case["expect"]:
            continue
        before = {row["task_id"]: row for row in case["tasks_before"] if row["correlation_id"] != case["correlation_id"]}
        after = {row["task_id"]: row for row in case["expect"]["tasks"] if row["correlation_id"] != case["correlation_id"]}
        assert before == after, case["name"]


def test_every_new_row_is_appended_in_creation_order_and_audited(doc: dict[str, Any]) -> None:
    for case in doc["cases"]:
        if "python_error" in case["expect"]:
            continue
        old = len(case["tasks_before"])
        new_rows = case["expect"]["tasks"][old:]
        assert [row["task_id"] for row in case["expect"]["tasks"][:old]] == [row["task_id"] for row in case["tasks_before"]]
        audits = [row for row in case["expect"]["transitions"] if row["from_state"] is None]
        assert [row["task_id"] for row in audits] == [row["task_id"] for row in new_rows], case["name"]
        assert all(row["actor"] == "system" and row["reason"] == "created" and row["to_state"] == "CREATED" for row in audits)
        for row in new_rows:
            assert re.fullmatch(r".+\.r[0-9]+", row["task_id"]) and row["state"] == "PLANNED" and row["title"].endswith(" (rerun)")


@pytest.mark.parametrize(("patch", "what"), [
    (("taskstore", "MAX_REWORK_LOOPS", 3), "the rework cap"),
    (("results", "RERUN_SUFFIX", re.compile(r"\.r\d+$")), "the lineage base"),
    (("taskstore", "ACCEPTED_ISSUER_STATES", frozenset({"DONE"})), "the issuer rule"),
])
def test_a_change_of_the_python_rules_changes_the_vectors(gen: ModuleType, regenerated: str, monkeypatch: pytest.MonkeyPatch,
                                                          patch: tuple[str, str, object], what: str) -> None:
    module, attribute, value = patch
    monkeypatch.setattr(getattr(gen, module), attribute, value)
    assert gen.render() != regenerated, f"{what} is not recorded in the vectors"
