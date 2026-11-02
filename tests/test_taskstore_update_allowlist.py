"""TaskStore.update accepts only `tasks` columns; any other key is rejected before SQL is built (SEC-8e8d7a84)."""
import json

import pytest


def _store(swarm_dir):
    from swarm.taskstore import TaskStore
    store = TaskStore(swarm_dir / "tasks.db")
    store.create(task_id="U-1", correlation_id="c", capability="code.backend", title="before")
    return store


@pytest.mark.parametrize("hostile", [
    "state=state, notes=notes --",
    "title=? OR 1=1 --",
    "title; DROP TABLE tasks",
    "no_such_column",
])
def test_hostile_key_rejected_and_row_unchanged(swarm_dir, hostile):
    store = _store(swarm_dir)
    before = store.get("U-1")
    with pytest.raises(ValueError, match="not an updatable tasks column") as exc:
        store.update("U-1", **{hostile: "pwned"})
    assert hostile in str(exc.value)
    assert store.get("U-1") == before


def test_mixed_call_with_hostile_key_applies_nothing(swarm_dir):
    store = _store(swarm_dir)
    before = store.get("U-1")
    with pytest.raises(ValueError):
        store.update("U-1", title="after", **{"notes=notes --": "x"})
    assert store.get("U-1") == before


def test_primary_key_is_not_updatable(swarm_dir):
    store = _store(swarm_dir)
    before = store.get("U-1")
    # task_id binds to update()'s own positional parameter, so Python rejects it before any SQL is built.
    with pytest.raises(TypeError):
        store.update("U-1", **{"task_id": "U-2"})
    assert store.get("U-1") == before


def test_allowlisted_update_applies(swarm_dir):
    store = _store(swarm_dir)
    row = store.update("U-1", title="after", priority="P1")
    assert row["title"] == "after" and row["priority"] == "P1"
    assert store.get("U-1")["title"] == "after"


def test_json_column_round_trips(swarm_dir):
    store = _store(swarm_dir)
    deps = ["A-1", "B-2"]
    row = store.update("U-1", depends_on=deps)
    assert row["depends_on"] == deps
    assert store.get("U-1")["depends_on"] == deps
    raw = store.conn.execute("SELECT depends_on FROM tasks WHERE task_id=?", ("U-1",)).fetchone()[0]
    assert json.loads(raw) == deps
