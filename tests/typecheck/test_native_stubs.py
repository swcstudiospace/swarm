"""The ``swarm._native`` stub and the compiled module export exactly the same five names.

The stub's declared names are read from its syntax tree, and the module's names come from
``dir()`` on the imported extension. The extension exists only in the wheel job, so the module
check skips when ``swarm._native`` cannot be imported.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

EXPECTED_NAMES = frozenset({"wire_codes", "code_from_wire", "retry_policy", "orch_status_invoke", "decode_reply_wire"})
STUB_PATH = Path(__file__).resolve().parents[2] / "swarm" / "_native.pyi"


def _declared_top_level_names(stub: Path) -> frozenset[str]:
    tree = ast.parse(stub.read_text(encoding="utf-8"), filename=str(stub))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return frozenset(names)


def test_stub_declares_exactly_the_five_exported_names() -> None:
    assert _declared_top_level_names(STUB_PATH) == EXPECTED_NAMES


def test_extension_exports_exactly_the_five_names() -> None:
    try:
        native = importlib.import_module("swarm._native")
    except ImportError as exc:
        pytest.skip(f"swarm._native extension not importable in this environment: {exc}")
    public = frozenset(name for name in dir(native) if not name.startswith("_"))
    assert public == EXPECTED_NAMES


def test_stub_names_match_the_extension() -> None:
    try:
        native = importlib.import_module("swarm._native")
    except ImportError as exc:
        pytest.skip(f"swarm._native extension not importable in this environment: {exc}")
    public = frozenset(name for name in dir(native) if not name.startswith("_"))
    assert _declared_top_level_names(STUB_PATH) == public
