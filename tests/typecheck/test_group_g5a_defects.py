"""Regression tests for runtime defects fixed while typing swarm/tools/a24_tools.py and swarm/tools/a26_tools.py.

``_condition_fields`` (A24, Falco rule lint) expanded a macro once per path that reached it, so a macro that names
another macro twice cost 2^depth expansions: a chain of forty such macros never returned. ``_inherits`` (A26, upgrade
safety) walked a base once per path, so a diamond chain of inheritance cost the same. Both now expand a macro or base once
when its expansion meets no cycle, and the counting probes below fail fast if the number of expansions grows again.
"""
from __future__ import annotations

from typing import Any

import pytest

from swarm.tools import a24_tools, a26_tools

DEPTH = 40


def _macro_chain(depth: int) -> dict[str, str]:
    macros = {"m0": "evt.type = open"}
    for i in range(1, depth + 1):
        macros[f"m{i}"] = f"m{i - 1} or m{i - 1}"
    return macros


def _diamond_chain(depth: int, root_bases: list[str]) -> dict[str, dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {"C0": {"name": "C0", "bases": root_bases}}
    for i in range(1, depth + 1):
        by_name[f"C{i}"] = {"name": f"C{i}", "bases": [f"C{i - 1}", f"C{i - 1}"]}
    return by_name


def test_falco_macro_named_twice_at_every_level_is_expanded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real = a24_tools._expand_condition
    calls = 0

    def counting(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args)

    monkeypatch.setattr(a24_tools, "_expand_condition", counting)

    fields, unresolved = a24_tools._condition_fields(f"m{DEPTH}", _macro_chain(DEPTH))

    assert fields == {"evt.type"}
    assert unresolved is False
    assert calls <= 2 * DEPTH + 2


def test_inheritance_diamond_chain_is_walked_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real = a26_tools._walk_bases
    calls = 0

    def counting(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args)

    monkeypatch.setattr(a26_tools, "_walk_bases", counting)
    by_name = _diamond_chain(DEPTH, [])

    assert a26_tools._inherits(by_name[f"C{DEPTH}"], by_name, "UUPS") == (False, False)
    assert calls <= 2 * DEPTH + 2


def test_inheritance_diamond_chain_still_reports_unresolved_and_found_bases() -> None:
    unresolved = _diamond_chain(DEPTH, ["ExternalBase"])
    found = _diamond_chain(DEPTH, ["UUPSUpgradeable"])

    assert a26_tools._inherits(unresolved[f"C{DEPTH}"], unresolved, "UUPS") == (False, True)
    assert a26_tools._inherits(found[f"C{DEPTH}"], found, "UUPS") == (True, False)
