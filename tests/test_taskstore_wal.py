"""TaskStore opens tasks.db in WAL mode with synchronous=NORMAL and stays correct across connections."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from swarm.handover import StoreReader
from swarm.taskstore import TaskStore


@pytest.fixture(autouse=True)
def _wal_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_TASKSTORE_WAL", "1")


def _pragma(path: Path, name: str) -> object:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"PRAGMA {name}").fetchone()[0]
    finally:
        conn.close()


def _mk(store: TaskStore, task_id: str) -> None:
    store.create(task_id=task_id, correlation_id="c-1", capability="code.backend", title=task_id)


def test_fresh_store_is_wal_and_normal(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    try:
        assert str(store.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert store.conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    finally:
        store.conn.close()


def test_delete_mode_database_is_converted_and_stays_wal(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    plain = sqlite3.connect(path)
    plain.execute("CREATE TABLE legacy (x INTEGER)")
    plain.commit()
    plain.close()
    assert str(_pragma(path, "journal_mode")).lower() == "delete"
    TaskStore(path).conn.close()
    assert str(_pragma(path, "journal_mode")).lower() == "wal"
    TaskStore(path).conn.close()
    assert str(_pragma(path, "journal_mode")).lower() == "wal"


def test_read_only_handover_reader_sees_committed_task(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    store = TaskStore(root=root)
    _mk(store, "T-wal")
    store.transition("T-wal", "VALIDATED")
    store.conn.close()
    with StoreReader(root) as reader:
        row = reader.task("T-wal")
    assert row is not None
    assert row["task_id"] == "T-wal"


def test_concurrent_opens_both_succeed_and_see_each_other(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    opened = threading.Barrier(2, timeout=30)
    committed = threading.Barrier(2, timeout=30)
    results: list[tuple[int, bool]] = []
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        # sqlite3 connections are bound to their creating thread: open, use and close the store here.
        try:
            opened.wait()
            store = TaskStore(path)
            try:
                _mk(store, f"T-{i}")
                committed.wait()
                results.append((i, store.get(f"T-{1 - i}") is not None))
            finally:
                store.conn.close()
        except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)
            opened.abort()
            committed.abort()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert sorted(results) == [(0, True), (1, True)]


def test_writers_on_separate_stores_do_not_lock(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    TaskStore(path).conn.close()
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        store = TaskStore(path)
        try:
            for i in range(25):
                tid = f"T-{n}-{i}"
                _mk(store, tid)
                store.transition(tid, "VALIDATED")
        except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)
        finally:
            store.conn.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    check = sqlite3.connect(path)
    try:
        assert check.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 100
    finally:
        check.close()


def test_default_is_rollback_journal_without_side_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWARM_TASKSTORE_WAL", raising=False)
    store = TaskStore(tmp_path / "tasks.db")
    try:
        assert str(store.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "delete"
    finally:
        store.conn.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tasks.db"]


@pytest.mark.parametrize("value", ["0", "true"])
def test_only_exact_one_enables_wal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SWARM_TASKSTORE_WAL", value)
    store = TaskStore(tmp_path / "tasks.db")
    try:
        assert str(store.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "delete"
    finally:
        store.conn.close()


def test_wal_mode_persists_when_variable_is_later_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tasks.db"
    TaskStore(path).conn.close()
    monkeypatch.delenv("SWARM_TASKSTORE_WAL")
    store = TaskStore(path)
    try:
        assert str(store.conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    finally:
        store.conn.close()
