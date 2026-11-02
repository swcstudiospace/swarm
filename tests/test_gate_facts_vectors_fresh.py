"""The committed gate-facts vectors still match the Python oracle.

`tests/parity/gen_gate_facts_vectors.py` seeds real `TaskStore`s and records what `_check_row`, `missing_gate_reasons`,
`unaccepted_gates` and `latest_verdicts` answer. This test regenerates that file in memory and compares it with the committed one
(`crates/gsd-route/tests/vectors/gate_facts.json`, replayed by the Rust tests of `gate_facts.rs`), so a change to the store's gate
logic, a reason string, an expiry constant, the verdict signing or the gate tables fails here until the vectors and the Rust module
are brought along.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_gate_facts_vectors.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_gate_facts_vectors", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generator() -> ModuleType:
    return _load()


@pytest.fixture(scope="module")
def regenerated(generator: ModuleType) -> str:
    return generator.render()


def test_committed_vectors_are_what_the_oracle_produces(generator: ModuleType, regenerated: str) -> None:
    committed = generator.VECTOR_PATH.read_text(encoding="utf-8")
    if committed != regenerated:
        old, new = json.loads(committed), json.loads(regenerated)
        changed = [a["name"] for a, b in zip(old["cases"], new["cases"], strict=False) if a != b]
        pytest.fail(f"gate_facts.json is stale: constants changed={old['constants'] != new['constants']}, "
                    f"cases {len(old['cases'])} -> {len(new['cases'])}, first changed cases {changed[:5]}; "
                    "run `python3 tests/parity/gen_gate_facts_vectors.py`")


def test_regeneration_is_deterministic(generator: ModuleType, regenerated: str) -> None:
    assert generator.render() == regenerated


def test_vectors_hold_no_key_material(generator: ModuleType, regenerated: str) -> None:
    for seed in (generator.KEY_SEED, generator.OTHER_KEY_SEED):
        assert generator.derive_key(seed) not in regenerated
        assert seed not in regenerated
    assert "SWARM_SIGNING_KEY" not in regenerated


def test_every_outcome_and_every_issuer_state_is_covered(generator: ModuleType, regenerated: str) -> None:
    doc = json.loads(regenerated)
    seen = {outcome for case in doc["cases"] for query in case["queries"] for _gate, outcome in query["checks"]}
    assert seen == set(doc["constants"]["outcomes"])
    names = {case["name"] for case in doc["cases"]}
    assert {f"issuer-{state}" for state in generator.PATHS} <= names
    assert len(names) == len(doc["cases"]), "case names must be unique"
