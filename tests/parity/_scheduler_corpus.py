"""The seeded corpus and the oracle runner of the Rust-vs-Python scheduler parity test (Phase 14.4, unit S1.2).

Every scheduler primitive the Rust runtime ports has one *behaviour* here. A behaviour has three parts:

* a generator (`build_cases`): the inputs, hand-written edge cases first, then seeded random ones;
* the Python oracle (`run_oracle`): what the Python scheduler returns for one input, running the real code (`scripts/swarm_run.py`,
  `swarm/claims.py`, `swarm/limiter.py`, `swarm/retry.py`), never a copy of it;
* the native driver (`run_native`): the same input through the `swarm._native.sched_*` surface that unit S1.4 builds.

`build_vectors` joins the first two into the committed files `tests/parity/fixtures/scheduler/<behaviour>.json`
(`{"behaviour", "seed", "schema", "oracle", "cases": [{"id", "input", "expected"}]}`, one case per line, ASCII only, no secrets).
`.planning/research/scheduler-oracle/dump_oracle.py` writes them; the test compares both legs with them.

Determinism. SEED fixes every random choice: behaviour `b` draws from `random.Random(f"{SEED}/{b}")` (string seeds hash with SHA-512, so
the stream is the same on every CPython 3). Times are multiples of 0.25 and values come from small pools, so every float is exact. Sizes
(hand + random cases): see SIZES. A file stays under MAX_CASES cases so it can be reviewed.

The surface the native driver assumes (the plan's proposal, `14.4-PLAN.md` line 37; the choices the plan leaves open are marked *chosen*):

* `sched_order_ready(rows: list[tuple[str | None, int | None, float | None]]) -> list[int]`: row = (priority, dag_depth, created_at); the
  result lists the row indices in dispatch order (a stable permutation).
* `sched_normalize_claim(pattern) -> str` and `sched_claims_overlap(a, b) -> bool`: `ValueError` for an invalid pattern.
* `ClaimTable()`: `acquire(owner, claims: list[tuple[str, str]]) -> tuple[bool, list[tuple[str, str, str]]]` (*chosen*: a claim is
  `(pattern, mode)` with mode `"exclusive"` or `"shared"`; the table normalises the pattern; an invalid pattern or mode raises `ValueError`
  before anything changes; a conflict is `(holder, held_pattern, requested_pattern)` in Python's order: requested claims in order, holders
  in the order they first acquired, claims in acquisition order); `release(owner) -> int`; `held() -> dict[str, list[tuple[str, str]]]`
  (*chosen*: owner to `(pattern, mode)` list in acquisition order).
* `AimdLimiter(max_n, min_n, cooldown_s, retry_cap_s, streak_threshold)` (`ValueError` unless `1 <= min_n <= max_n` and the cooldown is
  finite and >= 0; `streak_threshold` 0 turns the circuit off) with `current()`, `on_success(now)`, `on_rate_limit(now, retry_after_s)`,
  `on_failure(now, task_id)` and `on_burst_hit(submitted_at, now, retry_after_s) -> bool` (*chosen*: True when the limit dropped, as
  Rust's `RateLimitGate`; False for a hit of the same burst, `submitted_at < last decrease`, and for a hit at the floor). `now` and
  `submitted_at` are monotonic seconds, >= 0 (Python's gate starts at 0.0); `retry_after_s` may be None, negative, NaN or infinite.
  `on_rate_limit` is the bare limiter (halves on every call), `on_burst_hit` the gate in front of it (`Scheduler._backpressure`).
* `sched_backoff_s(attempt, base_s, cap_s, jitter, unit) -> float` (`ValueError` for jitter outside [0, 1] or base/cap <= 0): `unit` is the
  port's `random.uniform(-1, 1)` sample; Python draws `uniform(-jitter, jitter)`, which the oracle replaces by `unit * jitter` (`UnitRng`)
  so both legs multiply the same two doubles.
* `sched_drain_plan(cancel_grace_s, shutdown_budget_s, kill_grace_max_s, stop_grace_s, kill_settle_s) -> tuple[float, float]` (*chosen*:
  `(grace_s, kill_grace_s)`), the arithmetic of `Scheduler._cancel_drain`. The oracle runs that method itself with a fake clock.

A non-finite number inside a vector is the string "NaN", "Infinity" or "-Infinity" (JSON has no such number); drivers turn it back with
`float`. Not covered: a NaN `created_at` (Python's sort order is then input dependent), a negative `submitted_at`, an env-resolved cap
that is not positive and finite (the Python helpers resolve env values before Rust sees a number).
"""

from __future__ import annotations

import importlib.util
import json
import os
import random
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from swarm.claims import Claim, ClaimTable, _footprint, normalize_pattern, overlaps, parse_claims  # noqa: E402
from swarm.limiter import AimdLimiter  # noqa: E402
from swarm.retry import backoff_s  # noqa: E402

SEED = 20261028
SCHEMA = 1
MAX_CASES = 400
VECTORS_ENV = "SWARM_SCHED_VECTORS_DIR"  # a directory of vector files to use instead of the committed one (rehearsals, mutation checks)
DEFAULT_VECTORS_DIR = REPO / "tests" / "parity" / "fixtures" / "scheduler"
OVERRIDE_ENV = "SWARM_SCHED_PARITY_NATIVE"  # a Python file standing in for swarm._native (rehearses the native leg before S1.4 lands)

BEHAVIOURS: tuple[str, ...] = (
    "order_ready", "priority_fallback", "claim_normalize", "claim_overlap", "claim_conflict", "claim_table", "parse_claims", "aimd",
    "backoff", "drain_plan",
)
ORACLE_SOURCE = {
    "order_ready": "scripts/swarm_run.py order_ready",
    "priority_fallback": "scripts/swarm_run.py PRIORITY_RANK.get(priority, P2) through order_ready",
    "claim_normalize": "swarm/claims.py normalize_pattern",
    "claim_overlap": "swarm/claims.py overlaps",
    "claim_conflict": "swarm/claims.py _footprint and ClaimTable.acquire of two owners",
    "claim_table": "swarm/claims.py ClaimTable",
    "parse_claims": "swarm/claims.py parse_claims",
    "aimd": "swarm/limiter.py AimdLimiter and scripts/swarm_run.py Scheduler._backpressure (fake clock)",
    "backoff": "swarm/retry.py backoff_s (injected unit sample)",
    "drain_plan": "scripts/swarm_run.py Scheduler._cancel_drain (fake clock)",
}
# What the native surface must provide for a behaviour (parse_claims stays Python: the `claims_for` port calls it).
NATIVE_NAMES = {
    "order_ready": ("sched_order_ready",),
    "priority_fallback": ("sched_order_ready",),
    "claim_normalize": ("sched_normalize_claim",),
    "claim_overlap": ("sched_claims_overlap",),
    "claim_conflict": ("ClaimTable",),
    "claim_table": ("ClaimTable",),
    "aimd": ("AimdLimiter",),
    "backoff": ("sched_backoff_s",),
    "drain_plan": ("sched_drain_plan",),
}
NATIVE_BEHAVIOURS = tuple(NATIVE_NAMES)
RANDOM_COUNTS = {
    "order_ready": 100, "claim_normalize": 100, "claim_overlap": 150, "claim_conflict": 120, "claim_table": 40, "aimd": 50,
    "backoff": 150, "drain_plan": 150,
}
RETRY_CAP_ENV = "SWARM_RETRY_CAP_S"

EXCL, SHARED = "exclusive", "shared"


# ---- the Python oracle plumbing --------------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def swarm_run_module() -> ModuleType:
    """`scripts/swarm_run.py` loaded the way `tests/test_scheduler_continuous.py` does."""
    spec = importlib.util.spec_from_file_location("swarm_run_scheduler_oracle", REPO / "scripts" / "swarm_run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Clock:
    """A settable monotonic clock: the `Clock` protocol of swarm.limiter plus what `swarm_run` reads as `time.monotonic`."""

    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.now


class UnitRng:
    """Stands in for `random` in `swarm.retry.backoff_s`: `uniform(-jitter, jitter)` answers `unit * jitter`, the product Rust forms."""

    def __init__(self, unit: float) -> None:
        self.unit = unit

    def uniform(self, a: float, b: float) -> float:
        return self.unit * b


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


@contextmanager
def _patched(target: Any, **attrs: Any) -> Iterator[None]:
    saved = {name: getattr(target, name) for name in attrs}
    for name, value in attrs.items():
        setattr(target, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(target, name, value)


@contextmanager
def _environ(name: str, value: str) -> Iterator[None]:
    saved = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


def _attempt(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[bool, Any]:
    """(True, value), or (False, tag): "ValueError", or "raised:<Type>" for anything else (a Rust panic is a BaseException)."""
    try:
        return True, fn(*args, **kwargs)
    except ValueError:
        return False, "ValueError"
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
        return False, "raised:" + type(exc).__name__


def _outcome(convert: Callable[[Any], Any], fn: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    """`{"ok": convert(result)}`, or `{"error": tag}` when the call raised (see `_attempt`)."""
    ok, value = _attempt(fn, *args, **kwargs)
    return {"ok": convert(value)} if ok else {"error": value}


# ---- order_ready and priority_fallback -------------------------------------------------------------------------------------------

_PRIORITY_PROBE = [["P0", 0, 0], ["P1", 0, 0], ["P2", 0, 0], ["P3", 0, 0]]
_PRIORITY_VARIANTS: list[str | None] = [
    "P0", "P1", "P2", "P3", None, "", "P4", "P9", "P10", "P-1", "p0", "p2", "p3", " P0", "P0 ", "P2\n", "P02", "0", "2", "high", "urgent",
    "PO", "\uff30\uff12", "P\u00b2", "\u00e9",
]


def _order_cases() -> list[tuple[str, list[list[Any]]]]:
    inf, ninf = "Infinity", "-Infinity"
    return [
        ("empty", []),
        ("single", [["P1", 2, 3.5]]),
        ("priority-p3-to-p0", [["P3", 0, 0], ["P2", 0, 0], ["P1", 0, 0], ["P0", 0, 0]]),
        ("priority-beats-depth", [["P1", 0, 0], ["P0", 9, 99]]),
        ("depth-beats-created", [["P1", 2, 1], ["P1", 1, 50], ["P1", 0, 99]]),
        ("created-breaks-the-tie", [["P1", 1, 30.5], ["P1", 1, 10.25], ["P1", 1, 20]]),
        ("all-equal-keeps-input-order", [["P2", 1, 5]] * 6),
        ("none-priority-is-p2", [["P3", 0, 0], [None, 0, 0], ["P1", 0, 0], ["P2", 0, 0]]),
        ("none-priority-ties-with-p2-then-depth", [["P2", 2, 0], [None, 1, 0], ["P2", 0, 0]]),
        ("unknown-priorities-are-p2", [["P3", 0, 0], ["P9", 0, 0], ["p0", 0, 0], ["", 0, 0], ["high", 0, 0], ["P1", 0, 0]]),
        ("unknown-priority-ties-stay-stable", [["zz", 0, 0], ["P2", 0, 0], [None, 0, 0], ["P2", 0, 0], ["yy", 0, 0]]),
        ("lowercase-p0-is-not-p0", [["p0", 0, 0], ["P0", 0, 0], ["P1", 0, 0]]),
        ("none-depth-and-created-are-zero", [["P1", None, None], ["P1", 0, 0], ["P1", 0, -1], ["P1", -1, 5]]),
        ("none-created-ties-with-zero", [["P1", 1, None], ["P1", 1, 0], ["P1", 1, 0.0], ["P1", 1, -0.0]]),
        ("int-and-float-created-tie", [["P1", 0, 2.0], ["P1", 0, 2], ["P1", 0, 1.5]]),
        ("negative-depth-and-created", [["P1", -2, -5.5], ["P1", -2, -6.5], ["P1", -3, 9], ["P1", 0, -100]]),
        ("created-infinities", [["P1", 0, inf], ["P1", 0, 1.0e18], ["P1", 0, ninf], ["P1", 0, inf]]),
        ("epoch-created", [["P1", 0, 1760000000.25], ["P1", 0, 1760000000.0], ["P1", 0, 1759999999.75]]),
        ("depth-ladder-in-three-priorities", [["P2", 3, 0], ["P0", 2, 0], ["P2", 1, 0], ["P0", 0, 0], ["P3", 0, 0], ["P1", 5, 0]]),
        ("reversed-creation-order", [["P2", 0, 9], ["P2", 0, 8], ["P2", 0, 7], ["P2", 0, 6]]),
        ("already-ordered", [["P0", 0, 1], ["P0", 1, 1], ["P1", 0, 1], ["P2", 0, 1], ["P3", 0, 1]]),
        ("large-batch-two-priorities", [[f"P{i % 2}", i % 3, float(40 - i)] for i in range(40)]),
        ("zero-depth-none-priority-created-order", [[None, 0, 3], [None, 0, 1], [None, 0, 2]]),
    ]


def _order_cases_random() -> list[list[list[Any]]]:
    rng = random.Random(f"{SEED}/order_ready")
    priorities: list[str | None] = ["P0", "P1", "P2", "P3", "P2", "P1", None, "P9", "p1", ""]
    depths = [None, 0, 0, 1, 2, 3]
    created: list[float | int | None] = [None, 0, 1, 1.5, 2, 2.0, 3.25, 100.5]
    cases = []
    for _ in range(RANDOM_COUNTS["order_ready"]):
        count = rng.randint(0, 24)
        cases.append([[rng.choice(priorities), rng.choice(depths), rng.choice(created)] for _ in range(count)])
    return cases


def _oracle_order_ready(inp: dict[str, Any]) -> list[int]:
    rows = [{"idx": i, "priority": p, "dag_depth": d, "created_at": float(c) if isinstance(c, str) else c} for i, (p, d, c) in enumerate(inp["rows"])]
    return [row["idx"] for row in swarm_run_module().order_ready(rows)]


def _native_order_ready(native: Any, inp: dict[str, Any]) -> Any:
    rows = [(p, d, float(c) if isinstance(c, str) else c) for p, d, c in inp["rows"]]
    ok, value = _attempt(native.sched_order_ready, rows)
    return [int(i) for i in value] if ok else value


def _oracle_priority_fallback(inp: dict[str, Any]) -> dict[str, Any]:
    mod = swarm_run_module()
    rank = mod.PRIORITY_RANK.get(inp["priority"], mod.PRIORITY_RANK["P2"])
    return {"rank": rank, "probe": _oracle_order_ready({"rows": [*_PRIORITY_PROBE, [inp["priority"], 0, 0]]})}


def _native_priority_fallback(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    return {"probe": _native_order_ready(native, {"rows": [*_PRIORITY_PROBE, [inp["priority"], 0, 0]]})}


# ---- claim_normalize, claim_overlap, claim_conflict, claim_table, parse_claims ---------------------------------------------------

_NORMALIZE_HAND = [
    "src/a.py", "src/", "src/**", "src/**/", "**", "**/", "./src/a.py", "src//a.py", "src/./a.py", "./", ".", "..", "src/..", "../src",
    "src/../a", "", "   ", "/", "/src", "//src", "~", "~root/x", "a/~b", "~/x", "C:", "c:foo", "C:/x", "Z:\\x", "1:x", "ab:c", "\u00e9:a",
    "\u00e4:/x", "src\\a\\b.py", "\\src", "src\\", "src\\\\a", "a\\..\\b", "  src/a.py  ", "\tsrc/a.py\n", "src/a b.py", "src /a", "a/**/**/b",
    "a/**/**", "**/**", "**/**/", "a/***/b", "a/**b/c", "*", "*.py", "?", "[ab]", "[a-z]/x", "a[\\]b", "src/[ab]/", ".hidden/", "a/.hidden",
    "...", "a/.../b", "\u00a0src/a\u00a0", "\u2003src\u2003", "\u3000a\u3000", "\u0085a\u0085", "\x1ca/b\x1d", "\x1fa\x1f", "\u200ba", "\ufeffa",
    "\u00e9/\u00e8", "UPPER/Case", "a/b/", "a//", "a/./", "./a/./b/./",
]
_SEGMENT_POOL = ["a", "b", "src", "*", "**", "?", "[ab]", "x.py", "..", ".", " ", "~", "C:", "", ".hidden", "a b", "[", "]"]
_SEPARATORS = ["/", "/", "/", "//", "\\", "/./"]
_PREFIXES = ["", "", "", "/", "./", " ", "~", "C:/", "\t"]
_SUFFIXES = ["", "", "", "/", "//", " ", "\n", "/**", "/**/"]

_OVERLAP_HAND = [
    ("same", "src/a.py", "src/a.py"), ("different-file", "src/a.py", "src/b.py"), ("star-vs-file", "src/*.py", "src/a.py"),
    ("star-vs-other-ext", "src/*.py", "src/a.txt"), ("star-does-not-cross-dirs", "src/*", "src/a/b.py"), ("doublestar-crosses-dirs", "src/**", "src/a/b.py"),
    ("doublestar-matches-zero-dirs", "src/**/x.py", "src/x.py"), ("doublestar-middle", "src/**/x.py", "src/a/b/x.py"),
    ("doublestar-vs-doublestar", "a/**/z", "a/b/**"), ("doublestar-alone", "**", "anything/at/all"), ("trailing-slash-is-doublestar", "src/", "src/a/b"),
    ("dir-vs-sibling-dir", "src/", "lib/"), ("bare-dir-vs-child", "src", "src/a.py"), ("bare-name-vs-same", "src", "src"),
    ("question-mark", "a?.py", "ab.py"), ("question-mark-needs-a-char", "a?.py", "a.py"), ("question-vs-star", "a?", "a*"),
    ("question-vs-literal-length", "a??", "ab"), ("star-both-sides", "*a", "b*"), ("star-prefix-suffix-mismatch", "a*b", "b*a"),
    ("star-empty-run", "a*b", "ab"), ("double-star-in-segment", "a**b", "axyb"), ("triple-star-in-segment", "a***b", "ab"),
    ("class-vs-literal", "[ab].py", "a.py"), ("class-vs-other-literal", "[ab].py", "z.py"), ("class-range", "[a-b]", "q"),
    ("class-negated-is-conservative", "[!a]", "a"), ("class-vs-class", "[ab]", "[cd]"), ("class-vs-question", "[ab]", "?"),
    ("class-needs-one-char", "[ab]", "ab"), ("class-with-leading-bracket", "[]ab]", "x"), ("class-with-leading-bracket-length", "[]ab]", "xy"),
    ("empty-class-is-literal", "[]", "a"), ("empty-class-literal-match", "[]", "[]"), ("class-closes-at-first-bracket", "[a]]", "a]"),
    ("class-closes-at-first-bracket-2", "[a]]", "ax]"), ("unclosed-bracket-literal", "a[", "a["), ("unclosed-bracket-vs-question", "a[b", "a?b"),
    ("unclosed-bracket-vs-literal", "a[b", "ab"), ("nested-bracket-class", "[[]", "x"), ("bracket-class-three", "[]]", "q"),
    ("backslash-splits-segments", "a[\\]b", "a[/]b"), ("backslash-is-slash", "a\\b", "a/b"), ("dot-segments-removed", "./a/./b", "a/b"),
    ("duplicate-slashes", "a//b", "a/b"), ("leading-dot-slash", "./src/", "src/x"), ("case-sensitive", "SRC/a", "src/a"),
    ("unicode-literal", "\u00e9*", "\u00e9a"), ("unicode-literal-mismatch", "\u00e9a", "ea"), ("depth-mismatch", "a/b", "a/b/c"),
    ("star-depth-mismatch", "*/b", "a/b/c"), ("doublestar-both", "**/a", "**/b"), ("doublestar-then-literal", "**/a.py", "src/**/a.py"),
    ("dir-doublestar-vs-file", "a/**", "a"), ("whitespace-trimmed", "  a/b  ", "a/b"), ("absolute-left", "/a", "a"), ("absolute-right", "a", "/a"),
    ("parent-left", "a/../b", "b"), ("empty-left", "", "a"), ("empty-right", "a", ""), ("tilde", "~/a", "a"), ("drive", "C:/a", "a"),
    ("hidden-vs-star", ".git/*", ".git/config"), ("star-dot-star", "*.*", "a.b"), ("star-dot-star-no-dot", "*.*", "ab"),
]
_GLOB_POOL = ["a", "b", "ab", "*", "?", "**", "a*", "*b", "a?", "[ab]", "[a-b]", "[!a]", "x.py", "*.py", "?.py", "*.*", "a**b", "src", "lib"]

_CONFLICT_HAND = [
    ("bare-exclusive-vs-child-exclusive", ["src/tools", EXCL], ["src/tools/x.py", EXCL]),
    ("bare-exclusive-vs-child-shared", ["src/tools", EXCL], ["src/tools/x.py", SHARED]),
    ("bare-shared-vs-child-exclusive", ["src/tools", SHARED], ["src/tools/x.py", EXCL]),
    ("child-exclusive-vs-bare-shared", ["src/tools/x.py", EXCL], ["src/tools", SHARED]),
    ("bare-shared-vs-child-shared", ["src/tools", SHARED], ["src/tools/x.py", SHARED]),
    ("bare-shared-vs-same-bare-exclusive", ["src/tools", SHARED], ["src/tools", EXCL]),
    ("bare-exclusive-vs-sibling-prefix", ["src/tools", EXCL], ["src/toolsx", EXCL]),
    ("bare-exclusive-vs-sibling", ["src/tools", EXCL], ["src/other/x.py", EXCL]),
    ("bare-exclusive-vs-bare-exclusive", ["src", EXCL], ["src/a", EXCL]),
    ("bare-with-glob-parent", ["src/*/tools", EXCL], ["src/a/tools/x.py", EXCL]),
    ("glob-last-segment-is-itself", ["src/*", EXCL], ["src/a/b.py", EXCL]),
    ("glob-last-segment-vs-file", ["src/*", EXCL], ["src/a.py", SHARED]),
    ("doublestar-is-itself", ["src/**", EXCL], ["src/a/b.py", EXCL]),
    ("class-last-segment-is-glob", ["src/[ab]", EXCL], ["src/a/x", EXCL]),
    ("question-last-segment-is-glob", ["src/?", EXCL], ["src/a/x", EXCL]),
    ("trailing-slash-is-directory", ["src/", EXCL], ["src/a/b.py", SHARED]),
    ("trailing-slash-shared-vs-shared", ["src/", SHARED], ["src/a/b.py", SHARED]),
    ("shared-shared-same", ["a.py", SHARED], ["a.py", SHARED]),
    ("exclusive-exclusive-same", ["a.py", EXCL], ["a.py", EXCL]),
    ("exclusive-shared-same", ["a.py", EXCL], ["a.py", SHARED]),
    ("disjoint-exclusive", ["a.py", EXCL], ["b.py", EXCL]),
    ("case-sensitive", ["A.py", EXCL], ["a.py", EXCL]),
    ("dot-slash-normalised", ["./src//a.py", EXCL], ["src/a.py", EXCL]),
    ("backslash-normalised", ["src\\a", EXCL], ["src/a/b", EXCL]),
    ("doublestar-everything", ["**", EXCL], ["x/y/z", SHARED]),
    ("doublestar-everything-shared", ["**", SHARED], ["x/y/z", SHARED]),
    ("bare-vs-class-glob", ["src/ab", EXCL], ["src/[ab]b", SHARED]),
    ("dot-segment-bare", ["src/.", EXCL], ["src/x", EXCL]),
    ("hidden-dir-bare", [".git", EXCL], [".git/config", SHARED]),
    ("invalid-left-pattern", ["/abs", EXCL], ["a", EXCL]),
    ("invalid-right-pattern", ["a", EXCL], ["../b", EXCL]),
    ("invalid-mode", ["a", "weird"], ["a", EXCL]),
    ("empty-pattern", ["", EXCL], ["a", EXCL]),
]


def _glob_pattern(rng: random.Random) -> str:
    segments = [rng.choice(_GLOB_POOL) for _ in range(rng.randint(1, 4))]
    return "/".join(segments) + ("/" if rng.random() < 0.15 else "")


def _oracle_claim_normalize(inp: dict[str, Any]) -> dict[str, Any]:
    return _outcome(str, normalize_pattern, inp["pattern"])


def _native_claim_normalize(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    return _outcome(str, native.sched_normalize_claim, inp["pattern"])


def _oracle_claim_overlap(inp: dict[str, Any]) -> dict[str, Any]:
    return _outcome(bool, overlaps, inp["a"], inp["b"])


def _native_claim_overlap(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    return _outcome(bool, native.sched_claims_overlap, inp["a"], inp["b"])


def _oracle_claim_conflict(inp: dict[str, Any]) -> dict[str, Any]:
    made, claims = _attempt(lambda: [Claim(*inp["a"]), Claim(*inp["b"])])
    if not made:
        return {"error": claims}
    table = ClaimTable()
    table.acquire("A", [claims[0]])
    return {"footprint_a": list(_footprint(claims[0])), "footprint_b": list(_footprint(claims[1])), "conflict": not table.acquire("B", [claims[1]]).ok}


def _native_claim_conflict(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    table = native.ClaimTable()
    first_ok, first = _attempt(table.acquire, "A", [tuple(inp["a"])])
    second_ok, second = _attempt(table.acquire, "B", [tuple(inp["b"])])
    if not first_ok or not second_ok:
        return {"error": first if not first_ok else second}
    return {"conflict": not second[0]}


def _table_scenarios_hand() -> list[tuple[str, list[dict[str, Any]]]]:
    # ("acquire", owner, (pattern, mode), ...), ("release", owner) and ("held",)
    raw = [
        ("disjoint-owners-both-acquire", [("acquire", "A", ("src/**", EXCL)), ("acquire", "B", ("lib/**", EXCL)), ("held",)]),
        ("exclusive-overlap-names-the-holder", [("acquire", "A", ("src/**", EXCL)), ("acquire", "B", ("src/a.py", EXCL)), ("held",)]),
        ("shared-never-conflicts-with-shared", [("acquire", "A", ("src/**", SHARED)), ("acquire", "B", ("src/a.py", SHARED)), ("held",)]),
        (
            "shared-conflicts-with-exclusive",
            [("acquire", "A", ("src/**", SHARED)), ("acquire", "B", ("src/a.py", EXCL)), ("acquire", "C", ("src/b.py", SHARED))],
        ),
        ("exclusive-then-shared-conflicts", [("acquire", "A", ("src/a.py", EXCL)), ("acquire", "B", ("src/*.py", SHARED))]),
        (
            "owner-never-conflicts-with-itself",
            [("acquire", "A", ("src/**", EXCL)), ("acquire", "A", ("src/a.py", EXCL)), ("acquire", "A", ("src/a.py", SHARED)), ("held",)],
        ),
        (
            "reacquire-is-a-noop",
            [("acquire", "A", ("a.py", EXCL)), ("acquire", "A", ("a.py", EXCL)), ("acquire", "A", ("a.py", EXCL), ("b.py", EXCL)), ("held",)],
        ),
        ("duplicate-inside-one-request", [("acquire", "A", ("a.py", EXCL), ("a.py", EXCL), ("./a.py", EXCL)), ("held",)]),
        ("same-pattern-two-modes-are-two-claims", [("acquire", "A", ("a.py", EXCL)), ("acquire", "A", ("a.py", SHARED)), ("held",)]),
        (
            "all-or-nothing-on-conflict",
            [
                ("acquire", "A", ("src/a.py", EXCL)),
                ("acquire", "B", ("lib/free.py", EXCL), ("src/a.py", EXCL), ("docs/free.md", EXCL)),
                ("held",),
                ("acquire", "C", ("lib/free.py", EXCL)),
            ],
        ),
        (
            "release-counts-and-is-idempotent",
            [("acquire", "A", ("a", EXCL), ("b", EXCL)), ("release", "A"), ("release", "A"), ("release", "never-held"), ("held",)],
        ),
        (
            "release-frees-the-claims",
            [("acquire", "A", ("src/**", EXCL)), ("acquire", "B", ("src/a.py", EXCL)), ("release", "A"), ("acquire", "B", ("src/a.py", EXCL)), ("held",)],
        ),
        ("release-only-the-owner", [("acquire", "A", ("a", EXCL)), ("acquire", "B", ("b", EXCL)), ("release", "A"), ("held",)]),
        ("empty-acquire-succeeds-and-holds-nothing", [("acquire", "A"), ("held",), ("acquire", "B", ("a", EXCL)), ("acquire", "B"), ("held",)]),
        (
            "bare-exclusive-name-reserves-its-subtree",
            [
                ("acquire", "A", ("src/tools", EXCL)),
                ("acquire", "B", ("src/tools/x.py", EXCL)),
                ("acquire", "B", ("src/toolsx", EXCL)),
                ("acquire", "C", ("src/tools/**", SHARED)),
                ("held",),
            ],
        ),
        (
            "shared-bare-name-is-one-path",
            [("acquire", "A", ("src/tools", SHARED)), ("acquire", "B", ("src/tools/x.py", EXCL)), ("acquire", "C", ("src/tools", SHARED))],
        ),
        ("bare-name-vs-bare-name", [("acquire", "A", ("src", EXCL)), ("acquire", "B", ("src/a", EXCL)), ("acquire", "C", ("srcs", EXCL))]),
        (
            "first-conflict-owner-is-the-first-holder",
            [("acquire", "A", ("a/**", EXCL)), ("acquire", "B", ("a/b/**", SHARED)), ("acquire", "C", ("a/b/c", EXCL))],
        ),
        (
            "conflicts-are-listed-per-requested-claim",
            [("acquire", "A", ("a/x", EXCL)), ("acquire", "B", ("b/y", EXCL)), ("acquire", "C", ("b/y", EXCL), ("a/x", EXCL), ("a/*", EXCL))],
        ),
        (
            "holder-order-follows-first-acquisition",
            [("acquire", "A", ("a", EXCL)), ("acquire", "B", ("a/x", EXCL)), ("release", "A"), ("acquire", "A", ("a", EXCL)), ("acquire", "C", ("a", EXCL))],
        ),
        (
            "owner-claims-keep-acquisition-order",
            [("acquire", "A", ("z", EXCL)), ("acquire", "A", ("a", EXCL)), ("acquire", "A", ("m", SHARED)), ("held",)],
        ),
        (
            "patterns-are-held-normalised",
            [("acquire", "A", ("src/", EXCL), ("./lib//x", SHARED), ("a\\b", EXCL)), ("held",), ("acquire", "B", ("src/deep/f", EXCL))],
        ),
        ("conflict-reports-normalised-requested-pattern", [("acquire", "A", ("a/b", EXCL)), ("acquire", "B", ("./a//b", EXCL))]),
        (
            "invalid-pattern-changes-nothing",
            [("acquire", "A", ("good", EXCL), ("/abs", EXCL)), ("held",), ("acquire", "B", ("good", EXCL)), ("held",)],
        ),
        ("invalid-pattern-in-a-later-claim", [("acquire", "A", ("a", EXCL)), ("acquire", "B", ("b", EXCL), ("..", EXCL)), ("held",)]),
        ("invalid-mode-changes-nothing", [("acquire", "A", ("a", EXCL), ("b", "weird")), ("held",)]),
        ("empty-pattern-is-invalid", [("acquire", "A", ("", EXCL)), ("acquire", "A", ("  ", SHARED)), ("held",)]),
        (
            "conflict-then-owner-can-still-hold-its-own",
            [("acquire", "A", ("a", EXCL)), ("acquire", "B", ("a", EXCL)), ("acquire", "A", ("a/b", EXCL)), ("held",)],
        ),
        ("doublestar-blocks-everything-exclusive", [("acquire", "A", ("**", EXCL)), ("acquire", "B", ("x", SHARED)), ("acquire", "C", ("y/z", EXCL))]),
        ("class-overlap-is-conservative", [("acquire", "A", ("f[ab]", EXCL)), ("acquire", "B", ("fc", EXCL)), ("acquire", "C", ("g", EXCL))]),
    ]
    scenarios = []
    for slug, ops in raw:
        converted: list[dict[str, Any]] = []
        for op in ops:
            if op[0] == "acquire":
                converted.append({"op": "acquire", "owner": op[1], "claims": [list(claim) for claim in op[2:]]})
            elif op[0] == "release":
                converted.append({"op": "release", "owner": op[1]})
            else:
                converted.append({"op": "held"})
        scenarios.append((slug, converted))
    return scenarios


def _table_scenarios_random() -> list[list[dict[str, Any]]]:
    rng = random.Random(f"{SEED}/claim_table")
    owners = ["A", "B", "C", "D"]
    patterns = ["src/**", "src/a.py", "src/*.py", "src/tools", "lib/**", "lib/x/**", "docs/", "*.md", "README.md", "src/[ab]/*", "src/tools/x.py", "lib"]
    scenarios = []
    for _ in range(RANDOM_COUNTS["claim_table"]):
        ops: list[dict[str, Any]] = []
        for _ in range(rng.randint(4, 12)):
            roll = rng.random()
            if roll < 0.6:
                claims = [[rng.choice(patterns), rng.choice([EXCL, EXCL, SHARED])] for _ in range(rng.randint(0, 3))]
                ops.append({"op": "acquire", "owner": rng.choice(owners), "claims": claims})
            elif roll < 0.85:
                ops.append({"op": "release", "owner": rng.choice(owners)})
            else:
                ops.append({"op": "held"})
        scenarios.append(ops)
    return scenarios


def _held_view(held: Any) -> dict[str, list[list[str]]]:
    view: dict[str, list[list[str]]] = {}
    for owner, claims in held.items():
        view[owner] = [[claim.pattern, claim.mode] if isinstance(claim, Claim) else [claim[0], claim[1]] for claim in claims]
    return view


def _run_table(table: Any, ops: list[dict[str, Any]], oracle: bool) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for op in ops:
        if op["op"] == "acquire":
            claims_in: list[Any] = [tuple(c) for c in op["claims"]]
            if oracle:
                made, claims = _attempt(lambda raw=claims_in: [Claim(p, m) for p, m in raw])
                ok, value = _attempt(table.acquire, op["owner"], claims) if made else (False, claims)
            else:
                ok, value = _attempt(table.acquire, op["owner"], claims_in)
            if not ok:
                results.append({"error": value})
            elif oracle:
                results.append({"ok": value.ok, "conflicts": [[c.owner, c.held.pattern, c.requested.pattern] for c in value.conflicts]})
            else:
                results.append({"ok": bool(value[0]), "conflicts": [list(c) for c in value[1]]})
        elif op["op"] == "release":
            ok, value = _attempt(table.release, op["owner"])
            results.append({"released": int(value)} if ok else {"error": value})
        else:
            ok, value = _attempt(table.held)
            results.append({"held": _held_view(value)} if ok else {"error": value})
    return {"results": results, "final_held": _held_view(table.held())}


def _oracle_claim_table(inp: dict[str, Any]) -> dict[str, Any]:
    return _run_table(ClaimTable(), inp["ops"], True)


def _native_claim_table(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    return _run_table(native.ClaimTable(), inp["ops"], False)


_PARSE_HAND: list[tuple[str, Any]] = [
    ("notes-none", None), ("notes-not-a-dict", ["a"]), ("notes-string", "a"), ("no-claims-key", {"x": 1}), ("claims-none", {"claims": None}),
    ("claims-empty-list", {"claims": []}), ("claims-single-string", {"claims": "src/**"}), ("claims-list-of-strings", {"claims": ["a", "b/", "./c"]}),
    ("claims-dict-items", {"claims": [{"pattern": "a", "mode": "shared"}, {"pattern": "b"}, {"pattern": "c", "mode": "exclusive"}]}),
    ("claims-mixed", {"claims": ["a", {"pattern": "b", "mode": "shared"}]}), ("claims-not-a-list-dict", {"claims": {"pattern": "a"}}),
    ("claims-not-a-list-int", {"claims": 5}), ("claims-not-a-list-true", {"claims": True}), ("claims-item-int", {"claims": ["a", 3]}),
    ("claims-item-none", {"claims": [None]}), ("claims-item-list", {"claims": [["a", "shared"]]}), ("claim-dict-without-pattern", {"claims": [{"mode": "shared"}]}),
    ("claim-bad-mode", {"claims": [{"pattern": "a", "mode": "weird"}]}), ("claim-mode-none", {"claims": [{"pattern": "a", "mode": None}]}),
    ("claim-mode-case", {"claims": [{"pattern": "a", "mode": "SHARED"}]}), ("claim-pattern-int", {"claims": [{"pattern": 3}]}),
    ("claim-pattern-absolute", {"claims": ["/etc"]}), ("claim-pattern-parent", {"claims": ["a", "../b"]}), ("claim-pattern-empty", {"claims": [""]}),
    ("claims-empty-string", {"claims": ""}), ("claim-pattern-whitespace", {"claims": ["  "]}), ("claims-extra-keys-ignored", {"claims": [{"pattern": "a", "extra": 1}]}),
    ("claims-normalised", {"claims": ["src/", "a\\b", "./x//y"]}),
]


def _oracle_parse_claims(inp: dict[str, Any]) -> dict[str, Any]:
    ok, value = _attempt(parse_claims, inp["notes"])
    return {"ok": [[c.pattern, c.mode] for c in value]} if ok else {"error": value}


# ---- aimd ------------------------------------------------------------------------------------------------------------------------

_AIMD_BASE = {"max_n": 8, "min_n": 1, "cooldown_s": 10.0, "retry_cap_s": 120.0, "streak_threshold": 3}


def _aimd_scenarios_hand() -> list[tuple[str, dict[str, Any], list[list[Any]]]]:
    base = _AIMD_BASE
    rl = "rate_limit"
    return [
        ("halving-chain-to-the-floor", {**base, "max_n": 16}, [[rl, 100.0 + i, None] for i in range(6)]),
        ("floor-is-min-n", {**base, "max_n": 9, "min_n": 2}, [[rl, 100.0, None], [rl, 101.0, None], [rl, 102.0, None], ["current"]]),
        ("odd-halving-floors", {**base, "max_n": 7}, [[rl, 100.0, None], [rl, 101.0, None], [rl, 102.0, None]]),
        (
            "growth-waits-for-the-cooldown",
            base,
            [[rl, 100.0, None], ["success", 105.0], ["success", 109.5], ["success", 110.0], ["success", 111.0], ["success", 112.0], ["success", 113.0], ["success", 114.0], ["success", 115.0]],
        ),
        ("zero-cooldown-grows-at-once", {**base, "cooldown_s": 0.0}, [[rl, 100.0, None], ["success", 100.0], ["success", 100.0], ["success", 100.0]]),
        ("retry-after-longer-than-cooldown", base, [[rl, 100.0, 30.0], ["success", 120.0], ["success", 129.75], ["success", 130.0], ["success", 130.0]]),
        ("retry-after-shorter-than-cooldown", base, [[rl, 100.0, 3.0], ["success", 109.75], ["success", 110.0]]),
        ("retry-after-clamped-to-the-cap", {**base, "retry_cap_s": 50.0}, [[rl, 100.0, 1000000000.0], ["success", 149.5], ["success", 150.0]]),
        ("retry-after-infinity-clamps-to-the-cap", base, [[rl, 100.0, "Infinity"], ["success", 219.5], ["success", 220.0]]),
        ("retry-after-negative-infinity-is-ignored", base, [[rl, 100.0, "-Infinity"], ["success", 109.5], ["success", 110.0]]),
        ("retry-after-nan-is-ignored", base, [[rl, 100.0, "NaN"], ["success", 109.5], ["success", 110.0]]),
        ("retry-after-negative-is-ignored", base, [[rl, 100.0, -5.0], ["success", 109.5], ["success", 110.0]]),
        ("retry-after-zero-is-the-cooldown", base, [[rl, 100.0, 0.0], ["success", 109.5], ["success", 110.0]]),
        ("retry-after-none-is-the-cooldown", base, [[rl, 100.0, None], ["success", 109.5], ["success", 110.0]]),
        ("later-signal-never-shortens-the-hold", base, [[rl, 100.0, 100.0], [rl, 110.0, None], ["success", 150.0], ["success", 199.75], ["success", 200.0]]),
        ("later-signal-extends-the-hold", base, [[rl, 100.0, None], [rl, 105.0, 50.0], ["success", 150.0], ["success", 155.0]]),
        ("success-at-max-changes-nothing", base, [["success", 100.0], ["success", 101.0], ["current"]]),
        ("streak-trips-at-the-third-distinct-task", base, [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["failure", 100.0, "c"], ["failure", 100.0, "d"], ["failure", 100.0, "e"]]),
        ("one-task-failing-repeatedly-never-trips", base, [["failure", 100.0, "a"]] * 6),
        (
            "success-resets-and-rearms-the-circuit",
            base,
            [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["failure", 100.0, "c"], ["success", 101.0], ["failure", 102.0, "d"], ["failure", 102.0, "e"], ["failure", 102.0, "f"]],
        ),
        ("success-in-cooldown-resets-without-growing", base, [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["failure", 100.0, "c"], ["success", 105.0], ["success", 110.0]]),
        ("streak-halving-holds-growth", base, [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["failure", 100.0, "c"], ["success", 109.5], ["success", 110.0]]),
        ("streak-threshold-one", {**base, "streak_threshold": 1}, [["failure", 100.0, "a"], ["failure", 100.0, "a"], ["failure", 101.0, "b"], ["success", 102.0], ["failure", 103.0, "c"]]),
        ("streak-threshold-zero-is-off", {**base, "streak_threshold": 0}, [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["failure", 100.0, "c"], ["failure", 100.0, "d"]]),
        ("streak-threshold-two", {**base, "streak_threshold": 2}, [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["success", 101.0], ["failure", 102.0, "a"], ["failure", 102.0, "c"]]),
        ("streak-trip-at-the-floor", {**base, "max_n": 2}, [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["failure", 100.0, "c"], ["failure", 100.0, "d"], ["success", 110.0]]),
        (
            "burst-hit-is-deduped-for-sessions-submitted-before-the-decrease",
            base,
            [["burst_hit", 100.0, 101.0, None], ["burst_hit", 100.0, 102.0, None], ["burst_hit", 100.5, 102.0, None], ["burst_hit", 101.0, 103.0, None], ["burst_hit", 102.5, 104.0, None]],
        ),
        ("burst-hit-submitted-at-the-decrease-is-applied", base, [["burst_hit", 100.0, 101.0, 5.0], ["burst_hit", 101.0, 101.0, 5.0], ["burst_hit", 101.0, 102.0, None]]),
        (
            "burst-hit-at-the-floor-does-not-arm-the-gate",
            {**base, "max_n": 4, "min_n": 2},
            [["burst_hit", 100.0, 101.0, None], ["burst_hit", 100.0, 102.0, None], ["burst_hit", 101.5, 105.0, None], ["success", 120.0], ["burst_hit", 104.0, 121.0, None], ["burst_hit", 120.5, 122.0, None]],
        ),
        ("bare-rate-limit-does-not-arm-the-gate", base, [[rl, 100.0, None], ["burst_hit", 99.0, 101.0, None], ["burst_hit", 100.0, 102.0, None]]),
        ("burst-hit-extends-the-hold", base, [["burst_hit", 100.0, 101.0, 40.0], ["success", 130.0], ["success", 140.5], ["success", 141.0]]),
        ("burst-hit-retry-after-nan-and-inf", base, [["burst_hit", 100.0, 101.0, "NaN"], ["burst_hit", 101.0, 102.0, "Infinity"], ["success", 150.0], ["success", 222.0]]),
        ("burst-hit-retry-after-negative", base, [["burst_hit", 100.0, 101.0, -9.0], ["success", 110.5], ["success", 111.0]]),
        (
            "rate-limit-neither-counts-nor-resets-the-streak",
            base,
            [["failure", 100.0, "a"], ["failure", 100.0, "b"], ["burst_hit", 100.5, 101.0, None], ["failure", 102.0, "c"], ["success", 103.0]],
        ),
        ("zero-cooldown-burst-then-success", {**base, "cooldown_s": 0.0}, [["burst_hit", 100.0, 101.0, None], ["success", 101.0], ["burst_hit", 100.0, 102.0, None], ["burst_hit", 101.0, 102.0, None]]),
        ("single-slot-limiter", {**base, "max_n": 1}, [[rl, 100.0, None], ["success", 200.0], ["failure", 201.0, "a"], ["burst_hit", 202.0, 203.0, None]]),
        ("large-limiter", {**base, "max_n": 32, "min_n": 4}, [[rl, 100.0, None], [rl, 101.0, None], [rl, 102.0, None], [rl, 103.0, None], ["success", 120.0], ["success", 121.0]]),
        ("recovery-after-three-halvings", base, [[rl, 100.0, None], [rl, 101.0, None], [rl, 102.0, None]] + [["success", 112.0 + i] for i in range(9)]),
        ("ctor-max-zero", {**base, "max_n": 0, "min_n": 1}, []),
        ("ctor-min-above-max", {**base, "max_n": 2, "min_n": 3}, []),
        ("ctor-min-zero", {**base, "min_n": 0}, []),
        ("ctor-negative-cooldown", {**base, "cooldown_s": -1.0}, []),
        ("ctor-nan-cooldown", {**base, "cooldown_s": "NaN"}, []),
        ("ctor-infinite-cooldown", {**base, "cooldown_s": "Infinity"}, []),
        ("ctor-min-equals-max", {**base, "max_n": 3, "min_n": 3}, [[rl, 100.0, None], ["success", 200.0]]),
    ]


def _aimd_scenarios_random() -> list[tuple[dict[str, Any], list[list[Any]]]]:
    rng = random.Random(f"{SEED}/aimd")
    steps = [0.0, 0.0, 0.25, 0.5, 1.0, 5.0, 20.0, 70.0]
    retry_afters: list[float | str | None] = [None, None, 0.0, 5.0, 30.0, 200.0, 1000000000.0, "Infinity", "NaN", -3.0, "-Infinity"]
    tasks = ["T1", "T2", "T3", "T4", "T5"]
    scenarios = []
    for _ in range(RANDOM_COUNTS["aimd"]):
        max_n = rng.choice([1, 2, 4, 8, 9, 16, 32])
        params = {
            "max_n": max_n, "min_n": rng.randint(1, min(max_n, 3)), "cooldown_s": rng.choice([0.0, 5.0, 10.0, 60.0]),
            "retry_cap_s": rng.choice([1.0, 30.0, 120.0]), "streak_threshold": rng.choice([0, 1, 2, 3, 3, 5]),
        }
        now, ops = 100.0, []
        for _ in range(rng.randint(5, 24)):
            last_step = rng.choice(steps)
            now += last_step
            roll = rng.random()
            if roll < 0.3:
                ops.append(["success", now])
            elif roll < 0.45:
                ops.append(["rate_limit", now, rng.choice(retry_afters)])
            elif roll < 0.7:
                ops.append(["failure", now, rng.choice(tasks)])
            elif roll < 0.95:
                ops.append(["burst_hit", max(0.0, now - rng.choice([0.0, 0.25, 1.0, 5.0, 30.0])), now, rng.choice(retry_afters)])
            else:
                ops.append(["current"])
        scenarios.append((params, ops))
    return scenarios


def _oracle_aimd(inp: dict[str, Any]) -> dict[str, Any]:
    params, mod, clock = inp["params"], swarm_run_module(), _Clock(0.0)
    made, limiter = _attempt(AimdLimiter, params["max_n"], min_n=params["min_n"], cooldown_s=float(params["cooldown_s"]), clock=clock)
    if not made:
        return {"error": limiter}
    sched = object.__new__(mod.Scheduler)
    sched.limiter, sched.log, sched.cancel = limiter, [], SimpleNamespace(event=threading.Event())
    sched.tel, sched.mgr = SimpleNamespace(degraded=_noop, clear_degraded=_noop), SimpleNamespace(max_concurrent=params["max_n"])
    sched.fail_streak, sched._streak, sched._streak_tripped, sched._last_decrease = max(0, int(params["streak_threshold"])), set(), False, 0.0
    steps: list[dict[str, Any]] = []
    with _patched(mod, time=SimpleNamespace(monotonic=clock.monotonic)), _environ(RETRY_CAP_ENV, repr(float(params["retry_cap_s"]))):
        for op in inp["ops"]:
            kind, step = op[0], {}
            if kind == "success":
                clock.now = float(op[1])
                sched._backpressure(SimpleNamespace(rate_limited=None, submitted_at=0.0), False, {"task_id": "ok"}, "")
            elif kind == "rate_limit":
                clock.now = float(op[1])
                limiter.on_rate_limit(None if op[2] is None else float(op[2]))
            elif kind == "failure":
                clock.now = float(op[1])
                sched._backpressure(SimpleNamespace(rate_limited=None, submitted_at=0.0), True, {"task_id": op[2]}, "failed")
            elif kind == "burst_hit":
                clock.now, before = float(op[2]), limiter.current()
                hit = SimpleNamespace(rate_limited=0.0 if op[3] is None else float(op[3]), submitted_at=float(op[1]))
                sched._backpressure(hit, True, {"task_id": "hit"}, "rate limited")
                step["returned"] = limiter.current() < before
            steps.append({"current": limiter.current(), **step})
    return {"steps": steps}


def _native_aimd(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    p = inp["params"]
    made, limiter = _attempt(native.AimdLimiter, p["max_n"], p["min_n"], float(p["cooldown_s"]), float(p["retry_cap_s"]), p["streak_threshold"])
    if not made:
        return {"error": limiter}
    steps: list[dict[str, Any]] = []
    for op in inp["ops"]:
        kind, value, step = op[0], None, {}
        if kind == "success":
            _, value = _attempt(limiter.on_success, float(op[1]))
        elif kind == "rate_limit":
            _, value = _attempt(limiter.on_rate_limit, float(op[1]), None if op[2] is None else float(op[2]))
        elif kind == "failure":
            _, value = _attempt(limiter.on_failure, float(op[1]), op[2])
        elif kind == "burst_hit":
            ok, value = _attempt(limiter.on_burst_hit, float(op[1]), float(op[2]), None if op[3] is None else float(op[3]))
            step["returned"] = bool(value) if ok else value
        ok, current = _attempt(limiter.current)
        steps.append({"current": int(current) if ok else current, **step})
    return {"steps": steps}


# ---- backoff ---------------------------------------------------------------------------------------------------------------------


def _backoff_cases_hand() -> list[tuple[str, dict[str, Any]]]:
    overrides: list[tuple[str, dict[str, Any]]] = [(f"doubling-attempt-{n}", {"attempt": n}) for n in range(1, 11)]
    overrides += [(f"attempt-{n}-is-attempt-1", {"attempt": n}) for n in (0, -1, -100)]
    overrides += [(f"exponent-capped-at-62-attempt-{n}", {"attempt": n, "base_s": 1.0, "cap_s": 1.0e30}) for n in (62, 63, 64, 65, 100, 1000, 2147483647)]
    overrides += [
        (f"jitter-quarter-unit-{u}", {"attempt": 3, "base_s": 1.0, "cap_s": 100.0, "jitter": 0.25, "unit": u})
        for u in (-1.0, -0.5, 0.0, 0.5, 1.0, 0.123456789, -0.987654321)
    ]
    wide = {"base_s": 1.0, "cap_s": 100.0}
    overrides += [
        ("jitter-zero-ignores-the-unit", {"attempt": 3, **wide, "jitter": 0.0, "unit": 0.9}),
        ("jitter-one-unit-minus-one-is-zero", {"attempt": 3, **wide, "jitter": 1.0, "unit": -1.0}),
        ("jitter-one-unit-one-is-clamped-to-the-cap", {"attempt": 2, "base_s": 1.0, "cap_s": 3.0, "jitter": 1.0, "unit": 1.0}),
        ("unit-above-one-is-clamped", {"attempt": 2, **wide, "jitter": 1.0, "unit": 2.0}),
        ("unit-below-minus-one-is-clamped-to-zero", {"attempt": 2, **wide, "jitter": 1.0, "unit": -2.0}),
        ("unit-nan", {"attempt": 2, **wide, "jitter": 0.5, "unit": "NaN"}),
        ("unit-nan-without-jitter", {"attempt": 2, **wide, "jitter": 0.0, "unit": "NaN"}),
        ("jitter-negative", {"jitter": -0.1}),
        ("jitter-above-one", {"jitter": 1.0000001}),
        ("jitter-two", {"jitter": 2.0}),
        ("jitter-nan", {"jitter": "NaN"}),
        ("jitter-infinite", {"jitter": "Infinity"}),
        ("jitter-negative-infinite", {"jitter": "-Infinity"}),
        ("jitter-exactly-one-is-valid", {"jitter": 1.0, "unit": 0.5}),
        ("base-zero", {"base_s": 0.0}),
        ("base-negative", {"base_s": -1.0}),
        ("cap-zero", {"cap_s": 0.0}),
        ("cap-negative", {"cap_s": -5.0}),
        ("base-and-cap-zero", {"base_s": 0.0, "cap_s": 0.0}),
        ("bad-jitter-wins-over-bad-base", {"base_s": -1.0, "jitter": 3.0}),
        ("base-above-cap", {"base_s": 10.0, "cap_s": 1.0}),
        ("base-equals-cap", {"attempt": 5, "base_s": 2.0, "cap_s": 2.0}),
        ("huge-base", {"base_s": 1.0e300, "cap_s": 5.0}),
        ("huge-base-and-huge-exponent", {"attempt": 62, "base_s": 1.0e300, "cap_s": 1.0e308}),
        ("tiny-base", {"attempt": 3, "base_s": 1.0e-300, "cap_s": 5.0}),
        ("tiny-cap", {"attempt": 3, "base_s": 0.1, "cap_s": 1.0e-3, "jitter": 0.25, "unit": 0.5}),
        ("default-contract-attempt-1", {"attempt": 1, "jitter": 0.25, "unit": 0.5}),
        ("default-contract-attempt-6", {"attempt": 6, "jitter": 0.25, "unit": -0.5}),
        ("default-contract-attempt-7-hits-the-cap", {"attempt": 7, "jitter": 0.25, "unit": 1.0}),
        ("jitter-pushes-over-the-cap", {"attempt": 6, "jitter": 0.25, "unit": 1.0}),
        ("integer-valued-seconds", {"attempt": 3, "base_s": 2.0, "cap_s": 100.0, "jitter": 0.5, "unit": 0.25}),
    ]
    default = {"attempt": 1, "base_s": 0.1, "cap_s": 5.0, "jitter": 0.0, "unit": 0.0}
    return [(slug, {**default, **override}) for slug, override in overrides]


def _backoff_cases_random() -> list[dict[str, Any]]:
    rng = random.Random(f"{SEED}/backoff")
    cases = []
    for _ in range(RANDOM_COUNTS["backoff"]):
        attempt = rng.randint(-3, 70) if rng.random() < 0.85 else rng.choice([100, 1000, 2147483647])
        cases.append(
            {
                "attempt": attempt, "base_s": rng.choice([0.01, 0.1, 0.25, 1.0, 2.5]), "cap_s": rng.choice([0.5, 1.0, 5.0, 30.0, 120.0]),
                "jitter": rng.choice([0.0, 0.1, 0.25, 0.5, 1.0]), "unit": rng.uniform(-1, 1),
            }
        )
    return cases


def _oracle_backoff(inp: dict[str, Any]) -> dict[str, Any]:
    return _outcome(
        float, backoff_s, inp["attempt"], base=float(inp["base_s"]), cap=float(inp["cap_s"]), jitter=float(inp["jitter"]), rng=UnitRng(float(inp["unit"]))
    )


def _native_backoff(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    return _outcome(float, native.sched_backoff_s, inp["attempt"], float(inp["base_s"]), float(inp["cap_s"]), float(inp["jitter"]), float(inp["unit"]))


# ---- drain_plan ------------------------------------------------------------------------------------------------------------------


def _drain_cases_hand() -> list[tuple[str, dict[str, Any]]]:
    # overrides of the defaults: the SWARM_* values and constants of scripts/swarm_run.py (cancel grace 10, budget 15, kill 5, stop 10, settle 3)
    overrides: list[tuple[str, dict[str, Any]]] = [
        ("defaults", {}),
        ("roomy-budget-keeps-the-grace", {"shutdown_budget_s": 30.0}),
        ("budget-exactly-fits", {"shutdown_budget_s": 18.0}),
        ("budget-one-half-second-short", {"shutdown_budget_s": 17.5}),
        ("budget-equals-kill-plus-settle", {"shutdown_budget_s": 8.0}),
        ("budget-below-kill-plus-settle", {"shutdown_budget_s": 7.5}),
        ("budget-just-above-the-kill-floor", {"shutdown_budget_s": 3.5}),
        ("budget-equals-settle", {"shutdown_budget_s": 3.0}),
        ("budget-below-settle", {"shutdown_budget_s": 2.0}),
        ("budget-zero", {"shutdown_budget_s": 0.0}),
        ("budget-huge", {"shutdown_budget_s": 1000.0}),
        ("cancel-grace-zero", {"cancel_grace_s": 0.0, "shutdown_budget_s": 30.0}),
        ("cancel-grace-below-budget-room", {"cancel_grace_s": 3.0}),
        ("cancel-grace-fractional", {"cancel_grace_s": 2.5}),
        ("kill-grace-capped-by-stop-grace", {"shutdown_budget_s": 60.0, "kill_grace_max_s": 30.0, "stop_grace_s": 10.0}),
        ("kill-grace-capped-by-its-own-maximum", {"shutdown_budget_s": 60.0, "kill_grace_max_s": 7.5}),
        ("kill-grace-below-the-floor", {"shutdown_budget_s": 60.0, "kill_grace_max_s": 0.25}),
        ("stop-grace-below-the-floor", {"shutdown_budget_s": 60.0, "stop_grace_s": 0.25}),
        ("stop-grace-small", {"shutdown_budget_s": 60.0, "stop_grace_s": 2.0}),
        ("kill-grace-from-budget", {"shutdown_budget_s": 6.0}),
        ("settle-zero", {"kill_settle_s": 0.0}),
        ("settle-large", {"kill_settle_s": 10.0}),
        ("settle-larger-than-budget", {"kill_settle_s": 20.0}),
        ("all-zero-except-grace", {"shutdown_budget_s": 0.0, "kill_grace_max_s": 0.0, "stop_grace_s": 0.0, "kill_settle_s": 0.0}),
        ("tiny-everything", {"cancel_grace_s": 0.25, "shutdown_budget_s": 1.0, "kill_grace_max_s": 1.0, "stop_grace_s": 1.0, "kill_settle_s": 0.25}),
        ("long-shutdown-window", {"cancel_grace_s": 60.0, "shutdown_budget_s": 120.0, "kill_grace_max_s": 10.0, "stop_grace_s": 20.0, "kill_settle_s": 5.0}),
    ]
    default = {"cancel_grace_s": 10.0, "shutdown_budget_s": 15.0, "kill_grace_max_s": 5.0, "stop_grace_s": 10.0, "kill_settle_s": 3.0}
    return [(slug, {**default, **override}) for slug, override in overrides]


def _drain_cases_random() -> list[dict[str, Any]]:
    rng = random.Random(f"{SEED}/drain_plan")
    cases = []
    for _ in range(RANDOM_COUNTS["drain_plan"]):
        if rng.random() < 0.5:
            cancel, budget = rng.choice([0.0, 0.5, 1.0, 2.5, 3.0, 10.0, 30.0]), rng.choice([0.0, 1.0, 3.0, 3.5, 8.0, 8.5, 9.0, 10.0, 13.5, 15.0, 16.0, 18.0, 30.0, 120.0])
            kill_max, stop, settle = rng.choice([0.25, 1.0, 5.0, 7.5]), rng.choice([1.0, 2.0, 10.0]), rng.choice([0.0, 1.0, 3.0, 5.0])
        else:
            cancel, budget = rng.randint(0, 120) / 4, rng.randint(0, 240) / 4
            kill_max, stop, settle = rng.randint(1, 40) / 4, rng.randint(1, 80) / 4, rng.randint(0, 40) / 4
        cases.append({"cancel_grace_s": cancel, "shutdown_budget_s": budget, "kill_grace_max_s": kill_max, "stop_grace_s": stop, "kill_settle_s": settle})
    return cases


def _oracle_drain_plan(inp: dict[str, Any]) -> dict[str, Any]:
    mod, clock, seen = swarm_run_module(), _Clock(1000.0), {}

    def wait(_timeout: float = 0.25) -> set[Any]:
        clock.now += 1.0
        return set()

    def write_flag(_sdir: Any, _corr: str, _reason: str, grace_s: float) -> Path:
        seen["grace"] = float(grace_s)
        return Path(".")

    def kill(*, grace: float, reason: str) -> None:
        seen["kill_grace"] = float(grace)

    sched = object.__new__(mod.Scheduler)
    sched.cancel = SimpleNamespace(reason="swarm-timeout", at=clock.now, force=threading.Event(), event=threading.Event())
    sched.cancel.force.set()
    sched.cancel.event.set()
    sched.cancel_grace, sched.shutdown_budget = float(inp["cancel_grace_s"]), float(inp["shutdown_budget_s"])
    sched.tel, sched.inflight, sched.log, sched.sdir, sched.corr, sched._wait = SimpleNamespace(degraded=_noop), {"T-stuck": None}, [], Path("."), "c", wait
    with _patched(
        mod, time=SimpleNamespace(monotonic=clock.monotonic), write_cancel_flag=write_flag, kill_sessions=kill, KILL_GRACE_S=float(inp["kill_grace_max_s"]),
        STOP_GRACE_S=float(inp["stop_grace_s"]), KILL_SETTLE_S=float(inp["kill_settle_s"]),
    ):
        sched._cancel_drain()
    return {"grace": seen["grace"], "kill_grace": seen["kill_grace"]}


def _native_drain_plan(native: Any, inp: dict[str, Any]) -> dict[str, Any]:
    ok, value = _attempt(
        native.sched_drain_plan, float(inp["cancel_grace_s"]), float(inp["shutdown_budget_s"]), float(inp["kill_grace_max_s"]), float(inp["stop_grace_s"]),
        float(inp["kill_settle_s"]),
    )
    if not ok:
        return {"error": value}
    grace, kill_grace = value
    return {"grace": float(grace), "kill_grace": float(kill_grace)}


# ---- registry --------------------------------------------------------------------------------------------------------------------


def _hand_then_random(hand: list[tuple[str, Any]], random_inputs: list[Any], make: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
    cases = [{"id": f"hand-{i:03d}-{slug}", "input": make(value)} for i, (slug, value) in enumerate(hand)]
    cases += [{"id": f"rand-{i:03d}", "input": make(value)} for i, value in enumerate(random_inputs)]
    return cases


def _normalize_random() -> list[str]:
    rng = random.Random(f"{SEED}/claim_normalize")
    patterns = []
    for _ in range(RANDOM_COUNTS["claim_normalize"]):
        body = rng.choice(_SEPARATORS).join(rng.choice(_SEGMENT_POOL) for _ in range(rng.randint(1, 4)))
        patterns.append(rng.choice(_PREFIXES) + body + rng.choice(_SUFFIXES))
    return patterns


def _overlap_random() -> list[tuple[str, str]]:
    rng = random.Random(f"{SEED}/claim_overlap")
    pairs = []
    for _ in range(RANDOM_COUNTS["claim_overlap"]):
        pairs.append((_glob_pattern(rng), _glob_pattern(rng)))
    return pairs


def _conflict_random() -> list[tuple[list[Any], list[Any]]]:
    rng = random.Random(f"{SEED}/claim_conflict")
    pairs = []
    for _ in range(RANDOM_COUNTS["claim_conflict"]):
        pairs.append(([_glob_pattern(rng), rng.choice([EXCL, EXCL, SHARED])], [_glob_pattern(rng), rng.choice([EXCL, EXCL, SHARED])]))
    return pairs


def build_cases(behaviour: str) -> list[dict[str, Any]]:
    """The inputs of one behaviour: `{"id", "input"}` per case, hand-written first, then seeded random."""
    if behaviour == "order_ready":
        return _hand_then_random(_order_cases(), _order_cases_random(), lambda rows: {"rows": rows})
    if behaviour == "priority_fallback":
        return [{"id": f"hand-{i:03d}", "input": {"priority": v}} for i, v in enumerate(_PRIORITY_VARIANTS)]
    if behaviour == "claim_normalize":
        return _hand_then_random([("pattern", p) for p in _NORMALIZE_HAND], _normalize_random(), lambda p: {"pattern": p})
    if behaviour == "claim_overlap":
        return _hand_then_random([(slug, (a, b)) for slug, a, b in _OVERLAP_HAND], _overlap_random(), lambda ab: {"a": ab[0], "b": ab[1]})
    if behaviour == "claim_conflict":
        return _hand_then_random([(slug, (a, b)) for slug, a, b in _CONFLICT_HAND], _conflict_random(), lambda ab: {"a": ab[0], "b": ab[1]})
    if behaviour == "claim_table":
        return _hand_then_random(_table_scenarios_hand(), _table_scenarios_random(), lambda ops: {"ops": ops})
    if behaviour == "parse_claims":
        return _hand_then_random(_PARSE_HAND, [], lambda notes: {"notes": notes})
    if behaviour == "aimd":
        hand = [(slug, (params, ops)) for slug, params, ops in _aimd_scenarios_hand()]
        return _hand_then_random(hand, _aimd_scenarios_random(), lambda po: {"params": po[0], "ops": po[1]})
    if behaviour == "backoff":
        return _hand_then_random(_backoff_cases_hand(), _backoff_cases_random(), lambda case: case)
    if behaviour == "drain_plan":
        return _hand_then_random(_drain_cases_hand(), _drain_cases_random(), lambda case: case)
    raise KeyError(behaviour)


_ORACLES: dict[str, Callable[[dict[str, Any]], Any]] = {
    "order_ready": _oracle_order_ready, "priority_fallback": _oracle_priority_fallback, "claim_normalize": _oracle_claim_normalize,
    "claim_overlap": _oracle_claim_overlap, "claim_conflict": _oracle_claim_conflict, "claim_table": _oracle_claim_table,
    "parse_claims": _oracle_parse_claims, "aimd": _oracle_aimd, "backoff": _oracle_backoff, "drain_plan": _oracle_drain_plan,
}
_NATIVE: dict[str, Callable[[Any, dict[str, Any]], Any]] = {
    "order_ready": _native_order_ready, "priority_fallback": _native_priority_fallback, "claim_normalize": _native_claim_normalize,
    "claim_overlap": _native_claim_overlap, "claim_conflict": _native_claim_conflict, "claim_table": _native_claim_table, "aimd": _native_aimd,
    "backoff": _native_backoff, "drain_plan": _native_drain_plan,
}
# The part of an expected value the native surface can produce: the footprints are internal to the Rust table, the rank to `rank_or_p2`.
_NATIVE_VIEW: dict[str, Callable[[Any], Any]] = {
    "claim_conflict": lambda e: {"conflict": e["conflict"]} if "conflict" in e else e,
    "priority_fallback": lambda e: {"probe": e["probe"]},
}


def run_oracle(behaviour: str, inp: dict[str, Any]) -> Any:
    """What the Python scheduler returns for one input (JSON-able)."""
    return _ORACLES[behaviour](inp)


def run_native(native: Any, behaviour: str, inp: dict[str, Any]) -> Any:
    """What `swarm._native` (or a stand-in) returns for one input, in the shape of `native_view(expected)`."""
    return _NATIVE[behaviour](native, inp)


def native_view(behaviour: str, expected: Any) -> Any:
    """The part of a committed expected value the native leg compares."""
    return _NATIVE_VIEW.get(behaviour, lambda e: e)(expected)


# ---- vector files ----------------------------------------------------------------------------------------------------------------


def build_vectors(behaviour: str) -> dict[str, Any]:
    """The full vector document of one behaviour: the generated inputs and what the Python oracle returns for each."""
    cases = [{"id": case["id"], "input": case["input"], "expected": run_oracle(behaviour, case["input"])} for case in build_cases(behaviour)]
    return {"behaviour": behaviour, "seed": SEED, "schema": SCHEMA, "oracle": ORACLE_SOURCE[behaviour], "cases": cases}


def render_vectors(doc: dict[str, Any]) -> str:
    """The committed text of a vector document: valid JSON, ASCII only, one case per line, no NaN."""
    lines = ["{"]
    lines += [f'"{key}": {json.dumps(doc[key])},' for key in ("behaviour", "seed", "schema", "oracle")]
    lines.append('"cases": [')
    lines.append(",\n".join("  " + json.dumps(case, ensure_ascii=True, allow_nan=False) for case in doc["cases"]))
    lines += ["]", "}"]
    return "\n".join(lines) + "\n"


def vectors_dir() -> Path:
    """The directory the legs read: `SWARM_SCHED_VECTORS_DIR` when set, else the committed fixtures."""
    return Path(os.environ[VECTORS_ENV]) if os.environ.get(VECTORS_ENV) else DEFAULT_VECTORS_DIR


def load_vectors(behaviour: str, directory: Path | None = None) -> dict[str, Any]:
    with open((directory or vectors_dir()) / f"{behaviour}.json", encoding="utf-8") as handle:
        doc: dict[str, Any] = json.load(handle)
    return doc


# ---- the native leg's child process ----------------------------------------------------------------------------------------------


def native_child_main(directory: str) -> None:
    """Runs in a child process: loads `swarm._native` (or the stand-in named by OVERRIDE_ENV), runs every native behaviour over the vectors in
    `directory` and prints `{"import_error"}` or `{"missing": {behaviour: [names]}, "results": {behaviour: [value per case]}}` as JSON."""
    override = os.environ.get(OVERRIDE_ENV)
    try:
        if override:
            spec = importlib.util.spec_from_file_location("_sched_parity_native_override", override)
            assert spec is not None and spec.loader is not None
            native = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(native)
        else:
            from swarm._native_loader import load_native

            native = load_native()
    except ImportError as exc:
        print(json.dumps({"import_error": str(exc)[:400]}))
        return
    missing: dict[str, list[str]] = {}
    results: dict[str, list[Any]] = {}
    for behaviour in NATIVE_BEHAVIOURS:
        absent = [name for name in NATIVE_NAMES[behaviour] if not callable(getattr(native, name, None))]
        if absent:
            missing[behaviour] = absent
            continue
        results[behaviour] = [run_native(native, behaviour, case["input"]) for case in load_vectors(behaviour, Path(directory))["cases"]]
    print(json.dumps({"missing": missing, "results": results}))
