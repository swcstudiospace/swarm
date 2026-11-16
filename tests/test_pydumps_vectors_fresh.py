"""Freshness of `crates/gsd-route/tests/vectors/pydumps.json` (Phase 14.5, U3.1b).

The Rust crate `gsd-route` checks its Python-compatible JSON writer and its `SeamError` details against that committed file; the file's
expected strings are Python's own output. This test regenerates the whole file in memory from the real Python code
(`json.dumps`, `swarm.script_base.AgentScript._print`, `swarm.errors.SwarmError`) and compares it with the committed bytes, so a change in
any of them, or in the generator, fails here until the vectors are regenerated: `python3 tests/parity/gen_pydumps_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parents[1]
GENERATOR = REPO / "tests" / "parity" / "gen_pydumps_vectors.py"


def load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_pydumps_vectors", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_vectors_equal_a_fresh_generation() -> None:
    generator = load_generator()
    committed = generator.VECTOR_PATH.read_text(encoding="ascii")
    assert committed == generator.render(generator.build_vectors()), "pydumps.json is stale: run tests/parity/gen_pydumps_vectors.py"


def test_generation_is_deterministic() -> None:
    generator = load_generator()
    assert generator.render(generator.build_vectors()) == generator.render(generator.build_vectors())


def test_vectors_cover_every_section_and_edge() -> None:
    generator = load_generator()
    vectors = json.loads(generator.VECTOR_PATH.read_text(encoding="ascii"))
    assert len(vectors["dumps"]) >= 300 and len(vectors["parse"]) >= 100 and len(vectors["errors"]) >= 10
    refusals = {case["refuse"] for case in vectors["parse"]}
    assert refusals == {None, "bigint", "surrogate", "nonfinite", "invalid"}
    names = {case["name"] for case in vectors["dumps"]}
    for edge in ("float-e16", "float-negative-zero", "float-nan", "float-infinity", "ints-all-edges", "string-astral-emoji", "string-all-control-characters"):
        assert edge in names


def test_hand_written_cases_include_a_halfway_float() -> None:
    generator = load_generator()
    numbers = [number for _, value in generator.hand_values() for number in generator.floats_in(value) if math.isfinite(number)]
    assert any(generator.is_halfway(number) for number in numbers)
