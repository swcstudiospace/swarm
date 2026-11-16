"""Drift guard for `crates/gsd-route/tests/vectors/result_parse.json` (unit U3.5a of Phase 14.5).

The committed vectors record what the REAL Python oracle (`swarm.results.parse_result`, `validate_result`, `swarm.schema.validate` over
`swarm/schemas/task.result.v1.json`) answers for ~900 agent texts. The Rust port (`crates/gsd-route/src/result_parse.rs`) replays them. This
test regenerates the file in memory with `tests/parity/gen_result_vectors.py` and compares it with the committed bytes, so it fails when the
Python oracle (fence regex, JSON handling, the schema file, the validator, an error text) changes under the vectors, or when someone edits
the file by hand.

`SWARM_RESULT_VECTORS_FILE` points the comparison at another file (a scratch copy with one value changed shows that the test has teeth).
To refresh after an intended oracle change: `python3 tests/parity/gen_result_vectors.py`, then update the Rust constants it names.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests" / "parity"))

import gen_result_vectors as gen  # noqa: E402


def _committed() -> str:
    override = os.environ.get("SWARM_RESULT_VECTORS_FILE")
    path = Path(override) if override else gen.VECTOR_PATH
    return path.read_text(encoding="utf-8")


def _first_difference(committed: str, fresh: str) -> str:
    old = json.loads(committed)
    new = json.loads(fresh)
    if old.get("oracle") != new["oracle"]:
        return "the oracle block differs (pattern, schema digest, state enum, whitespace table or caps)"
    old_cases = {case["id"]: case for case in old.get("cases", [])}
    new_cases = {case["id"]: case for case in new["cases"]}
    for case_id, case in new_cases.items():
        if old_cases.get(case_id) != case:
            return f"case {case_id!r} differs: committed {json.dumps(old_cases.get(case_id))[:300]} fresh {json.dumps(case)[:300]}"
    removed = sorted(set(old_cases) - set(new_cases))
    return f"cases only in the committed file: {removed[:5]}" if removed else "header fields differ"


def test_committed_vectors_are_what_the_python_oracle_produces_now() -> None:
    committed = _committed()
    fresh = gen.render()
    assert committed == fresh, (
        "result_parse.json is stale: " + _first_difference(committed, fresh) + "; run python3 tests/parity/gen_result_vectors.py"
    )


def test_committed_file_pins_the_seed_and_covers_every_class() -> None:
    doc = json.loads(_committed())
    assert doc["schema"] == gen.SCHEMA
    assert doc["seed"] == gen.FUZZ_SEED
    cases = doc["cases"]
    assert len({case["id"] for case in cases}) == len(cases)
    outcomes = {(case["parse"]["outcome"], case["validate"]["outcome"], case["rust"]["parse"], case["rust"]["validate"]) for case in cases}
    assert {
        ("value", "ok", "exact", "exact"),
        ("value", "error", "exact", "exact"),
        ("value", "error", "exact", "not_started"),
        ("value", "ok", "not_started", "not_started"),
        ("none", "error", "exact", "exact"),
        ("raises", "skipped", "not_started", "not_started"),
    } <= outcomes
    states = {json.loads(case["parse"]["value"])["state"] for case in cases if case["parse"]["value"] and case["validate"]["outcome"] == "ok"}
    assert set(gen.SCHEMA_ENUM) <= states, "every state the schema allows must appear in an accepted result"
