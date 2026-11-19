"""Tests for swarm.graph_planner: validation, waves, conflicts, makespan."""
from __future__ import annotations

import pytest

from swarm.graph_planner import (
    CycleError,
    DuplicateNodeError,
    FileConflictError,
    GraphError,
    GraphPlanner,
    TaskNode,
    UnknownDependencyError,
    plan_graph,
    topological_waves,
)


def test_empty_graph_yields_empty_plan() -> None:
    plan = plan_graph([])
    assert plan.waves == ()
    assert plan.assignments == {}
    assert plan.branches == {}
    assert plan.order == ()
    assert plan.makespan == 0.0


def test_single_task() -> None:
    plan = plan_graph([{"id": "only", "files": ["a.py"], "complexity": 3}])
    assert plan.waves == (("only",),)
    assert plan.makespan == 3.0
    assert plan.branches == {"only": "graph/only"}
    assignment = plan.assignments["only"]
    assert (assignment.wave, assignment.start, assignment.finish) == (0, 0.0, 3.0)
    assert assignment.branch == "graph/only"


def test_chain_is_sequential_and_makespan_sums() -> None:
    nodes = [
        {"id": "c", "dependencies": ["b"], "duration": 2},
        {"id": "a", "duration": 1},
        {"id": "b", "dependencies": ["a"], "duration": 4},
    ]
    plan = plan_graph(nodes)
    assert plan.waves == (("a",), ("b",), ("c",))
    assert plan.order == ("a", "b", "c")
    assert plan.makespan == 7.0
    assert plan.assignments["c"].start == 5.0


def test_diamond_parallel_wave_uses_max_duration() -> None:
    nodes = [
        {"id": "root", "duration": 1},
        {"id": "left", "dependencies": ["root"], "duration": 5},
        {"id": "right", "dependencies": ["root"], "duration": 2},
        {"id": "join", "dependencies": ["left", "right"], "duration": 1},
    ]
    plan = plan_graph(nodes)
    assert plan.waves == (("root",), ("left", "right"), ("join",))
    assert plan.makespan == 7.0  # 1 + max(5, 2) + 1
    assert plan.assignments["left"].start == plan.assignments["right"].start == 1.0


def test_explicit_duration_wins_over_complexity_and_default_is_one() -> None:
    plan = plan_graph([
        {"id": "a", "complexity": 9, "duration": 2},
        {"id": "b"},
    ])
    assert plan.assignments["a"].duration == 2.0
    assert plan.assignments["b"].duration == 1.0
    assert plan.makespan == 2.0  # same wave: max(2, 1)


def test_stable_ordering_regardless_of_input_order() -> None:
    forward = [{"id": name, "priority": 0} for name in ("delta", "alpha", "charlie")]
    shuffled = [forward[2], forward[0], forward[1]]
    assert plan_graph(forward).waves == plan_graph(shuffled).waves == (("alpha", "charlie", "delta"),)


def test_priority_orders_within_a_wave() -> None:
    plan = plan_graph([
        {"id": "low", "priority": 1},
        {"id": "high", "priority": 10},
        {"id": "mid", "priority": 5},
    ])
    assert plan.waves == (("high", "mid", "low"),)


def test_mapping_and_dataclass_inputs_plan_identically() -> None:
    as_dicts = [{"id": "a", "files": ["x.py"]}, {"id": "b", "dependencies": ["a"]}]
    as_nodes = [TaskNode(id="a", files=["x.py"]), TaskNode(id="b", dependencies=["a"])]
    assert plan_graph(as_dicts).waves == plan_graph(as_nodes).waves


def test_graph_planner_class_delegates() -> None:
    planner = GraphPlanner()
    nodes = [{"id": "a"}, {"id": "b", "dependencies": ["a"]}]
    assert planner.waves(nodes) == plan_graph(nodes).waves
    assert [n.id for n in planner.validate(nodes)] == ["a", "b"]


@pytest.mark.parametrize("nodes", [
    [{"id": "a", "dependencies": ["a"]}],
    [{"id": "a", "dependencies": ["b"]}, {"id": "b", "dependencies": ["a"]}],
    [{"id": "a", "dependencies": ["c"]}, {"id": "b", "dependencies": ["a"]}, {"id": "c", "dependencies": ["b"]}],
])
def test_cycles_are_refused(nodes: list[dict]) -> None:
    with pytest.raises(CycleError):
        plan_graph(nodes)


def test_unknown_dependency_is_refused() -> None:
    with pytest.raises(UnknownDependencyError, match="ghost"):
        plan_graph([{"id": "a", "dependencies": ["ghost"]}])


def test_duplicate_ids_are_refused() -> None:
    with pytest.raises(DuplicateNodeError, match="dup"):
        plan_graph([{"id": "dup"}, {"id": "dup"}])


@pytest.mark.parametrize("bad", [
    {"id": ""},
    {"id": "a", "duration": -1},
    {"id": "a", "complexity": float("inf")},
    {"id": "a", "duration": "soon"},
    {"id": "a", "files": [42]},
    {"id": "a", "dependencies": [None]},
])
def test_malformed_nodes_are_refused(bad: dict) -> None:
    with pytest.raises(GraphError):
        plan_graph([bad])


def test_overlapping_files_in_parallel_wave_are_refused() -> None:
    nodes = [
        {"id": "a", "files": ["src/shared.py"]},
        {"id": "b", "files": ["src/shared.py"]},
    ]
    with pytest.raises(FileConflictError, match="both claim"):
        plan_graph(nodes)


def test_nested_path_overlap_in_parallel_wave_is_refused() -> None:
    nodes = [
        {"id": "a", "files": ["pkg"]},
        {"id": "b", "files": ["pkg/mod.py"]},
    ]
    with pytest.raises(FileConflictError):
        topological_waves(nodes)


def test_same_file_in_sequential_waves_is_allowed() -> None:
    nodes = [
        {"id": "a", "files": ["src/shared.py"], "duration": 1},
        {"id": "b", "files": ["src/shared.py"], "dependencies": ["a"], "duration": 1},
    ]
    assert plan_graph(nodes).makespan == 2.0


def test_disjoint_files_in_parallel_wave_are_allowed() -> None:
    nodes = [
        {"id": "a", "files": ["src/a.py"]},
        {"id": "b", "files": ["src/b.py"]},
        {"id": "c"},  # a node claiming no files never conflicts
    ]
    assert plan_graph(nodes).waves == (("a", "b", "c"),)


def test_branch_names_are_unique_and_slugified() -> None:
    plan = plan_graph([{"id": "add login form"}, {"id": "add/login"}])
    assert set(plan.branches.values()) == {"graph/add-login-form", "graph/add-login"}
    assert len(set(plan.branches.values())) == 2
