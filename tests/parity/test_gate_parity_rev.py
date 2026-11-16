"""Parity of `rev_gate` on Python and on the host engine (phase 14.5, unit U3.6d).

Each cell of `_rev_gate_corpus.CASES` runs the real `scripts/rev_gate.py --json` and the same argv through the binary with the
selector's request shape. A served cell must be answered by the binary, and its reply must equal Python's (verdict, runs,
findings with their ids and order, stats, envelope payload and summary; the envelope's clock and random fields are masked). A
declined cell must come back as `E-DEP` with `write_state` `not_started`, which is the selector's fallback trigger, so Python
answers it. Unset `GSD_ENGINE_BIN` skips.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _rev_gate_corpus as corpus  # noqa: E402


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        pytest.skip("GSD_ENGINE_BIN is not set")
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is not executable: {value}")
    return value


@pytest.mark.parametrize("case", corpus.CASES, ids=[case.name for case in corpus.CASES])
def test_cell_matches_python_or_falls_back(case: corpus.Case, tmp_path: Path) -> None:
    binary = _binary()
    root_p, argv_p, env_p = corpus.materialize(case, tmp_path / "py")
    oracle = corpus.run_python(root_p, argv_p, env_p)
    root_r, argv_r, env_r = corpus.materialize(case, tmp_path / "rs")
    code, reply = corpus.run_binary(binary, root_r, argv_r, env_r)
    if not case.served:
        assert corpus.declined(code, reply), f"{case.name} must fall back to Python: {code} {reply}"
        return
    assert corpus.answered(code, reply), f"{case.name} must be answered by the binary: {code} {reply}"
    assert case.json, f"{case.name}: a text-mode call is never served"
    assert code == oracle.returncode, f"{case.name}\n{oracle.stdout}\n{oracle.stderr}"
    assert corpus.comparable(json.loads(oracle.stdout), argv_p) == corpus.comparable(reply, argv_r), case.name
