"""The committed gate-issue vectors still match the Python oracle.

`tests/parity/gen_gate_issue_vectors.py` runs the real `swarm.verdicts.issue_gate` over seeded `TaskStore`s under one fake clock and a pinned
id source, with throwaway keys, and records the verdict file, the envelopes, the events, the refusals and the rows. This test regenerates
that file in memory and compares it with the committed one (`crates/gsd-taskwriter/tests/vectors/gate_issue.json`, replayed by
`crates/gsd-taskwriter/tests/gate_issue.rs`), so a change to a refusal text, the advisory reason, the payload layout, the canonical form,
the severity rule or the row layout fails here until the vectors, and then the Rust tail, are regenerated:
`python3 tests/parity/gen_gate_issue_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_gate_issue_vectors.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_gate_issue_vectors", GENERATOR)
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


def test_committed_vectors_are_what_the_oracle_produces(gen: ModuleType, regenerated: str) -> None:
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    if committed != regenerated:
        old, new = json.loads(committed), json.loads(regenerated)
        changed = [a["id"] for a, b in zip(old["cases"], new["cases"], strict=False) if a != b]
        pytest.fail(f"gate_issue.json is stale: cases {len(old['cases'])} -> {len(new['cases'])}, first changed cases {changed[:5]}; "
                    "run `python3 tests/parity/gen_gate_issue_vectors.py`")


def test_a_changed_word_in_the_committed_file_is_detected(gen: ModuleType, regenerated: str) -> None:
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    assert committed == regenerated
    for word in ("simulated gate failure", "is not a running gate task", "fail-closed: no signing key configured", "agent session"):
        assert word in committed
        assert committed.replace(word, word.replace("e", "E", 1), 1) != regenerated, word


def test_regeneration_of_one_case_is_deterministic(gen: ModuleType) -> None:
    spec = next(s for s in gen.specs() if s.case_id == "ed25519-fail-feedback")
    assert json.dumps(gen.run(spec)) == json.dumps(gen.run(spec))


def test_the_file_holds_no_key_and_no_seed(gen: ModuleType, regenerated: str) -> None:
    for secret in (gen.HMAC_PHRASE, gen.ED25519_SEED_PHRASE, gen.ED25519_SEED_PHRASE.encode().hex()):
        assert secret not in regenerated
    doc: dict[str, Any] = json.loads(regenerated)
    assert doc["head"]["placeholders"] == ["@ed25519", "@hmac"]
    assert any(case["env"].get("SWARM_SIGNING_KEY") == "@hmac" for case in doc["cases"])


def test_the_corpus_covers_each_branch_of_issue_gate(regenerated: str) -> None:
    doc: dict[str, Any] = json.loads(regenerated)
    events = {event["payload"].get("reason") for case in doc["cases"] for event in case["python"]["events"] if event["name"] == "gate.verdict.unrecorded"}
    for reason in ("no --task-id", "not a gate task", "empty gate_for", "dry-run outside a runner dry-run",
                   "agent session: the runner records this gate after the session"):
        assert reason in events, reason
    assert any(r and r.startswith("fail-closed: no signing key configured") for r in events)
    assert any(case["python"]["outcome"] == "error" for case in doc["cases"])
    assert any(event["name"] == "security.dev_key" for case in doc["cases"] for event in case["python"]["events"])
    assert any(row["verdict"] == "fail" for case in doc["cases"] for row in case["python"]["after"]["verdicts"])
