"""U5 thread stress through swarm._native (Phase 11).

Eight threads call into the extension at the same time, the shape of the daemon pool in
scripts/swarm_run.py. The run must finish with no hang, return identical verdicts on a second run,
and stay within the 60 s budget of the U5 acceptance. Each thread's operation sequence is fixed by
its index, so the verdicts are deterministic and can be compared across runs.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
import pytest

from swarm._native_loader import load_native

pytest.importorskip("swarm._native")  # every test loads the extension; the pure-Python leg does not build it

THREADS = 8
OPS_PER_THREAD = 200
WAIT_BUDGET_S = 50.0
SUITE_BUDGET_S = 60.0
UNKNOWN_CODE = "E-UNKNOWN"


def _list_request(root: Path) -> bytes:
    """A valid list-mode snapshot over an empty store: every key the wire requires."""
    swarm_dir = os.path.join(str(root), ".swarm")
    return json.dumps({
        "mode": "list",
        "agent": "A01",
        "script": "orch_status",
        "correlation": None,
        "db_path": os.path.join(swarm_dir, "tasks.db"),
        "swarm_dir": swarm_dir,
        "events": 0,
        "tasks": [],
        "verdicts": [],
    }).encode("utf-8")


def _worker(native: Any, codes: list[str], request: bytes, index: int) -> list[str]:
    """Runs OPS_PER_THREAD operations: a wire code, an unknown code, or an orch_status call."""
    outcomes: list[str] = []
    for op in range(OPS_PER_THREAD):
        step = (index * OPS_PER_THREAD + op) % (len(codes) + 2)
        if step < len(codes):
            outcomes.append(native.code_from_wire(codes[step]))
        elif step == len(codes):
            try:
                native.code_from_wire(UNKNOWN_CODE)
            except ValueError:
                outcomes.append("ValueError")
        else:
            decoded = native.decode_reply_wire(native.orch_status_invoke(request))
            digest = hashlib.sha256(decoded["stdout"].encode("utf-8")).hexdigest()[:16]
            outcomes.append(f"{decoded['exit_code']}:{digest}")
    return outcomes


def _run_once(native: Any, root: Path) -> list[list[str]]:
    codes = list(native.wire_codes())
    request = _list_request(root)
    with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        futures = [pool.submit(_worker, native, codes, request, index) for index in range(THREADS)]
        _, not_done = concurrent.futures.wait(futures, timeout=WAIT_BUDGET_S)
    assert not not_done, f"{len(not_done)} thread(s) did not finish within {WAIT_BUDGET_S} s"
    return [future.result() for future in futures]


def test_threads_finish_without_hangs_and_repeat_their_verdicts(tmp_path: Path) -> None:
    native = load_native()
    # Create the store once on one thread, so the concurrent reads see an existing schema.
    native.orch_status_invoke(_list_request(tmp_path))

    started = time.monotonic()
    first = _run_once(native, tmp_path)
    second = _run_once(native, tmp_path)
    elapsed = time.monotonic() - started

    assert all(len(outcomes) == OPS_PER_THREAD for outcomes in first)
    assert first == second
    assert elapsed < SUITE_BUDGET_S, f"two thread runs took {elapsed:.1f} s"
