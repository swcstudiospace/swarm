"""The committed plan vectors still match the Python oracle.

`tests/parity/gen_plan_vectors.py` runs the real `scripts/orch_plan.py` (`AgentScript.main`: argv parsing, `run()`, the printed reply) over
seeded in-memory `TaskStore`s and records the request, the host facts and what Python did (reply bytes, rows, audit rows, plan file,
events, graph binding, controller kickoff, refusals). This test regenerates that file in memory and compares it with the committed one
(`crates/gsd-route/tests/vectors/plan_derive.json`, replayed by the Rust tests of `plan_derive.rs`), so a change to a pattern row, a budget,
a refusal text, the reuse rule, the prefix rule, the blast-radius lanes or the plan file layout fails here until the vectors, and then the
Rust derivation, are regenerated: `python3 tests/parity/gen_plan_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_plan_vectors.py"
PROBE = ("_probe",)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_plan_vectors", GENERATOR)
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


@pytest.fixture(scope="module")
def probe(gen: ModuleType) -> str:
    return json.dumps(gen.build_cases(PROBE))


def test_committed_vectors_are_what_the_oracle_produces(gen: ModuleType, regenerated: str) -> None:
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    if committed != regenerated:
        old, new = json.loads(committed), json.loads(regenerated)
        changed = [a["name"] for a, b in zip(old["cases"], new["cases"], strict=False) if a != b]
        pytest.fail(f"plan_derive.json is stale: constants changed={old['constants'] != new['constants']}, units changed={old['units'] != new['units']}, "
                    f"cases {len(old['cases'])} -> {len(new['cases'])}, first changed cases {changed[:5]}; "
                    "run `python3 tests/parity/gen_plan_vectors.py`")


def test_a_changed_word_in_the_committed_file_is_detected(gen: ModuleType, regenerated: str) -> None:
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    assert committed == regenerated
    for word in ("Greptile review of the merged branch", "brief validated by A01", "max_tokens", "plan.updated"):
        assert word in committed
        assert committed.replace(word, word.replace("e", "E", 1), 1) != regenerated, word


def test_regeneration_of_a_subset_is_deterministic(gen: ModuleType, probe: str) -> None:
    assert json.dumps(gen.build_cases(PROBE)) == probe


def test_vectors_hold_no_key_material(gen: ModuleType, regenerated: str) -> None:
    assert gen.base.derive_key(gen.base.KEY_SEED) not in regenerated
    assert gen.base.KEY_SEED not in regenerated
    assert "SWARM_SIGNING_KEY" not in regenerated
    assert gen.FIXED_ROOT in regenerated


def test_no_recorded_path_names_the_temporary_directory(doc: dict[str, Any]) -> None:
    text = json.dumps(doc)
    assert "plan-vectors-" not in text


def test_case_names_are_unique_and_every_scenario_group_is_present(doc: dict[str, Any]) -> None:
    names = [case["name"] for case in doc["cases"]]
    assert len(names) == len(set(names))
    groups = {case["group"] for case in doc["cases"]}
    assert groups >= {"pattern", "brief", "dry-run", "prefix", "reuse", "graph", "signing", "kickoff", "custom", "parallel", "parallel-slices", "probe"}
    for pattern in doc["constants"]["pattern_names"]:
        for risk in ("low", "medium", "high"):
            assert f"pattern-{pattern}-{risk}" in names
            assert f"dry-run-{pattern}" in names


def test_every_outcome_the_rust_derivation_must_reproduce_is_recorded(doc: dict[str, Any]) -> None:
    cases = doc["cases"]
    expect = [case["expect"] for case in cases]
    assert any(e["plan_file"] and not e["error"] for e in expect), "a creation"
    assert any(e["error"] and e["tasks_unchanged"] for e in expect), "a refusal that leaves the store unchanged"
    assert any(e["python_error"] for e in expect), "a crash of the Python body"
    assert any(e["kickoff"] for e in expect), "a controller kickoff"
    assert any(e["graph_bind"] and e["graph_bind"]["explicit_graph_id"] for e in expect), "an explicit graph id"
    assert any(c["request"]["dry_run"] and not c["expect"]["error"] for c in cases), "a dry run"
    assert any(c["rust"] == "not_started" and c["expect"]["error"] for c in cases), "a decline where Python refuses with its own error"
    assert any(c["rust"] == "not_started" and not c["expect"]["error"] and not c["expect"]["python_error"] for c in cases), "a decline where Python succeeds"
    reused = [c for c in cases if c["expect"]["stdout"] and '"reused": true' in c["expect"]["stdout"]]
    assert reused and all(not c["expect"]["tasks"] and not c["expect"]["transitions"] and not c["expect"]["plan_file"] for c in reused)
    assert all(c["why"] for c in cases if c["rust"] == "not_started") and not any(c["why"] for c in cases if c["rust"] == "plan")


def test_a_creation_is_three_audit_rows_per_task(doc: dict[str, Any]) -> None:
    for case in doc["cases"]:
        expect = case["expect"]
        if case["rust"] != "plan" or expect["error"] or not expect["tasks"]:
            continue
        rows = [(t["task_id"], t["from_state"], t["to_state"], t["actor"], t["reason"]) for t in expect["transitions"]]
        pattern = case["name"]
        assert len(rows) == 3 * len(expect["tasks"]), pattern
        for task in expect["tasks"]:
            mine = [row for row in rows if row[0] == task["task_id"]]
            assert [(r[1], r[2], r[3]) for r in mine] == [(None, "CREATED", "system"), ("CREATED", "VALIDATED", "A01"), ("VALIDATED", "PLANNED", "A01")], pattern
            assert mine[1][4] == "brief validated by A01" and mine[2][4].startswith("pattern="), pattern


@pytest.mark.parametrize(("patch", "what"), [
    (("orch_plan", "_budget", lambda risk: {"max_tokens": 1}), "the budget table"),
    (("orch_plan", "_derived_prefix", lambda corr, n: "T" + corr.replace("-", "")[: n + 1]), "the derived prefix"),
    (("orch_plan", "_reusable", lambda *a, **k: None), "the reuse decision"),
    (("orch_plan", "_plan_sha_rows", lambda rows: [list(r[:8]) for r in rows]), "the rows the plan hash covers"),
    (("blast_radius", "_overlaps", lambda a, b: a == b), "the disjointness analysis"),
    (("blast_radius", "_DEFAULT_CAPABILITY", {"A05": "code.backend", "A06": "ux.spec", "A15": "docs.bundle"}), "the default lane capability"),
    (("blast_radius", "_GATE_ROWS", {"review": ("rv", "gate.review", "A09", "Review", {"role": "review"}),
                                     "quality": ("qa", "gate.quality", "A08", "Quality", {"role": "gate"}),
                                     "security": ("sec", "gate.security", "A10", "Security", {"role": "gate"}),
                                     "release": ("rel", "release.plan", "A12", "Release", {"role": "gate"})}), "the lane gate rows"),
    (("taskstore", "GATES_BY_RISK", {"low": ["review"], "medium": ["review"], "high": ["review", "quality"]}), "the gates of each risk class"),
])
def test_a_change_of_the_python_rules_changes_the_vectors(gen: ModuleType, probe: str, monkeypatch: pytest.MonkeyPatch,
                                                          patch: tuple[str, str, object], what: str) -> None:
    module, attribute, value = patch
    owner = gen.orch_plan if module == "orch_plan" else getattr(gen, module)
    monkeypatch.setattr(owner, attribute, value)
    assert json.dumps(gen.build_cases(PROBE)) != probe, f"{what} is not recorded in the vectors"


def test_a_change_of_a_pattern_row_changes_the_constants(gen: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    before = json.dumps(gen.constants())
    rows = list(gen.orch_plan.PATTERNS["hotfix"])
    monkeypatch.setitem(gen.orch_plan.PATTERNS, "hotfix", rows[:-1])
    assert json.dumps(gen.constants()) != before
