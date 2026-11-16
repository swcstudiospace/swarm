"""Phase 14.4 parity (unit S1.2): the scheduler primitives of the Python runtime against their Rust ports in `swarm._native`.

The committed vectors (`tests/parity/fixtures/scheduler/<behaviour>.json`, built by `_scheduler_corpus.py`, written by
`.planning/research/scheduler-oracle/dump_oracle.py`) record what the Python scheduler returns: `order_ready` with its P2 fallback for an
unknown or missing priority, claim normalisation, overlap, footprint and `ClaimTable` scenarios, the AIMD limiter (success, rate limit with
and without retry-after, the failure-streak circuit, the once-per-burst gate, all on a fake clock), the backoff delay for a seeded jitter
sample, and the cancel-drain arithmetic. Two legs read them:

* ORACLE leg (the `*_oracle_*` tests; runs under SWARM_PURE_PYTHON=1, never loads the extension): the Python code still returns every
  committed value, and the committed inputs are what the seeded generator builds. This is the drift guard: when the Python scheduler
  changes, these fail until the vectors are regenerated on purpose.
* NATIVE leg (the `*_native_*` tests): the same inputs through `swarm._native.sched_*`, `ClaimTable` and `AimdLimiter` in a child process,
  compared with the committed values (see `_scheduler_corpus.py` for the surface it calls). The leg is skipped only when `swarm._native`
  cannot be imported at all. An extension that imports but lacks a scheduler name FAILS, naming unit S1.4.

The native leg fails in a checkout whose extension was built before unit S1.4 (NativeVectors) landed, because that extension has no scheduler
functions. That is intended: the orchestrator commits S1.2 and S1.4 together, and the leg must pass with 0 skips once the extension is rebuilt.

Two hooks, both for rehearsals: SWARM_SCHED_VECTORS_DIR points both legs at another directory of vector files (a scratch copy with one value
changed shows that the oracle leg has teeth), and SWARM_SCHED_PARITY_NATIVE names a Python file that stands in for `swarm._native` (it rehearses
the native leg and the comparison itself before the real functions exist). A failure log names the case id and prints values truncated.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's
from _scheduler_corpus import (
    BEHAVIOURS,
    MAX_CASES,
    NATIVE_BEHAVIOURS,
    NATIVE_NAMES,
    OVERRIDE_ENV,
    SCHEMA,
    SEED,
    VECTORS_ENV,
    UnitRng,
    build_cases,
    load_vectors,
    native_view,
    run_oracle,
    vectors_dir,
)
from swarm.retry import backoff_s

REPO = Path(__file__).resolve().parents[2]
CHILD_TIMEOUT_S = 150
REPORT_LIMIT = 5
SHOW_CHARS = 300

# What each behaviour's vectors must contain, so a degenerate corpus (every case the same outcome) cannot pass: label -> predicate over the cases.
Cases = list[dict[str, Any]]
COVERAGE: dict[str, dict[str, Callable[[Cases], bool]]] = {
    "order_ready": {
        "empty input": lambda cases: any(not c["input"]["rows"] for c in cases),
        "reordering": lambda cases: any(c["expected"] != sorted(c["expected"]) for c in cases),
        "stable ties": lambda cases: any(len(c["expected"]) > 1 and c["expected"] == sorted(c["expected"]) for c in cases),
        "unknown or missing priority": lambda cases: any(row[0] not in ("P0", "P1", "P2", "P3") for c in cases for row in c["input"]["rows"]),
    },
    "priority_fallback": {
        "every rank": lambda cases: {c["expected"]["rank"] for c in cases} == {0, 1, 2, 3},
        "fallback to P2": lambda cases: any(c["input"]["priority"] not in ("P0", "P1", "P2", "P3") and c["expected"]["rank"] == 2 for c in cases),
    },
    "claim_normalize": {
        "accepted": lambda cases: any("ok" in c["expected"] for c in cases),
        "rejected": lambda cases: any("error" in c["expected"] for c in cases),
        "changed by normalising": lambda cases: any(c["expected"].get("ok") not in (None, c["input"]["pattern"]) for c in cases),
    },
    "claim_overlap": {
        "overlapping": lambda cases: any(c["expected"].get("ok") is True for c in cases),
        "disjoint": lambda cases: any(c["expected"].get("ok") is False for c in cases),
        "rejected": lambda cases: any("error" in c["expected"] for c in cases),
    },
    "claim_conflict": {
        "conflict": lambda cases: any(c["expected"].get("conflict") is True for c in cases),
        "no conflict": lambda cases: any(c["expected"].get("conflict") is False for c in cases),
        "subtree footprint": lambda cases: any(len(c["expected"].get("footprint_a", [])) == 2 for c in cases),
        "rejected": lambda cases: any("error" in c["expected"] for c in cases),
    },
    "claim_table": {
        "a conflict": lambda cases: any(not r.get("ok", True) and r.get("conflicts") for c in cases for r in c["expected"]["results"]),
        "several conflicts": lambda cases: any(len(r.get("conflicts", [])) > 1 for c in cases for r in c["expected"]["results"]),
        "an accepted acquire": lambda cases: any(r.get("ok") is True for c in cases for r in c["expected"]["results"]),
        "a release that freed claims": lambda cases: any(r.get("released", 0) > 0 for c in cases for r in c["expected"]["results"]),
        "a release that freed nothing": lambda cases: any(r.get("released") == 0 for c in cases for r in c["expected"]["results"]),
        "a rejected claim": lambda cases: any("error" in r for c in cases for r in c["expected"]["results"]),
        "a held snapshot": lambda cases: any(r.get("held") for c in cases for r in c["expected"]["results"]),
    },
    "parse_claims": {
        "accepted": lambda cases: any("ok" in c["expected"] for c in cases),
        "rejected": lambda cases: any("error" in c["expected"] for c in cases),
    },
    "aimd": {
        "constructor refused": lambda cases: any("error" in c["expected"] for c in cases),
        "halving": lambda cases: any(b["current"] < a["current"] for c in cases for a, b in _pairs(c)),
        "growth": lambda cases: any(b["current"] > a["current"] for c in cases for a, b in _pairs(c)),
        "a hit counted": lambda cases: any(s.get("returned") is True for c in cases for s in c["expected"].get("steps", [])),
        "a hit deduplicated": lambda cases: any(s.get("returned") is False for c in cases for s in c["expected"].get("steps", [])),
        "streak halving": lambda cases: any(c["input"]["ops"][i][0] == "failure" and b["current"] < a["current"] for c in cases for i, (a, b) in enumerate(_pairs(c))),
    },
    "backoff": {
        "delay": lambda cases: any("ok" in c["expected"] for c in cases),
        "refused": lambda cases: any("error" in c["expected"] for c in cases),
        "clamped to the cap": lambda cases: any(c["expected"].get("ok") == c["input"]["cap_s"] for c in cases),
        "clamped to zero": lambda cases: any(c["expected"].get("ok") == 0.0 for c in cases),
        "past the exponent cap": lambda cases: any(c["input"]["attempt"] > 63 and "ok" in c["expected"] for c in cases),
    },
    "drain_plan": {
        "uncapped grace": lambda cases: any(c["expected"]["grace"] == c["input"]["cancel_grace_s"] for c in cases),
        "budget-capped grace": lambda cases: any(0 < c["expected"]["grace"] < c["input"]["cancel_grace_s"] for c in cases),
        "no grace left": lambda cases: any(c["expected"]["grace"] == 0.0 and c["input"]["cancel_grace_s"] > 0 for c in cases),
        "kill grace at its floor": lambda cases: any(c["expected"]["kill_grace"] == 0.5 for c in cases),
        "kill grace below the stop grace": lambda cases: any(c["expected"]["kill_grace"] < c["input"]["stop_grace_s"] for c in cases),
    },
}


def _pairs(case: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Consecutive (previous, next) steps of an AIMD case; the first step is compared with the start (the starting step has `current` == max_n)."""
    steps = case["expected"].get("steps", [])
    if not steps:
        return []
    start = {"current": case["input"]["params"]["max_n"]}
    return list(zip([start, *steps], steps))


def _show(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=True)
    return text if len(text) <= SHOW_CHARS else text[:SHOW_CHARS] + f"... ({len(text)} chars)"


def _mismatches(cases: Cases, expected: list[Any], got: list[Any]) -> list[str]:
    """One line per differing case: id, input, expected, got."""
    lines = []
    for case, want, have in zip(cases, expected, got):
        if want != have:
            lines.append(f"{case['id']}: input {_show(case['input'])}\n    expected {_show(want)}\n    got      {_show(have)}")
    return lines


def _report(title: str, lines: list[str], total: int) -> str:
    shown = "\n  ".join(lines[:REPORT_LIMIT])
    return f"{title}: {len(lines)} of {total} cases differ (first {min(REPORT_LIMIT, len(lines))}):\n  {shown}"


# ---- the oracle leg --------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("behaviour", BEHAVIOURS)
def test_oracle_returns_the_committed_vectors(behaviour: str) -> None:
    cases = load_vectors(behaviour)["cases"]
    got = [run_oracle(behaviour, case["input"]) for case in cases]

    lines = _mismatches(cases, [case["expected"] for case in cases], got)
    assert not lines, _report(f"{behaviour}: the Python oracle no longer returns the committed vectors (regenerate them on purpose with dump_oracle.py)", lines, len(cases))


@pytest.mark.parametrize("behaviour", BEHAVIOURS)
def test_oracle_vector_inputs_are_the_seeded_corpus(behaviour: str) -> None:
    doc = load_vectors(behaviour)
    committed = [{"id": case["id"], "input": case["input"]} for case in doc["cases"]]

    assert (doc["behaviour"], doc["seed"], doc["schema"]) == (behaviour, SEED, SCHEMA)
    assert 0 < len(committed) <= MAX_CASES
    assert len({case["id"] for case in committed}) == len(committed)
    assert committed == json.loads(json.dumps(build_cases(behaviour))), f"{behaviour}: the committed inputs are not what the seeded generator builds"


@pytest.mark.parametrize("behaviour", BEHAVIOURS)
def test_oracle_vectors_cover_the_edge_cases(behaviour: str) -> None:
    cases = load_vectors(behaviour)["cases"]

    missing = [label for label, present in COVERAGE[behaviour].items() if not present(cases)]
    assert not missing, f"{behaviour}: the vectors lack: {missing}"


@pytest.mark.parametrize("attempt", [1, 2, 5, 9, 70])
def test_oracle_backoff_unit_stand_in_agrees_with_the_real_random_stream(attempt: int) -> None:
    # The vectors feed `backoff_s` a unit sample through UnitRng; the real call draws `uniform(-jitter, jitter)` from `random`. Same delay up to rounding.
    for seed in range(25):
        real = backoff_s(attempt, base=0.1, cap=5.0, jitter=0.25, rng=random.Random(seed))
        unit = random.Random(seed).uniform(-1, 1)
        stand_in = backoff_s(attempt, base=0.1, cap=5.0, jitter=0.25, rng=UnitRng(unit))

        assert stand_in == pytest.approx(real, rel=1e-12, abs=1e-15), f"seed {seed}"


# ---- the native leg --------------------------------------------------------------------------------------------------------------

_CHILD = r"""
import json, sys
repo = sys.argv[1]
sys.path.insert(0, repo)
sys.path.insert(0, repo + "/tests/parity")
try:
    import _scheduler_corpus as corpus
except ImportError as exc:
    if "swarm._native" not in str(exc):
        raise
    print(json.dumps({"import_error": str(exc)[:400]}))
else:
    corpus.native_child_main(sys.argv[2])
"""


@pytest.fixture(scope="module")
def native_report() -> dict[str, Any]:
    """One child process runs every native behaviour over the vectors; the extension never loads in the test process."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(REPO))}
    for name in (OVERRIDE_ENV, VECTORS_ENV):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if os.environ.get(OVERRIDE_ENV):  # a stand-in is plain Python over the oracles: it must not need the extension either
        env["SWARM_PURE_PYTHON"] = "1"
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(REPO), str(vectors_dir())], cwd=REPO, env=env, capture_output=True, text=True, timeout=CHILD_TIMEOUT_S)
    assert proc.returncode == 0, f"the native leg's child process failed (exit {proc.returncode}):\n{proc.stderr[-2000:]}"
    report: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    if "import_error" in report:
        pytest.skip(f"swarm._native cannot be imported: {report['import_error']}")
    return report


def test_native_surface_has_every_scheduler_name(native_report: dict[str, Any]) -> None:
    absent = sorted({name for names in native_report["missing"].values() for name in names})

    assert not absent, (
        f"swarm._native imports but lacks {absent}: the scheduler vector surface is built by Phase 14.4 unit S1.4 (NativeVectors) and this extension "
        "predates it. Build the extension from the S1.4 sources; this leg must never be skipped."
    )


@pytest.mark.parametrize("behaviour", NATIVE_BEHAVIOURS)
def test_native_returns_the_committed_vectors(behaviour: str, native_report: dict[str, Any]) -> None:
    absent = native_report["missing"].get(behaviour)
    if absent:
        pytest.fail(f"{behaviour}: swarm._native lacks {absent}; it is built by Phase 14.4 unit S1.4 (NativeVectors), so this extension predates it")
    cases = load_vectors(behaviour)["cases"]
    got = native_report["results"][behaviour]

    assert len(got) == len(cases)
    lines = _mismatches(cases, [native_view(behaviour, case["expected"]) for case in cases], got)
    assert not lines, _report(f"{behaviour}: swarm._native differs from the Python oracle ({NATIVE_NAMES[behaviour]})", lines, len(cases))
