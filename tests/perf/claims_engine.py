#!/usr/bin/env python3
"""CPU cost of the opt-in native claim table (`SWARM_CLAIMS_ENGINE=rust`): the Python `ClaimTable`, the `NativeClaimTable` adapter and the raw
`swarm._native.ClaimTable`, on the same claim lists.

Every figure is the CPU time of the measuring thread (`time.thread_time_ns`): wall time is meaningless on a shared host. A figure is the CPU
spent on one whole input: the minimum over the passes of that input (they run back to back, so a warm Python table has warm overlap caches),
averaged over the inputs. The inputs are seeded claim lists with the mix of `tests/perf/scheduler_engines.py` (module trees, bare files, shared
libs, globs that collide sometimes). Before any timing, the adapter's results and held view are compared with the Python table's for exactly
the timed inputs; a mismatch aborts the run. Needs the extension: run it with an interpreter that has `swarm._native`.

    python3 tests/perf/claims_engine.py [--passes 7] [--inputs 30] [--big-passes 3] > tests/perf/records/14.4/claims-engine.txt

The 500-claim scenario (a scheduling pass over 500 ready tasks that all declare claims) is slow in Python: it runs one input, `--big-passes` times.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.pop("SWARM_PURE_PYTHON", None)
os.environ["SWARM_CLAIMS_ENGINE"] = "rust"

import swarm.claims as C  # noqa: E402
from swarm._native_loader import load_native  # noqa: E402

EXCL, SHARED = C.EXCLUSIVE, C.SHARED
SEED = 20261029


def claim_tuples(rng: random.Random, n: int) -> list[tuple[str, str]]:
    """The claim mix of `tests/perf/scheduler_engines.py::claim_tuples` (span 1.5 n): module trees, bare files, shared libs, test globs."""
    span = max(8, int(n * 1.5))
    claims = []
    for _ in range(n):
        roll, a = rng.random(), rng.randrange(span)
        if roll < 0.5:
            claims.append((f"src/m{a}/**", EXCL))
        elif roll < 0.7:
            claims.append((f"docs/p{a}.md", EXCL))
        elif roll < 0.85:
            claims.append((f"lib/s{a % 5}/", SHARED))
        else:
            claims.append((f"tests/t{a}/*.py", EXCL))
    return claims


def clear_caches() -> None:
    C._overlap_normalized.cache_clear()
    C._segments_overlap.cache_clear()


def one_acquire(table: Any, claims: list[Any]) -> None:
    table.acquire("R", claims)
    table.release("R")


def sequential(table: Any, claims: list[Any]) -> None:
    for i, one in enumerate(claims):
        table.acquire(f"T-{i:04d}", one)


# per scenario kind: the driver, and how a claim list becomes the table's argument (Claim objects, `(pattern, mode)` tuples)
DRIVE: dict[str, Callable[[Any, Any], None]] = {"one": one_acquire, "seq": sequential}
PREPARE: dict[str, tuple[Callable[[list[Any]], Any], Callable[[list[Any]], Any]]] = {
    "one": (lambda claims: claims, lambda claims: [(c.pattern, c.mode) for c in claims]),
    "seq": (lambda claims: [[c] for c in claims], lambda claims: [[(c.pattern, c.mode)] for c in claims]),
}


def cpu(run: Callable[[], None]) -> int:
    start = time.thread_time_ns()
    run()
    return time.thread_time_ns() - start


def measure(inputs: list[Any], make: Callable[[], Any], prep: Callable[[Any], Any], drive: Callable[[Any, Any], None], passes: int, cold: bool) -> float:
    """Milliseconds of thread CPU per input: for each input the minimum over `passes` of the drive time (a fresh table every pass, the argument
    built outside the timing), averaged over the inputs. `cold` clears the overlap caches of the Python table before every pass."""
    total = 0
    for item in inputs:
        arg = prep(item)
        best = None
        for _ in range(passes):
            table = make()
            if cold:
                clear_caches()
            spent = cpu(lambda t=table, a=arg: drive(t, a))
            best = spent if best is None or spent < best else best
        assert best is not None
        total += best
    return total / len(inputs) / 1e6


def check_equal(inputs: list[list[tuple[str, str]]], kind: str) -> None:
    for tuples in inputs:
        adapter, oracle = C.make_claim_table(), C.ClaimTable()
        assert type(adapter) is C.NativeClaimTable, "the extension was not selected"
        if kind == "one":
            claims = [C.Claim(p, m) for p, m in tuples]
            got, want = [adapter.acquire("R", claims)], [oracle.acquire("R", claims)]
        else:
            claims = [C.Claim(p, m) for p, m in tuples]
            got = [adapter.acquire(f"T-{i:04d}", [c]) for i, c in enumerate(claims)]
            want = [oracle.acquire(f"T-{i:04d}", [c]) for i, c in enumerate(claims)]
        assert got == want, "the adapter's results differ from the Python table's"
        assert list(adapter.held().items()) == list(oracle.held().items()), "the held views differ"
        assert adapter._python is None, "the adapter fell back to Python"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--passes", type=int, default=7)
    parser.add_argument("--inputs", type=int, default=30)
    parser.add_argument("--big-passes", type=int, default=3)
    args = parser.parse_args()
    native = load_native()
    print(f"claims engine record: thread CPU time (time.thread_time_ns), minimum over passes; seed {SEED}; python {sys.version.split()[0]}")
    print(f"loadavg at start: {os.getloadavg()}")
    scenarios = (
        ("one acquire of 50 claims, then release (the cost of one task's claims)", 50, "one", args.inputs, args.passes),
        ("50 sequential single-claim acquires into a fresh table (a pass over 50 ready tasks)", 50, "seq", args.inputs, args.passes),
        ("500 sequential single-claim acquires into a fresh table (a pass over 500 ready tasks that all declare claims)", 500, "seq", 1, args.big_passes),
    )
    for title, n, kind, count, passes in scenarios:
        rng = random.Random(f"{SEED}/{n}/{kind}")
        inputs = [claim_tuples(rng, n) for _ in range(count)]
        check_equal(inputs, kind)
        objs = [[C.Claim(p, m) for p, m in tuples] for tuples in inputs]
        drive = DRIVE[kind]
        as_objects, as_tuples = PREPARE[kind]
        rows = (
            ("Python ClaimTable, caches warm (steady state)", C.ClaimTable, as_objects, False),
            ("Python ClaimTable, caches cleared per input (first sight)", C.ClaimTable, as_objects, True),
            ("NativeClaimTable adapter (Claim objects in, ClaimResult out)", C.make_claim_table, as_objects, False),
            ("raw swarm._native.ClaimTable ((pattern, mode) tuples)", native.ClaimTable, as_tuples, False),
        )
        print(f"\n{title}; {count} input(s), {passes} pass(es)")
        figures = []
        for label, make, prep, cold in rows:
            ms = measure(objs, make, prep, drive, passes, cold)
            figures.append(ms)
            print(f"  {label:<66} {ms:10.3f} ms/input")
        warm, first, adapter_ms, raw = figures
        print(f"  adapter vs Python warm: {warm / adapter_ms:6.1f}x faster; vs first sight: {first / adapter_ms:6.1f}x; adapter / raw native: {adapter_ms / raw:4.2f}x")
    print(f"\nloadavg at end: {os.getloadavg()}")


if __name__ == "__main__":
    main()
