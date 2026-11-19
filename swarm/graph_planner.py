"""Deterministic graph planner for parallel swarm work.

Accepts task nodes described by ``id``, ``files``, ``dependencies``,
``complexity``, ``priority`` and/or ``duration`` (as mappings, :class:`TaskNode`
instances, or objects with matching attributes), validates the graph, and
returns a :class:`Plan` with topological waves, branch names, per-node work
assignments, and a makespan.

Semantics
---------
* Waves are Kahn topological levels: a node sits in the earliest wave after
  all of its dependencies, so every node in a wave may run in parallel.
* Within a wave nodes are ordered by descending ``priority`` then ascending
  ``id``. This canonical order is independent of input ordering.
* A node's duration is its explicit ``duration`` when given, otherwise its
  ``complexity`` (default ``1.0``).
* The makespan is the sum, over waves, of the longest duration in the wave
  (nodes in a wave run in parallel); assignments start when the previous
  wave finishes.
* Nodes that would run in parallel (same wave) must not claim overlapping
  files - equal paths or a path nested under another claimed path - or
  planning is refused with :class:`FileConflictError`. Sequential waves may
  safely reuse files.
* Unknown dependencies, duplicate ids, malformed fields and dependency
  cycles are refused before any plan is produced.

No CLI or registry integration: importing this module is the whole surface.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Assignment",
    "CycleError",
    "DuplicateNodeError",
    "FileConflictError",
    "GraphError",
    "GraphPlanner",
    "Plan",
    "TaskNode",
    "UnknownDependencyError",
    "plan_graph",
    "topological_waves",
    "validate_graph",
]


class GraphError(ValueError):
    """Base class for every graph-planning refusal."""


class DuplicateNodeError(GraphError):
    """Two nodes share the same id."""


class UnknownDependencyError(GraphError):
    """A node depends on an id that is not in the graph."""


class CycleError(GraphError):
    """The dependency graph contains a cycle."""


class FileConflictError(GraphError):
    """Nodes scheduled in parallel claim overlapping files."""


@dataclass(frozen=True)
class TaskNode:
    """One unit of work. ``files``/``dependencies`` accept a single string."""

    id: str
    files: Iterable[str] = ()
    dependencies: Iterable[str] = ()
    complexity: float = 1.0
    priority: float = 0
    duration: float | None = None

    def __post_init__(self) -> None:
        # Normalise through the same coercion the planner applies, so a
        # TaskNode and the equivalent mapping always plan identically.
        object.__setattr__(self, "files", _as_str_tuple(self.files, "files"))
        object.__setattr__(self, "dependencies", _as_str_tuple(self.dependencies, "dependencies"))


@dataclass(frozen=True)
class Assignment:
    """Where and when one node runs."""

    node_id: str
    branch: str
    wave: int
    start: float
    finish: float
    duration: float
    files: tuple[str, ...]
    dependencies: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    """The result of :func:`plan_graph`."""

    waves: tuple[tuple[str, ...], ...]
    assignments: dict[str, Assignment]
    makespan: float
    order: tuple[str, ...] = field(default=())
    branches: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.order:
            object.__setattr__(self, "order", tuple(n for wave in self.waves for n in wave))
        if not self.branches:
            object.__setattr__(self, "branches", {n: a.branch for n, a in self.assignments.items()})


def _as_str_tuple(value: Any, what: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items: tuple[Any, ...] = (value,)
    elif isinstance(value, Iterable):
        items = tuple(value)
    else:
        raise GraphError(f"{what} must be a string or iterable of strings, got {value!r}")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise GraphError(f"{what} entries must be non-empty strings, got {item!r}")
        if item not in out:  # de-duplicated, first occurrence wins
            out.append(item)
    return tuple(out)


def _number(value: Any, what: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GraphError(f"{what} must be a finite number, got {value!r}")
    return float(value)


def _field(node: Any, name: str, default: Any) -> Any:
    if isinstance(node, Mapping):
        return node.get(name, default)
    return getattr(node, name, default)


def _coerce(node: Any) -> TaskNode:
    if isinstance(node, TaskNode):
        return node
    node_id = _field(node, "id", None)
    if not isinstance(node_id, str) or not node_id.strip():
        raise GraphError(f"node id must be a non-empty string, got {node_id!r}")
    complexity = _number(_field(node, "complexity", 1.0), f"complexity of {node_id!r}")
    assert complexity is not None
    if complexity < 0:
        raise GraphError(f"complexity of {node_id!r} must be >= 0, got {complexity}")
    duration = _number(_field(node, "duration", None), f"duration of {node_id!r}", allow_none=True)
    if duration is not None and duration < 0:
        raise GraphError(f"duration of {node_id!r} must be >= 0, got {duration}")
    priority = _number(_field(node, "priority", 0), f"priority of {node_id!r}")
    assert priority is not None
    return TaskNode(
        id=node_id,
        files=_as_str_tuple(_field(node, "files", ()), f"files of {node_id!r}"),
        dependencies=_as_str_tuple(_field(node, "dependencies", ()), f"dependencies of {node_id!r}"),
        complexity=complexity,
        priority=priority,
        duration=duration,
    )


def _normalise(nodes: Iterable[Any]) -> list[TaskNode]:
    if nodes is None or isinstance(nodes, (str, Mapping)):
        raise GraphError(f"nodes must be an iterable of nodes, got {nodes!r}")
    coerced = [_coerce(n) for n in nodes]
    seen: set[str] = set()
    for node in coerced:
        if node.id in seen:
            raise DuplicateNodeError(f"duplicate node id {node.id!r}")
        seen.add(node.id)
    by_id = {n.id: n for n in coerced}
    for node in coerced:
        for dep in node.dependencies:
            if dep not in by_id:
                raise UnknownDependencyError(f"node {node.id!r} depends on unknown node {dep!r}")
    return coerced


def _sort_key(node: TaskNode) -> tuple[float, str]:
    return (-node.priority, node.id)


def _waves(nodes: list[TaskNode]) -> tuple[tuple[TaskNode, ...], ...]:
    remaining = {n.id: n for n in nodes}
    placed: set[str] = set()
    waves: list[tuple[TaskNode, ...]] = []
    while remaining:
        ready = [n for n in remaining.values() if all(d in placed for d in n.dependencies)]
        if not ready:  # nothing progressable: everything left is in/downstream of a cycle
            cycle = _find_cycle(remaining)
            detail = " -> ".join(cycle) if cycle else ", ".join(sorted(remaining))
            raise CycleError(f"dependency cycle detected: {detail}")
        ready.sort(key=_sort_key)
        waves.append(tuple(ready))
        for node in ready:
            placed.add(node.id)
            del remaining[node.id]
    return tuple(waves)


def _find_cycle(remaining: dict[str, TaskNode]) -> list[str]:
    """Return one dependency cycle among ``remaining`` (DFS, deterministic)."""
    state: dict[str, int] = {}  # 0=unvisited implied, 1=in stack, 2=done
    stack: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        state[node_id] = 1
        stack.append(node_id)
        for dep in sorted(remaining[node_id].dependencies):
            if dep not in remaining:
                continue
            if state.get(dep) == 1:
                return stack[stack.index(dep):] + [dep]
            if state.get(dep, 0) == 0 and (found := visit(dep)) is not None:
                return found
        stack.pop()
        state[node_id] = 2
        return None

    for node_id in sorted(remaining):
        if state.get(node_id, 0) == 0 and (found := visit(node_id)) is not None:
            return found
    return sorted(remaining)


def _paths_overlap(a: str, b: str) -> bool:
    a, b = a.rstrip("/"), b.rstrip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _check_file_conflicts(waves: tuple[tuple[TaskNode, ...], ...]) -> None:
    for index, wave in enumerate(waves):
        owner: dict[str, str] = {}
        for node in wave:
            for path in node.files:
                for claimed, holder in owner.items():
                    if holder != node.id and _paths_overlap(claimed, path):
                        raise FileConflictError(
                            f"nodes {holder!r} and {node.id!r} both claim overlapping file "
                            f"{path!r} (vs {claimed!r}) in parallel wave {index}"
                        )
                owner.setdefault(path, node.id)


def _branch_name(node_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", node_id).strip("-.")
    return f"graph/{slug or 'task'}"


def _effective_duration(node: TaskNode) -> float:
    return node.duration if node.duration is not None else node.complexity


def validate_graph(nodes: Iterable[Any]) -> tuple[TaskNode, ...]:
    """Coerce and validate ``nodes``; return them in canonical wave order."""
    coerced = _normalise(nodes)
    waves = _waves(coerced)
    _check_file_conflicts(waves)
    return tuple(node for wave in waves for node in wave)


def topological_waves(nodes: Iterable[Any]) -> tuple[tuple[str, ...], ...]:
    """Validated topological waves as tuples of node ids."""
    coerced = _normalise(nodes)
    waves = _waves(coerced)
    _check_file_conflicts(waves)
    return tuple(tuple(n.id for n in wave) for wave in waves)


def plan_graph(nodes: Iterable[Any]) -> Plan:
    """Validate ``nodes`` and return waves, branches, assignments and makespan."""
    coerced = _normalise(nodes)
    waves = _waves(coerced)
    _check_file_conflicts(waves)

    assignments: dict[str, Assignment] = {}
    branches: dict[str, str] = {}
    makespan = 0.0
    for index, wave in enumerate(waves):
        wave_span = max((_effective_duration(n) for n in wave), default=0.0)
        for node in wave:
            duration = _effective_duration(node)
            branch = _branch_name(node.id)
            branches[node.id] = branch
            assignments[node.id] = Assignment(
                node_id=node.id,
                branch=branch,
                wave=index,
                start=makespan,
                finish=makespan + duration,
                duration=duration,
                files=tuple(node.files),
                dependencies=tuple(node.dependencies),
            )
        makespan += wave_span
    return Plan(
        waves=tuple(tuple(n.id for n in wave) for wave in waves),
        assignments=assignments,
        makespan=makespan,
        order=tuple(n.id for wave in waves for n in wave),
        branches=branches,
    )


class GraphPlanner:
    """Thin reusable wrapper around :func:`plan_graph`."""

    def plan(self, nodes: Iterable[Any]) -> Plan:
        return plan_graph(nodes)

    def waves(self, nodes: Iterable[Any]) -> tuple[tuple[str, ...], ...]:
        return topological_waves(nodes)

    def validate(self, nodes: Iterable[Any]) -> tuple[TaskNode, ...]:
        return validate_graph(nodes)
