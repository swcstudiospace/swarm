#!/usr/bin/env python3
"""Scheduler engine measurements: what Rust could remove from the Python scheduler, in CPU time (Phase 14.4, decision D-P21).

Stdlib only; every figure is the CPU time of the measuring thread (`time.thread_time_ns`, the process CPU time `time.process_time_ns` only
where worker threads do the work), because the hosts this runs on are saturated and wall time is not a figure there. For every input a
measurement is the minimum of several passes; a table row is the median and the p99 over the inputs (with fewer than 100 inputs the p99 is
the largest one). Nothing is timed before parity is proved: the corpus of `tests/parity/_scheduler_corpus.py` is replayed against the
native extension, and every input that is timed is compared between the Python code and `swarm._native` first. A mismatch ends the run.

Sections:

1. primitive latency, Python against `swarm._native` called from Python (the PyO3 call overhead is inside the native figures, which is
   the cost a Python facade would pay): `order_ready`, claim acquisition, the AIMD limiter, `backoff`, the drain plan;
2. the Python decision pass: the real `Scheduler._schedule` with every port stubbed to a trivial body, `select_batch` and `order_ready`;
3. the port bodies that stay in Python in an architecture where Rust owns the decisions and Python owns the effects, on a real SQLite
   Task Store of 50 and 500 tasks: reconcile, dispatchable, the transitions, set_notes, `_complete`;
4. a synthesis (the Rust figures quoted from `crates/gsd-sched-core/examples/loop_bench.rs` are labelled `quoted`; everything else is
   `measured` by this run) and a verdict;
5. the dry-run proxy: the CPU of a 13-task `swarm_run.py --dry-run` and a per-thread profile (`profile`, the pure-Python twin of cProfile) of where
   its scheduler thread spends it.

Run (about an hour on a saturated host; detach it):

    TMPDIR=<work dir> python3 tests/perf/scheduler_engines.py --work <work dir> --loop-bench-log <loop_bench.log>

It reads the repository only; every file it writes is under `--work`.

Paired scheduler runtimes (Phase 14.4, S5.2 and S6; decision D-P21), a separate mode:

    python3 tests/perf/scheduler_engines.py --scheduler-paired --json-out <record.json> [--samples 50] [--warmup 3] [--watcher-log <watch.log>]
    python3 tests/perf/scheduler_engines.py --qualify-json <record.json>

`--scheduler-paired` times `scripts/swarm_run.py` on the path (a) fixture, a 12-task fake-omp run and one-round passes over 50 and 500 ready
tasks, each with GSD_ENGINE=python and GSD_ENGINE=rust in alternating order, and writes the raw record. `--qualify-json` exits 0 only for a
complete quiet candidate whose paired p50 and p95 are both strictly below 1.00 on every required fixture; every other case exits 1 with its
reasons. Qualification is evidence for the S6 record. It is not approval, and nothing here claims a speed-up.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import gc
import hashlib
import importlib.util
import json
import math
import os
import platform
import random
import resource
import runpy
import shutil
import sqlite3
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO / "tests" / "parity" / "_scheduler_corpus.py"
SEED = 20261029
EXCL, SHARED = "exclusive", "shared"
CORR = "C-ENGINES"

# The Rust figures the task statement quotes from the loop bench (stub ports, release build, host load 25 to 55). Never measured here.
QUOTED = {
    "order_ready_50_us": 1.0,
    "order_ready_500_us": 23.8,
    "claims_acquire_50_us": 18.1,
    "claims_sequential_50_us": 1384.0,
    "aimd_ns": "3 to 56",
    "backoff_ns": 2.6,
    "pass_p50_us": "30 to 80",
    "pass_p99_ms": 270.0,
    "loop_cpu_pass_us": "162 to 848",
    "cpu_per_task_us": "146 to 231",
    "thread_per_dispatch_ms_at_32": 2.0,
}


# ---- output -------------------------------------------------------------------------------------------------------------------------

_LINES: list[str] = []


def out(text: str = "") -> None:
    """One line of the report, on the real stdout (the sections redirect the process stdout while they run) and kept for `--save`."""
    _LINES.append(text)
    print(text, file=sys.__stdout__, flush=True)


def loadavg() -> str:
    try:
        return Path("/proc/loadavg").read_text(encoding="utf-8").strip()
    except OSError:
        return "unavailable"


def fmt_ns(ns: float) -> str:
    if ns >= 1e6:
        return f"{ns / 1e6:.2f} ms"
    if ns >= 1e3:
        return f"{ns / 1e3:.2f} us"
    return f"{ns:.0f} ns"


# ---- statistics and timing ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stat:
    median: float
    p99: float
    n: int


def summarize(values: Sequence[float]) -> Stat:
    ordered = sorted(values)
    rank = max(1, math.ceil(0.99 * len(ordered)))
    return Stat(statistics.median(ordered), ordered[rank - 1], len(ordered))


Prep = Callable[[], Callable[[], object]]


def _best(prep: Prep, passes: int, clock: Callable[[], int]) -> float:
    best = math.inf
    for _ in range(passes):
        run = prep()
        t0 = clock()
        run()
        t1 = clock()
        best = min(best, float(t1 - t0))
    return best


def _empty_prep() -> Callable[[], object]:
    def run() -> None:
        return None

    return run


def loop_baseline(items: Sequence[Any]) -> Prep:
    """An empty loop of the same shape as the timed one: `for a in items` for plain items, `for a, b, ... in items` for tuples."""
    first = items[0] if items else None
    arity = len(first) if isinstance(first, tuple) else 1

    def prep() -> Callable[[], object]:
        if arity == 1:
            def run1() -> None:
                for _a in items:
                    pass

            return run1
        if arity == 2:
            def run2() -> None:
                for _a, _b in items:
                    pass

            return run2
        if arity == 3:
            def run3() -> None:
                for _a, _b, _c in items:
                    pass

            return run3
        if arity == 4:
            def run4() -> None:
                for _a, _b, _c, _d in items:
                    pass

            return run4
        if arity == 5:
            def run5() -> None:
                for _a, _b, _c, _d, _e in items:
                    pass

            return run5
        raise ValueError(f"no loop baseline for arity {arity}")

    return prep


@dataclass
class Plan:
    """What to time for one input: `prep()` builds a fresh closure per pass (untimed); `run()` is timed; `inner` operations happen in it."""

    prep: Prep
    inner: int = 1
    baseline: Prep | None = None


def measure(inputs: Sequence[Any], build: Callable[[Any], Plan], passes: int, clock: Callable[[], int] = time.thread_time_ns) -> Stat:
    """Per-operation CPU (ns): per input the minimum over `passes` runs minus the same-shaped empty run, divided by `inner`; then median and p99."""
    values: list[float] = []
    for item in inputs:
        plan = build(item)
        gc.collect()
        raw = _best(plan.prep, passes, clock)
        floor = _best(plan.baseline or _empty_prep_prep, passes, clock)
        values.append(max(0.0, raw - floor) / plan.inner)
    return summarize(values)


def _empty_prep_prep() -> Callable[[], object]:
    return _empty_prep()


# ---- result tables ------------------------------------------------------------------------------------------------------------------


@dataclass
class Row:
    label: str
    stat: Stat
    native: bool = False


@dataclass
class Findings:
    no_gain: list[str] = field(default_factory=list)
    gain: list[str] = field(default_factory=list)
    figures: dict[str, float] = field(default_factory=dict)  # measured medians in ns, keyed for the synthesis


FINDINGS = Findings()


def group(title: str, rows: list[Row], key: str = "", per_task: int = 0) -> None:
    """Prints a group; the first row is the Python baseline. Native rows get the ratio to it (and to the second Python row, if any) and a verdict."""
    python_rows = [r for r in rows if not r.native]
    base = python_rows[0]
    alt = python_rows[1] if len(python_rows) > 1 else None
    out(f"-- {title}   [measured, CPU per operation, {base.stat.n} inputs]")
    head = f"   {'variant':<64}{'p50':>12}{'p99':>12}{'p50 x':>9}{'p99 x':>9}"
    if alt is not None:
        head += f"{'p50 x alt':>11}"
    if per_task:
        head += f"{'p50/task':>12}"
    out(head + "  verdict")
    for row in rows:
        rm = row.stat.median / base.stat.median if base.stat.median else math.nan
        rp = row.stat.p99 / base.stat.p99 if base.stat.p99 else math.nan
        line = f"   {row.label:<64}{fmt_ns(row.stat.median):>12}{fmt_ns(row.stat.p99):>12}{rm:>8.2f}x{rp:>8.2f}x"
        if alt is not None:
            line += f"{(row.stat.median / alt.stat.median if alt.stat.median else math.nan):>10.2f}x"
        if per_task:
            line += f"{fmt_ns(row.stat.median / per_task):>12}"
        verdict = ""
        if row.native:
            if rm < 1.0 and rp < 1.0:
                verdict = "native faster"
                FINDINGS.gain.append(f"{title} / {row.label}: p50 {rm:.2f}x p99 {rp:.2f}x")
            elif rm < 1.0:
                verdict = "MIXED (p50 faster, p99 not)"
                FINDINGS.no_gain.append(f"{title} / {row.label}: p50 {rm:.2f}x p99 {rp:.2f}x (mixed)")
            else:
                verdict = "NO GAIN (native slower)"
                FINDINGS.no_gain.append(f"{title} / {row.label}: p50 {rm:.2f}x p99 {rp:.2f}x")
        out(line + (f"  {verdict}" if verdict else ""))
        if key:
            FINDINGS.figures[f"{key}.{'native' if row.native else 'py'}.{row.label}"] = row.stat.median
    out()


# ---- environment --------------------------------------------------------------------------------------------------------------------


@dataclass
class Env:
    work: Path
    mod: Any  # scripts/swarm_run.py
    native: Any  # swarm._native
    corpus: Any  # tests/parity/_scheduler_corpus.py
    claims: Any  # swarm.claims
    limiter: Any  # swarm.limiter
    retry: Any  # swarm.retry
    store_mod: Any  # swarm.taskstore
    inputs: int
    passes: int
    rng: random.Random = field(default_factory=lambda: random.Random(SEED))


def isolation(work: Path) -> dict[str, str]:
    """The environment that keeps lifecycle-manager side effects (state_store.append_gsd_line, the Claude state file) out of the repository."""
    state = work / "claude-state"
    state.mkdir(parents=True, exist_ok=True)
    return {"SWARM_GSD_STATE_FILE": "", "SWARM_CLAUDE_STATE_DIR": str(state)}


def load_env(work: Path, inputs: int, passes: int) -> Env:
    os.environ.update(isolation(work))  # nothing this script runs may write the repository's .planning/STATE.md or .claude/swarm-state.json
    os.environ["SWARM_DIR"] = str(work / "swarmdir")
    os.environ.pop("SWARM_PURE_PYTHON", None)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("sched_engines_corpus", CORPUS_PATH)
    assert spec is not None and spec.loader is not None
    corpus = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = corpus
    spec.loader.exec_module(corpus)
    import swarm.claims as claims
    import swarm.limiter as limiter
    import swarm.retry as retry
    import swarm.taskstore as store_mod
    from swarm._native_loader import load_native

    return Env(work, corpus.swarm_run_module(), load_native(), corpus, claims, limiter, retry, store_mod, inputs, passes)


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=repr)


def noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def noop_list(*_args: Any, **_kwargs: Any) -> list[str]:
    return []


# ---- parity gate --------------------------------------------------------------------------------------------------------------------


def corpus_gate(env: Env) -> list[str]:
    """Every native behaviour of the parity corpus: the Python oracle against the extension, case by case."""
    c, failures = env.corpus, []
    for behaviour in c.NATIVE_BEHAVIOURS:
        cases = c.build_cases(behaviour)
        bad = []
        for case in cases:
            want = c.native_view(behaviour, c.run_oracle(behaviour, case["input"]))
            got = c.run_native(env.native, behaviour, case["input"])
            if canon(want) != canon(got):
                bad.append(case["id"])
        out(f"   corpus {behaviour:<18} {len(cases):>4} cases  {'PARITY' if not bad else 'MISMATCH ' + str(bad[:5])}")
        failures += [f"corpus {behaviour} {b}" for b in bad]
    return failures


# ---- section 1: primitives ----------------------------------------------------------------------------------------------------------

PRIORITIES = ("P0", "P1", "P2", "P2", "P2", "P3", "P1", None)


def order_rows(rng: random.Random, n: int) -> list[tuple[str | None, int | None, float | None]]:
    rows = []
    for _ in range(n):
        created = None if rng.random() < 0.03 else 1_790_000_000.0 + rng.randrange(0, 4000) / 4.0
        rows.append((rng.choice(PRIORITIES), rng.choice((None, 0, 1, 1, 2, 3, 4)), created))
    return rows


def order_dicts(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    return [{"idx": i, "task_id": f"T-{i:04d}", "priority": p, "dag_depth": d, "created_at": c} for i, (p, d, c) in enumerate(rows)]


def section_order_ready(env: Env, gate: bool) -> list[str]:
    mod, native = env.mod, env.native
    failures: list[str] = []
    for n in (50, 500):
        rng = random.Random(f"{SEED}/order/{n}")
        cases = [order_rows(rng, n) for _ in range(env.inputs)]
        sets = [(rows, order_dicts(rows)) for rows in cases]
        if gate:
            bad = 0
            for rows, dicts in sets:
                want = [t["idx"] for t in mod.order_ready(dicts)]
                got = list(native.sched_order_ready(rows))
                facade = [t["idx"] for t in facade_order(native, dicts)]
                if want != got or want != facade:
                    bad += 1
                    failures.append(f"order_ready n={n}")
            out(f"   order_ready n={n}: {len(sets)} inputs, Python order == native order == facade order: {'PARITY' if not bad else 'MISMATCH'}")
            continue
        inner = max(1, 2000 // n)

        def py_plan(item: tuple[Any, Any], inner: int = inner) -> Plan:
            dicts = item[1]
            order = mod.order_ready

            def prep() -> Callable[[], object]:
                def run() -> None:
                    for _ in range(inner):
                        order(dicts)

                return run

            return Plan(prep, inner, loop_baseline(range(inner)))  # type: ignore[arg-type]

        def native_plan(item: tuple[Any, Any], inner: int = inner) -> Plan:
            rows = item[0]
            fn = native.sched_order_ready

            def prep() -> Callable[[], object]:
                def run() -> None:
                    for _ in range(inner):
                        fn(rows)

                return run

            return Plan(prep, inner, loop_baseline(range(inner)))  # type: ignore[arg-type]

        def facade_plan(item: tuple[Any, Any], inner: int = inner) -> Plan:
            dicts = item[1]

            def prep() -> Callable[[], object]:
                def run() -> None:
                    for _ in range(inner):
                        facade_order(native, dicts)

                return run

            return Plan(prep, inner, loop_baseline(range(inner)))  # type: ignore[arg-type]

        group(
            f"order_ready, {n} rows",
            [
                Row("Python order_ready (scripts/swarm_run.py)", measure(sets, py_plan, env.passes)),
                Row("native sched_order_ready, rows prebuilt (PyO3 call only)", measure(sets, native_plan, env.passes), True),
                Row("native facade: build rows from dicts + call + map back", measure(sets, facade_plan, env.passes), True),
            ],
            key=f"prim.order_ready.{n}",
        )
    return failures


def facade_order(native: Any, ready: list[dict[str, Any]]) -> list[dict[str, Any]]:
    order = native.sched_order_ready([(t.get("priority"), t.get("dag_depth"), t.get("created_at")) for t in ready])
    return [ready[i] for i in order]


def claim_tuples(rng: random.Random, n: int, span: int | None = None) -> list[tuple[str, str]]:
    """n single claims over a pool of `span` module names (default 1.5 n) that collides sometimes: module trees, bare files (footprint of two patterns),
    shared libs, globs."""
    span = max(8, int(n * 1.5)) if span is None else span
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


def clear_claim_caches(env: Env) -> None:
    env.claims._overlap_normalized.cache_clear()
    env.claims._segments_overlap.cache_clear()


def section_claims(env: Env, gate: bool) -> list[str]:
    native, claims_mod, corpus = env.native, env.claims, env.corpus
    failures: list[str] = []
    scenarios = (
        ("50 sequential single-claim acquires into a fresh table", 50, "seq"),
        ("one acquire of 50 claims", 50, "one"),
        ("acquire 50 claims, then release", 50, "rel"),
        ("500 sequential single-claim acquires into a fresh table", 500, "seq"),
    )
    for title, n, kind in scenarios:
        big = n == 500
        count = 2 if big else env.inputs
        passes = 2 if big else env.passes
        rng = random.Random(f"{SEED}/claims/{n}/{kind}")
        inputs = [claim_tuples(rng, n) for _ in range(count)]
        if gate:
            bad = 0
            for tuples in inputs:  # in the corpus format: Python ClaimTable against native.ClaimTable for exactly the timed inputs
                if kind == "seq":
                    ops: list[dict[str, Any]] = [{"op": "acquire", "owner": f"T-{i:04d}", "claims": [list(c)]} for i, c in enumerate(tuples)]
                else:
                    ops = [{"op": "acquire", "owner": "R", "claims": [list(c) for c in tuples]}]
                    if kind == "rel":
                        ops.append({"op": "release", "owner": "R"})
                inp = {"ops": ops}
                if canon(corpus.run_oracle("claim_table", inp)) != canon(corpus.run_native(native, "claim_table", inp)):
                    bad += 1
                    failures.append(f"claim_table {title}")
            out(f"   claims '{title}': {len(inputs)} inputs, Python ClaimTable == native ClaimTable (results and held view): "
                f"{'PARITY' if not bad else 'MISMATCH'}")
            continue

        def make(cold: bool, mode: str, title: str = title, kind: str = kind) -> Callable[[list[tuple[str, str]]], Plan]:
            def build(tuples: list[tuple[str, str]]) -> Plan:
                objs = [[claims_mod.Claim(p, m)] for p, m in tuples]
                flat = [c[0] for c in objs]
                owners = [f"T-{i:04d}" for i in range(len(tuples))]
                pairs = list(zip(owners, objs, strict=True))
                tpairs = list(zip(owners, [[c] for c in tuples], strict=True))

                def prep() -> Callable[[], object]:
                    if cold:
                        clear_claim_caches(env)
                    if mode == "py":
                        table: Any = claims_mod.ClaimTable()
                    else:
                        table = native.ClaimTable()
                    acquire = table.acquire
                    if kind == "seq":
                        if mode == "py":
                            def run_py() -> None:
                                for owner, c in pairs:
                                    acquire(owner, c)

                            return run_py
                        if mode == "native":
                            def run_nat() -> None:
                                for owner, c in tpairs:
                                    acquire(owner, c)

                            return run_nat

                        def run_conv() -> None:
                            for owner, c in pairs:
                                acquire(owner, [(x.pattern, x.mode) for x in c])

                        return run_conv
                    release = table.release
                    if mode == "py":
                        def run_one_py() -> None:
                            acquire("R", flat)
                            if kind == "rel":
                                release("R")

                        return run_one_py
                    if mode == "native":
                        def run_one_nat() -> None:
                            acquire("R", tuples)
                            if kind == "rel":
                                release("R")

                        return run_one_nat

                    def run_one_conv() -> None:
                        acquire("R", [(x.pattern, x.mode) for x in flat])
                        if kind == "rel":
                            release("R")

                    return run_one_conv

                return Plan(prep, 1)

            return build

        warm_label = "Python swarm.claims.ClaimTable (lru caches warm; at 500 the 4096-entry overlap cache thrashes)" if big else \
            "Python swarm.claims.ClaimTable (lru caches warm: steady state)"
        rows = [Row(warm_label, measure(inputs, make(False, "py"), passes))]
        if kind == "seq" and not big:
            rows.append(Row("Python swarm.claims.ClaimTable (lru caches cleared: first sight)", measure(inputs, make(True, "py"), passes)))
        rows.append(Row("native ClaimTable, claims as (pattern, mode) tuples", measure(inputs, make(False, "native"), passes), True))
        rows.append(Row("native ClaimTable, claims converted from Claim objects", measure(inputs, make(False, "conv"), passes), True))
        group(f"claims: {title} (CPU per scenario, not per claim)", rows, key=f"prim.claims.{kind}.{n}")
    return failures


def aimd_inputs() -> list[dict[str, Any]]:
    params = []
    for max_n in (4, 8, 16, 32, 64):
        for lo, cooldown, streak in ((1, 0.0, 3), (1, 10.0, 5), (2, 60.0, 0), (1, 0.0, 0), (2, 10.0, 3), (1, 30.0, 4)):
            params.append({"max_n": max_n, "min_n": lo, "cooldown_s": cooldown, "retry_cap_s": 120.0, "streak_threshold": streak})
    return params


def aimd_ops(kind: str, count: int = 256) -> list[list[Any]]:
    if kind == "success":
        return [["rate_limit", 0.0, None]] + [["success", 1.0 + 0.25 * i] for i in range(count - 1)]
    if kind == "rate_limit":
        return [["rate_limit", 0.25 * i, None] for i in range(count)]
    return [["failure", 0.25 * i, f"T-{i % 40:04d}"] for i in range(count)]


def py_scheduler_stub(env: Env, limiter: Any, params: dict[str, Any]) -> Any:
    """A Scheduler built without its constructor, as the corpus builds one for `_backpressure`."""
    sched = object.__new__(env.mod.Scheduler)
    sched.limiter, sched.log, sched.cancel = limiter, [], SimpleNamespace(event=threading.Event())
    sched.tel, sched.mgr = SimpleNamespace(degraded=noop, clear_degraded=noop), SimpleNamespace(max_concurrent=params["max_n"])
    sched.fail_streak, sched._streak, sched._streak_tripped, sched._last_decrease = int(params["streak_threshold"]), set(), False, 0.0
    return sched


def section_aimd(env: Env, gate: bool) -> list[str]:
    native, corpus = env.native, env.corpus
    failures: list[str] = []
    mono = time.monotonic
    plist = aimd_inputs()
    if gate:
        for kind in ("success", "rate_limit", "failure"):
            bad = 0
            for params in plist:
                inp = {"params": params, "ops": aimd_ops(kind)}
                if canon(corpus.run_oracle("aimd", inp)) != canon(corpus.run_native(native, "aimd", inp)):
                    bad += 1
                    failures.append(f"aimd {kind}")
            out(f"   aimd '{kind}' x {len(plist)} parameter sets x 256 ops, fake clock: Python scheduler/limiter == native, every step: "
                f"{'PARITY' if not bad else 'MISMATCH'}")
        return failures
    for kind in ("success", "rate_limit", "failure"):

        def plan_bare(params: dict[str, Any], kind: str = kind) -> Plan:
            items: list[Any] = [()] * 255 if kind == "success" else [()] * 256

            def prep() -> Callable[[], object]:
                lim = env.limiter.AimdLimiter(params["max_n"], min_n=params["min_n"], cooldown_s=params["cooldown_s"])
                if kind == "success":
                    lim.on_rate_limit()
                    fn = lim.on_success
                else:
                    fn = lim.on_rate_limit

                def run() -> None:
                    for _ in items:
                        fn()

                return run

            return Plan(prep, len(items), loop_baseline([1] * len(items)))

        def plan_backpressure(params: dict[str, Any], kind: str = kind) -> Plan:
            n_ops = 255 if kind == "success" else 256
            items = [(f"T-{i % 40:04d}",) for i in range(n_ops)]

            def prep() -> Callable[[], object]:
                lim = env.limiter.AimdLimiter(params["max_n"], min_n=params["min_n"], cooldown_s=params["cooldown_s"])
                sched = py_scheduler_stub(env, lim, params)
                if kind == "success":
                    lim.on_rate_limit()
                bp = sched._backpressure
                ok_run = SimpleNamespace(rate_limited=None, submitted_at=0.0)
                hit_run = SimpleNamespace(rate_limited=0.0, submitted_at=1e18)

                if kind == "success":
                    def run_ok() -> None:
                        for (tid,) in items:
                            bp(ok_run, False, {"task_id": tid}, "")

                    return run_ok
                if kind == "rate_limit":
                    def run_hit() -> None:
                        for (tid,) in items:
                            bp(hit_run, True, {"task_id": tid}, "rate limited")

                    return run_hit

                def run_fail() -> None:
                    for (tid,) in items:
                        bp(ok_run, True, {"task_id": tid}, "failed")

                return run_fail

            return Plan(prep, len(items), loop_baseline(items))

        def plan_native(params: dict[str, Any], kind: str = kind) -> Plan:
            n_ops = 255 if kind == "success" else 256
            items = [(f"T-{i % 40:04d}",) for i in range(n_ops)]

            def prep() -> Callable[[], object]:
                lim = native.AimdLimiter(params["max_n"], params["min_n"], params["cooldown_s"], params["retry_cap_s"], params["streak_threshold"])
                if kind == "success":
                    lim.on_rate_limit(mono(), None)
                    fn_s = lim.on_success

                    def run_s() -> None:
                        for _ in items:
                            fn_s(mono())

                    return run_s
                if kind == "rate_limit":
                    fn_r = lim.on_rate_limit

                    def run_r() -> None:
                        for _ in items:
                            fn_r(mono(), None)

                    return run_r
                fn_f = lim.on_failure

                def run_f() -> None:
                    for (tid,) in items:
                        fn_f(mono(), tid)

                return run_f

            return Plan(prep, len(items), loop_baseline(items))

        rows = []
        if kind != "failure":
            rows.append(Row(f"Python AimdLimiter.on_{kind}() (bare limiter op)", measure(plist, plan_bare, env.passes)))
        rows.append(Row(f"Python Scheduler._backpressure ({kind}: limiter + streak + log)", measure(plist, plan_backpressure, env.passes)))
        rows.append(Row(f"native AimdLimiter.on_{kind} (+ time.monotonic() per call)", measure(plist, plan_native, env.passes), True))
        group(f"AIMD {kind}", rows, key=f"prim.aimd.{kind}")
    # the PyO3 floor: a getter that does no work
    lim = native.AimdLimiter(8, 1, 1.0, 120.0, 3)
    getter = lim.current

    def plan_floor(_: Any) -> Plan:
        items = [()] * 1000

        def prep() -> Callable[[], object]:
            def run() -> None:
                for _ in items:
                    getter()

            return run

        return Plan(prep, 1000, loop_baseline([1] * 1000))

    floor = measure([0] * 5, plan_floor, env.passes)
    out(f"   [measured] PyO3 call floor, native AimdLimiter.current() from Python: p50 {fmt_ns(floor.median)} per call")
    FINDINGS.figures["prim.pyo3_floor"] = floor.median
    out()
    return failures


def section_backoff(env: Env, gate: bool) -> list[str]:
    from swarm.limiter import env_seconds

    native, corpus, retry = env.native, env.corpus, env.retry
    failures: list[str] = []
    rng = random.Random(f"{SEED}/backoff")
    cases = []
    for _ in range(env.inputs):
        attempts = [1, 2, 3, 4, 5, 6, 7, 8] * 25
        rng.shuffle(attempts)
        cases.append({"base": rng.choice((0.05, 0.1, 0.25)), "cap": rng.choice((1.0, 5.0, 30.0)), "jitter": rng.choice((0.1, 0.25, 0.5, 1.0)),
                      "attempts": attempts, "seed": rng.randrange(1 << 30)})
    if gate:
        bad = 0
        for case in cases:
            for attempt in range(1, 9):
                inp = {"attempt": attempt, "base_s": case["base"], "cap_s": case["cap"], "jitter": case["jitter"], "unit": rng.uniform(-1, 1)}
                if canon(corpus.run_oracle("backoff", inp)) != canon(corpus.run_native(native, "backoff", inp)):
                    bad += 1
                    failures.append("backoff")
        out(f"   backoff: {len(cases)} parameter sets x 8 attempts, Python backoff_s (injected sample) == sched_backoff_s: "
            f"{'PARITY' if not bad else 'MISMATCH'}")
        return failures

    def plan_py(case: dict[str, Any]) -> Plan:
        attempts, base, cap, jitter = case["attempts"], case["base"], case["cap"], case["jitter"]

        def prep() -> Callable[[], object]:
            generator = random.Random(case["seed"])
            fn = retry.backoff_s

            def run() -> None:
                for a in attempts:
                    fn(a, base=base, cap=cap, jitter=jitter, rng=generator)

            return run

        return Plan(prep, len(attempts), loop_baseline(attempts))

    def plan_native(case: dict[str, Any]) -> Plan:
        attempts, base, cap, jitter = case["attempts"], case["base"], case["cap"], case["jitter"]

        def prep() -> Callable[[], object]:
            uniform = random.Random(case["seed"]).uniform
            fn = native.sched_backoff_s

            def run() -> None:
                for a in attempts:
                    fn(a, base, cap, jitter, uniform(-1.0, 1.0))

            return run

        return Plan(prep, len(attempts), loop_baseline(attempts))

    def plan_prod_py(case: dict[str, Any]) -> Plan:
        attempts = case["attempts"]

        def prep() -> Callable[[], object]:
            fn = retry.backoff_s

            def run() -> None:
                for a in attempts:
                    fn(a)

            return run

        return Plan(prep, len(attempts), loop_baseline(attempts))

    def plan_prod_native(case: dict[str, Any]) -> Plan:
        attempts = case["attempts"]

        def prep() -> Callable[[], object]:
            uniform = random.Random(case["seed"]).uniform
            fn, env_s = native.sched_backoff_s, env_seconds

            def run() -> None:
                for a in attempts:
                    fn(a, env_s("SWARM_RETRY_BASE_S", 0.1), env_s("SWARM_RETRY_CAP_S", 5.0), 0.25, uniform(-1.0, 1.0))

            return run

        return Plan(prep, len(attempts), loop_baseline(attempts))

    group(
        "backoff, explicit base/cap/jitter",
        [Row("Python swarm.retry.backoff_s", measure(cases, plan_py, env.passes)),
         Row("native sched_backoff_s (+ rng.uniform sample per call)", measure(cases, plan_native, env.passes), True)],
        key="prim.backoff",
    )
    group(
        "backoff, the production call backoff_s(attempt) (env defaults, module random)",
        [Row("Python backoff_s(attempt)", measure(cases, plan_prod_py, env.passes)),
         Row("native facade: two env lookups + sample + sched_backoff_s", measure(cases, plan_prod_native, env.passes), True)],
        key="prim.backoff_prod",
    )
    return failures


def section_drain(env: Env, gate: bool) -> list[str]:
    native, corpus = env.native, env.corpus
    failures: list[str] = []
    cases = [c["input"] for c in corpus.build_cases("drain_plan") if c["id"].startswith("rand-")][: env.inputs]
    if gate:
        bad = 0
        for inp in cases:
            want = corpus.run_oracle("drain_plan", inp)  # the real Scheduler._cancel_drain with a fake clock
            got = drain_python(inp)
            if want != {"grace": got[0], "kill_grace": got[1]} or canon(corpus.run_native(native, "drain_plan", inp)) != canon(want):
                bad += 1
                failures.append("drain_plan")
        out(f"   drain plan: {len(cases)} inputs, inline arithmetic == the real Scheduler._cancel_drain == sched_drain_plan: "
            f"{'PARITY' if not bad else 'MISMATCH'}")
        return failures

    def plan_py(inp: dict[str, Any]) -> Plan:
        items = [(inp["cancel_grace_s"], inp["shutdown_budget_s"], inp["kill_grace_max_s"], inp["stop_grace_s"], inp["kill_settle_s"])] * 100

        def prep() -> Callable[[], object]:
            def run() -> None:
                for cancel, budget, kill_max, stop, settle in items:
                    kill_grace = max(0.5, min(kill_max, float(stop), budget - settle))
                    max(0.0, min(cancel, budget - kill_grace - settle))

            return run

        return Plan(prep, len(items), loop_baseline(items))

    def plan_native(inp: dict[str, Any]) -> Plan:
        items = [(inp["cancel_grace_s"], inp["shutdown_budget_s"], inp["kill_grace_max_s"], inp["stop_grace_s"], inp["kill_settle_s"])] * 100

        def prep() -> Callable[[], object]:
            fn = native.sched_drain_plan

            def run() -> None:
                for cancel, budget, kill_max, stop, settle in items:
                    fn(cancel, budget, kill_max, stop, settle)

            return run

        return Plan(prep, len(items), loop_baseline(items))

    group(
        "drain plan (once per cancelled run, never on the dispatch path)",
        [Row("Python: the two max/min lines of Scheduler._cancel_drain, inline", measure(cases, plan_py, env.passes)),
         Row("native sched_drain_plan", measure(cases, plan_native, env.passes), True)],
        key="prim.drain",
    )
    return failures


def drain_python(inp: dict[str, Any]) -> tuple[float, float]:
    kill_grace = max(0.5, min(inp["kill_grace_max_s"], float(inp["stop_grace_s"]), inp["shutdown_budget_s"] - inp["kill_settle_s"]))
    grace = max(0.0, min(inp["cancel_grace_s"], inp["shutdown_budget_s"] - kill_grace - inp["kill_settle_s"]))
    return grace, kill_grace


# ---- section 2: the Python decision pass --------------------------------------------------------------------------------------------


@dataclass
class DecisionInput:
    ready: list[dict[str, Any]]
    waiting: list[dict[str, Any]]
    internal: dict[str, int]
    claims: dict[str, list[tuple[str, str]]]
    n: int


def decision_input(env: Env, rng: random.Random, n: int, claim_fraction: float) -> DecisionInput:
    now = time.time()
    ready: list[dict[str, Any]] = []
    internal: dict[str, int] = {}
    claim_map: dict[str, list[tuple[str, str]]] = {}
    for i in range(n):
        tid = f"T-{i:04d}"
        tuples = claim_tuples(rng, 1 + (rng.random() < 0.3), span=max(8, int(n * 1.5))) if rng.random() < claim_fraction else []
        claim_map[tid] = tuples
        notes: dict[str, Any] = {"claims": [env.claims.Claim(p, m) for p, m in tuples]}
        roll = rng.random()
        if roll < 0.05:
            notes["retry_after"] = now + 600.0
        elif roll < 0.07:
            internal[tid] = env.mod.INTERNAL_FAILURE_LIMIT
        ready.append({"task_id": tid, "state": "PLANNED", "agent_id": f"A{5 + i % 5:02d}", "capability": "code.backend", "priority": rng.choice(PRIORITIES),
                      "dag_depth": rng.choice((0, 0, 1, 2, 3)), "created_at": 1_790_000_000.0 + i * 0.25, "attempt": 0, "rework_loops": 0,
                      "depends_on": [], "notes_json": notes, "budget": {"max_wall_s": 1800}})
    waiting = [{"task_id": f"W-{i:04d}", "state": "CREATED", "agent_id": "A05", "capability": "code.backend", "priority": "P2", "dag_depth": 4,
                "created_at": 1_790_000_100.0 + i, "attempt": 0, "rework_loops": 0, "depends_on": ["T-0000"], "notes_json": {}, "budget": {}}
               for i in range(n // 4)]
    return DecisionInput(ready, waiting, internal, claim_map, n)


def make_scheduler(env: Env, inp: DecisionInput, cap: int, ceiling: int, real_book: bool, picked: list[str]) -> Any:
    mod = env.mod
    tel = SimpleNamespace(run_queued=noop, transition=noop, scheduler_tick=noop, degraded=noop, clear_degraded=noop, finish=noop)
    sched = object.__new__(mod.Scheduler)
    everything = inp.ready + inp.waiting
    sched.rounds, sched.corr, sched.ctx, sched.log = 0, CORR, SimpleNamespace(emit=noop), []
    sched.store = SimpleNamespace(list=lambda correlation_id=None, **_k: everything)
    sched.leases, sched.handoffs, sched.inflight, sched.barrier, sched.budget, sched.dispatched = SimpleNamespace(acquire=lambda t, a: LEASE_OK), None, {}, False, 10**9, 0
    sched.mgr, sched.limiter, sched.claims = SimpleNamespace(max_concurrent=cap), env.limiter.AimdLimiter(cap), env.claims.ClaimTable()
    sched.error_handler, sched.tel, sched.repo = mod.ErrorHandler(), tel, Path(".")
    sched.book = mod.RunBook(tel) if real_book else SimpleNamespace(seen=noop, count=lambda _s: 0, opened=noop, done=noop)
    sched._internal, sched._wake_wall = dict(inp.internal), None
    sched._submit = lambda task, agent: picked.append(task["task_id"])
    sched._tick = noop
    return sched


LEASE_OK = SimpleNamespace(dispatch=True, holder=None, status="granted")


def section_decision(env: Env, gate: bool) -> list[str]:
    mod = env.mod
    failures: list[str] = []
    holder: dict[str, Any] = {"ready": []}
    agents_by_ceiling: dict[int, dict[str, dict[str, Any]]] = {}

    def agents(ceiling: int) -> dict[str, dict[str, Any]]:
        if ceiling not in agents_by_ceiling:
            agents_by_ceiling[ceiling] = {f"A{k:02d}": {"id": f"A{k:02d}", "slug": f"a{k:02d}", "max_parallel": ceiling} for k in range(5, 10)}
        return agents_by_ceiling[ceiling]

    current_ceiling = {"c": 1}
    patches = {
        "reconcile": noop_list,
        "log_phase": noop,
        "dispatchable": lambda *_a, **_k: holder["ready"],
        "resolve_agent": lambda task: agents(current_ceiling["c"])[task["agent_id"]],
        "parse_claims": lambda notes: notes["claims"],
    }
    with env.corpus._patched(mod, **patches):
        for n in (50, 500):
            for label, fraction, cap_arg, ceiling_arg, tag in DECISION_CONFIGS:
                cap, ceiling = (n if cap_arg is None else cap_arg), (n if ceiling_arg is None else ceiling_arg)
                stress = n == 500 and fraction == 1.0  # the Python claim scan is quadratic (seconds a pass at 500): fewer inputs and passes
                count = 3 if stress else (env.inputs if n == 50 else max(8, env.inputs // 2))
                passes = 2 if stress else env.passes
                rng = random.Random(f"{SEED}/decision/{n}/{tag}")
                inputs = [decision_input(env, rng, n, fraction) for _ in range(count)]
                current_ceiling["c"] = ceiling
                if gate:  # integrated parity: the batch Python selects equals the one the native primitives select, in the same order
                    bad, checked = 0, inputs[: 2 if stress else 5]
                    for inp in checked:
                        holder["ready"] = inp.ready
                        picked: list[str] = []
                        sched = make_scheduler(env, inp, cap, ceiling, False, picked)
                        sched._schedule()
                        if picked != native_selection(env, inp, cap, ceiling):
                            bad += 1
                            failures.append(f"decision n={n} {tag}")
                    out(f"   decision n={n} ({label}): Python _schedule batch == batch of sched_order_ready + native ClaimTable "
                        f"({len(checked)} inputs, {len(checked[0].ready)} ready): {'PARITY' if not bad else 'MISMATCH'}")
                    continue

                def schedule_plan(inp: DecisionInput, cold: bool, real_book: bool, cap: int = cap, ceiling: int = ceiling) -> Plan:
                    def prep() -> Callable[[], object]:
                        current_ceiling["c"] = ceiling
                        holder["ready"] = inp.ready
                        if cold:
                            clear_claim_caches(env)
                        sched = make_scheduler(env, inp, cap, ceiling, real_book, [])
                        return sched._schedule

                    return Plan(prep)

                def select_plan(inp: DecisionInput, cap: int = cap, ceiling: int = ceiling) -> Plan:
                    def prep() -> Callable[[], object]:
                        current_ceiling["c"] = ceiling
                        table, skipped = env.claims.ClaimTable(), {}
                        ordered = mod.order_ready(inp.ready)

                        def run() -> None:
                            mod.select_batch(SimpleNamespace(transition=noop), ordered, cap, SimpleNamespace(acquire=lambda t, a: LEASE_OK),
                                             running={}, claims=table, skipped=skipped)

                        return run

                    return Plan(prep)

                def order_plan(inp: DecisionInput) -> Plan:
                    ready = inp.ready

                    def prep() -> Callable[[], object]:
                        def run() -> None:
                            mod.order_ready(ready)

                        return run

                    return Plan(prep)

                def native_plan(inp: DecisionInput, cap: int = cap, ceiling: int = ceiling) -> Plan:
                    def prep() -> Callable[[], object]:
                        def run() -> None:
                            native_selection(env, inp, cap, ceiling)

                        return run

                    return Plan(prep)

                rows: list[tuple[str, str, Stat, bool]] = [
                    ("sched_book", "Scheduler._schedule, ports stubbed, RunBook real (claim caches warm)",
                     measure(inputs, lambda i: schedule_plan(i, False, True), passes), False),
                ]
                if not stress:
                    rows.append(("sched_cold", "Scheduler._schedule, ports stubbed, RunBook real (claim caches cleared)",
                                 measure(inputs, lambda i: schedule_plan(i, True, True), passes), False))
                rows += [
                    ("sched_nobook", "Scheduler._schedule, ports stubbed, RunBook no-op: the decision part",
                     measure(inputs, lambda i: schedule_plan(i, False, False), passes), False),
                    ("select", "select_batch alone, real ClaimTable", measure(inputs, select_plan, passes), False),
                    ("order", "order_ready alone", measure(inputs, order_plan, passes), False),
                    ("native", "native replay of the decision: sched_order_ready + filters + native ClaimTable (via PyO3)",
                     measure(inputs, native_plan, passes), True),
                ]
                base = next(r[2] for r in rows if r[0] == "sched_nobook")
                out(f"-- decision pass, {n} ready tasks, {label}   [measured, CPU per pass; {len(inputs)} inputs, min of {passes} passes]")
                out(f"   {'variant':<92}{'p50':>12}{'p99':>12}{'p50/task':>12}{'p50 x':>9}{'p99 x':>9}")
                for key, text, stat, is_native in rows:
                    line = f"   {text:<92}{fmt_ns(stat.median):>12}{fmt_ns(stat.p99):>12}{fmt_ns(stat.median / n):>12}"
                    if is_native:
                        rm, rp = stat.median / base.median, stat.p99 / base.p99
                        verdict = "native faster" if rm < 1.0 and rp < 1.0 else "NO GAIN"
                        line += f"{rm:>8.2f}x{rp:>8.2f}x  {verdict} (vs the decision part)"
                        (FINDINGS.gain if verdict == "native faster" else FINDINGS.no_gain).append(
                            f"decision pass {n} ready, {label}: native replay p50 {rm:.2f}x p99 {rp:.2f}x of the Python decision part")
                    out(line)
                    FINDINGS.figures[f"dec.{n}.{tag}.{key}"] = stat.median
                info = env.claims._overlap_normalized.cache_info()
                out(f"   [measured] overlap cache after these passes: size {info.currsize} of {info.maxsize}, hits {info.hits}, misses {info.misses}")
                out()
    return failures


DECISION_CONFIGS = (
    ("every task declares claims, cap = ceiling = n (stress: every task examined)", 1.0, None, None, "all"),
    ("1 task in 10 declares claims, cap = ceiling = n", 0.1, None, None, "tenth"),
    ("1 task in 10 declares claims, cap 8, class ceiling 4: slots run out early", 0.1, 8, 4, "cap8"),
)


def native_selection(env: Env, inp: DecisionInput, cap: int, ceiling: int) -> list[str]:
    """The batch of `_schedule` computed with the native primitives: order, the retry and internal-failure filters, then slots, class ceilings and claims."""
    now = time.time()
    ready = inp.ready
    order = env.native.sched_order_ready([(t["priority"], t["dag_depth"], t["created_at"]) for t in ready])
    limit = env.mod.INTERNAL_FAILURE_LIMIT
    eligible = []
    for i in order:
        t = ready[i]
        until = t["notes_json"].get("retry_after")
        if isinstance(until, (int, float)) and until > now:
            continue
        if inp.internal.get(t["task_id"], 0) >= limit:
            continue
        eligible.append(t)
    table, batch, slots, replicas = env.native.ClaimTable(), [], cap, {}
    for t in eligible:
        if slots <= 0:
            break
        tid, aid = t["task_id"], t["agent_id"]
        if replicas.get(aid, 0) >= ceiling:
            continue
        if not table.acquire(tid, inp.claims[tid])[0]:
            continue
        replicas[aid] = replicas.get(aid, 0) + 1
        batch.append(tid)
        slots -= 1
    return batch


# ---- section 3: the port bodies on a real Task Store ---------------------------------------------------------------------------------


def build_store(env: Env, path: Path, n: int, seed: int) -> None:
    """n tasks in blocks of 20: a DONE root, eight ready tasks, four blocked ones, two sessions in flight, two review targets each with a live
    gate task (a running session), and a FAILED task (out of attempts in every second block)."""
    S = env.store_mod.TaskState
    rng = random.Random(f"{SEED}/store/{n}/{seed}")
    store = env.store_mod.TaskStore(path)
    with store.transaction():
        for i in range(n):
            tid, slot, root = f"T-{i:04d}", i % 20, f"T-{(i // 20) * 20:04d}"
            deps: list[str] = [] if slot == 0 else [root]
            notes: dict[str, Any] = {}
            if 9 <= slot <= 12:
                deps = [f"T-{i - 8:04d}"]
            if slot in (13, 14, 15, 17):
                notes = {"running": f"sid-{i}"}
            if slot in (15, 17):
                notes["gate"], notes["gate_for"] = "review", [f"T-{i + 1:04d}"]
            store.create(task_id=tid, correlation_id=CORR, capability="code.backend", title=f"engines bench {i}", dag_depth=i // 20,
                         priority=rng.choice(("P0", "P1", "P2", "P2", "P3")), agent_id=f"A{5 + i % 5:02d}", depends_on=deps, notes=notes)
            if slot == 0:
                store.update(tid, state=S.DONE.value)
            elif 1 <= slot <= 12:
                store.update(tid, state=S.PLANNED.value)
            elif slot in (13, 14, 15, 17):
                store.update(tid, state=S.IN_PROGRESS.value, attempt=1)
            elif slot in (16, 18):
                store.update(tid, state=S.IN_REVIEW.value, attempt=1)
            else:
                exhausted = (i // 20) % 2 == 1
                store.update(tid, state=S.FAILED.value, attempt=3 if exhausted else 1)
    store.conn.close()


def open_copy(env: Env, base: Path, dest_dir: Path) -> Any:
    shutil.rmtree(dest_dir, ignore_errors=True)
    dest_dir.mkdir(parents=True)
    shutil.copyfile(base, dest_dir / "tasks.db")
    return env.store_mod.TaskStore(dest_dir / "tasks.db")


def section_ports(env: Env) -> list[str]:
    mod, S = env.mod, env.store_mod.TaskState
    stores = env.work / "stores"
    shutil.rmtree(stores, ignore_errors=True)
    stores.mkdir(parents=True)
    cheap = env.inputs < 30 or env.passes < 7  # a quick run keeps the small sizes
    devnull = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - closed at the end of the section
    with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
        for n in (50, 500):
            count, passes, reps = ((3, 2, 1) if cheap else (8, 5, 3)) if n == 50 else ((2, 2, 1) if cheap else (4, 3, 1))
            bases = []
            for k in range(count):
                base = stores / f"base_{n}_{k}.db"
                build_store(env, base, n, k)
                bases.append((k, base))
            probe = open_copy(env, bases[0][1], stores / "probe")
            ready_now = len(probe.ready(CORR))
            states: dict[str, int] = {}
            for t in probe.list(correlation_id=CORR):
                states[t["state"]] = states.get(t["state"], 0) + 1
            probe.conn.close()
            out(f"-- port bodies on a real SQLite Task Store, {n} tasks ({ready_now} ready at the start; states {states}); "
                f"{count} stores, min of {passes} passes   [measured, CPU]", )
            inflight_of = lambda store: frozenset(t["task_id"] for t in store.list(correlation_id=CORR, state=S.IN_PROGRESS.value)  # noqa: E731
                                                  if t["notes_json"].get("running") is not None)

            settled_cache: dict[int, tuple[Any, frozenset[str]]] = {}

            def settled(item: tuple[int, Path]) -> tuple[Any, frozenset[str]]:
                if item[0] not in settled_cache:  # one settled store per input, shared by the read-only steady ops
                    store = open_copy(env, item[1], stores / f"settled_{n}_{item[0]}")
                    mod.reconcile(store, CORR, noop)
                    mod.dispatchable(store, CORR, noop)
                    mod.dispatchable(store, CORR, noop)
                    settled_cache[item[0]] = (store, inflight_of(store))
                return settled_cache[item[0]]

            def steady_plan(item: tuple[int, Path], op: str) -> Plan:
                store, inflight = settled(item)
                reps_n = reps

                def prep() -> Callable[[], object]:
                    def run() -> None:
                        for _ in range(reps_n):
                            if op == "reconcile":
                                mod.reconcile(store, CORR, noop, leases=None, handoffs=None)
                            elif op == "dispatchable":
                                mod.dispatchable(store, CORR, noop, handoffs=None, skip=inflight)
                            elif op == "list":
                                store.list(correlation_id=CORR)
                            elif op == "log_phase":
                                mod.log_phase("reconcile", {"round": 1}, source="A01@swarm_run", correlation_id=CORR, root=env.work)
                            else:
                                mod.reconcile(store, CORR, noop, leases=None, handoffs=None)
                                mod.log_phase("reconcile", {"round": 1}, source="A01@swarm_run", correlation_id=CORR, root=env.work)
                                mod.dispatchable(store, CORR, noop, handoffs=None, skip=inflight)
                                store.list(correlation_id=CORR)

                    return run

                return Plan(prep, reps_n)

            rows = []
            steady_ops = [("reconcile", "reconcile (IN_REVIEW targets with a live gate task)"), ("dispatchable", "dispatchable (ready, rework, FAILED scan)"),
                          ("list", "store.list(correlation_id) (the telemetry loop's read)"), ("log_phase", "log_phase('reconcile') -> events.jsonl")]
            if n == 50:
                steady_ops.append(("all", "begin_pass body measured together: reconcile + log_phase + dispatchable + list"))
            for op, label in steady_ops:
                stat = measure(bases, lambda item, op=op: steady_plan(item, op), passes)  # noqa: B023
                rows.append(Row(label, stat))
                FINDINGS.figures[f"port.{n}.{op}"] = stat.median
            out(f"   {'port body (steady state, a store already settled by one pass)':<80}{'p50/call':>12}{'p99/call':>12}{'p50/task':>12}")
            for row in rows:
                out(f"   {row.label:<80}{fmt_ns(row.stat.median):>12}{fmt_ns(row.stat.p99):>12}{fmt_ns(row.stat.median / n):>12}")

            def mutate_plan(item: tuple[int, Path], op: str) -> Plan:
                k_tasks = 10
                dest = stores / f"mut_{op}_{n}_{item[0]}"

                def prep() -> Callable[[], object]:
                    store = open_copy(env, item[1], dest)
                    if op == "transition":
                        todo = [t["task_id"] for t in store.ready(CORR)][:k_tasks]

                        def run_tr() -> None:
                            for tid in todo:
                                store.transition(tid, S.CLAIMED, reason="awarded to A05")
                                store.transition(tid, S.IN_PROGRESS, reason="lease started")

                        return run_tr
                    if op == "set_notes":
                        todo = [t["task_id"] for t in store.ready(CORR)][:k_tasks]

                        def run_sn() -> None:
                            for tid in todo:
                                store.set_notes(tid, retry_after=time.time() + 1.5)

                        return run_sn
                    running = [t for t in store.list(correlation_id=CORR, state=S.IN_PROGRESS.value) if t["notes_json"].get("running")][:k_tasks]
                    sched = complete_stub(env, store)
                    done_state, error = ("IN_REVIEW", None) if op == "complete_ok" else ("FAILED", "E-TIMEOUT: engines bench")
                    futures = []
                    for t in running:
                        future: Future = Future()
                        future.set_result(mod.Done(t["task_id"], t["agent_id"], done_state, error))
                        run_handle = SimpleNamespace(run_id=f"{t['task_id']}.a1", rate_limited=None, submitted_at=0.0, started=noop, slot_acquired=noop)
                        sched.inflight[t["task_id"]] = mod.Inflight(t, {"id": t["agent_id"], "slug": "a"}, f"sid-{t['task_id']}", run_handle, future)
                        sched.book.opened(run_handle.run_id)
                        futures.append(future)

                    def run_cp() -> None:
                        for future in futures:
                            sched._complete(future)

                    return run_cp

                running_total = sum(1 for i in range(n) if i % 20 in (13, 14, 15, 17))
                inner = 2 * k_tasks if op == "transition" else (k_tasks if op == "set_notes" else min(k_tasks, running_total))
                return Plan(prep, inner)

            rows = []
            for op, label in (("transition", "store.transition CLAIMED then IN_PROGRESS (per transition; live.json refresh included)"),
                              ("set_notes", "store.set_notes(retry_after=...) (the retry write)"),
                              ("complete_ok", "Scheduler._complete, success, real events, stub run"),
                              ("complete_failed", "Scheduler._complete, transient failure (+ _schedule_retry write)")):
                stat = measure(bases, lambda item, op=op: mutate_plan(item, op), passes)  # noqa: B023
                rows.append(Row(label, stat))
                FINDINGS.figures[f"port.{n}.{op}"] = stat.median
            for row in rows:
                out(f"   {row.label:<80}{fmt_ns(row.stat.median):>12}{fmt_ns(row.stat.p99):>12}")
            out()
    devnull.close()
    return []


def complete_stub(env: Env, store: Any) -> Any:
    mod = env.mod
    sched = object.__new__(mod.Scheduler)
    tel = SimpleNamespace(run_queued=noop, transition=noop, scheduler_tick=noop, degraded=noop, clear_degraded=noop, finish=noop)
    sched.inflight, sched.store, sched.claims, sched.log, sched.rounds, sched.corr = {}, store, env.claims.ClaimTable(), [], 1, CORR
    sched.mgr, sched.cancel = SimpleNamespace(release=noop, max_concurrent=8), SimpleNamespace(event=threading.Event(), reason=None)
    sched.book, sched.tel, sched.ctx, sched.args = mod.RunBook(tel), tel, SimpleNamespace(root=env.work, emit=noop), SimpleNamespace(dry_run=False)
    sched.limiter, sched.fail_streak, sched._streak, sched._streak_tripped, sched._last_decrease = env.limiter.AimdLimiter(8), 3, set(), False, 0.0
    sched.extension_down, sched.closed = False, False
    return sched


# ---- section 4: pool, GIL attach, synthesis ------------------------------------------------------------------------------------------


def section_pool(env: Env) -> None:
    mod = env.mod
    jobs = 64
    for workers in (8, 32):
        reps = list(range(max(4, env.inputs // 2)))

        def plan(_: int, workers: int = workers, warm: bool = False) -> Plan:
            def prep() -> Callable[[], object]:
                pool = mod.WorkerPool(workers, "engines")
                if warm:
                    for f in [pool.submit(noop) for _ in range(workers)]:
                        f.result()

                def run() -> None:
                    for f in [pool.submit(noop) for _ in range(jobs)]:
                        f.result()
                    pool.shutdown()

                return run

            return Plan(prep, jobs)

        cold = measure(reps, plan, 3, clock=time.process_time_ns)
        steady = measure(reps, lambda i, workers=workers: plan(i, workers, True), 3, clock=time.process_time_ns)
        FINDINGS.figures[f"pool.{workers}.cold"] = cold.median
        FINDINGS.figures[f"pool.{workers}.steady"] = steady.median
        out(f"   [measured] Python WorkerPool({workers}), {jobs} trivial jobs, process CPU per job: fresh pool (thread starts included) "
            f"p50 {fmt_ns(cold.median)} p99 {fmt_ns(cold.p99)}; pool already running p50 {fmt_ns(steady.median)} p99 {fmt_ns(steady.p99)}")
    out(f"   [quoted] Rust pool, one thread per dispatch (loop bench): about {QUOTED['thread_per_dispatch_ms_at_32']} ms per job at 32 workers, "
        f"per-pass p99 up to {QUOTED['pass_p99_ms']} ms")
    out()


def gil_probe() -> dict[str, float | str]:
    """What a thread that Python did not create pays to enter the interpreter (PyGILState_Ensure, the call `Python::attach` makes in PyO3):
    a pthread whose start routine is a ctypes callback, against a pthread whose start routine does nothing. Wall time, minimum and median."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        callback_type = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)
        stamp = [0]

        @callback_type
        def body(_arg: int | None) -> None:
            stamp[0] = time.perf_counter_ns()

        create, join = libc.pthread_create, libc.pthread_join
        create.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        join.argtypes = [ctypes.c_ulong, ctypes.c_void_p]
        entry = ctypes.cast(body, ctypes.c_void_p)
        nothing = ctypes.cast(libc.getpid, ctypes.c_void_p)

        def spawn(start: ctypes.c_void_p) -> int:
            tid = ctypes.c_ulong()
            t0 = time.perf_counter_ns()
            if create(ctypes.byref(tid), None, start, None) != 0:
                raise OSError("pthread_create failed")
            join(tid.value, None)
            return time.perf_counter_ns() - t0

        for _ in range(20):
            spawn(entry)
            spawn(nothing)
        with_cb = sorted(spawn(entry) for _ in range(400))
        without = sorted(spawn(nothing) for _ in range(400))
        # process CPU over batches (the foreign thread's CPU counts, the waits do not): the minimum batch of each kind, per spawn
        batch = 100
        cpu_with, cpu_without = [], []
        for _ in range(15):
            t0 = time.process_time_ns()
            for _ in range(batch):
                spawn(entry)
            cpu_with.append((time.process_time_ns() - t0) / batch)
            t0 = time.process_time_ns()
            for _ in range(batch):
                spawn(nothing)
            cpu_without.append((time.process_time_ns() - t0) / batch)
        # contended: the main thread runs Python code and never blocks, so the foreign thread waits for the switch interval
        lat = []
        for _ in range(12):
            stamp[0] = 0
            tid = ctypes.c_ulong()
            t0 = time.perf_counter_ns()
            create(ctypes.byref(tid), None, entry, None)
            while stamp[0] == 0:
                pass
            lat.append(stamp[0] - t0)
            join(tid.value, None)
        lat.sort()
        return {
            "attach_cpu_ns": float(min(cpu_with) - min(cpu_without)), "attach_min_ns": float(with_cb[0] - without[0]),
            "attach_p50_ns": float(statistics.median(with_cb) - statistics.median(without)),
            "contended_min_ns": float(lat[0]), "contended_p50_ns": float(statistics.median(lat)), "switch_interval_s": sys.getswitchinterval(),
        }
    except (OSError, AttributeError, ValueError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def prim(fig: dict[str, float], key: str, side: str, label_start: str) -> float:
    """A measured primitive median in ns by group key, side ('py' or 'native') and the start of the row label; nan when absent."""
    prefix = f"{key}.{side}.{label_start}"
    return next((v for k, v in fig.items() if k.startswith(prefix)), math.nan)


def synthesis(env: Env, gil: dict[str, float | str], loop_log: Path | None) -> None:
    fig = FINDINGS.figures
    out("== 4. synthesis (measured = this run; quoted = the loop bench figures given in the task, not re-measured by this script)")
    if loop_log is not None and loop_log.exists():
        out(f"-- output of this unit's own `cargo run --release --example loop_bench -p gsd-sched-core` ({loop_log.name}; measured by cargo, "
            "outside this script, release build, private target dir, same saturated host):")
        for line in loop_log.read_text(encoding="utf-8", errors="replace").splitlines():
            out("   | " + line)
        out()
    out("-- the quoted Rust primitives against this run's native calls from Python (the difference is PyO3 argument conversion and call overhead)")
    out(f"   order_ready 50 rows   quoted {QUOTED['order_ready_50_us']} us        measured via PyO3: call only {fmt_ns(prim(fig, 'prim.order_ready.50', 'native', 'native sched_order_ready'))}, "
        f"with row building {fmt_ns(prim(fig, 'prim.order_ready.50', 'native', 'native facade'))}")
    out(f"   order_ready 500 rows  quoted {QUOTED['order_ready_500_us']} us       measured via PyO3: call only {fmt_ns(prim(fig, 'prim.order_ready.500', 'native', 'native sched_order_ready'))}, "
        f"with row building {fmt_ns(prim(fig, 'prim.order_ready.500', 'native', 'native facade'))}")
    out(f"   one acquire of 50     quoted {QUOTED['claims_acquire_50_us']} us       measured via PyO3: {fmt_ns(prim(fig, 'prim.claims.one.50', 'native', 'native ClaimTable, claims as'))}")
    out(f"   50 sequential acquires quoted {QUOTED['claims_sequential_50_us']:.0f} us     measured via PyO3: {fmt_ns(prim(fig, 'prim.claims.seq.50', 'native', 'native ClaimTable, claims as'))}")
    out(f"   AIMD ops              quoted {QUOTED['aimd_ns']} ns        measured via PyO3: success {fmt_ns(prim(fig, 'prim.aimd.success', 'native', 'native'))}, "
        f"rate limit {fmt_ns(prim(fig, 'prim.aimd.rate_limit', 'native', 'native'))}, failure {fmt_ns(prim(fig, 'prim.aimd.failure', 'native', 'native'))} "
        f"(PyO3 floor {fmt_ns(fig.get('prim.pyo3_floor', math.nan))})")
    out(f"   backoff               quoted {QUOTED['backoff_ns']} ns        measured via PyO3: {fmt_ns(prim(fig, 'prim.backoff', 'native', 'native'))} (with the rng sample)")
    out()
    if "error" in gil:
        out(f"   GIL attach per port call: UNMEASURED ({gil['error']}); the Engine B figures below leave it out and are therefore lower bounds")
        attach_us = 0.0
    else:
        attach_us = max(0.0, float(gil["attach_cpu_ns"])) / 1e3
        out(f"   [measured, proxy] a thread Python did not create entering the interpreter (ctypes pthread callback: PyGILState_Ensure/Release, the CPython "
            f"call under PyO3's attach; not PyO3 itself), GIL free: {attach_us:.1f} us of process CPU per attach (minimum batch of 100 spawns over a bare "
            f"pthread create+join); in wall time p50 {float(gil['attach_p50_ns']) / 1e3:.1f} us, min {float(gil['attach_min_ns']) / 1e3:.1f} us")
        out(f"   [measured, proxy] the same while the interpreter thread runs Python code without blocking: p50 {float(gil['contended_p50_ns']) / 1e6:.2f} ms, "
            f"min {float(gil['contended_min_ns']) / 1e6:.2f} ms (switch interval {float(gil['switch_interval_s']) * 1e3:.1f} ms); not added to the sums below")
    out("   A Rust loop thread pays one attach per port call: per dispatched task agent_for, claims_for, lease, submit and complete (5), and begin_pass "
        "and observe_pass once per pass (crates/gsd-sched-core/src/ports.rs).")
    out()
    pool_py = fig.get("pool.32.steady", math.nan) / 1e3
    thread_extra = QUOTED["thread_per_dispatch_ms_at_32"] * 1e3 - pool_py
    verdicts: list[tuple[str, float, float, float]] = []
    out("   The begin_pass cost of a pass is spread over the n tasks that pass dispatches, the most favourable share for the decision code: the continuous "
        "scheduler runs a pass after every completion. Port-body stores hold n tasks (about 40% ready), the decision inputs n ready tasks.")
    out()
    for n in (50, 500):
        begin = sum(fig[f"port.{n}.{op}"] for op in ("reconcile", "log_phase", "dispatchable", "list")) / 1e3  # the four calls of the pass, summed
        trans = fig[f"port.{n}.transition"] / 1e3
        comp = fig[f"port.{n}.complete_ok"] / 1e3
        for tag, claims_text in (("tenth", "1 task in 10 declares claims"), ("all", "every task declares claims (stress)")):
            dec = fig[f"dec.{n}.{tag}.sched_nobook"] / 1e3
            book = max(0.0, fig[f"dec.{n}.tenth.sched_book"] - fig[f"dec.{n}.tenth.sched_nobook"]) / 1e3  # the RunBook loop does not depend on claims
            order_py, select_py = fig[f"dec.{n}.{tag}.order"] / 1e3, fig[f"dec.{n}.{tag}.select"] / 1e3
            rust = fig[f"dec.{n}.{tag}.native"] / 1e3
            ports_task = (begin + book) / n + 2 * trans + comp
            py_task = dec / n + ports_task
            gil_task = 5 * attach_us + 2 * attach_us / n
            engine_b = ports_task + gil_task + rust / n
            engine_b_thread = engine_b + thread_extra
            floor = ports_task + gil_task
            out(f"-- {n} ready tasks, {claims_text}; one pass dispatching all of them; microseconds of CPU")
            out(f"   Python decision code (ordering, eligibility, select_batch, claims)          {dec:12.1f} per pass {dec / n:10.2f} /task  [measured]")
            out(f"      of which order_ready {order_py:.1f} and select_batch {select_py:.1f} per pass                                                [measured]")
            out(f"   native replay of the same decision (via PyO3, what a Python facade pays)    {rust:12.1f} per pass {rust / n:10.2f} /task  [measured]")
            out(f"   Python bookkeeping that stays Python (RunBook loop over every task)         {book:12.1f} per pass {book / n:10.2f} /task  [measured]")
            out(f"   begin_pass body on a store of {n} tasks (reconcile + log_phase + dispatchable + list) {begin:12.1f} per pass {begin / n:10.2f} /task  [measured]")
            out(f"   submit path, two transitions per task (commit + live.json refresh each)                           {2 * trans:10.2f} /task  [measured]")
            out(f"   complete path, Scheduler._complete with real events                                               {comp:10.2f} /task  [measured]")
            out(f"   GIL attach, 5 + 2/{n} port calls per task                                                          {gil_task:10.2f} /task  [measured proxy]")
            out(f"   => Python engine {py_task:.2f} us/task, decision code {100 * dec / n / py_task:.3f}% of it")
            out(f"   => Engine B (Rust decisions, Python ports, attach) {engine_b:.2f} us/task = {engine_b / py_task:.4f}x the Python engine")
            out(f"      with decisions free and attach free: {ports_task / py_task:.4f}x (the bound on any gain: {100 * (1 - ports_task / py_task):.3f}%);"
                f" with decisions free and the measured attach: {floor / py_task:.4f}x")
            out(f"      with the quoted thread-per-dispatch pool (+{thread_extra:.0f} us/task over the Python pool's {pool_py:.1f} us measured per job): "
                f"{engine_b_thread / py_task:.3f}x")
            out()
            verdicts.append((f"{n} ready, {claims_text}", engine_b / py_task, engine_b_thread / py_task, 100 * (1 - ports_task / py_task)))
    out("-- where Rust gives NO gain in this run (native/Python p50 >= 1.00x or p99 >= 1.00x; rows above):")
    for line in FINDINGS.no_gain or ["(none)"]:
        out(f"   - {line}")
    out("-- where the native call is faster in both p50 and p99:")
    for line in FINDINGS.gain or ["(none)"]:
        out(f"   - {line}")
    out()
    faster = [v for v in verdicts if v[1] < 0.95]
    parts = [f"{name}: Engine B {ratio:.3f}x Python (bound {bound:.2f}% gain; {thread_ratio:.2f}x with the quoted thread-per-dispatch pool)"
             for name, ratio, thread_ratio, bound in verdicts]
    ratios = [fig[f"dec.{n}.{tag}.native"] / fig[f"dec.{n}.{tag}.sched_nobook"] for n in (50, 500) for tag in ("tenth", "all", "cap8")]
    tail = (f" The decision code alone is cheaper natively (the native replay costs {min(ratios):.2f}x to {max(ratios):.2f}x of the Python decision part at p50), "
            f"but it is only {min(v[3] for v in verdicts):.3f}% to {max(v[3] for v in verdicts):.2f}% of the per-task CPU, the rest being port bodies that stay Python.")
    if faster:
        head = ("VERDICT: a measurable end-to-end speedup (more than 5% of the per-task CPU, thread-per-dispatch pool excluded) is possible only where the Python claim scan "
                "dominates: " + "; ".join(f"{v[0]} ({v[1]:.3f}x)" for v in faster) + ". Everywhere else it is not")
    else:
        head = "VERDICT: no measurable end-to-end speedup is possible in any scenario measured"
    out(head + "." + tail + " Per scenario: " + "; ".join(parts) + ".")
    out()


# ---- section 5: the dry-run proxy ---------------------------------------------------------------------------------------------------


def child_env(env: Env, name: str) -> dict[str, str]:
    sdir = env.work / name
    shutil.rmtree(sdir, ignore_errors=True)
    sdir.mkdir(parents=True)
    child = dict(os.environ)
    child.update({"SWARM_DIR": str(sdir), "TMPDIR": str(env.work), **isolation(env.work)})
    for key in ("SWARM_PURE_PYTHON",):
        child.pop(key, None)
    return child


def plan_dry_run(env: Env, name: str) -> dict[str, str]:
    child = child_env(env, name)
    subprocess.run([sys.executable, str(REPO / "scripts" / "orch_plan.py"), "--brief-text", "bench", "--pattern", "feature", "--risk-class", "medium"],
                   env=child, cwd=REPO, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
    return child


def dry_run_cpu(env: Env, runs: int) -> list[float]:
    cpu = []
    for k in range(runs):
        child = plan_dry_run(env, f"dry_{k}")
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        subprocess.run([sys.executable, str(REPO / "scripts" / "swarm_run.py"), "--dry-run", "--json"], env=child, cwd=REPO, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu.append(after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime)
    return cpu


def profile_child(out_path: str) -> None:
    """Runs scripts/swarm_run.py --dry-run --json under the `profile` module (the pure-Python twin of cProfile, same pstats output) with the CPU time
    of the profiled thread as the clock. cProfile cannot be used: on CPython 3.12 it records every thread through sys.monitoring and mixes the call
    stacks of the scheduler thread and the worker threads (it printed negative cumulative times with a thread-CPU clock); `profile` is per thread."""
    import profile

    sys.argv = [str(REPO / "scripts" / "swarm_run.py"), "--dry-run", "--json"]

    def target() -> None:
        try:
            with contextlib.redirect_stdout(open(os.devnull, "w", encoding="utf-8")):  # noqa: SIM115
                runpy.run_path(sys.argv[0], run_name="__main__")
        except SystemExit:
            pass

    prof = profile.Profile(time.thread_time)
    prof.runcall(target)
    prof.dump_stats(out_path)


def dry_run_profile(env: Env) -> None:
    import pstats

    child = plan_dry_run(env, "dry_profile")
    prof_path = env.work / "dry_profile.prof"
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "--profile-child", str(prof_path)], env=child, cwd=REPO, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900)
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    tree_cpu = after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime
    stats = pstats.Stats(str(prof_path))
    total = stats.total_tt  # type: ignore[attr-defined]
    table = stats.stats  # type: ignore[attr-defined]

    def cum(name: str, file_part: str) -> tuple[float, int]:
        ct, calls = 0.0, 0
        for (filename, _line, func), (_cc, nc, _tt, c, _callers) in table.items():
            if func == name and file_part in filename:
                ct, calls = ct + c, calls + nc
        return ct, calls

    def own_time(file_part: str) -> float:
        return sum(tt for (filename, _l, _f), (_cc, _nc, tt, _ct, _cl) in table.items() if file_part in filename)

    out("-- per-thread profile of one dry run (`profile`, the pure-Python twin of cProfile: cProfile on CPython 3.12 mixes the scheduler thread with the workers "
        "and printed negative times) [measured]")
    out(f"   profiled thread = the scheduler loop, clock = its CPU time, profiler overhead included: {total:.3f} s; the whole process tree used {tree_cpu:.3f} s "
        "of CPU for this run (workers and the startup included, unprofiled)")
    out(f"   {'function':<46}{'calls':>8}{'cum CPU':>12}{'share of profiled thread':>28}")
    rows = [("Scheduler.loop (the whole scheduling loop)", "loop", "swarm_run"), ("Scheduler._schedule (one pass)", "_schedule", "swarm_run"),
            ("  select_batch", "select_batch", "swarm_run"), ("  order_ready", "order_ready", "swarm_run"),
            ("  dispatchable", "dispatchable", "swarm_run"), ("  reconcile", "reconcile", "results.py"),
            ("  log_phase (a runlog event per pass)", "log_phase", "runlog.py"), ("  RunBook.seen (telemetry rows)", "seen", "swarm_run"),
            ("Scheduler._submit (spawn events, subagent id)", "_submit", "swarm_run"), ("Scheduler._complete", "_complete", "swarm_run"),
            ("  Scheduler._backpressure (limiter + streak)", "_backpressure", "swarm_run"),
            ("runlog.emit (every event, any caller)", "emit", "runlog.py"), ("redact.redact (inside emit)", "redact", "redact.py"),
            ("observability.emit_gsd_omp", "emit_gsd_omp", "observability"), ("observability.stream_subagent_status", "stream_subagent_status", "observability"),
            ("TaskStore.transition", "transition", "taskstore.py"), ("TaskStore.get", "get", "taskstore.py"), ("TaskStore.list", "list", "taskstore.py"),
            ("TaskStore.set_notes", "set_notes", "taskstore.py"), ("TaskStore.ready", "ready", "taskstore.py"), ("TaskStore.update", "update", "taskstore.py")]
    for label, func, file_part in rows:
        ct, calls = cum(func, file_part)
        out(f"   {label:<46}{calls:>8}{ct * 1e3:>10.1f} ms{100 * ct / total:>27.2f}%")
    sqlite_time = sum(tt for (filename, _l, func), (_cc, _nc, tt, _ct, _cl) in table.items() if filename in ("", "~") and func in ("execute", "executescript", "commit"))
    out(f"   {'taskstore.py own time (all functions)':<46}{'':>8}{own_time('taskstore.py') * 1e3:>10.1f} ms{100 * own_time('taskstore.py') / total:>27.2f}%")
    out(f"   {'sqlite3 execute/executescript/commit, own time':<46}{'':>8}{sqlite_time * 1e3:>10.1f} ms{100 * sqlite_time / total:>27.2f}%")
    out("   (cumulative times nest: _schedule includes select_batch, dispatchable, reconcile and log_phase; the shares do not add up)")
    out()


# ---- paired scheduler runtimes (Phase 14.4, S5.2 and S6; decision D-P21) ------------------------------------------------------------

# The paired sampler times the scheduler the product runs: `scripts/swarm_run.py` as a subprocess, once with GSD_ENGINE=python and once with
# GSD_ENGINE=rust, on identical seeded inputs, each leg on a fresh SWARM_DIR built before its clock starts, in alternating order. Before a
# fixture is timed, its first pair is verified: results, Task Store states and transitions, event types, and the engine each leg reports on its
# `scheduler_engine:` log line (a rust leg answered by python is a fallback). A failing, mismatching or fallback sample refuses the run. The
# record is written before anything is judged. `--qualify-json` judges it: exit 0 only for a complete quiet candidate whose paired p50 and p95
# are both strictly below 1.00 on every required fixture; every other case exits 1 and names its reasons. A record is evidence, never a
# verdict, and nothing here claims a speed-up.

PAIRED_SAMPLES = 50  # measured pairs per fixture (the A2 protocol)
PAIRED_WARMUP = 3  # warm-up pairs per fixture, timed and discarded (the A2 protocol)
RECORD_KIND = "scheduler-paired"
RECORD_SCHEMA = 1
QUIET_LOAD = 1.5  # the A2 quiet rule: 1-minute load below this (STATE.md P-A2)
LOAD_EVERY_S = 10.0  # /proc/loadavg is read this often during a run
WATCHER_MAX_AGE_S = 900  # the quiet line's log must have been written at most this long before the run started (review M2)
ENGINES = ("python", "rust")
READY_PASS_PARALLEL = 8  # --max-parallel of a ready-task pass: one --once round dispatches at most this many of the ready tasks
A2_BRIEF = "a2 baseline fixture"  # the brief tests/perf/run.sh plans its path (a) fixture from
BENCH_SIGNING_KEY = hashlib.sha256(b"agent-swarm scheduler-paired benchmark").hexdigest()  # one fixed key for every leg, not a deployment key
RESULT_KEYS = ("status", "complete", "counts", "cancelled", "escalated", "scheduler", "max_parallel", "effective_parallel")
FINGERPRINT_SOURCES = ("scripts/swarm_run.py", "scripts/orch_plan.py", "swarm/rust_scheduler.py", "swarm/taskstore.py",
                       "tests/perf/scheduler_engines.py", "crates/gsd-sched-core", "crates/gsd-python")
FINGERPRINT_GLOBS = ("swarm/**/*.py",)  # every swarm module the timed legs import; __pycache__ is excluded

# The fake omp speaks the protocol the black-box suite's fake session speaks (a session event, the yield, agent_end) and nothing more. It
# is a protocol fixture for the 12-task walltime, never performance evidence by itself.
_FAKE_OMP = '''import json, re, sys
prompt = sys.stdin.read()
tid = re.search(r'"task_id": "([^"]+)"', prompt).group(1)
res = {"task_id": tid, "state": "IN_REVIEW", "outputs": [{"kind": "code.backend", "uri": "file://x", "version": "1", "digest": ""}],
       "metrics": {}, "summary_md": "ok"}
asst = {"role": "assistant", "content": [{"type": "text", "text": "done"},
                                         {"type": "toolCall", "id": "t1", "name": "yield", "arguments": {"data": res}}],
        "stopReason": "toolUse", "usage": {"cost": {"total": 0.01}}}
tres = {"role": "toolResult", "toolCallId": "t1", "toolName": "yield", "isError": False,
        "details": {"data": res, "status": "success"}}
print(json.dumps({"type": "session", "version": 3, "id": "fake-" + tid, "cwd": ""}), flush=True)
print(json.dumps({"type": "turn_end", "message": asst, "toolResults": [tres]}), flush=True)
print(json.dumps({"type": "agent_end", "messages": [{"role": "user", "content": prompt}, asst, tres], "isTerminal": True,
                  "yielded": True}), flush=True)
'''


@dataclass(frozen=True)
class Fixture:
    """One paired fixture. Mode `a2` is A2 path (a); `omp` is the 12-task fake-omp run; `ready` is one scheduling round over `tasks` ready tasks."""

    id: str
    mode: str
    tasks: int  # plan size; 0 for the A2 brief
    expect_rc: int
    expect_status: str


# A ready-task pass plans N independent tasks and runs one --once round, which dispatches at most READY_PASS_PARALLEL of them and at most
# the A05 max_parallel (every ready task is an A05 task): an incomplete run (status fail, exit 1) by design. A wrong exit or status refuses
# the sample like any other failure.
FIXTURES = (
    Fixture("a2-path-a", "a2", 0, 0, "ok"),
    Fixture("fake-omp-12", "omp", 12, 0, "ok"),
    Fixture("ready-50", "ready", 50, 1, "fail"),
    Fixture("ready-500", "ready", 500, 1, "fail"),
)
REQUIRED_FIXTURES = tuple(fx.id for fx in FIXTURES)


def _plan(n: int) -> dict[str, Any]:
    """The seeded plan of n independent code tasks: the same bytes on every run."""
    return {"tasks": [{"id": f"t{i:03d}", "capability": "code.backend", "agent": "A05", "title": f"paired fixture task {i:03d}",
                       "depends_on": [], "gates": []} for i in range(n)]}


def ready_dispatch_expectation() -> int:
    """How many ready tasks one --once round admits: the smaller of the round's --max-parallel and the A05 max_parallel in agents.json,
    since every ready task is an A05 task. Read from the manifest, so a change to the cap changes the expectation and the digest."""
    manifest = json.loads((REPO / "agents.json").read_text(encoding="utf-8"))
    cap = next(int(a["max_parallel"]) for a in manifest["agents"] if a.get("id") == "A05")
    return min(READY_PASS_PARALLEL, cap)


def fixture_digest(fx: Fixture) -> str:
    """sha256 of a fixture's seeded inputs and expectations; a record measured on other inputs does not qualify."""
    spec = {"id": fx.id, "mode": fx.mode, "tasks": fx.tasks, "brief": A2_BRIEF if fx.mode == "a2" else None,
            "plan": _plan(fx.tasks) if fx.mode != "a2" else None, "expect": [fx.expect_rc, fx.expect_status],
            "ready_parallel": [READY_PASS_PARALLEL, ready_dispatch_expectation()] if fx.mode == "ready" else None}
    return hashlib.sha256(canon(spec).encode("utf-8")).hexdigest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_fingerprint() -> dict[str, str]:
    """sha256 of each source file the paired legs run or measure: the listed files, every file under a listed directory (build output
    excluded), and every file the FINGERPRINT_GLOBS match (__pycache__ excluded)."""
    found: dict[str, str] = {}
    for rel in FINGERPRINT_SOURCES:
        base = REPO / rel
        if base.is_file():
            files = [base]
        elif base.is_dir():
            files = sorted(p for p in base.rglob("*") if p.is_file() and "target" not in p.relative_to(base).parts and "__pycache__" not in p.parts)
        else:
            files = []
        if not files:
            found[rel] = "absent"
        for p in files:
            found[p.relative_to(REPO).as_posix()] = _sha(p)
    for pattern in FINGERPRINT_GLOBS:
        for p in sorted(REPO.glob(pattern)):
            if p.is_file() and "__pycache__" not in p.relative_to(REPO).parts:
                found[p.relative_to(REPO).as_posix()] = _sha(p)
    return found


def extension_fingerprint() -> dict[str, str]:
    """sha256 of the built swarm._native extension the rust legs import."""
    libs = sorted((REPO / "swarm").glob("_native*.so"))
    return {p.relative_to(REPO).as_posix(): _sha(p) for p in libs} or {"swarm/_native": "absent"}


def elf_debug_sections(path: Path) -> list[str]:
    """Names of the `.debug*` sections of a little-endian ELF64 file. A dev-profile build has them; the release build has none."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4:6] != b"\x02\x01":
        raise ValueError(f"{path.name} is not a little-endian ELF64 file")
    shoff = struct.unpack_from("<Q", data, 0x28)[0]
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)

    def header(i: int) -> tuple[int, ...]:
        return struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize)

    names_hdr = header(shstrndx)
    table = data[names_hdr[4]: names_hdr[4] + names_hdr[5]]
    names = [table[header(i)[0]: table.index(b"\0", header(i)[0])].decode("ascii", "replace") for i in range(shnum)]
    return [name for name in names if name.startswith(".debug")]


def extension_debug_sections() -> int:
    """Debug sections across the built extension(s): 0 for a release build, -1 when there is no extension to measure."""
    libs = sorted((REPO / "swarm").glob("_native*.so"))
    if not libs:
        return -1
    return sum(len(elf_debug_sections(p)) for p in libs)


def gsd_engine_binary() -> Path:
    """The gsd_engine binary the rust leg's gates run: GSD_ENGINE_BIN from the operator's environment (made absolute), else the selector's default path."""
    raw = os.environ.get("GSD_ENGINE_BIN", "").strip()
    return Path(raw).resolve() if raw else REPO / "target" / "release" / "gsd_engine"


def binary_fingerprint() -> dict[str, str]:
    """sha256 of the gsd_engine binary the rust legs run; 'absent' when the file is missing."""
    binary = gsd_engine_binary()
    return {"path": str(binary), "sha256": _sha(binary) if binary.is_file() else "absent"}


def _tool_version(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    return done.stdout.strip() or "unavailable"


def host_fingerprint() -> dict[str, Any]:
    return {"uname": " ".join(platform.uname()), "machine": platform.machine(), "nproc": os.cpu_count(), "python": sys.version.split()[0],
            "rustc": _tool_version(["rustc", "--version"]), "configuration": "product: GSD_ENGINE selects the engine, SWARM_PURE_PYTHON unset"}


def fingerprint_now() -> dict[str, Any]:
    return {"source": source_fingerprint(), "extension": extension_fingerprint(), "binary": binary_fingerprint(),
            "extension_debug_sections": extension_debug_sections(),
            "fixtures": {fx.id: fixture_digest(fx) for fx in FIXTURES}, "host": host_fingerprint()}


def nearest_rank(values: Sequence[float], pct: int) -> float:
    """Nearest-rank percentile: the value at 1-based rank ceil(pct * n / 100) of the sorted values (integer rank arithmetic)."""
    ordered = sorted(values)
    if not ordered:
        raise ValueError("no values to take a percentile of")
    rank = max(1, -((-pct * len(ordered)) // 100))
    return ordered[rank - 1]


def pair_ratios(pairs: Sequence[dict[str, Any]]) -> list[float]:
    return [pr["rust_ns"] / pr["python_ns"] for pr in pairs]


def watcher_last_line(path: str | None) -> str | None:
    """The last non-blank line of the watch_quiet.sh log, or None when there is no such log."""
    if not path or not Path(path).is_file():
        return None
    lines = [ln.strip() for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    return lines[-1] if lines else None


def watcher_state(path: str | None) -> dict[str, Any]:
    """The watch_quiet.sh log as the run sees it at one moment: its mtime (epoch and UTC) and its last line. Empty values when there is no log."""
    try:
        if not path or not Path(path).is_file():
            return {"mtime_epoch": None, "mtime_utc": None, "last_line": None}
        mtime = Path(path).stat().st_mtime
        return {"mtime_epoch": mtime, "mtime_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(mtime)), "last_line": watcher_last_line(path)}
    except OSError:
        return {"mtime_epoch": None, "mtime_utc": None, "last_line": None}


def _load1_now() -> float:
    """The 1-minute load average the host reports now: the start-of-run check reads it before any fixture is timed."""
    return float(Path("/proc/loadavg").read_text(encoding="utf-8").split()[0])


class LoadSampler:
    """/proc/loadavg and the watcher's last line, read before the run, every LOAD_EVERY_S seconds during it, and after it."""

    def __init__(self, watcher_log: str | None = None) -> None:
        self.readings: list[dict[str, Any]] = []
        self._t0 = time.monotonic()
        self._stop = threading.Event()
        self._watcher = watcher_log
        self._thread = threading.Thread(target=self._loop, name="loadavg", daemon=True)

    def _read(self, at: str) -> None:
        line = Path("/proc/loadavg").read_text(encoding="utf-8").strip()
        self.readings.append({"at": at, "elapsed_s": round(time.monotonic() - self._t0, 3), "load1": float(line.split()[0]), "line": line,
                              "watcher": watcher_last_line(self._watcher)})

    def _loop(self) -> None:
        while not self._stop.wait(LOAD_EVERY_S):
            self._read("during")

    def start(self) -> None:
        self._read("before")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        self._read("after")


class Refused(Exception):
    """A sample that cannot count as evidence: the run stops, and the record says why."""


@dataclass
class LegRun:
    wall_ns: int
    rc: int
    stdout: str
    stderr: str


@dataclass
class Observation:
    """What one leg left behind: its wall time, exit code, the engine that answered, and the results, store and events it wrote."""

    engine: str
    wall_ns: int
    rc: int
    served_by: str
    result: dict[str, Any]
    store: list[Any]
    events: list[str]
    engine_line: str
    error: str = ""


LegFn = Callable[[str, str], Observation]


def execute_leg(argv: Sequence[str], cwd: Path, env: dict[str, str]) -> LegRun:
    """One timed subprocess: the wall clock runs around the child only."""
    start = time.perf_counter_ns()
    done = subprocess.run(list(argv), cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=3600)
    wall = time.perf_counter_ns() - start
    return LegRun(wall, done.returncode, done.stdout, done.stderr)


def served_by(lines: Sequence[str]) -> str:
    """The engine named by the one `scheduler_engine: <name>` line swarm_run writes per run; any other count of lines is `unknown`."""
    names = [ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("scheduler_engine:")]
    return names[0] if len(names) == 1 else "unknown"


def store_snapshot(sdir: Path, corr: str | None) -> list[Any]:
    """Sorted (task, state) pairs of the run's tasks, and the Task Store's transition count. Each leg plans its own store, so the correlation prefix of a task id differs between the legs; the part after the first hyphen is compared."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from swarm.taskstore import TaskStore

    store = TaskStore(sdir / "tasks.db")
    try:
        rows = sorted([str(t["task_id"]).split("-", 1)[-1], str(t["state"])] for t in store.list(correlation_id=corr or None))
        transitions = int(store.conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0])
    finally:
        store.conn.close()
    return [rows, transitions]


def event_types(sdir: Path) -> list[str]:
    """The distinct event types in the run's events.jsonl. Counts follow timing, so they are not compared."""
    log = sdir / "events.jsonl"
    if not log.is_file():
        return []
    kinds: set[str] = set()
    for ln in log.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            rec = json.loads(ln)
            kinds.add(str(rec.get("type")) if isinstance(rec, dict) else "<not an object>")
    return sorted(kinds)


def observe(engine: str, run: LegRun, sdir: Path) -> Observation:
    """The untimed reading of one leg: its JSON result, the engine line, the store and the events. Nothing here is timed. `dispatched` counts
    the finished dispatches in the run's log: `--once` drains every dispatch before it returns, and each finished one leaves a
    `round N: <task> [<agent>] sid=... -> <outcome>` line."""
    try:
        res = json.loads(run.stdout)
    except ValueError:
        res = None
    if not isinstance(res, dict):
        return Observation(engine, run.wall_ns, run.rc, "unknown", {}, [], [], "", "stdout is not a JSON object")
    log = res.get("log") if isinstance(res.get("log"), list) else []
    lines = [ln for ln in log if isinstance(ln, str) and ln.startswith("scheduler_engine:")]
    dispatched = sum(1 for ln in log if isinstance(ln, str) and ln.startswith("round ") and " sid=" in ln)
    result = {**{k: res.get(k) for k in RESULT_KEYS}, "dispatched": dispatched}
    try:
        store = store_snapshot(sdir, res.get("correlation_id"))
        events = event_types(sdir)
    except (OSError, ValueError, sqlite3.Error) as exc:
        return Observation(engine, run.wall_ns, run.rc, served_by(lines), {}, [], [], " | ".join(lines), f"store or events unreadable: {exc}")
    return Observation(engine, run.wall_ns, run.rc, served_by(lines), result, store, events, " | ".join(lines))


def leg_problems(fx: Fixture, obs: Observation) -> list[str]:
    """Why one leg cannot be timed or counted: an error, the wrong exit code, the wrong engine or a fallback, the wrong status, any cancelled
    task or run, or, for a ready pass, a dispatch count other than the one the pass takes."""
    problems: list[str] = []
    if obs.error:
        problems.append(f"{obs.engine} leg: {obs.error}")
    if obs.rc != fx.expect_rc:
        problems.append(f"{obs.engine} leg: exit {obs.rc}, the fixture expects {fx.expect_rc}")
    if obs.served_by != obs.engine:
        kind = "fallback" if obs.engine == ENGINES[1] else "wrong engine"
        problems.append(f"{obs.engine} leg: {kind}: the scheduler log says {obs.served_by!r}")
    if obs.result.get("status") != fx.expect_status:
        problems.append(f"{obs.engine} leg: status {obs.result.get('status')!r}, the fixture expects {fx.expect_status!r}")
    counts = obs.result.get("counts") if isinstance(obs.result.get("counts"), dict) else {}
    if counts.get("CANCELLED"):
        problems.append(f"{obs.engine} leg: {counts.get('CANCELLED')!r} CANCELLED task(s)")
    if obs.result.get("cancelled") is not None:
        problems.append(f"{obs.engine} leg: cancelled run: {obs.result.get('cancelled')!r}")
    if fx.mode == "ready":
        want = min(fx.tasks, ready_dispatch_expectation())
        if obs.result.get("dispatched") != want:
            problems.append(f"{obs.engine} leg: dispatched {obs.result.get('dispatched')!r}, the one-round pass takes {want}")
    return problems


def result_mismatches(py: Observation, rs: Observation) -> list[str]:
    """What must be identical between the engines: the results, the Task Store states and transitions, and the event types."""
    diffs: list[str] = []
    if py.result != rs.result:
        diffs.append("result")
    if py.store != rs.store:
        diffs.append("store")
    if py.events != rs.events:
        diffs.append("events")
    return diffs


def _other(engine: str) -> str:
    return ENGINES[1] if engine == ENGINES[0] else ENGINES[0]


def _pair(fx: Fixture, leg: LegFn, first: str, tag: str) -> dict[str, Observation]:
    """One pair: both engines, `first` before the other. Raises Refused on any failing, fallback or mismatching leg."""
    obs: dict[str, Observation] = {}
    for engine in (first, _other(first)):
        ob = leg(engine, tag)
        problems = leg_problems(fx, ob)
        if problems:
            raise Refused(f"{tag}: " + "; ".join(problems))
        obs[engine] = ob
    diffs = result_mismatches(obs[ENGINES[0]], obs[ENGINES[1]])
    if diffs:
        raise Refused(f"{tag}: parity mismatch in {', '.join(diffs)}")
    return obs


def measure_fixture(fx: Fixture, leg: LegFn, samples: int, warmup: int, pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Verify, warm up, then measure one fixture. The first pair verifies the leg (its times are dropped), then `warmup` pairs are timed and
    discarded, then `samples` alternating pairs are measured. Measured pairs are appended to `pairs` as they finish, so a refused run keeps the
    pairs it had. Raises Refused on the first failing, fallback or mismatching sample."""
    verified = _pair(fx, leg, ENGINES[0], "verify")
    for w in range(warmup):
        _pair(fx, leg, ENGINES[w % 2], f"warmup-{w}")
    for j in range(samples):
        first = ENGINES[j % 2]
        obs = _pair(fx, leg, first, f"sample-{j}")
        py, rs = obs[ENGINES[0]], obs[ENGINES[1]]
        pairs.append({"index": j, "order": [first, _other(first)], "python_ns": py.wall_ns, "rust_ns": rs.wall_ns,
                      "python_rc": py.rc, "rust_rc": rs.rc, "ratio": rs.wall_ns / py.wall_ns,
                      "counts": {e: obs[e].result.get("counts") for e in ENGINES},
                      "cancelled": {e: obs[e].result.get("cancelled") for e in ENGINES},
                      "dispatched": {e: obs[e].result.get("dispatched") for e in ENGINES}})
    ratios = pair_ratios(pairs)
    walls = {e: [pr[f"{e}_ns"] for pr in pairs] for e in ENGINES}
    return {
        "id": fx.id, "digest": fixture_digest(fx), "status": "ok", "reason": "", "samples": samples, "warmup_pairs": warmup,
        "skipped": samples - len(pairs), "expect": {"rc": fx.expect_rc, "status": fx.expect_status},
        "parity": {"ok": True, "checked": ["result", "store", "events", "served_by"], "verify_pair": True, "mismatches": []},
        "served_by": {e: verified[e].served_by for e in ENGINES},
        "witness": {e: verified[e].engine_line for e in ENGINES},
        "pairs": pairs,
        "ratio": {"p50": nearest_rank(ratios, 50), "p95": nearest_rank(ratios, 95)},
        "legs_ms": {e: {"p50": nearest_rank(walls[e], 50) / 1e6, "p95": nearest_rank(walls[e], 95) / 1e6} for e in ENGINES},
    }


def failed_fixture(fx: Fixture, samples: int, warmup: int, pairs: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    return {"id": fx.id, "digest": fixture_digest(fx), "status": "failed", "reason": reason, "samples": samples, "warmup_pairs": warmup,
            "skipped": samples - len(pairs), "expect": {"rc": fx.expect_rc, "status": fx.expect_status}, "pairs": list(pairs)}


def leg_env(sdir: Path, claude: Path, tmp: Path) -> dict[str, str]:
    """The child environment both engines share: the caller's environment without any SWARM_, GSD_ or SUBSTRATE_ variable, plus a fresh
    SWARM_DIR, no GSD state mirror, a scratch Claude state directory and the benchmark signing key. GSD_ENGINE is added to the run only."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_", "GSD_", "SUBSTRATE_"))}
    env.update({"SWARM_DIR": str(sdir), "SWARM_GSD_STATE_FILE": "", "SWARM_CLAUDE_STATE_DIR": str(claude),
                "SWARM_SIGNING_KEY": BENCH_SIGNING_KEY, "TMPDIR": str(tmp)})
    return env


def _plan_argv(fx: Fixture, repo: Path, root: Path) -> list[str]:
    orch_plan = [sys.executable, str(REPO / "scripts" / "orch_plan.py")]
    if fx.mode == "a2":
        return orch_plan + ["--brief-text", A2_BRIEF, "--pattern", "feature", "--risk-class", "medium", "--json"]
    plan = root / "plan.json"
    plan.write_text(json.dumps(_plan(fx.tasks), sort_keys=True), encoding="utf-8")
    return orch_plan + ["--plan", str(plan), "--prefix", "T", "--repo", str(repo), "--risk-class", "low", "--json"]


def setup_fixture(fx: Fixture, repo: Path, root: Path, env: dict[str, str]) -> None:
    """The fixture's store, planned by scripts/orch_plan.py on the leg's own SWARM_DIR. Runs before the clock; a failure refuses the sample."""
    done = subprocess.run(_plan_argv(fx, repo, root), cwd=REPO, env=env, capture_output=True, text=True, check=False, timeout=900)
    if done.returncode != 0:
        raise Refused(f"setup: orch_plan exited {done.returncode}: {done.stderr.strip()[-300:]}")


def run_argv(fx: Fixture, repo: Path, omp_bin: Path) -> list[str]:
    swarm_run = [sys.executable, str(REPO / "scripts" / "swarm_run.py")]
    if fx.mode == "a2":
        return swarm_run + ["--dry-run", "--json"]  # tests/perf/run.sh path (a), as run.sh runs it
    if fx.mode == "omp":
        return swarm_run + ["--runtime", "omp", "--omp-bin", str(omp_bin), "--repo", str(repo), "--json"]
    return swarm_run + ["--dry-run", "--once", "--max-parallel", str(READY_PASS_PARALLEL), "--repo", str(repo), "--json"]


def fake_omp(work: Path) -> Path:
    path = work / "fake-omp" / "omp"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n" + _FAKE_OMP, encoding="utf-8")
    path.chmod(0o755)
    return path


def make_leg(fx: Fixture, work: Path, omp_bin: Path) -> LegFn:
    """One leg end to end: a fresh SWARM_DIR and repository, the store planned (untimed), the timed swarm_run subprocess, then the reading (untimed)."""

    def leg(engine: str, tag: str) -> Observation:
        root = work / fx.id / f"{engine}-{tag}"
        shutil.rmtree(root, ignore_errors=True)
        sdir, repo, claude = root / "swarm", root / "repo", root / "claude-state"
        for d in (sdir, repo, claude):
            d.mkdir(parents=True)
        env = leg_env(sdir, claude, root)
        setup_fixture(fx, repo, root, env)
        run_env = {**env, "GSD_ENGINE": engine}
        if engine == "rust":
            binary = gsd_engine_binary()
            if not binary.is_file():
                raise Refused(f"rust leg: gsd_engine binary absent at {binary}; build it or set GSD_ENGINE_BIN")
            # leg_env strips GSD_* variables, and SWARM_SIGNING_KEY makes the selector require the binary's digest.
            run_env.update({"GSD_ENGINE_BIN": str(binary), "GSD_ENGINE_BIN_SHA256": _sha(binary)})
        run = execute_leg(run_argv(fx, repo, omp_bin), REPO, run_env)
        obs = observe(engine, run, sdir)
        shutil.rmtree(root, ignore_errors=True)
        return obs

    return leg


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def run_paired(json_out: Path, samples: int, warmup: int, watcher_log: str | None, work: Path) -> int:
    """Measures every fixture and writes the raw record to `json_out`. Any failure, including an unexpected exception, becomes the run's
    reason and nothing after it is measured; the record is written in every case. Returns 0 only for a complete run."""
    started_epoch = time.time()
    at_start = watcher_state(watcher_log)  # M2: the quiet line's log as the run starts
    record: dict[str, Any] = {
        "kind": RECORD_KIND, "schema": RECORD_SCHEMA, "status": "failed", "reason": "run interrupted", "started": _utc(),
        "started_epoch": started_epoch,
        "protocol": {"samples": samples, "warmup": warmup,
                     "order": "alternating: the first pair of each fixture verifies the legs; warm-up pairs follow; measured pair j runs python first when j is even",
                     "clock": "perf_counter_ns around the swarm_run child only", "engines": list(ENGINES),
                     "required_fixtures": list(REQUIRED_FIXTURES), "load_every_s": LOAD_EVERY_S, "quiet_load": QUIET_LOAD,
                     "watcher_max_age_s": WATCHER_MAX_AGE_S},
        "fixtures": {},
    }
    refusal = ""
    loader: LoadSampler | None = None
    try:
        record["fingerprint"] = fingerprint_now()
        # Refused before the first fixture is timed: a stale, contended or non-A2 watcher line, or a debug build, must not spend the quiet
        # window and only then be refused at qualification.
        preflight = _build_reasons(record["fingerprint"]) + _watcher_reasons(
            {"at_start": at_start, "at_end": at_start, "last_line": at_start.get("last_line")}, started_epoch, [])
        load_now = _load1_now()
        if load_now >= QUIET_LOAD:
            preflight.append(f"host not quiet at start: the 1-minute load is {load_now:.2f}, at or above the quiet rule {QUIET_LOAD}")
        if preflight:
            raise Refused("preflight: " + "; ".join(preflight))
        loader = LoadSampler(watcher_log)
        loader.start()
        omp_bin = fake_omp(work)
        for fx in FIXTURES:
            pairs: list[dict[str, Any]] = []
            try:
                record["fixtures"][fx.id] = measure_fixture(fx, make_leg(fx, work, omp_bin), samples, warmup, pairs)
            except Exception as exc:  # noqa: BLE001 - any failure is the run's reason, and the record is still written
                refusal = f"{fx.id}: {type(exc).__name__}: {exc}"
                record["fixtures"][fx.id] = failed_fixture(fx, samples, warmup, pairs, refusal)
                break
    except Exception as exc:  # noqa: BLE001
        refusal = refusal or f"run: {type(exc).__name__}: {exc}"
    finally:
        if loader is not None:
            try:
                loader.stop()
            except Exception as exc:  # noqa: BLE001 - the load sampler must never mask the run's reason
                refusal = refusal or f"load sampler: {type(exc).__name__}: {exc}"
            record["load"] = {"readings": loader.readings}
        try:
            record["fingerprint_end"] = fingerprint_now()
        except Exception as exc:  # noqa: BLE001
            refusal = refusal or f"end fingerprint: {type(exc).__name__}: {exc}"
        at_end = watcher_state(watcher_log)
        record["watcher"] = {"log": watcher_log, "at_start": at_start, "at_end": at_end, "last_line": at_end["last_line"]}
        complete = not refusal and all(record["fixtures"].get(fx.id, {}).get("status") == "ok" for fx in FIXTURES)
        record["status"] = "ok" if complete else "failed"
        record["reason"] = "" if complete else (refusal or "run interrupted before every fixture was measured")
        record["finished"] = _utc()
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for fx in FIXTURES:
        rec = record["fixtures"].get(fx.id, {})
        if rec.get("status") == "ok":
            out(f"   {fx.id:<14} {len(rec['pairs'])} measured pairs; paired ratio p50 {rec['ratio']['p50']:.3f} p95 {rec['ratio']['p95']:.3f} "
                "(measured, not a verdict)")
        else:
            out(f"   {fx.id:<14} refused: {rec.get('reason', 'not run')}")
    out(f"record: {json_out} (status {record['status']}{'' if complete else ': ' + record['reason']})")
    return 0 if complete else 1


# ---- the qualifier: evidence in, one verdict out, fail closed -----------------------------------------------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _expected_order(j: int) -> list[str]:
    return [ENGINES[0], ENGINES[1]] if j % 2 == 0 else [ENGINES[1], ENGINES[0]]


def _build_reasons(fp: dict[str, Any]) -> list[str]:
    """A measurement is taken on a release build only: a debug gsd_engine, a dev-profile extension or a missing extension is refused
    before any fixture runs and again at qualification, so a debug build cannot qualify (review finding 4)."""
    reasons: list[str] = []
    binary = fp.get("binary") if isinstance(fp.get("binary"), dict) else {}
    path = str(binary.get("path") or "")
    if "/debug/" in path.replace("\\", "/"):
        reasons.append(f"debug build: the gsd_engine binary {path} is a debug build; measure with "
                       "cargo build -p gsd-adapter --bin gsd_engine --release --locked")
    debug = fp.get("extension_debug_sections")
    if not _is_int(debug):
        reasons.append("malformed record: no extension_debug_sections value")
    elif debug == -1:
        reasons.append("missing extension: no swarm/_native*.so to measure; build it with maturin develop --release --locked")
    elif debug != 0:
        reasons.append(f"debug build: the native extension carries {debug} debug section(s); measure after "
                       "maturin develop --release --locked")
    host = fp.get("host") if isinstance(fp.get("host"), dict) else {}
    if host.get("rustc") in (None, "", "unavailable"):
        reasons.append("unverified host: rustc did not report a version, so the toolchain of the measurement is unknown; "
                       "re-measure with rustc on PATH")
    return reasons


def _fingerprint_reasons(fp: Any, now: dict[str, Any], label: str) -> list[str]:
    """The fingerprint taken at the run's `label` (start or end) must equal the current one: sources, extension, binary, fixture inputs, host."""
    if not isinstance(fp, dict):
        return [f"malformed record: no {label} fingerprint object"]
    reasons: list[str] = []
    for key in ("source", "extension", "binary", "fixtures"):
        if fp.get(key) != now[key]:
            reasons.append(f"fingerprint mismatch: {key} at {label} (measured on other inputs; re-measure)")
    host = fp.get("host") if isinstance(fp.get("host"), dict) else {}
    for key in ("uname", "python", "nproc", "rustc"):
        if host.get(key) != now["host"][key]:
            reasons.append(f"fingerprint mismatch: host {key} at {label}")
    reasons += _build_reasons(fp)
    return reasons


def _watcher_reasons(watcher: Any, started: Any, readings: Any) -> list[str]:
    """The quiet line the record relies on: the A2 quiet line; its log written at most WATCHER_MAX_AGE_S before the run started
    (0 <= started - mtime <= bound); the log's mtime unchanged across the run; and the same line in every load reading."""
    if not isinstance(watcher, dict):
        return ["missing watcher result: the record has no watcher object"]
    at_start = watcher.get("at_start") if isinstance(watcher.get("at_start"), dict) else {}
    at_end = watcher.get("at_end") if isinstance(watcher.get("at_end"), dict) else {}
    last = watcher.get("last_line")
    reasons: list[str] = []
    if not isinstance(last, str) or not last:
        reasons.append("missing watcher result: no watch_quiet.sh line was recorded (A2_WATCHER_LOG unset or empty)")
    elif last.startswith("result: not quiet"):
        reasons.append(f"contended watcher: the last watcher line is not quiet: {last}")
    elif not last.startswith("result: quiet "):
        reasons.append(f"malformed watcher result: {last}")
    elif "rule: relaxed" in last:
        reasons.append(f"relaxed watcher: the quiet window used the relaxed rule, not A2: {last}")
    elif not last.endswith("rule: A2)"):
        reasons.append(f"non-A2 watcher: {last}")
    mtime = at_start.get("mtime_epoch")
    if not _is_num(mtime):
        reasons.append("missing watcher log mtime at run start")
    elif not _is_num(started):
        reasons.append("malformed record: no started_epoch")
    elif not 0 <= started - mtime <= WATCHER_MAX_AGE_S:
        reasons.append(f"stale or future watcher result: the quiet line's log was written {started - mtime:.0f} s before the run started "
                       f"(the bound is {WATCHER_MAX_AGE_S} s)")
    if at_end.get("mtime_epoch") != mtime:
        reasons.append("watcher log changed during the run: its mtime at the start and at the end differ")
    for r in readings if isinstance(readings, list) else []:
        seen = r.get("watcher") if isinstance(r, dict) else None
        if not isinstance(r, dict) or seen != last:
            reasons.append(f"watcher changed during the run: a load reading saw {seen!r}, the final line is {last!r}")
            break
    return reasons


def _load_reasons(load: Any) -> list[str]:
    readings = load.get("readings") if isinstance(load, dict) else None
    if not isinstance(readings, list) or not all(isinstance(r, dict) and _is_num(r.get("load1")) for r in readings):
        return ["malformed record: load readings are missing or not numbers"]
    reasons: list[str] = []
    if not {"before", "after"} <= {r.get("at") for r in readings}:
        reasons.append("insufficient load readings: the record needs readings before and after the run")
    hot = [r["load1"] for r in readings if r["load1"] >= QUIET_LOAD]
    if hot:
        reasons.append(f"load >= {QUIET_LOAD}: {len(hot)} of {len(readings)} readings at or above {QUIET_LOAD} (max {max(hot):.2f})")
    return reasons


def _pair_leg_reasons(fid: str, j: int, pr: dict[str, Any], want_dispatch: int | None) -> list[str]:
    """Per leg of one pair: no cancelled task and no cancelled run; a readable dispatch count, which for a ready pass must be the count the pass takes."""
    reasons: list[str] = []
    counts = pr.get("counts") if isinstance(pr.get("counts"), dict) else {}
    cancelled = pr.get("cancelled") if isinstance(pr.get("cancelled"), dict) else {}
    dispatched = pr.get("dispatched") if isinstance(pr.get("dispatched"), dict) else {}
    for e in ENGINES:
        leg_counts = counts.get(e)
        if not isinstance(leg_counts, dict):
            reasons.append(f"malformed pair {j} in fixture {fid}: no {e} counts")
            continue
        if leg_counts.get("CANCELLED"):
            reasons.append(f"cancelled task: fixture {fid} pair {j} {e} leg has {leg_counts.get('CANCELLED')!r} CANCELLED task(s)")
        if e not in cancelled or cancelled.get(e) is not None:
            reasons.append(f"cancelled run: fixture {fid} pair {j} {e} leg reports cancelled {cancelled.get(e)!r}")
        n = dispatched.get(e)
        if not _is_int(n):
            reasons.append(f"malformed pair {j} in fixture {fid}: no {e} dispatch count")
        elif want_dispatch is not None and n != want_dispatch:
            reasons.append(f"dispatch: fixture {fid} pair {j} {e} leg dispatched {n}, the one-round pass takes {want_dispatch}")
    return reasons


def _fixture_reasons(fid: str, fx: Any) -> list[str]:
    spec = next(f for f in FIXTURES if f.id == fid)
    if not isinstance(fx, dict):
        return [f"malformed fixture {fid}: not an object"]
    reasons: list[str] = []
    if fx.get("status") != "ok":
        reasons.append(f"fixture {fid}: status {fx.get('status')!r}: {fx.get('reason') or 'no reason recorded'}")
    if fx.get("skipped") != 0:
        reasons.append(f"fixture {fid}: skipped samples: {fx.get('skipped')!r}")
    if fx.get("warmup_pairs") != PAIRED_WARMUP:
        reasons.append(f"fixture {fid}: {fx.get('warmup_pairs')!r} warm-up pairs, the protocol takes {PAIRED_WARMUP}")
    parity = fx.get("parity") if isinstance(fx.get("parity"), dict) else {}
    if parity.get("ok") is not True:
        reasons.append(f"fixture {fid}: parity mismatch: {parity.get('mismatches') or 'not recorded'}")
    served = fx.get("served_by") if isinstance(fx.get("served_by"), dict) else {}
    if served.get("python") != ENGINES[0] or served.get("rust") != ENGINES[1]:
        reasons.append(f"fixture {fid}: native fallback or unknown engine: served_by {served}")
    pairs = fx.get("pairs")
    if not isinstance(pairs, list) or len(pairs) != PAIRED_SAMPLES:
        count = len(pairs) if isinstance(pairs, list) else "no"
        reasons.append(f"fixture {fid}: {count} measured pairs, the protocol takes {PAIRED_SAMPLES}")
        return reasons
    want_dispatch = min(spec.tasks, ready_dispatch_expectation()) if spec.mode == "ready" else None
    ratios: list[float] = []
    for j, pr in enumerate(pairs):
        if not isinstance(pr, dict) or not (_is_int(pr.get("python_ns")) and _is_int(pr.get("rust_ns"))) \
                or pr["python_ns"] <= 0 or pr["rust_ns"] <= 0:
            reasons.append(f"malformed pair {j} in fixture {fid}: needs positive integer python_ns and rust_ns")
            return reasons
        if pr.get("index") != j or pr.get("order") != _expected_order(j):
            reasons.append(f"pairing: fixture {fid} pair {j} has index {pr.get('index')!r} and order {pr.get('order')!r}; "
                           f"the alternation takes {_expected_order(j)}")
        if pr.get("python_rc") != spec.expect_rc or pr.get("rust_rc") != spec.expect_rc:
            reasons.append(f"failing sample: fixture {fid} pair {j} exited python {pr.get('python_rc')!r} and rust {pr.get('rust_rc')!r}, "
                           f"expected {spec.expect_rc}")
        reasons += _pair_leg_reasons(fid, j, pr, want_dispatch)
        ratios.append(pr["rust_ns"] / pr["python_ns"])
    p50, p95 = nearest_rank(ratios, 50), nearest_rank(ratios, 95)
    if not p50 < 1.0:
        reasons.append(f"fixture {fid}: paired p50 {p50:.3f} is not strictly below 1.00 (rust is not faster at the median)")
    if not p95 < 1.0:
        reasons.append(f"fixture {fid}: paired p95 {p95:.3f} is not strictly below 1.00")
    return reasons


def qualify_record(record: Any, now: dict[str, Any]) -> list[str]:
    """Every reason the record is not a complete quiet candidate with paired p50 and p95 strictly below 1.00 on each required fixture.
    An empty list qualifies. Evidence only: a qualifying record is not approval for a default change."""
    if not isinstance(record, dict):
        return ["malformed record: the top level is not a JSON object"]
    reasons: list[str] = []
    if record.get("kind") != RECORD_KIND or record.get("schema") != RECORD_SCHEMA:
        reasons.append(f"malformed record: kind and schema must be {RECORD_KIND!r} and {RECORD_SCHEMA}")
    if record.get("status") != "ok":
        reasons.append(f"run status {record.get('status')!r}: {record.get('reason') or 'no reason recorded'}")
    protocol = record.get("protocol") if isinstance(record.get("protocol"), dict) else {}
    if protocol.get("samples") != PAIRED_SAMPLES:
        reasons.append(f"wrong sample count: the record took {protocol.get('samples')!r} samples, the protocol takes {PAIRED_SAMPLES}")
    if protocol.get("warmup") != PAIRED_WARMUP:
        reasons.append(f"wrong warm-up count: the record took {protocol.get('warmup')!r}, the protocol takes {PAIRED_WARMUP}")
    reasons += _fingerprint_reasons(record.get("fingerprint"), now, "start")
    reasons += _fingerprint_reasons(record.get("fingerprint_end"), now, "end")
    load = record.get("load")
    readings = load.get("readings") if isinstance(load, dict) else None
    reasons += _watcher_reasons(record.get("watcher"), record.get("started_epoch"), readings)
    reasons += _load_reasons(load)
    fixtures = record.get("fixtures")
    if not isinstance(fixtures, dict):
        return reasons + ["malformed record: no fixtures object"]
    for fid in REQUIRED_FIXTURES:
        if fid not in fixtures:
            reasons.append(f"missing fixture {fid}")
        else:
            reasons += _fixture_reasons(fid, fixtures[fid])
    return reasons


def qualify(path: Path) -> int:
    """`--qualify-json`: exit 0 only for a complete quiet candidate. Every other case exits 1 and names its reasons."""
    record: Any = None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        reasons = qualify_record(record, fingerprint_now())
    except (OSError, ValueError) as exc:
        reasons = [f"missing or malformed record {path}: {exc}"]
    if reasons:
        out("qualify: NOT QUALIFIED; this record gives no verdict, and the default engine stays python")
        for reason in reasons:
            out(f"   - {reason}")
        return 1
    out("qualify: QUALIFIED as a complete quiet candidate (evidence for the S6 record, not approval for a default change)")
    for fid in REQUIRED_FIXTURES:
        ratios = pair_ratios(record["fixtures"][fid]["pairs"])
        out(f"   {fid:<14} paired p50 {nearest_rank(ratios, 50):.3f} p95 {nearest_rank(ratios, 95):.3f}")
    return 0


# ---- main ---------------------------------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--work", type=Path, default=None, help="work directory (default: a new directory under the temp dir)")
    parser.add_argument("--inputs", type=int, default=30, help="inputs per primitive (the p99 is the largest below 100)")
    parser.add_argument("--passes", type=int, default=7, help="passes per input; the minimum is kept")
    parser.add_argument("--loop-bench-log", type=Path, default=None, help="output of `cargo run --release --example loop_bench -p gsd-sched-core`, embedded in section 4")
    parser.add_argument("--save", type=Path, default=None, help="also write the report to this file")
    parser.add_argument("--skip-dry-run", action="store_true", help="skip section 5")
    parser.add_argument("--skip", default="", help="comma list of sections to leave out while debugging: prim, decision, ports, pool (also leaves out the synthesis)")
    parser.add_argument("--profile-child", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--scheduler-paired", action="store_true", help="paired Python/Rust scheduler runtimes (S5.2, S6): needs --json-out; see the module docstring")
    parser.add_argument("--json-out", type=Path, default=None, help="with --scheduler-paired: the raw paired record (written even when a sample refuses the run)")
    parser.add_argument("--samples", type=int, default=PAIRED_SAMPLES, help=f"measured pairs per fixture (the protocol takes {PAIRED_SAMPLES})")
    parser.add_argument("--warmup", type=int, default=PAIRED_WARMUP, help=f"warm-up pairs per fixture, discarded (the protocol takes {PAIRED_WARMUP})")
    parser.add_argument("--watcher-log", default=None, help="the watch_quiet.sh log (A2_WATCHER_LOG): recorded with the pairs and judged by --qualify-json")
    parser.add_argument("--qualify-json", type=Path, default=None, help="judge a paired record: exit 0 only for a complete quiet candidate with paired p50 and p95 strictly below 1.00; any other case exits 1 with reasons")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.profile_child:
        profile_child(args.profile_child)
        return 0
    if args.qualify_json is not None:
        return qualify(args.qualify_json)
    if args.scheduler_paired:
        if args.json_out is None:
            parser.error("--scheduler-paired needs --json-out PATH")
        if args.samples < 1 or args.warmup < 0:
            parser.error("--samples must be at least 1 and --warmup at least 0")
        work = (args.work or Path(tempfile.mkdtemp(prefix="sched_paired_"))).resolve()
        try:
            return run_paired(args.json_out, args.samples, args.warmup, args.watcher_log, work)
        finally:
            if args.work is None:
                shutil.rmtree(work, ignore_errors=True)
    work = (args.work or Path(tempfile.mkdtemp(prefix="sched_engines_"))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    env = load_env(work, max(3, args.inputs), max(2, args.passes))
    gc.collect()
    gc.disable()
    out("scheduler engines: CPU cost of the scheduler's own code, Python against swarm._native (Phase 14.4, D-P21)")
    out(f"python {sys.version.split()[0]}, repository {REPO}, work {work}, {env.inputs} inputs per row, min of {env.passes} passes")
    out(f"/proc/loadavg at start: {loadavg()}   (nproc {os.cpu_count()})")
    out()
    out("== 0. parity gate (nothing is timed until every row below says PARITY)")
    failures: list[str] = []
    if "gate" in args.skip.split(","):
        out("   (gate skipped by --skip while debugging: this run is NOT a valid measurement)")
    else:
        failures = corpus_gate(env)
        failures += section_order_ready(env, True)
        failures += section_claims(env, True)
        failures += section_aimd(env, True)
        failures += section_backoff(env, True)
        failures += section_drain(env, True)
        failures += section_decision(env, True)
    out("   port bodies (section 3) and the pool: Python only, no native counterpart to compare")
    out()
    if failures:
        out(f"PARITY FAILED ({len(failures)} mismatches, first: {failures[:5]}): no timing is meaningful, stopping")
        out(f"/proc/loadavg at end: {loadavg()}")
        save(args.save)
        return 1
    out("== 1. primitive latency: Python against swarm._native called from Python (the PyO3 call overhead is inside the native rows)")
    out(f"   [measured] thread CPU time, min of {env.passes} passes per input, median and p99 over {env.inputs} inputs; verdict 'native faster' needs p50 and p99 below 1.00x")
    out(f"   /proc/loadavg: {loadavg()}")
    out()
    skipped = {s for s in args.skip.split(",") if s}
    if "prim" not in skipped:
        section_order_ready(env, False)
        section_claims(env, False)
        section_aimd(env, False)
        section_backoff(env, False)
        section_drain(env, False)
    out(f"/proc/loadavg: {loadavg()}")
    out()
    out("== 2. the Python decision pass: the real Scheduler._schedule with every port stubbed to a trivial body")
    out("   (reconcile, dispatchable, log_phase, resolve_agent, parse_claims, leases, submit and telemetry are trivial; the ClaimTable, order_ready,")
    out("    select_batch, the AIMD limiter and ErrorHandler are the real code; select_batch is a module function of scripts/swarm_run.py)")
    out()
    if "decision" not in skipped:
        section_decision(env, False)
    out(f"/proc/loadavg: {loadavg()}")
    out()
    out("== 3. the port bodies that stay in Python under architecture B (real SQLite Task Store; stub run; no leases)")
    out()
    if "ports" not in skipped:
        section_ports(env)
    out(f"/proc/loadavg: {loadavg()}")
    out()
    out("== 3b. worker pool dispatch and the GIL attach probe")
    if "pool" not in skipped:
        section_pool(env)
    gil = gil_probe()
    if not skipped:
        synthesis(env, gil, args.loop_bench_log)
    if not args.skip_dry_run:
        out("== 5. the dry-run proxy: scripts/orch_plan.py then scripts/swarm_run.py --dry-run --json (13 tasks), fresh SWARM_DIR per run")
        cpu = dry_run_cpu(env, 3)
        out("   [measured] CPU of the process tree (RUSAGE_CHILDREN, user+sys) for three runs: " + ", ".join(f"{c:.2f} s" for c in cpu)
            + f"; median {statistics.median(cpu):.2f} s")
        dry_run_profile(env)
    out(f"/proc/loadavg at end: {loadavg()}")
    save(args.save)
    return 0


def save(path: Path | None) -> None:
    if path is not None:
        path.write_text("\n".join(_LINES) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
