"""Atomic projection writes under thread contention: temp names unique per call, no lost writes, no torn reads."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from conftest import ROOT

from swarm.atomic_file import write_text_atomic
from swarm.observability import write_claude_state
from swarm.state_store import SwarmStateStore

THREADS = 8


def _runner() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_run_atomic_under_test", ROOT / "scripts" / "swarm_run.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _leftovers(*dirs: Path) -> list[Path]:
    return [p for d in dirs for p in d.iterdir() if p.name.endswith(".tmp")]


class _ReplaceSpy:
    """Wraps os.replace: records temp names and counts the exceptions callers might swallow."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.real = os.replace
        self.names: list[str] = []
        self.errors = 0
        self._lock = threading.Lock()
        monkeypatch.setattr(os, "replace", self)

    def __call__(self, src: Any, dst: Any) -> None:
        with self._lock:
            self.names.append(Path(src).name)
        try:
            self.real(src, dst)
        except BaseException:
            with self._lock:
                self.errors += 1
            raise


def _hammer(work: Callable[[int], None], n_threads: int = THREADS) -> list[BaseException]:
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            work(i)
        except BaseException as exc:  # collected, asserted by the caller
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_helper_writes_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_text_atomic(target, "hello\n")
    assert target.read_text() == "hello\n"
    assert _leftovers(tmp_path) == []


def test_helper_failure_keeps_old_content_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "out.json"
    target.write_text("old")

    def boom(src: Any, dst: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_text_atomic(target, "new")
    assert target.read_text() == "old"
    assert _leftovers(tmp_path) == []


def test_helper_uses_distinct_temp_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _ReplaceSpy(monkeypatch)
    target = tmp_path / "out.json"
    write_text_atomic(target, "a")
    write_text_atomic(target, "b")
    assert len(spy.names) == 2 and spy.names[0] != spy.names[1]


@pytest.mark.parametrize("shared", [True, False], ids=["shared-store", "store-per-thread"])
def test_state_store_concurrent_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shared: bool) -> None:
    per_thread = 100
    stores = [SwarmStateStore(root=tmp_path)] * THREADS if shared else [SwarmStateStore(root=tmp_path) for _ in range(THREADS)]
    reader = SwarmStateStore(root=tmp_path)
    spy = _ReplaceSpy(monkeypatch)
    first_done = threading.Event()
    writers_done = threading.Event()
    empty_reads: list[int] = []

    def write(i: int) -> None:
        for j in range(per_thread):
            stores[i].update("RUNNING", corr=f"c-{i}-{j}", progress={"i": i, "j": j})
            first_done.set()

    def read() -> None:
        first_done.wait()
        while not writers_done.is_set():
            if not reader.get():
                empty_reads.append(1)

    readers = [threading.Thread(target=read) for _ in range(2)]
    for t in readers:
        t.start()
    errors = _hammer(write)
    writers_done.set()
    for t in readers:
        t.join()

    assert errors == []
    assert spy.errors == 0, "a rename failed and the store swallowed it: a write was lost"
    assert empty_reads == []
    for path in (reader.claude_path, reader.swarm_path):
        assert json.loads(path.read_text())["swarm_state"] == "RUNNING"
    assert _leftovers(reader.claude_path.parent, reader.swarm_path.parent) == []


def test_write_claude_state_concurrent(tmp_path: Path) -> None:
    def work(i: int) -> None:
        for j in range(50):
            write_claude_state({"swarm_state": "RUNNING", "i": i, "j": j}, root=tmp_path)

    errors = _hammer(work)
    assert errors == []
    path = write_claude_state({"swarm_state": "DONE"}, root=tmp_path)
    assert json.loads(path.read_text()) == {"swarm_state": "DONE"}
    assert _leftovers(path.parent) == []


def test_write_cancel_flag_concurrent(tmp_path: Path) -> None:
    run = _runner()

    def work(i: int) -> None:
        for j in range(50):
            run.write_cancel_flag(tmp_path, "corr-1", f"reason-{i}-{j}", 5.0)

    errors = _hammer(work)
    assert errors == []
    path = run.write_cancel_flag(tmp_path, "corr-1", "final", 5.0)
    flag = json.loads(path.read_text())
    assert {"requested_wall", "reason", "grace_s"} <= flag.keys()
    assert flag["reason"] == "final"
    assert _leftovers(path.parent) == []
