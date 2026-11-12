"""The committed transition vectors are what the real `TaskStore` produces today.

`crates/gsd-route/tests/vectors/transition.json` is the oracle for the Rust decision `decide_transition`. It is regenerated here in memory
from `swarm/taskstore.py` and `swarm/results.py` and compared with the committed file byte for byte, so a change of `LEGAL_TRANSITIONS`,
`MAX_REWORK_LOOPS`, a message text, the rework stash or the attempt cap fails this test until the vectors (and then the Rust code) are
regenerated: `python3 tests/parity/gen_transition_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_transition_vectors.py"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_transition_vectors", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves the defining module through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _load_generator()


@pytest.fixture(scope="module")
def committed(gen: ModuleType) -> str:
    return str(gen.VECTOR_PATH.read_text(encoding="utf-8"))


def test_committed_vectors_equal_the_regenerated_ones(gen: ModuleType, committed: str) -> None:
    assert gen.render() == committed, "stale vectors: run `python3 tests/parity/gen_transition_vectors.py` and review the diff"


def test_regeneration_is_deterministic(gen: ModuleType) -> None:
    assert gen.render() == gen.render()


def test_the_matrix_holds_every_state_pair_and_ids_are_unique(gen: ModuleType, committed: str) -> None:
    doc = json.loads(committed)
    states = doc["constants"]["states"]
    matrix = [(c["task"]["state"], c["request"]["to"]) for c in doc["cases"] if c["group"] == "matrix"]
    assert matrix == [(src, dst) for src in states for dst in states]
    assert len(matrix) == 196
    ids = [c["id"] for c in doc["cases"]]
    assert len(ids) == len(set(ids))


def test_the_vectors_record_the_legal_table_of_the_store(gen: ModuleType, committed: str) -> None:
    legal = json.loads(committed)["constants"]["legal"]
    live = {state.value: sorted(t.value for t in targets) for state, targets in gen.taskstore.LEGAL_TRANSITIONS.items()}
    assert legal == live
    ok_edges = {(c["task"]["state"], c["request"]["to"]) for c in json.loads(committed)["cases"]
                if c["group"] == "matrix" and c["expect"]["outcome"] == "ok"}
    assert ok_edges == {(src, dst) for src, targets in live.items() for dst in targets}


def test_every_outcome_kind_and_rust_refusal_is_present(committed: str) -> None:
    cases = json.loads(committed)["cases"]
    assert {c["expect"]["outcome"] for c in cases} == {"ok", "error", "exception"}
    codes = {c["expect"]["error"]["code"] for c in cases if c["expect"]["outcome"] == "error"}
    assert codes == {"E-CONTRACT", "E-POLICY", "E-TIMEOUT", "E-INPUT"}
    assert any(c["rust_not_started"] for c in cases)
    assert any(len(c["expect"].get("audit", [])) == 2 for c in cases), "the auto-escalation double row"


def test_a_new_legal_edge_changes_the_vectors(gen: ModuleType, committed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    table = {state: set(targets) for state, targets in gen.taskstore.LEGAL_TRANSITIONS.items()}
    table[gen.taskstore.TaskState.DONE].add(gen.taskstore.TaskState.CANCELLED)
    monkeypatch.setattr(gen.taskstore, "LEGAL_TRANSITIONS", table)
    assert gen.render() != committed


def test_a_new_rework_cap_changes_the_vectors(gen: ModuleType, committed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen.taskstore, "MAX_REWORK_LOOPS", 3)
    assert gen.render() != committed
