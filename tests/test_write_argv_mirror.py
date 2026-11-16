"""The argv grammar of the Rust `--transition` route against Python's real argparse parser (Phase 14.5, U3.2a).

`tests/parity/gen_write_argv_vectors.py` runs the real parser of `scripts/orch_status.py` over a corpus and records what it did, and the
grammar the Rust route serves (`reference_served`, the Python statement of `crates/gsd-route/src/transition_args.rs`). Three things are
held here:

* freshness: the committed `transition_args.json` is what the generator produces now, so a change to the parser's options or to
  argparse's behaviour fails until the vectors and the Rust parser (which replays the file) are brought along;
* the mirror: whatever the route serves, argparse parses to the same values, and an argv argparse rejects or reads as a help request is
  never served;
* the selector: `rust_serves_write` of `scripts/orch_status.py` (once it exists) serves exactly the same set.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PARITY = Path(__file__).resolve().parent / "parity"
sys.path.insert(0, str(PARITY))

import gen_write_argv_vectors as generator  # noqa: E402

Served = Callable[[list[str]], dict[str, Any] | None]


@pytest.fixture(scope="module")
def script() -> ModuleType:
    return generator.load_script()


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    return json.loads(generator.VECTOR_PATH.read_text(encoding="utf-8"))["cases"]


def mirror_violations(served: Served, argv_list: list[list[str]], script: ModuleType) -> list[str]:
    """Every argv `served` accepts although argparse rejects it or parses it to other values than `served` reports."""
    problems = []
    for argv in argv_list:
        fields = served(argv)
        if fields is None:
            continue
        parsed = generator.argparse_result(script, argv)
        if not parsed["ok"]:
            problems.append(f"{argv!r}: served, but argparse exits {parsed['exit']}: {parsed['error']}")
        elif parsed["fields"] != fields:
            problems.append(f"{argv!r}: served as {fields}, argparse parsed {parsed['fields']}")
    return problems


def test_committed_vectors_are_what_the_oracle_produces() -> None:
    committed = generator.VECTOR_PATH.read_text(encoding="utf-8")
    regenerated = generator.render()
    if committed != regenerated:
        old, new = json.loads(committed)["cases"], json.loads(regenerated)["cases"]
        changed = [a["name"] for a, b in zip(old, new, strict=False) if a != b]
        pytest.fail(f"transition_args.json is stale: cases {len(old)} -> {len(new)}, first changed {changed[:5]}; "
                    "run `python3 tests/parity/gen_write_argv_vectors.py`")


def test_regeneration_is_deterministic() -> None:
    assert generator.render() == generator.render()


def test_every_served_argv_parses_identically_under_argparse(script: ModuleType, cases: list[dict[str, Any]]) -> None:
    served_cases = [case["argv"] for case in cases if generator.reference_served(case["argv"]) is not None]
    assert len(served_cases) >= 100
    assert mirror_violations(generator.reference_served, served_cases, script) == []


def test_every_argv_argparse_rejects_is_not_served(script: ModuleType, cases: list[dict[str, Any]]) -> None:
    rejected = [case for case in cases if not generator.argparse_result(script, case["argv"])["ok"]]
    assert len(rejected) >= 100
    assert [case["name"] for case in rejected if generator.reference_served(case["argv"]) is not None] == []
    assert [case["name"] for case in rejected if case["rust_serves"]] == []


def test_the_committed_decision_is_the_reference_grammar(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        fields = generator.reference_served(case["argv"])
        assert case["rust_serves"] is (fields is not None), case["name"]
        assert case["rust_fields"] == fields, case["name"]


def test_argparse_agrees_with_the_committed_verdicts(script: ModuleType, cases: list[dict[str, Any]]) -> None:
    for case in cases:
        assert generator.argparse_result(script, case["argv"]) == case["argparse"], case["name"]


@pytest.mark.parametrize("argv", [
    ["--trans", "T-1", "DONE", "--json"],
    ["--tran", "T-1", "DONE", "--json"],
    ["--transition", "T-1", "DONE", "--js"],
    ["--transition", "T-1", "DONE", "--json", "--reas", "x"],
    ["--transition", "T-1", "--json"],
    ["--transition", "T-1", "DONE", "EXTRA", "--json"],
    ["--transition", "T-1", "DONE", "--reason", "--json"],
    ["--transition=T-1", "DONE", "--json"],
    ["--transition", "T-1", "DONE", "--json=1"],
])
def test_the_checker_has_teeth_against_a_grammar_that_serves_what_argparse_rejects(argv: list[str], script: ModuleType) -> None:
    """A served function that accepts the argv under test (the shape of an abbreviation or arity bug) is caught by `mirror_violations`."""

    def lenient(candidate: list[str]) -> dict[str, Any] | None:
        fields = generator.reference_served(["--transition", "T-1", "DONE", "--json"])
        return fields if candidate == argv else generator.reference_served(candidate)

    assert generator.argparse_result(script, argv)["ok"] is False
    assert len(mirror_violations(lenient, [argv], script)) == 1


def test_the_checker_catches_a_served_argv_that_argparse_parses_to_other_values(script: ModuleType) -> None:
    """`--repo` is `--root`'s destination: a grammar that files it elsewhere reports other values than argparse parsed."""
    argv = ["--transition", "T-1", "DONE", "--json", "--repo", "/b"]

    def repo_as_default_root(candidate: list[str]) -> dict[str, Any] | None:
        fields = generator.reference_served(candidate)
        return None if fields is None else {**fields, "root": "."}

    violations = mirror_violations(repo_as_default_root, [argv], script)
    assert len(violations) == 1 and "argparse parsed" in violations[0]


def test_the_corpus_holds_the_traps_the_route_must_refuse(cases: list[dict[str, Any]]) -> None:
    names = {case["name"] for case in cases}
    for required in ("abbrev-trans", "abbrev-tran", "transition-one", "transition-three", "no-json", "reason-missing-last",
                     "json-twice", "root-inline", "root-sep", "repo-sep", "repo-inline", "dry-run", "task-id-valid", "task-id-dotdot",
                     "correlation-id-valid", "correlation-id-invalid", "unmodelled-ingest", "unmodelled-history", "unknown-flag",
                     "empty-argv", "transition-empty-pair", "order-base-00", "order-base-23", "root-twice", "reason-twice"):
        assert required in names
    assert len(names) == len(cases) >= 300


def test_the_selector_serves_exactly_the_grammar_the_route_serves(script: ModuleType, cases: list[dict[str, Any]]) -> None:
    serves_write = getattr(script, "rust_serves_write", None)
    if serves_write is None:
        pytest.skip("scripts/orch_status.py has no rust_serves_write yet (unit U3.2b)")
    disagreements = [case["name"] for case in cases if bool(serves_write(case["argv"])) is not case["rust_serves"]]
    assert disagreements == []
