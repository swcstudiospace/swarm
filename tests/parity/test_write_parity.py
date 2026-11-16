"""Parity of `orch_status --transition` on the Python script and the gsd_engine binary (Phase 14.5, unit U3.2c).

The cells are those of `_write_matrix.py`: the 351 transition vectors from the real TaskStore plus the added dry-run, id, grammar and lease
cells. Each cell runs from the same restored store on the Python script, on the script with GSD_ENGINE=rust (the selector), and on the
binary alone; stdout, the store dump and the effects must match, and a cell the binary must answer must not fall back. The matrix runs once
per module (`harness.run_write_cells`, the code `harness.py --rust` runs too) and each cell is one test.

The binary comes from GSD_ENGINE_BIN; the cell tests skip, with a message that names it, when it is unset. Under CI a job that names the
binary (GSD_ENGINE_BIN) or the build (GSD_ENGINE_BUILD) it tests has promised one: there the cells FAIL instead of skipping
(`binary_required`, the rule of tests/test_keys_never_in_guest.py), so a job that forgot to build or name it cannot read as green.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _write_matrix as matrix  # noqa: E402
import harness  # noqa: E402


def binary_required() -> bool:
    """Under CI a job that names the binary (`GSD_ENGINE_BIN`) or the build (`GSD_ENGINE_BUILD`) it tests has promised one."""
    return bool(os.environ.get("CI")) and bool(os.environ.get("GSD_ENGINE_BIN") or os.environ.get("GSD_ENGINE_BUILD"))


def _engine_binary(what: str) -> str:
    """The binary named by GSD_ENGINE_BIN, for the cells of `what`. Unset skips, naming the variable, outside a job that promised one and
    FAILS inside it; one that is set but unusable fails everywhere."""
    if not os.environ.get("GSD_ENGINE_BIN"):
        reason = f"GSD_ENGINE_BIN is not set: point it at the built gsd_engine binary to run {what}"
        if binary_required():
            pytest.fail(f"{reason}. CI is set and this job names its binary or build: the cells must run, not skip")
        pytest.skip(reason)
    try:
        return harness._rust_binary()
    except harness.RustLegUnavailable as exc:
        pytest.fail(f"GSD_ENGINE_BIN is set but unusable: {exc}")


CELLS = matrix.load_cells()
Results = dict[str, list[harness.Check]]


@pytest.fixture(scope="module")  # type: ignore[untyped-decorator, unused-ignore]  # pytest is Any in a mypy environment without it
def matrix_results(tmp_path_factory: pytest.TempPathFactory) -> Results:
    binary = _engine_binary("the transition parity matrix")
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


# --- the signed cells (unit U3.4d): a transition to APPROVED reads the signing keys and the signed verdict rows -----------------------
#
# Each cell builds a store with the REAL `make_verdict` under the key profile `sign`, then runs the call under the profile `call` on the
# Python script (the truth), on the script with GSD_ENGINE=rust (the selector) and on the binary alone, from identical copies of that store.
# stdout, exit code, the `tasks`/`transitions`/`verdicts`/`artifacts` dump and the effects must match. GSD_PARITY_BACKEND=sandbox runs them
# on the sandbox backend; only a binary built with the sandbox-write feature (GSD_ENGINE_BUILD=sandbox-write) serves there, any other build
# refuses before the write and Python answers.

BACKEND = os.environ.get("GSD_PARITY_BACKEND", "host")
SANDBOX_WRITES = os.environ.get("GSD_ENGINE_BUILD") == "sandbox-write"
SIGNED = matrix.SIGNED_CELLS
SEED_SLOTS = ("hmac", "other", "ed", "file")


@dataclass
class Leg:
    """What one leg of a signed cell printed and left."""

    probe: harness.Probe
    dump: str
    effects: str
    started: bool = False


def _leg(sandbox: harness.Sandbox, cell: matrix.SignedCell, probe: harness.Probe, started: bool = False) -> Leg:
    db = sandbox.swarm_dir / "tasks.db"
    return Leg(probe, matrix.dump_store(db, *cell.masks), matrix.effects_of(sandbox.swarm_dir, [], *cell.masks), started)


def _differences(who: str, truth: Leg, other: Leg, cell: matrix.SignedCell, *, effects: bool) -> list[str]:
    problems: list[str] = []
    if truth.probe.exit_code != other.probe.exit_code:
        problems.append(f"{who}: exit {other.probe.exit_code} vs Python {truth.probe.exit_code}")
    want = matrix.mask(truth.probe.stdout.decode("utf-8", "backslashreplace"), *cell.masks)
    got = matrix.mask(other.probe.stdout.decode("utf-8", "backslashreplace"), *cell.masks)
    if want != got:
        problems.append(f"{who}: stdout differs, {harness._first_difference(want, got)}")
    if truth.dump != other.dump:
        problems.append(f"{who}: store dump differs, {harness._first_difference(truth.dump, other.dump)}")
    if effects and truth.effects != other.effects:
        problems.append(f"{who}: effects differ, {harness._first_difference(truth.effects, other.effects)}")
    if effects and truth.probe.stderr != other.probe.stderr:
        problems.append(f"{who}: stderr differs, {truth.probe.stderr[:100]!r} vs {other.probe.stderr[:100]!r}")
    return problems


def _seed(sandbox: harness.Sandbox, cell: matrix.SignedCell, secret_values: dict[str, str]) -> Path:
    """Builds the cell's store with `make_verdict` under the SIGN profile. The run-log events of the signing (a dev-key audit) land in a
    scratch directory, never in the state the legs start from; returns that directory."""
    scratch = sandbox.base / "seed-scratch"
    scratch.mkdir()
    env = sandbox.env({**matrix.profile_env(cell.sign, secret_values, sandbox.base), "SWARM_DIR": str(scratch), "PYTHONPATH": str(harness.REPO),
                       "SWARM_GSD_STATE_FILE": "", "SWARM_CLAUDE_STATE_DIR": str(sandbox.base / "claude-state")})
    spec = matrix.seed_spec(matrix.SCENARIOS[cell.scenario], sandbox.swarm_dir / "tasks.db", scratch)
    done = subprocess.run([sys.executable, "-c", matrix.SEED_SCRIPT], input=json.dumps(spec).encode(), env=env, cwd=harness.REPO,
                          capture_output=True, timeout=harness.PROCESS_TIMEOUT_S, check=False)
    assert done.returncode == 0, f"the store of {cell.cell_id} could not be built: {done.stderr.decode(errors='replace')[-300:]}"
    return scratch


def _direct(sandbox: harness.Sandbox, cell: matrix.SignedCell, argv: list[str], initial: Path, overrides: dict[str, str],
            request_keys: tuple[str, ...]) -> tuple[Leg, Path]:
    """The binary alone, started the way a script with no selector starts it (the whole environment), with an effects file."""
    sandbox.restore(initial)
    manifest = sandbox.base / "probe" / "effects.json"
    manifest.write_bytes(b"")
    env = sandbox.env({"GSD_RUST_BACKEND": sandbox.backend, **overrides})
    workspace = str(sandbox.workspace)
    request = {"agent": "A01", "script": "orch_status", "argv": [*argv, "--root", workspace, "--json"], "root": workspace,
               "env": {key: env[key] for key in request_keys if env.get(key)}, "effects_path": str(manifest)}
    assert sandbox.rust_bin is not None
    done = subprocess.run([sandbox.rust_bin], input=json.dumps(request, ensure_ascii=False).encode("utf-8"), capture_output=True,
                          cwd=sandbox.workspace, env=env, timeout=harness.PROCESS_TIMEOUT_S, check=False)
    probe = harness.Probe(done.returncode, done.stdout, done.stderr, harness._parse_object(done.stdout))
    return _leg(sandbox, cell, probe), manifest


def _truth_problems(cell: matrix.SignedCell, truth: Leg, sandbox: harness.Sandbox) -> list[str]:
    """Python's own answer is the one the cell says it is: approved, or the cell's error (E-POLICY unless the cell names another code) with
    the cell's text, and the rows are what that leaves."""
    row, audit = matrix.task_state(sandbox.swarm_dir / "tasks.db", matrix.TARGET)
    assert row is not None
    payload = truth.probe.payload or {}
    if cell.message is None:
        ok = truth.probe.exit_code == 0 and row["state"] == "APPROVED" and audit[-1]["to_state"] == "APPROVED"
        return [] if ok else [f"Python did not approve: exit {truth.probe.exit_code}, state {row['state']}"]
    error = payload.get("error") or {}
    ok = (truth.probe.exit_code == 2 and error.get("code") == cell.code and cell.message in str(error.get("message"))
          and row["state"] == "IN_REVIEW")
    return [] if ok else [f"Python's answer is not the {cell.code} {cell.message!r}: exit {truth.probe.exit_code}, {str(error.get('message'))[:120]!r}"]


def run_signed_cell(base: Path, cell: matrix.SignedCell, binary: str, request_keys: tuple[str, ...]) -> list[str]:
    """Every problem of one signed cell (empty: the binary leaves Python's bytes, store and effects, or refuses and Python answers)."""
    sandbox = harness.Sandbox(base / cell.cell_id, cell.cell_id, binary, BACKEND, SANDBOX_WRITES)
    try:
        secret_values = {slot: secrets.token_hex(32) for slot in SEED_SLOTS}
        scratch = _seed(sandbox, cell, secret_values)
        argv = ["--transition", matrix.TARGET, "APPROVED", "--reason", "signed cell"]
        initial = sandbox.snapshot("initial")
        if cell.key_file:
            matrix.write_conventional_key(sandbox.base / "xdg", secret_values["file"])
        overrides = {**matrix.profile_env(cell.call, secret_values, sandbox.base), "SWARM_GSD_STATE_FILE": "",
                     "SWARM_CLAUDE_STATE_DIR": str(sandbox.base / "claude-state")}

        def script_leg(engine: str) -> Leg:
            sandbox.restore(initial)
            probe = harness.invoke(sandbox, "orch_status", argv, engine=engine, env=overrides)
            return _leg(sandbox, cell, probe, engine != "python" and sandbox.started_marker.exists())

        truth = script_leg("python")
        problems = _truth_problems(cell, truth, sandbox)
        if "security.dev_key" in truth.effects:
            problems.append("Python's APPROVED transition emitted security.dev_key: only sign_envelope does, so the manifest needs it too")
        signed_dev = cell.sign.startswith("dev")
        audited = (scratch / "events.jsonl").exists() and "security.dev_key" in (scratch / "events.jsonl").read_text(encoding="utf-8")
        if signed_dev != audited:
            problems.append(f"the dev-key signing audit {'is missing' if signed_dev else 'appeared'} in the seed scratch: the control is broken")
        chosen = script_leg("selector")
        direct, manifest = _direct(sandbox, cell, argv, initial, overrides, request_keys)
        refused = harness._not_started(direct.probe)
        refuses_all = BACKEND == "sandbox" and not SANDBOX_WRITES
        expected = "the sandbox backend of a build without sandbox-write refuses" if refuses_all else cell.fallback
        if (refused is not None) != (expected is not None):
            problems.append(f"the binary {'refused with not_started' if refused else 'answered'}, the cell expects it to "
                            f"{'answer' if expected is None else 'refuse: ' + expected}")
        problems += _differences("selector leg", truth, chosen, cell, effects=True)
        written = [manifest.read_text(encoding="utf-8")] if manifest.exists() else []
        for who, leg in (("Python", truth), ("selector leg", chosen), ("binary alone", direct)):
            seen = [leg.probe.stdout.decode("utf-8", "backslashreplace"), leg.probe.stderr.decode("utf-8", "backslashreplace"), leg.dump,
                    leg.effects, *written]
            problems += [f"{who}: the {slot} secret of the cell is in its output or store" for slot, value in secret_values.items()
                         if any(value in text for text in seen)]
        if cell.key_file:
            # a conventional key file beside an isolated child: the selector answers in Python and never starts the binary
            if chosen.started:
                problems.append("the selector started the binary beside a conventional key file: Python answers that call")
        elif not chosen.started:
            problems.append("the selector did not start the binary")
        if refused is not None:
            untouched = matrix.dump_store(initial / "state" / ".swarm" / "tasks.db", *cell.masks)
            if direct.dump != untouched or manifest.read_bytes():
                problems.append("a refused call changed the store or wrote an effects manifest")
        else:
            problems += _differences("binary alone", truth, direct, cell, effects=False)
            if "security.dev_key" in manifest.read_text(encoding="utf-8"):
                problems.append("the effects manifest names security.dev_key, which Python's transition never emits")
        return problems
    finally:
        shutil.rmtree(sandbox.base, ignore_errors=True)


@pytest.fixture(scope="module")  # type: ignore[untyped-decorator, unused-ignore]
def signed_results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, list[str]]:
    binary = _engine_binary("the signed APPROVED cells")
    root = tmp_path_factory.mktemp("signed-parity")
    anchor = harness.Sandbox(root / "anchor", "signed_cells", binary, BACKEND, SANDBOX_WRITES)
    request_keys = tuple(harness._selector(anchor).RUST_ENV_KEYS)
    outcomes: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {cell.cell_id: pool.submit(run_signed_cell, root / "cells", cell, binary, request_keys) for cell in SIGNED}
        for cell_id, future in futures.items():
            try:
                outcomes[cell_id] = future.result()
            except Exception as exc:  # noqa: BLE001 - one broken cell must not hide the others
                outcomes[cell_id] = [f"the cell did not run to its end: {type(exc).__name__}: {exc}"]
    return outcomes


def test_the_signed_cells_name_every_case_of_the_key_policy_and_the_gate_facts() -> None:
    ids = [cell.cell_id for cell in SIGNED]
    assert len(ids) == len(set(ids))
    assert all(cell.scenario in matrix.SCENARIOS and cell.sign in matrix.PROFILES and cell.call in matrix.PROFILES for cell in SIGNED)
    joined = " ".join(ids)
    for needed in ("pass-hmac", "pass-ed25519", "pass-both-keys", "pass-dev-key-opt-in", "refused-failing-verdict", "refused-expired-verdict",
                   "refused-unaccepted-gate-task", "refused-wrong-correlation", "refused-rows-before-verdicts-since",
                   "refused-tampered-signature", "refused-dry-run-rows", "refused-no-key", "refused-require-key-without-a-key",
                   "key-file-reference-goes-to-python"):
        assert needed in joined, needed
    fallbacks = {cell.cell_id for cell in SIGNED if cell.fallback is not None}
    assert fallbacks == {"key-file-reference-goes-to-python", "refused-dev-rows-beside-a-conventional-key-file",
                         "refused-invalid-findings-on-a-required-row", "refused-null-findings-on-a-required-row",
                         "refused-invalid-findings-on-a-gate-that-is-not-required", "refused-invalid-findings-on-a-row-with-a-null-gate"}
    assert {cell.cell_id for cell in SIGNED if cell.key_file} == {"refused-dev-rows-beside-a-conventional-key-file"}
    assert {cell.code for cell in SIGNED} == {"E-POLICY", "E-INPUT", "E-INTERNAL"}
    approved = [cell for cell in SIGNED if cell.message is None]
    refused = [cell for cell in SIGNED if cell.message is not None]
    assert len(approved) >= 14 and len(refused) >= 20  # both outcomes are covered, so neither is a vacuous pass


@pytest.mark.parametrize("cell", SIGNED, ids=[cell.cell_id for cell in SIGNED])  # type: ignore[untyped-decorator, unused-ignore]
def test_signed_cell_matches_python(cell: matrix.SignedCell, signed_results: dict[str, list[str]]) -> None:
    assert not signed_results[cell.cell_id], "\n".join(signed_results[cell.cell_id])


@pytest.mark.parametrize(  # type: ignore[untyped-decorator, unused-ignore]
    ("ci", "binary", "build", "outcome"),
    [
        ("true", None, None, pytest.skip.Exception),  # the `test` job: CI set, nothing named
        ("true", None, "default", pytest.fail.Exception),  # a job that names its build must name its binary
        ("true", "{missing}", None, pytest.fail.Exception),  # a named binary that is not there
        ("true", "{missing}", "sandbox-write", pytest.fail.Exception),
        (None, None, None, pytest.skip.Exception),
        (None, None, "default", pytest.skip.Exception),  # outside CI a developer without a binary skips
        (None, "{missing}", None, pytest.fail.Exception),  # a named binary that is not there fails everywhere
    ],
)
def test_the_cells_fail_where_a_job_promised_a_binary_and_skip_otherwise(
    ci: str | None, binary: str | None, build: str | None, outcome: type[BaseException], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name, value in (("CI", ci), ("GSD_ENGINE_BIN", binary and binary.replace("{missing}", str(tmp_path / "no-engine"))),
                        ("GSD_ENGINE_BUILD", build)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(BaseException) as caught:  # a skip is a BaseException too: it must not pass for a failure
        _engine_binary("the cells")
    assert caught.type is outcome
