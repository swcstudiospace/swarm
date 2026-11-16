"""The committed catalog vectors are what the real `scripts/swarm_tool.py` prints today (Phase 14.5, U3.7c).

`crates/gsd-route/tests/vectors/catalog.json` is the oracle of the pure Rust `swarm_tool catalog` (`crates/gsd-route/src/catalog.rs`). It is generated
from the real script by `tests/parity/gen_catalog_vectors.py`, over `agents.json` and `prompts/skills` of this checkout. A change to a tool, a skill, an
agent, the match rule, the tiers or the reply layout fails here until the vectors, and then the Rust function, are regenerated:
`python3 tests/parity/gen_catalog_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_catalog_vectors.py"
PROBES = (
    ["catalog", "--caller", "A05", "--json"],
    ["catalog", "--caller", "a05-backend", "--query", "route NORM", "--json"],
    ["catalog", "--caller", "A99", "--json"],
    ["catalog", "--caller", "A05", "--kind", "skills", "--json"],
)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_catalog_vectors", GENERATOR)
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


def _probe(gen: ModuleType) -> str:
    with gen.real_sources():
        return json.dumps([gen.case(f"probe{i}", argv) for i, argv in enumerate(PROBES)])


def test_committed_vectors_are_what_the_oracle_produces(gen: ModuleType, regenerated: str) -> None:
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    if committed != regenerated:
        old, new = json.loads(committed), json.loads(regenerated)
        changed = [a["name"] for a, b in zip(old["cases"], new["cases"], strict=False) if a != b]
        pytest.fail(f"catalog.json is stale: registry changed={old['registry'] != new['registry']}, skills changed={old['skills'] != new['skills']}, "
                    f"units changed={old['units'] != new['units']}, cases {len(old['cases'])} -> {len(new['cases'])}, first changed {changed[:5]}; "
                    "run `python3 tests/parity/gen_catalog_vectors.py`")


def test_the_corpus_has_every_agent_kind_and_decline_class(doc: dict[str, Any]) -> None:
    names = {case["name"] for case in doc["cases"]}
    agents = [a["id"] for a in doc["registry"]["agents"]]
    assert len(agents) == 30
    for ident in agents:
        assert {f"real/{ident}/{kind}" for kind in ("tools", "skills", "all", "text", "by-slug")} <= names
    classes = {case["why"] for case in doc["cases"] if case["rust"] == "not_started"}
    assert classes == {"non-ascii-repr", "non-ascii-query", "agent-shape", "tool-shape", "str-of-value"}
    assert {case["expect"]["exit"] for case in doc["cases"]} == {0, 2}


def test_the_file_holds_no_host_path_trace_or_key(gen: ModuleType, regenerated: str) -> None:
    for needle in (str(gen.REPO), "Traceback", '"trace"', "/root/", "/tmp/", "catalog-vectors-"):
        assert needle not in regenerated, needle


def test_real_callers_read_uncached_print_what_the_vectors_record(gen: ModuleType, doc: dict[str, Any]) -> None:
    wanted = {"real/A05/all", "real/A13/skills", "real/A30/tools", "real/A01/query/review gate", "real/A27/text"}
    recorded = {case["name"]: case for case in doc["cases"] if case["name"] in wanted}
    assert set(recorded) == wanted
    with gen.real_sources(uncached=True):
        for name, case in recorded.items():
            code, stdout = gen.run_script(case["argv"])
            assert (code, stdout) == (case["expect"]["exit"], case["expect"]["stdout"]), name


def test_the_registry_slice_loses_nothing_the_catalog_reads(gen: ModuleType, doc: dict[str, Any]) -> None:
    """`catalog` over the whole document and over a document rebuilt from the slice answer alike, for every caller, kind and query."""
    with gen.real_sources() as data:
        sliced = gen.registry_slice(data)
        rebuilt = {"agents": [dict(agent) for agent in sliced["agents"]], "shared_tools": sliced["tools"]}
        assert [a["id"] for a in sliced["agents"]] == [a["id"] for a in data["agents"]]
        for agent in data["agents"]:
            for kind in gen.KINDS:
                for query in (None, "route", "NORM review", "zzzz"):
                    whole = gen.swarm_tool.catalog(data, agent, kind, query)
                    assert gen.swarm_tool.catalog(rebuilt, agent, kind, query) == whole, (agent["id"], kind, query)
    assert doc["registry"] == sliced


@pytest.mark.parametrize("patch", [
    ("swarm_tool", "_matches", lambda query, *texts: True),
    ("tool_registry", "tool_tier", lambda tool: "X"),
    ("skill_registry", "SHARED_OWNER", "nobody"),
])
def test_a_change_of_the_python_rules_changes_the_vectors(gen: ModuleType, monkeypatch: pytest.MonkeyPatch, patch: tuple[str, str, object]) -> None:
    before = _probe(gen)
    module, attribute, value = patch
    owner = getattr(gen, module)
    monkeypatch.setattr(owner, attribute, value)
    assert _probe(gen) != before, f"{module}.{attribute} is not recorded in the vectors"


def test_a_changed_word_in_the_committed_file_is_detected(gen: ModuleType, regenerated: str) -> None:
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    assert committed == regenerated
    for word in ("unknown caller", "dry-run: swarm_tool catalog", "permitted_callers", "caller_slug"):
        assert word in committed
