"""AC-14.7-06: the health, status and results read paths create nothing and report unsupported layouts explicitly.

Read paths under test: `StoreReader` / `read_task` / `handover_status` / `list_handovers` / `read_events` /
`latest_correlation` (swarm/handover.py, swarm/runlog.py, swarm/paths.py) and the `scripts/orch_status.py` read grammar
(`--json` status and `--json --history`), run as a child process exactly as an operator or the OMP plugin runs it.

Evidence is the filesystem itself: a snapshot (every entry under the project root, HOME and XDG_CONFIG_HOME: kind, size,
mtime_ns and SHA-256 of each file) is taken before and after each read, and the test fails on any created, removed or
changed entry. A `mode=ro` string in the reader is not proof and no assertion relies on it; the kernel-refusal case
proves the directory is really unwritable before the read runs.

Layouts: fresh root (no store), rollback-journal store and WAL store with sidecars absent (the writer closed cleanly),
a directory where the store file should be, a non-database file, a database without the task tables, a store location
that cannot hold a directory (a regular file on the path), and a store on a permission-denied directory (non-root only).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from swarm.handover import StoreReader, handover_status, list_handovers, read_task
from swarm.paths import latest_correlation
from swarm.runlog import read_events
from swarm.taskstore import TaskStore

REPO = Path(__file__).resolve().parents[1]
ORCH_STATUS = REPO / "scripts" / "orch_status.py"
_ROUTE_ENV = ("SWARM_DIR", "SWARM_TASKSTORE_WAL", "GSD_ENGINE", "GSD_RUST_BACKEND")
_SIDECAR_PREFIX = "tasks.db-"
_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.fixture(autouse=True)
def _no_inherited_store_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every test names its own swarm dir through SWARM_DIR (set in _build); no inherited engine or WAL choice may change
    # which files a read touches.
    for name in _ROUTE_ENV:
        monkeypatch.delenv(name, raising=False)


def _sidecars(swarm: Path) -> list[str]:
    if not swarm.is_dir():
        return []
    return sorted(p.name for p in swarm.iterdir() if p.name.startswith(_SIDECAR_PREFIX))


def _assert_store_shape(swarm: Path, *, wal: bool) -> None:
    """Precondition of a store case: the journal mode the writer left, and no sidecar left behind by its last close."""
    conn = sqlite3.connect(swarm / "tasks.db")
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()
    assert mode == ("wal" if wal else "delete")
    assert _sidecars(swarm) == []


def _build(kind: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Lays out `kind` under `root`, points SWARM_DIR at the swarm dir that layout names, and returns that dir."""
    if kind == "unwritable-location":
        root.mkdir(parents=True)
        (root / "blocked").write_text("a regular file: no directory can be created below it\n", encoding="utf-8")
        swarm = root / "blocked" / ".swarm"
        monkeypatch.setenv("SWARM_DIR", str(swarm))
        return swarm
    swarm = root / ".swarm"
    monkeypatch.setenv("SWARM_DIR", str(swarm))
    if kind == "fresh":
        root.mkdir(parents=True)
        return swarm
    if kind in ("rollback", "wal"):
        monkeypatch.setenv("SWARM_TASKSTORE_WAL", "1" if kind == "wal" else "0")
        root.mkdir(parents=True)
        with TaskStore(root=root) as store:
            store.create(task_id="T-1", correlation_id="c-1", capability="code.backend", title="fixture task")
        _assert_store_shape(swarm, wal=kind == "wal")
        return swarm
    # Unsupported layouts: the swarm dir and its .gitignore exist as swarm_dir(create=True) leaves them.
    root.mkdir(parents=True)
    swarm.mkdir()
    (swarm / ".gitignore").write_text("*", encoding="utf-8")
    db = swarm / "tasks.db"
    if kind == "dir-store":
        db.mkdir()
    elif kind == "garbage-store":
        db.write_bytes(b"this file is not a SQLite database\n" * 32)
    elif kind == "foreign-store":
        conn = sqlite3.connect(db)
        try:
            conn.execute("CREATE TABLE other (x INTEGER)")
            conn.commit()
        finally:
            conn.close()
    else:
        raise ValueError(kind)
    return swarm


def _watched(root: Path) -> list[Path]:
    """The trees a read must leave alone: the project root (which holds `.swarm`, `.planning`, anything a read could
    create) and the HOME and XDG_CONFIG_HOME trees that conftest points key discovery at."""
    return [root, Path(os.environ["HOME"]), Path(os.environ["XDG_CONFIG_HOME"])]


def _record(path: Path, seen: dict[str, tuple]) -> None:
    if path.is_symlink():
        seen[str(path)] = ("symlink", os.readlink(path))
    elif path.is_dir():
        seen[str(path)] = ("dir",)
    elif path.is_file():
        info = path.stat()
        seen[str(path)] = ("file", info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
    elif path.exists():
        seen[str(path)] = ("other",)


def _snapshot(paths: list[Path]) -> dict[str, tuple]:
    """Every entry under each path as the filesystem reports it now. An absent path contributes nothing, so a created
    path shows up as a new key."""
    seen: dict[str, tuple] = {}
    for top in paths:
        _record(top, seen)
        if top.is_dir() and not top.is_symlink():
            for dirpath, dirnames, filenames in os.walk(top):
                for name in dirnames + filenames:
                    _record(Path(dirpath) / name, seen)
    return seen


def _changes(before: dict[str, tuple], after: dict[str, tuple]) -> list[str]:
    out: list[str] = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before:
            out.append(f"created {key}")
        elif key not in after:
            out.append(f"removed {key}")
        elif before[key] != after[key]:
            out.append(f"changed {key}")
    return out


def _assert_untouched(before: dict[str, tuple], after: dict[str, tuple]) -> None:
    changed = _changes(before, after)
    assert changed == [], "read path mutated the filesystem:\n  " + "\n  ".join(changed)


@contextmanager
def _kernel_read_only(swarm: Path) -> Iterator[None]:
    """The swarm dir and its database refuse writes for this uid, by permission bits; the probe proves the refusal."""
    db = swarm / "tasks.db"
    os.chmod(db, 0o444)
    os.chmod(swarm, 0o555)
    try:
        with pytest.raises(PermissionError):
            (swarm / "probe").write_text("x", encoding="utf-8")
        yield
    finally:
        os.chmod(swarm, 0o755)
        os.chmod(db, 0o644)


def _orch_status(root: Path, swarm: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """`scripts/orch_status.py --json` as a child process, pinned to the Python read path with SWARM_DIR set to `swarm`."""
    env = {k: v for k, v in os.environ.items() if k not in _ROUTE_ENV}
    env.update(SWARM_DIR=str(swarm), SWARM_PURE_PYTHON="1", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(ORCH_STATUS), "--root", str(root), "--json", *args], cwd=str(root),
                          env=env, capture_output=True, text=True, timeout=120, check=False)


def _payload(proc: subprocess.CompletedProcess[str]) -> dict:
    assert proc.stdout.strip(), f"no JSON on stdout (exit {proc.returncode}): {proc.stderr[-800:]}"
    return json.loads(proc.stdout)


def _explicit_outcome(proc: subprocess.CompletedProcess[str]) -> None:
    """A success that says so in its JSON status, or a failure with a diagnostic. A silent exit is not allowed."""
    if proc.returncode == 0:
        assert _payload(proc)["status"] == "ok"
    else:
        assert (proc.stdout + proc.stderr).strip(), f"exit {proc.returncode} with no diagnostic"


@pytest.mark.parametrize("kind", ["rollback", "wal"])
def test_store_reader_reads_supported_store_without_creating_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    root = tmp_path / "proj"
    _build(kind, root, monkeypatch)
    before = _snapshot(_watched(root))
    with StoreReader(root) as reader:
        assert reader.available
        row = reader.task("T-1")
        transitions = reader.transitions("T-1")
        artifacts = reader.artifacts("T-1")
        verdicts = reader.verdicts("T-1")
    by_helper = read_task("T-1", root=root)
    handover = handover_status("T-1", root=root)
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)
    assert row is not None and row["task_id"] == "T-1" and row["state"] == "CREATED"
    assert by_helper is not None and by_helper["state"] == row["state"]
    assert isinstance(transitions, list) and isinstance(artifacts, list) and verdicts == []
    assert handover["status"] == "missing"


def test_read_helpers_on_fresh_root_report_no_store_and_create_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    _build("fresh", root, monkeypatch)
    before = _snapshot(_watched(root))
    with StoreReader(root) as reader:
        assert reader.available is False
        assert reader.task("T-1") is None
        assert reader.verdicts("T-1") == []
    assert read_task("T-1", root=root) is None
    assert list_handovers("T-1", root=root) == []
    assert handover_status("T-1", root=root)["status"] == "missing"
    assert read_events(root=root) == []
    assert latest_correlation(root) is None
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)


def test_directory_in_store_place_is_reported_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    _build("dir-store", root, monkeypatch)
    before = _snapshot(_watched(root))
    with StoreReader(root) as reader:
        assert reader.available is False
        assert reader.task("T-1") is None
        assert reader.verdicts("T-1") == []
    assert read_task("T-1", root=root) is None
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)


def test_non_database_store_file_is_refused_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    _build("garbage-store", root, monkeypatch)
    before = _snapshot(_watched(root))
    with pytest.raises(sqlite3.DatabaseError):
        with StoreReader(root) as reader:
            reader.task("T-1")
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)


def test_store_without_task_tables_is_refused_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    _build("foreign-store", root, monkeypatch)
    before = _snapshot(_watched(root))
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with StoreReader(root) as reader:
            reader.task("T-1")
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)


def test_store_reader_on_unwritable_location_reports_unavailable_and_creates_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    _build("unwritable-location", root, monkeypatch)
    before = _snapshot(_watched(root))
    with StoreReader(root) as reader:
        assert reader.available is False
        assert reader.task("T-1") is None
    assert read_task("T-1", root=root) is None
    assert list_handovers("T-1", root=root) == []
    assert handover_status("T-1", root=root)["status"] == "missing"
    assert read_events(root=root) == []
    assert latest_correlation(root) is None
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)


@pytest.mark.skipif(_IS_ROOT, reason="root bypasses permission bits; the unwritable-location cases refuse for every uid")
def test_store_reader_on_kernel_read_only_store_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    swarm = _build("rollback", root, monkeypatch)
    with _kernel_read_only(swarm):
        before = _snapshot(_watched(root))
        with StoreReader(root) as reader:
            row = reader.task("T-1")
        after = _snapshot(_watched(root))
    assert row is not None and row["task_id"] == "T-1"
    _assert_untouched(before, after)


@pytest.mark.parametrize("kind", ["fresh", "rollback", "wal"])
def test_orch_status_read_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    root = tmp_path / "proj"
    swarm = _build(kind, root, monkeypatch)
    before = _snapshot(_watched(root))
    proc = _orch_status(root, swarm)
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)
    payload = _payload(proc)
    assert payload["status"] == "ok"
    assert [t["task_id"] for t in payload["tasks"]] == (["T-1"] if kind != "fresh" else [])


@pytest.mark.parametrize("kind", ["rollback", "wal"])
def test_orch_status_history_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    root = tmp_path / "proj"
    swarm = _build(kind, root, monkeypatch)
    before = _snapshot(_watched(root))
    proc = _orch_status(root, swarm, "--history", "T-1")
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)
    payload = _payload(proc)
    assert payload["status"] == "ok" and payload["task_id"] == "T-1"
    assert len(payload["history"]) >= 1


@pytest.mark.parametrize("kind", ["dir-store", "garbage-store", "foreign-store"])
def test_orch_status_on_unsupported_store_fails_explicitly_and_creates_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    root = tmp_path / "proj"
    swarm = _build(kind, root, monkeypatch)
    before = _snapshot(_watched(root))
    proc = _orch_status(root, swarm)
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)
    assert proc.returncode != 0
    assert _payload(proc)["status"] == "error"


def test_orch_status_on_unwritable_location_fails_explicitly_and_creates_nothing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    swarm = _build("unwritable-location", root, monkeypatch)
    before = _snapshot(_watched(root))
    proc = _orch_status(root, swarm)
    after = _snapshot(_watched(root))
    _assert_untouched(before, after)
    assert proc.returncode != 0
    _explicit_outcome(proc)


@pytest.mark.skipif(_IS_ROOT, reason="root bypasses permission bits; the unwritable-location cases refuse for every uid")
def test_orch_status_on_kernel_read_only_store_creates_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    swarm = _build("rollback", root, monkeypatch)
    with _kernel_read_only(swarm):
        before = _snapshot(_watched(root))
        proc = _orch_status(root, swarm)
        after = _snapshot(_watched(root))
    _assert_untouched(before, after)
    _explicit_outcome(proc)
