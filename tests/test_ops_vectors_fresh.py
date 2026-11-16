"""The committed writer-ops vectors are what the real `TaskStore` produces today.

`crates/gsd-taskwriter/tests/vectors/ops.json` is the oracle for the Rust writer ops (`TaskStoreWriter::apply_ops`). It is regenerated here in
memory from `swarm/taskstore.py` and compared with the committed file byte for byte, so a change of `set_notes`, `append_feedback`,
`add_artifact`, `create`, `update`, the state machine or the clock use fails this test until the vectors (and then the Rust code) are
regenerated: `python3 tests/parity/gen_ops_vectors.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

GENERATOR = Path(__file__).resolve().parent / "parity" / "gen_ops_vectors.py"


def _load_generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_ops_vectors", GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves the defining module through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _load_generator()


@pytest.fixture(scope="module")
def committed(gen: ModuleType) -> str:
    return str(gen.VECTOR_PATH.read_text(encoding="utf-8"))


def _cases(committed: str) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(committed)["cases"]
    return cases


def test_committed_vectors_equal_a_fresh_generation(gen: ModuleType, committed: str) -> None:
    assert gen.render() == committed, "stale vectors: run `python3 tests/parity/gen_ops_vectors.py` and review the diff"


def test_regeneration_is_deterministic(gen: ModuleType) -> None:
    assert gen.render() == gen.render()


def test_ids_are_unique_and_every_op_kind_and_outcome_is_present(committed: str) -> None:
    cases = _cases(committed)
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids))
    assert {op["op"] for case in cases for op in case["ops"]} == {
        "set_notes", "add_artifact", "append_feedback", "create_task", "set_depends_on", "transition"}
    assert {(case["python"]["outcome"], case["rust"]["outcome"]) for case in cases} == {
        ("ok", "same"), ("ok", "not_started"), ("error", "same"), ("exception", "not_started")}


def test_a_failed_list_rolls_back_everything(committed: str) -> None:
    failed = [case for case in _cases(committed) if case["python"]["outcome"] != "ok"]
    assert len(failed) > 10
    for case in failed:
        assert case["python"]["after"] == case["initial"], case["id"]


def test_the_wire_plan_of_a_transition_is_what_python_wrote(committed: str) -> None:
    by_id = {case["id"]: case for case in _cases(committed)}
    cap = by_id["transition-rework-cap-escalates-with-two-audit-rows"]
    plan = cap["ops"][0]["plan"]
    assert [(row["from"], row["to"]) for row in plan["audit"]] == [("IN_REVIEW", "CHANGES_REQUESTED"), ("CHANGES_REQUESTED", "ESCALATED")]
    assert plan["to"] == "ESCALATED"
    stash = by_id["transition-rework-stashes-the-notes"]["ops"][0]["plan"]["columns"]
    assert list(stash) == ["state", "rework_loops", "notes"]
    assert stash["notes"]["rework_evidence"] == [{"result": {"summary": "fail"}, "meta": {"tokens": 5}}]


def test_a_changed_notes_merge_changes_the_vectors(gen: ModuleType, committed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def replace_notes(self: Any, task_id: str, **kv: object) -> dict[str, Any]:
        with self.transaction():
            self.get(task_id)
            self.update(task_id, notes=json.dumps(kv))
        return dict(self.get(task_id))

    monkeypatch.setattr(gen.taskstore.TaskStore, "set_notes", replace_notes)
    assert gen.render() != committed


def test_a_changed_outputs_append_changes_the_vectors(gen: ModuleType, committed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def artifact_row_only(self: Any, task_id: str, *, kind: str, uri: str, version: str = "1", digest: str = "", producer: str = "") -> None:
        self.conn.execute("INSERT INTO artifacts (task_id, kind, uri, version, digest, producer, ts) VALUES (?,?,?,?,?,?,?)",
                          (task_id, kind, uri, version, digest, producer, gen.NOW))
        self.get(task_id)

    monkeypatch.setattr(gen.taskstore.TaskStore, "add_artifact", artifact_row_only)
    assert gen.render() != committed


def test_a_changed_create_default_changes_the_vectors(gen: ModuleType, committed: str, monkeypatch: pytest.MonkeyPatch) -> None:
    real_create = gen.taskstore.TaskStore.create

    def create_with_a_smaller_cap(self: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("max_attempts", 1)
        return dict(real_create(self, **kwargs))

    monkeypatch.setattr(gen.taskstore.TaskStore, "create", create_with_a_smaller_cap)
    assert gen.render() != committed
