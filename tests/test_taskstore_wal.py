"""TaskStore opens tasks.db in WAL mode with synchronous=NORMAL and stays correct across connections."""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

import pytest

from swarm import taskstore as taskstore_mod
from swarm.clock import FakeClock
from swarm.errors import ErrorCode, SwarmError
from swarm.handover import StoreReader
from swarm.taskstore import TaskStore
from swarm.verdicts import advisory_envelope


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


class _CommitFails:
    """Connection proxy whose commit raises, so the outermost transaction fails after its writes."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def commit(self) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


class _JournalModeFails(sqlite3.Connection):
    """Connection whose journal_mode pragmas always fail, as on a read-only or locked database."""

    def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
        if "journal_mode" in sql.lower():
            raise sqlite3.OperationalError("cannot change into wal mode")
        return super().execute(sql, *args)


@pytest.mark.xfail(strict=True, reason="BLOCKED-PIN P1-02: the Rust mirror of notes parsing is in crates/gsd-route (pinned); the Python fix changes the reply and must land with the mirror")
@pytest.mark.parametrize("notes", ["{not json", "[1, 2]"])
def test_malformed_notes_raise_e_contract(tmp_path: Path, notes: str) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    try:
        _mk(store, "T-notes")
        store.conn.execute("UPDATE tasks SET notes=? WHERE task_id='T-notes'", (notes,))
        store.conn.commit()
        with pytest.raises(SwarmError) as exc:
            store.get("T-notes")
        assert exc.value.code is ErrorCode.E_CONTRACT
    finally:
        store.conn.close()


def test_transaction_commit_failure_rolls_back(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    try:
        _mk(store, "T-commit")
        real = store.conn
        store.conn = _CommitFails(real)  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            with store.transaction():
                store.conn.execute("UPDATE tasks SET title='changed' WHERE task_id='T-commit'")
        store.conn = real
        assert store.get("T-commit")["title"] == "T-commit"
    finally:
        store.conn.close()


@pytest.mark.xfail(strict=True, reason="BLOCKED-DISK P1-10: the Rust mirror (crates/gsd-taskwriter/src/ops.rs NewTask) must reject unknown risk_class and priority in the same change; cargo verification is blocked by disk")
def test_create_rejects_unknown_risk_class(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    try:
        with pytest.raises(SwarmError) as exc:
            store.create(task_id="T-risk", correlation_id="c-1", capability="code.backend", risk_class="extreme")
        assert exc.value.code is ErrorCode.E_CONTRACT
        assert store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        store.conn.close()


@pytest.mark.xfail(strict=True, reason="BLOCKED-DISK P1-10: the Rust mirror (crates/gsd-taskwriter/src/ops.rs NewTask) must reject unknown risk_class and priority in the same change; cargo verification is blocked by disk")
def test_create_rejects_unknown_priority(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    try:
        with pytest.raises(SwarmError) as exc:
            store.create(task_id="T-prio", correlation_id="c-1", capability="code.backend", priority="P9")
        assert exc.value.code is ErrorCode.E_CONTRACT
        assert store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        store.conn.close()


def test_add_artifact_keeps_concurrent_outputs(tmp_path: Path) -> None:
    path = tmp_path / "tasks.db"
    seed = TaskStore(path)
    try:
        _mk(seed, "T-art")
    finally:
        seed.conn.close()
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def add(tag: str) -> None:
        store = TaskStore(path)  # one connection per thread: sqlite3 connections are thread-bound
        try:
            barrier.wait()
            for i in range(25):
                store.add_artifact("T-art", kind="file", uri=f"{tag}-{i}")
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)
        finally:
            store.conn.close()

    threads = [threading.Thread(target=add, args=(tag,)) for tag in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    check = TaskStore(path)
    try:
        assert len(check.get("T-art")["outputs"]) == 50
    finally:
        check.conn.close()


def test_journal_failure_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    real_connect = sqlite3.connect
    monkeypatch.setattr(taskstore_mod.sqlite3, "connect",
                        lambda *a, **kw: real_connect(*a, factory=_JournalModeFails, **kw))
    caplog.set_level(logging.DEBUG, logger="swarm.taskstore")
    store = TaskStore(tmp_path / "tasks.db")
    try:
        assert any(r.name == "swarm.taskstore" and "journal_mode" in r.getMessage() for r in caplog.records)
    finally:
        store.conn.close()


def test_taskstore_close_releases_connection(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "tasks.db")
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.conn.execute("SELECT 1")
    with TaskStore(tmp_path / "tasks.db") as scoped:
        scoped.conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        scoped.conn.execute("SELECT 1")


def test_verdict_issued_at_uses_injected_clock() -> None:
    env = advisory_envelope(gate="review", task_id="T-clk", agent_id="A09", findings=[], runs={}, expires_s=60,
                            correlation_id="c-1", extra=None, reason="test", clock=FakeClock(start_wall=1000.0))
    assert env["payload"]["issued_at"] == 1000.0
