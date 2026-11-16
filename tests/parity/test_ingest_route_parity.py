"""Parity of `orch_status --ingest` on Python and on the host `gsd_engine` (phase 14.5, unit U3.5e).

Each cell of `_ingest_corpus.py` seeds a store, then the same result is ingested by the Python script and by the selector
with `GSD_ENGINE=rust`. States, artifact rows, transition targets and the named effect events must match. A served cell
must be answered by the binary: a `not_started` refusal would make the selector run Python, and the cell would still
match without proving the route. Unset `GSD_ENGINE_BIN` skips, and fails under CI when the job promised a binary.
"""
import json
import os

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _ingest_corpus as corpus  # noqa: E402


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        reason = "GSD_ENGINE_BIN is not set: point it at the built gsd_engine binary to run the ingest parity cells"
        if os.environ.get("CI") and os.environ.get("GSD_ENGINE_BUILD"):
            pytest.fail(f"{reason}. CI is set and this job names its build: the cells must run, not skip")
        pytest.skip(reason)
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is set but not executable: {value}")
    return value


@pytest.mark.parametrize("cell", corpus.CELLS, ids=[cell.name for cell in corpus.CELLS])
def test_cell_matches_python(cell: corpus.Cell, tmp_path: Path) -> None:
    binary = _binary()
    py_root, rs_root, probe = tmp_path / "py", tmp_path / "rs", tmp_path / "probe"
    corpus.seed(py_root, cell)
    shutil.copytree(py_root / ".swarm", rs_root / ".swarm")
    shutil.copytree(py_root / ".swarm", probe / ".swarm")
    result = tmp_path / "result.json"
    if not cell.missing:
        state = "IN_REVIEW" if cell.name in ("review", "escalate", "advisory-skip") else "IN_PROGRESS"
        result.write_text(json.dumps(corpus.result_body("H-1", state)), encoding="utf-8")
    else:
        result = tmp_path / "missing.json"
    py = corpus.run(py_root, cell, "python", result)
    rs = corpus.run(rs_root, cell, "rust", result, binary)
    py_snap, rs_snap = corpus.snapshot(py_root), corpus.snapshot(rs_root)
    py_body, rs_body = corpus.loads(py.stdout), corpus.loads(rs.stdout)
    if py_body is not None and rs_body is not None and "summary" in py_body:
        assert py_body["summary"] == rs_body.get("summary"), f"{py.stdout}\n{rs.stdout}"
    if py_body is not None and rs_body is not None and "error" in py_body:
        assert py_body["error"]["code"] == rs_body["error"]["code"], f"{py.stdout}\n{rs.stdout}"
        assert py_body["error"]["message"] == rs_body["error"]["message"], f"{py.stdout}\n{rs.stdout}"
    assert py.returncode == rs.returncode, f"exit {py.returncode} vs {rs.returncode}\n{py.stdout}\n{rs.stdout}\n{rs.stderr}"
    assert py_snap["tasks"] == rs_snap["tasks"] == list(cell.expect_states.items())
    assert py_snap["artifacts"] == rs_snap["artifacts"]
    assert py_snap["transitions"] == rs_snap["transitions"]
    assert corpus.named_events(py_snap["events"]) == corpus.named_events(rs_snap["events"])
    for event in cell.expect_events:
        assert event in rs_snap["events"], f"{cell.name} did not emit {event}: {rs_snap['events']}"
    if cell.expect_code is not None:
        body = corpus.loads(rs.stdout)
        assert body is not None and body["error"]["code"] == cell.expect_code, rs.stdout
    if cell.served:
        assert corpus.binary_answered(probe, cell, result, binary), f"{cell.name} was handed back to Python"
