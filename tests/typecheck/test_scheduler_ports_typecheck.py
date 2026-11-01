"""The ``swarm.rust_scheduler`` facade's public names and annotations, read from its syntax tree.

``swarm/`` is checked with ``mypy --strict`` (mypy.ini). This test pins what the strict check cannot see on its own: the facade
declares ``__all__`` with exactly its public names, every public callable is fully annotated, the sixteen callbacks the native
loop reads are exactly the public methods of the port adapter, and the native call passes the cleanup fence and the sixteen
options as keywords and nothing else. The module is read, not imported, so the check runs without the extension and without the
runner.
"""
from __future__ import annotations

import ast
from pathlib import Path

FACADE = Path(__file__).resolve().parents[2] / "swarm" / "rust_scheduler.py"
EXPECTED_PUBLIC = frozenset({"RunSettings", "RunnerHelpers", "RustScheduler", "SchedulerController"})
CALLBACKS = frozenset(
    {
        "begin_pass", "agent_for", "claims_for", "block", "lease", "observe_pass", "submit", "worker",
        "internal_failure", "complete", "set_retry_after", "random_unit", "cancel_state", "cancel_begin", "kill", "log",
    }
)
RUN_NATIVE_KEYWORDS = frozenset(
    {
        "cleanup_fence", "max_concurrent", "barrier", "once", "budget", "max_rounds", "fail_streak", "cooldown_s",
        "retry_cap_s", "backoff_base_s", "backoff_cap_s", "wait_tick_s", "internal_failure_limit", "cancel_grace_s",
        "shutdown_budget_s", "kill_grace_s", "kill_settle_s",
    }
)


def _tree() -> ast.Module:
    return ast.parse(FACADE.read_text(encoding="utf-8"), filename=str(FACADE))


def _declared_all(tree: ast.Module) -> list[str] | None:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            assert isinstance(node.value, (ast.List, ast.Tuple)), "__all__ must be a list or tuple literal"
            return [elt.value for elt in node.value.elts if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
    return None


def _public_top_level(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return frozenset(name for name in names if not name.startswith("_"))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} is not defined in the facade")


def _assert_annotated(fn: ast.FunctionDef) -> None:
    for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]:
        if arg.arg in ("self", "cls"):
            continue
        assert arg.annotation is not None, f"{fn.name}: parameter {arg.arg!r} is not annotated"
    for extra in (fn.args.vararg, fn.args.kwarg):
        if extra is not None:
            assert extra.annotation is not None, f"{fn.name}: parameter {extra.arg!r} is not annotated"
    assert fn.returns is not None, f"{fn.name}: no return annotation"


def test_all_declares_exactly_the_public_names() -> None:
    declared = _declared_all(_tree())
    assert declared is not None, "the facade must declare __all__"
    assert len(declared) == len(set(declared)), "__all__ must not repeat a name"
    assert frozenset(declared) == EXPECTED_PUBLIC


def test_public_top_level_names_are_the_expected_set() -> None:
    assert _public_top_level(_tree()) == EXPECTED_PUBLIC


def test_public_callables_are_fully_annotated() -> None:
    for node in _tree().body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for member in node.body:
                if isinstance(member, ast.FunctionDef):
                    _assert_annotated(member)
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            _assert_annotated(node)


def test_the_port_adapter_exposes_exactly_the_sixteen_callbacks() -> None:
    adapter = _class(_tree(), "_Ports")
    names = {m.name for m in adapter.body if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")}
    assert names == CALLBACKS


def test_run_native_gets_the_fence_and_the_sixteen_options_as_keywords_only() -> None:
    calls = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_run_native"
    ]
    assert len(calls) == 1, "exactly one native call"
    call = calls[0]
    assert len(call.args) == 1, "ports is the only positional argument"
    assert all(kw.arg is not None for kw in call.keywords), "no **mapping expansion into the native call"
    assert len(call.keywords) == 17
    assert {kw.arg for kw in call.keywords} == RUN_NATIVE_KEYWORDS


def test_the_facade_does_not_import_the_runner() -> None:
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            assert "swarm_run" not in module and not module.startswith("scripts"), module
