"""Parity of `orch_status --transition` on the Python script and the gsd_engine binary (Phase 14.5, unit U3.2c).

The cells are those of `_write_matrix.py`: the 351 transition vectors from the real TaskStore plus the added dry-run, id, grammar and lease
cells. Each cell runs from the same restored store on the Python script, on the script with GSD_ENGINE=rust (the selector), and on the
binary alone; stdout, the store dump and the effects must match, and a cell the binary must answer must not fall back. The matrix runs once
per module (`harness.run_write_cells`, the code `harness.py --rust` runs too) and each cell is one test.

The binary comes from GSD_ENGINE_BIN; the cell tests skip, with a message that names it, when it is unset.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _write_matrix as matrix  # noqa: E402
import harness  # noqa: E402

CELLS = matrix.load_cells()
Results = dict[str, list[harness.Check]]


@pytest.fixture(scope="module")  # type: ignore[untyped-decorator, unused-ignore]  # pytest is Any in a mypy environment without it
def matrix_results(tmp_path_factory: pytest.TempPathFactory) -> Results:
    if not os.environ.get("GSD_ENGINE_BIN"):
        pytest.skip("GSD_ENGINE_BIN is not set: point it at the built gsd_engine binary to run the transition parity matrix")
    try:
        binary = harness._rust_binary()
    except harness.RustLegUnavailable as exc:
        pytest.fail(f"GSD_ENGINE_BIN is set but unusable: {exc}")
    root = tmp_path_factory.mktemp("write-parity")
    return {cell.cell_id: checks for cell, checks in harness.run_write_cells(root, CELLS, binary)}


def test_every_vector_case_is_a_cell_and_the_added_cells_have_distinct_ids() -> None:
    cases = matrix.vector_cases()
    ids = [cell.cell_id for cell in CELLS]
    assert len(ids) == len(set(ids))
    assert [cell.cell_id for cell in CELLS[:len(cases)]] == [f"vec-{case['id']}" for case in cases]
    assert len(cases) == 351
    assert Counter(cell.group for cell in CELLS)["lease"] >= 3  # a terminal state each: CANCELLED, FAILED, DONE


def test_the_derived_refusals_agree_with_the_vectors_flags() -> None:
    assert matrix.cross_check(matrix.vector_cases()) == []


def test_lease_cells_cover_every_terminal_state_and_the_skipped_settle() -> None:
    by_id = {cell.cell_id: cell for cell in CELLS}
    targets = {by_id[name].argv[2] for name in ("extra-lease-cancelled-releases", "extra-lease-failed-releases", "extra-lease-done-completes")}
    assert targets == {"CANCELLED", "FAILED", "DONE"}
    running = by_id["extra-lease-running-defers"].row
    assert running is not None and '"running"' in str(running["notes"]) and '"lease_id"' in str(running["notes"])


def test_known_divergences_name_cells_and_every_entry_is_used() -> None:
    named = {cell.known for cell in CELLS if cell.known is not None}
    assert named == set(matrix.KNOWN_DIVERGENCES)
    assert all(cell.known is None for cell in CELLS if cell.group == "lease")  # D-1 is fixed: no lease cell may name a divergence
    assert {cell.cell_id for cell in CELLS if cell.known == "D-2"} == {"extra-ids-reach-the-run-log"}
    assert all(cell.known is None for cell in CELLS if cell.fallback is not None)  # a cell Python answers cannot diverge


@pytest.mark.parametrize("cell", CELLS, ids=[cell.cell_id for cell in CELLS])  # type: ignore[untyped-decorator, unused-ignore]
def test_cell_matches_python(cell: matrix.Cell, matrix_results: Results) -> None:
    checks = matrix_results[cell.cell_id]
    assert checks, f"{cell.cell_id} ran no checks"
    failed = [f"{check.label}: {check.detail}" for check in checks if not check.ok]
    assert not failed, "\n".join(failed)
    script_and_binary = [check.served_by for check in checks if "default selector" not in check.label]
    if cell.fallback is None:
        assert script_and_binary == ["rust", "rust"], f"{cell.cell_id} must be served by Rust, not {script_and_binary}"
    else:
        assert all(served.startswith("fallback:") for served in script_and_binary), f"{cell.cell_id}: {script_and_binary}"


def test_the_matrix_counts(matrix_results: Results) -> None:
    served = Counter(check.served_by for checks in matrix_results.values() for check in checks if "default selector" not in check.label)
    refused = sum(cell.fallback is not None for cell in CELLS)
    assert served["rust"] == 2 * (len(CELLS) - refused)
    assert sum(count for name, count in served.items() if name.startswith("fallback:")) == 2 * refused
