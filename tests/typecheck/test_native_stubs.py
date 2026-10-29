"""The ``swarm._native`` stub and the compiled module export exactly the same public names.

The stub's declared names are read from its syntax tree, and the module's names come from
``dir()`` on the imported extension. Both sides apply the same rule: a name that starts with an
underscore is not public. The stub must also declare ``__all__`` with exactly those names, because
PyO3 generates ``__all__`` on the extension and ``mypy.stubtest`` fails the wheel job when the stub
lacks it. The extension exists only in the wheel job, so the module checks skip when
``swarm._native`` cannot be imported; the ``__all__`` check reads the stub only and always runs.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

EXPECTED_NAMES = frozenset(
    {
        "wire_codes", "code_from_wire", "retry_policy", "orch_status_invoke", "decode_reply_wire", "redact_spans",
        "sched_order_ready", "sched_normalize_claim", "sched_claims_overlap", "sched_backoff_s", "sched_drain_plan", "ClaimTable", "AimdLimiter",
    }
)
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
    return frozenset(name for name in names if not name.startswith("_"))


def _declared_all(stub: Path) -> list[str] | None:
    tree = ast.parse(stub.read_text(encoding="utf-8"), filename=str(stub))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            assert isinstance(node.value, (ast.List, ast.Tuple)), "__all__ must be a list or tuple literal for mypy to read it"
            return [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    return None


def test_stub_declares_all_with_exactly_the_exported_names() -> None:
    declared = _declared_all(STUB_PATH)
    assert declared is not None, "the stub must declare __all__ (the extension has one, so stubtest would fail without it)"
    assert len(declared) == len(set(declared)), "__all__ must not repeat a name"
    assert frozenset(declared) == EXPECTED_NAMES


def test_stub_declares_exactly_the_exported_names() -> None:
    assert _declared_top_level_names(STUB_PATH) == EXPECTED_NAMES


def test_extension_exports_exactly_the_expected_names() -> None:
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
