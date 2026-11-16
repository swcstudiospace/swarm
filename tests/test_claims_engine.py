"""`SWARM_CLAIMS_ENGINE`: the opt-in Rust claim table behind `swarm.claims.make_claim_table`, the scheduler's only table constructor.

Three layers. (1) The switch (every value, warn-once per reason, fallback reasons, `SWARM_PURE_PYTHON`) is proved with fake native modules
that follow the native contract over the Python oracle, so it runs on every host. (2) A native failure at ANY call of a script, before or after
the native table changed, leaves the caller with the oracle's answers and the claims held so far. (3) Differential tests drive the real
extension through `NativeClaimTable` and the Python `ClaimTable` side by side (the committed oracle vectors `claim_table.json`, hand-made
mode ambiguities, seeded random acquire/release sequences compared in full at every step), plus the thread tests. Layer (3) skips, with a
message naming `swarm._native`, only when the extension cannot be imported; run it with an interpreter that has it installed.
"""
from __future__ import annotations

import functools
import importlib
import logging
import random
import sys
import threading
import time
import types
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

import swarm.claims as C
from swarm.claims import EXCLUSIVE, SHARED, Claim, ClaimConflict, ClaimResult, ClaimTable, NativeClaimTable, make_claim_table

sys.path.insert(0, str(Path(__file__).resolve().parent / "parity"))
from _scheduler_corpus import _run_table, load_vectors  # noqa: E402  (the committed oracle vectors and their runner: not copied here)

NATIVE = "swarm._native"
LOGGER = "swarm.claims"
Ops = Sequence[tuple[Any, ...]]


class FakeNativeTable:
    """The contract of `swarm._native.ClaimTable` (`(pattern, mode)` in, `(holder, held_pattern, requested_pattern)` out) over the Python
    oracle, with optional failure injection: the `fail_at`-th call (0-based, any method) raises, before or after the table changed."""

    def __init__(self, fail_at: int | None = None, *, after: bool = False) -> None:
        self._table = ClaimTable()
        self.calls = 0
        self._fail_at = fail_at
        self._after = after

    def _count(self) -> bool:
        """Counts a call; True when this call must raise."""
        self.calls += 1
        return self._fail_at == self.calls - 1

    def acquire(self, owner: str, claims: list[tuple[str, str]]) -> tuple[bool, list[tuple[str, str, str]]]:
        fail = self._count()
        if fail and not self._after:
            raise RuntimeError("injected acquire failure")
        result = self._table.acquire(owner, [Claim(pattern, mode) for pattern, mode in claims])
        if fail:
            raise RuntimeError("injected acquire failure after the table changed")
        return result.ok, [(c.owner, c.held.pattern, c.requested.pattern) for c in result.conflicts]

    def release(self, owner: str) -> int:
        fail = self._count()
        if fail and not self._after:
            raise RuntimeError("injected release failure")
        count = self._table.release(owner)
        if fail:
            raise RuntimeError("injected release failure after the table changed")
        return count

    def held(self) -> dict[str, list[tuple[str, str]]]:
        if self._count():
            raise RuntimeError("injected held failure")
        return {owner: [(c.pattern, c.mode) for c in claims] for owner, claims in self._table.held().items()}


class FakeNative(types.ModuleType):
    """A stand-in for `swarm._native` whose `ClaimTable` records its constructions and builds `factory()`."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        super().__init__(NATIVE)
        self.built = 0
        self._factory = factory

    def ClaimTable(self) -> Any:  # noqa: N802 - the extension's own name
        self.built += 1
        return self._factory()


class ObservedTable:
    """Wraps a native table: `hooks[owner]` runs after the native acquire of that owner, `fail_held` makes `held` raise."""

    def __init__(self, inner: Any, hooks: dict[str, Callable[[], None]] | None = None) -> None:
        self._inner = inner
        self._hooks = hooks or {}
        self.fail_held = False

    def acquire(self, owner: str, claims: list[tuple[str, str]]) -> Any:
        result = self._inner.acquire(owner, claims)
        hook = self._hooks.get(owner)
        if hook is not None:
            hook()
        return result

    def release(self, owner: str) -> Any:
        return self._inner.release(owner)

    def held(self) -> Any:
        if self.fail_held:
            raise RuntimeError("injected held failure")
        return self._inner.held()


@pytest.fixture(autouse=True)
def _clean_engine_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the default engine, no extension and no warning reported yet."""
    monkeypatch.delenv(C._ENGINE_ENV, raising=False)
    monkeypatch.setattr(C, "_ENGINE_WARNED", set())
    monkeypatch.setitem(sys.modules, NATIVE, None)  # import of swarm._native fails unless a test installs a fake or the real module


@pytest.fixture
def rust(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SWARM_CLAIMS_ENGINE=rust` with the suite's `SWARM_PURE_PYTHON=1` lifted so the selector reaches the extension."""
    monkeypatch.setenv(C._ENGINE_ENV, "rust")
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)


@functools.cache
def _real_native() -> tuple[types.ModuleType | None, str]:
    """The real extension (or None and why not), imported once per process: PyO3 modules are not meant to be initialised twice."""
    saved = sys.modules.pop(NATIVE, None)
    try:
        return importlib.import_module(NATIVE), ""
    except ImportError as exc:
        return None, str(exc)
    finally:
        if saved is not None:
            sys.modules[NATIVE] = saved


@pytest.fixture
def real(monkeypatch: pytest.MonkeyPatch, rust: None) -> types.ModuleType:
    """The real `swarm._native`, installed as the module `make_claim_table` imports; the test skips when it cannot be imported."""
    module, why = _real_native()
    if module is None:
        pytest.skip(f"the extension {NATIVE} cannot be imported ({why}); build it (`maturin develop`) to run the native claim table legs")
    monkeypatch.setitem(sys.modules, NATIVE, module)
    return module


@pytest.fixture(params=["fake", "real"])
def native_class(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, rust: None) -> Callable[[], Any]:
    """A constructor of native tables: the contract fake, and the real class (skipped when the extension is absent)."""
    if request.param == "fake":
        return FakeNativeTable
    module, why = _real_native()
    if module is None:
        pytest.skip(f"the extension {NATIVE} cannot be imported ({why}); build it (`maturin develop`) to run the native claim table legs")
    native: Callable[[], Any] = module.ClaimTable
    return native


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def claim(pattern: str, mode: str = EXCLUSIVE) -> Claim:
    return Claim(pattern, mode)


# ---- the switch --------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "python"])
def test_default_engines_never_touch_native(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str | None) -> None:
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    fake = FakeNative(FakeNativeTable)
    monkeypatch.setitem(sys.modules, NATIVE, fake)
    if value is not None:
        monkeypatch.setenv(C._ENGINE_ENV, value)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        table = make_claim_table()
    assert type(table) is ClaimTable
    assert fake.built == 0
    assert warnings_of(caplog) == []


def test_rust_builds_one_native_table_per_call_and_wraps_it(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    fake = FakeNative(FakeNativeTable)
    monkeypatch.setitem(sys.modules, NATIVE, fake)
    first, second = make_claim_table(), make_claim_table()
    assert type(first) is NativeClaimTable and type(second) is NativeClaimTable
    assert fake.built == 2
    first.acquire("A", [claim("a")])
    assert second.held() == {}  # independent tables


def test_the_engine_is_read_per_call_not_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(FakeNativeTable))
    kinds = []
    for value in ("rust", "python", None, "rust"):
        if value is None:
            monkeypatch.delenv(C._ENGINE_ENV)
        else:
            monkeypatch.setenv(C._ENGINE_ENV, value)
        kinds.append(type(make_claim_table()))
    assert kinds == [NativeClaimTable, ClaimTable, ClaimTable, NativeClaimTable]


@pytest.mark.parametrize("value", ["Rust", "RUST", "rust ", "fast", "1", "true"])
def test_an_unknown_value_uses_python_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    fake = FakeNative(FakeNativeTable)
    monkeypatch.setitem(sys.modules, NATIVE, fake)
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    monkeypatch.setenv(C._ENGINE_ENV, value)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        tables = [make_claim_table(), make_claim_table()]
    assert [type(t) for t in tables] == [ClaimTable, ClaimTable]
    assert fake.built == 0
    messages = warnings_of(caplog)
    assert len(messages) == 1
    assert repr(value) in messages[0] and C._ENGINE_ENV in messages[0]


def test_pure_python_overrides_rust_with_one_warning(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeNative(FakeNativeTable)
    monkeypatch.setitem(sys.modules, NATIVE, fake)
    monkeypatch.setenv(C._ENGINE_ENV, "rust")
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        tables = [make_claim_table(), make_claim_table()]
    assert [type(t) for t in tables] == [ClaimTable, ClaimTable]
    assert fake.built == 0
    messages = warnings_of(caplog)
    assert len(messages) == 1 and "SWARM_PURE_PYTHON=1" in messages[0]


@pytest.mark.parametrize("pure", ["0", "", "true"])
def test_only_the_exact_value_one_counts_as_pure_python(monkeypatch: pytest.MonkeyPatch, pure: str) -> None:
    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(FakeNativeTable))
    monkeypatch.setenv(C._ENGINE_ENV, "rust")
    monkeypatch.setenv("SWARM_PURE_PYTHON", pure)
    assert type(make_claim_table()) is NativeClaimTable


def test_a_missing_extension_falls_back_with_one_warning_and_no_import_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        first = make_claim_table()
        monkeypatch.setitem(sys.modules, NATIVE, FakeNative(FakeNativeTable))  # appears later: the negative cache still answers Python
        second = make_claim_table()
    assert type(first) is ClaimTable and type(second) is ClaimTable
    messages = warnings_of(caplog)
    assert len(messages) == 1
    assert C._ENGINE_ENV in messages[0] and "swarm._native" in messages[0]


def test_a_stale_extension_without_claim_table_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    monkeypatch.setitem(sys.modules, NATIVE, types.ModuleType(NATIVE))  # a module with no ClaimTable
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        tables = [make_claim_table(), make_claim_table()]
    assert [type(t) for t in tables] == [ClaimTable, ClaimTable]
    messages = warnings_of(caplog)
    assert len(messages) == 1 and "no ClaimTable" in messages[0]


def test_a_native_constructor_failure_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    def boom() -> Any:
        raise RuntimeError("constructor exploded")

    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(boom))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        tables = [make_claim_table(), make_claim_table()]
    assert [type(t) for t in tables] == [ClaimTable, ClaimTable]
    messages = warnings_of(caplog)
    assert len(messages) == 1 and "RuntimeError" in messages[0] and "constructor exploded" not in messages[0]


def test_each_reason_warns_once_and_reasons_are_independent(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        monkeypatch.setenv(C._ENGINE_ENV, "turbo")
        make_claim_table()
        make_claim_table()
        monkeypatch.setenv(C._ENGINE_ENV, "rust")
        monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
        make_claim_table()
        make_claim_table()
        monkeypatch.delenv("SWARM_PURE_PYTHON")
        make_claim_table()  # the extension is missing
        make_claim_table()
    messages = warnings_of(caplog)
    assert len(messages) == 3
    assert "'turbo'" in messages[0] and "SWARM_PURE_PYTHON=1" in messages[1] and "swarm._native is unavailable" in messages[2]


# ---- a native failure in the middle of a table's life -------------------------------------------------------------------------------

SCRIPT: Ops = (
    ("acquire", "A", [claim("src/**"), claim("docs/", SHARED)]),
    ("acquire", "B", [claim("lib/x", SHARED), claim("src/a.py")]),  # conflicts with A
    ("acquire", "B", [claim("lib/x", SHARED), claim("lib/y")]),
    ("held",),
    ("acquire", "C", [claim("lib/x"), claim("docs/readme.md")]),  # conflicts with B (lib/x) and A (docs/)
    ("release", "A"),
    ("acquire", "C", [claim("src/tools"), claim("docs/readme.md", SHARED)]),
    ("acquire", "D", [claim("src/tools/x.py", SHARED)]),  # under C's exclusive bare name
    ("release", "A"),
    ("held",),
    ("acquire", "B", [claim("lib/z", SHARED)]),
    ("release", "C"),
    ("held",),
)


def play(table: Any, ops: Ops) -> list[object]:
    """Runs `ops` on `table` and returns one comparable value per op: the whole ClaimResult, the release count or the held view with its order."""
    out: list[object] = []
    for op in ops:
        if op[0] == "acquire":
            out.append(table.acquire(op[1], op[2]))
        elif op[0] == "release":
            out.append(table.release(op[1]))
        else:
            out.append(list(table.held().items()))
    return out


def test_the_script_has_conflicts_a_release_that_counts_and_a_release_that_does_not() -> None:
    results = play(ClaimTable(), SCRIPT)
    conflicts = [r for r in results if isinstance(r, ClaimResult) and not r.ok]
    assert len(conflicts) == 3 and [len(r.conflicts) for r in conflicts] == [1, 2, 1]
    assert [r for r in results if isinstance(r, int)] == [2, 0, 2]  # A, A again (nothing held), C


@pytest.mark.parametrize("after", [False, True], ids=["raises-before-change", "raises-after-change"])
@pytest.mark.parametrize("position", range(len(SCRIPT)))
def test_a_native_failure_at_any_call_keeps_the_oracles_answers_and_the_claims_held(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None, position: int, after: bool
) -> None:
    reference = play(ClaimTable(), SCRIPT)
    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(lambda: FakeNativeTable(fail_at=position, after=after)))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        table = make_claim_table()
        assert type(table) is NativeClaimTable
        assert play(table, SCRIPT) == reference
    messages = warnings_of(caplog)
    assert len(messages) == 1 and "RuntimeError" in messages[0] and "injected" not in messages[0]
    assert table._python is not None and table._native is None and table._journal == {}  # one-way: the Python table serves the rest


def test_a_second_native_failure_in_another_table_does_not_warn_again(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(lambda: FakeNativeTable(fail_at=0)))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(2):
            assert make_claim_table().acquire("A", [claim("a")]) == ClaimResult(True, ())
    assert len(warnings_of(caplog)) == 1


def test_a_native_answer_that_disagrees_with_the_journal_falls_back_to_the_oracle(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    class Liar(FakeNativeTable):
        def acquire(self, owner: str, claims: list[tuple[str, str]]) -> tuple[bool, list[tuple[str, str, str]]]:
            ok, raw = super().acquire(owner, claims)
            return ok, [("ghost", held, requested) for _holder, held, requested in raw]  # a holder the table never saw

    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(Liar))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        table = make_claim_table()
        table.acquire("A", [claim("a/**")])
        result = table.acquire("B", [claim("a/b")])
    assert result == ClaimResult(False, (ClaimConflict("A", claim("a/**"), claim("a/b")),))
    assert len(warnings_of(caplog)) == 1


def test_a_native_failure_message_is_bounded(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None) -> None:
    class Verbose(FakeNativeTable):
        def acquire(self, owner: str, claims: list[tuple[str, str]]) -> tuple[bool, list[tuple[str, str, str]]]:
            raise ValueError("x" * 5000)

    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(Verbose))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        make_claim_table().acquire("A", [claim("a")])
    (message,) = warnings_of(caplog)
    assert len(message) < 600


def test_native_error_log_names_only_the_exception_class(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    def boom() -> Any:
        raise RuntimeError("pattern-7f3c must not reach the log")

    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(boom))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert type(make_claim_table()) is ClaimTable
    (message,) = warnings_of(caplog)
    assert "swarm._native.ClaimTable failed (RuntimeError);" in message
    assert "pattern-7f3c" not in message


def test_native_error_log_omits_the_message_on_a_mid_run_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    class Leaky(FakeNativeTable):
        def acquire(self, owner: str, claims: list[tuple[str, str]]) -> tuple[bool, list[tuple[str, str, str]]]:
            raise RuntimeError("pattern-7f3c must not reach the log")

    monkeypatch.setitem(sys.modules, NATIVE, FakeNative(Leaky))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        table = make_claim_table()
        assert table.acquire("A", [claim("a")]) == ClaimResult(True, ())
    (message,) = warnings_of(caplog)
    assert "swarm._native.ClaimTable failed (RuntimeError);" in message
    assert "pattern-7f3c" not in message


# ---- the adapter against the Python table over the real extension ---------------------------------------------------------------------


def adapter_pair() -> tuple[NativeClaimTable, ClaimTable]:
    table = make_claim_table()
    assert type(table) is NativeClaimTable, "the real extension was expected to be selected"
    return table, ClaimTable()


def assert_never_fell_back(table: NativeClaimTable, caplog: pytest.LogCaptureFixture) -> None:
    assert table._python is None, "the native table failed and the adapter fell back: the legs below would then compare Python with Python"
    assert warnings_of(caplog) == []


def corpus_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = load_vectors("claim_table")["cases"]
    return cases


def test_the_committed_claim_table_vectors_are_not_degenerate() -> None:
    cases = corpus_cases()
    outcomes = [r for case in cases for r in case["expected"]["results"]]
    assert len(cases) >= 40
    assert sum(1 for r in outcomes if r.get("ok") is False) >= 20, "the vectors need refused acquires"
    assert sum(1 for r in outcomes if r.get("released", 0) > 0) >= 10 and sum(1 for r in outcomes if r.get("released") == 0) >= 3
    assert sum(1 for r in outcomes if "error" in r) >= 3, "the vectors need invalid patterns and modes"


@pytest.mark.parametrize("case", corpus_cases(), ids=lambda case: str(case["id"]))
def test_the_adapter_reproduces_every_committed_oracle_vector(real: types.ModuleType, caplog: pytest.LogCaptureFixture, case: dict[str, Any]) -> None:
    table, reference = adapter_pair()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        got = _run_table(table, case["input"]["ops"], True)
    assert got == _run_table(reference, case["input"]["ops"], True)
    assert got == case["expected"]
    assert_never_fell_back(table, caplog)


MODE_SCENARIOS: dict[str, Ops] = {
    # one holder with the same pattern in both modes: a requested shared claim collides with its exclusive one only
    "holder-holds-both-modes-of-a-pattern": (
        ("acquire", "A", [claim("a", SHARED), claim("a")]),
        ("acquire", "B", [claim("a", SHARED)]),
        ("acquire", "B", [claim("a")]),
        ("held",),
    ),
    # one request with the same pattern in both modes, in both orders, and a duplicate
    "request-has-both-modes-of-a-pattern": (
        ("acquire", "A", [claim("a", SHARED), claim("a")]),
        ("acquire", "B", [claim("a"), claim("a", SHARED)]),
        ("acquire", "B", [claim("a", SHARED), claim("a"), claim("a", SHARED)]),
        ("acquire", "B", [claim("a"), claim("a")]),
    ),
    "exclusive-holder-shared-request-and-back": (
        ("acquire", "A", [claim("lib/**")]),
        ("acquire", "B", [claim("lib/x", SHARED), claim("lib/y")]),
        ("release", "A"),
        ("acquire", "A", [claim("lib/**", SHARED)]),
        ("acquire", "B", [claim("lib/x", SHARED), claim("lib/y")]),
        ("acquire", "C", [claim("lib/x")]),
    ),
    "several-holders-per-requested-claim": (
        ("acquire", "A", [claim("src/a.py", SHARED), claim("src/b.py")]),
        ("acquire", "B", [claim("src/*.py", SHARED)]),
        ("acquire", "C", [claim("src/**", SHARED), claim("docs/")]),
        ("acquire", "D", [claim("src/a.py"), claim("src/b.py", SHARED), claim("docs/x")]),
    ),
    "bare-name-exclusive-reserves-its-subtree": (
        ("acquire", "A", [claim("src/tools")]),
        ("acquire", "B", [claim("src/tools/x.py", SHARED)]),
        ("acquire", "B", [claim("src/toolsx")]),
        ("acquire", "C", [claim("src/tools", SHARED)]),
        ("release", "A"),
        ("acquire", "A", [claim("src/tools", SHARED)]),
        ("acquire", "B", [claim("src/tools/x.py")]),  # a SHARED bare name is just that path
        ("acquire", "B", [claim("src/tools")]),
    ),
}


@pytest.mark.parametrize("name", MODE_SCENARIOS)
def test_the_adapter_names_the_same_claims_in_the_same_order_as_the_oracle(
    real: types.ModuleType, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    table, reference = adapter_pair()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        got = play(table, MODE_SCENARIOS[name])
    assert got == play(reference, MODE_SCENARIOS[name])
    assert_never_fell_back(table, caplog)


def test_a_requested_claim_is_matched_to_the_held_claim_of_its_own_mode(real: types.ModuleType) -> None:
    table, _ = adapter_pair()
    table.acquire("A", [claim("a", SHARED), claim("a")])
    assert table.acquire("B", [claim("a", SHARED)]) == ClaimResult(False, (ClaimConflict("A", claim("a"), claim("a", SHARED)),))
    assert table.acquire("B", [claim("a"), claim("a", SHARED)]) == ClaimResult(
        False,
        (
            ClaimConflict("A", claim("a", SHARED), claim("a")),
            ClaimConflict("A", claim("a"), claim("a")),
            ClaimConflict("A", claim("a"), claim("a", SHARED)),
        ),
    )


def test_the_first_conflict_owner_is_the_first_holder_to_have_acquired(real: types.ModuleType) -> None:
    """`select_batch` reports `claim-conflict:<conflicts[0].owner>`: the owner of the first collision in the oracle's order."""
    table, _ = adapter_pair()
    for owner, pattern in (("T-4", "a/x"), ("T-2", "a/y"), ("T-1", "a/z"), ("T-5", "b/x")):
        assert table.acquire(owner, [claim(pattern)]).ok
    taken = table.acquire("T-7", [claim("a/*"), claim("b/*")])
    assert [c.owner for c in taken.conflicts] == ["T-4", "T-2", "T-1", "T-5"]  # one requested claim, holders in acquisition order
    table.release("T-4")
    assert table.acquire("T-4", [claim("a/x")]).ok  # it holds again, but now it acquired last
    assert [c.owner for c in table.acquire("T-7", [claim("a/*")]).conflicts] == ["T-2", "T-1", "T-4"]
    assert [c.owner for c in table.acquire("T-7", [claim("b/*"), claim("a/y")]).conflicts] == ["T-5", "T-2"]  # requested claims in order
    assert table.acquire("T-4", [claim("a/x/deeper")]).ok  # an owner never conflicts with itself (its own bare a/x covers this)


def test_release_is_idempotent_and_counts_what_it_drops(real: types.ModuleType) -> None:
    table, _ = adapter_pair()
    table.acquire("A", [claim("a"), claim("b", SHARED), claim("a")])  # the repeated claim is held once
    table.acquire("B", [claim("c")])
    assert table.release("A") == 2
    assert table.release("A") == 0
    assert table.release("never-held") == 0
    assert table.held() == {"B": (claim("c"),)}
    assert table.acquire("C", [claim("a"), claim("b")]).ok  # what A held is free again
    assert table.release("B") == 1 and table.release("B") == 0


def test_an_empty_request_holds_nothing_and_a_held_snapshot_is_not_changed_by_later_calls(real: types.ModuleType) -> None:
    table, _ = adapter_pair()
    assert table.acquire("A", []) == ClaimResult(True, ())
    assert table.held() == {}
    table.acquire("A", [claim("a")])
    snapshot = table.held()
    table.release("A")
    table.acquire("B", [claim("b")])
    assert snapshot == {"A": (claim("a"),)} and table.held() == {"B": (claim("b"),)}
    assert all(type(claims) is tuple for claims in snapshot.values())


OWNERS = ["A", "B", "C", "D", "E", "F"]
PATTERNS = ["src/**", "src/a.py", "src/*.py", "src/tools", "lib/**", "lib/x/**", "docs/", "*.md", "README.md", "src/[ab]/*", "src/tools/x.py",
            "lib", "a", "a/b", "?", "**"]
SEED = 20261029


def random_script(rng: random.Random, length: int) -> list[tuple[Any, ...]]:
    ops: list[tuple[Any, ...]] = []
    for _ in range(length):
        roll = rng.random()
        if roll < 0.65:
            wanted = [claim(rng.choice(PATTERNS), rng.choice([EXCLUSIVE, SHARED])) for _ in range(rng.randint(0, 4))]
            if wanted and rng.random() < 0.3:
                wanted.append(claim(wanted[0].pattern, SHARED if wanted[0].mode == EXCLUSIVE else EXCLUSIVE))  # the same pattern in both modes
            ops.append(("acquire", rng.choice(OWNERS), wanted))
        else:
            ops.append(("release", rng.choice(OWNERS)))
    return ops


def test_seeded_random_sequences_agree_with_the_oracle_in_every_result_and_every_held_view(
    real: types.ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    rng = random.Random(SEED)
    steps = refused = multi = mixed = both_modes = 0
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(60):
            table, reference = adapter_pair()
            for op in random_script(rng, 40):
                got, want = play(table, [op])[0], play(reference, [op])[0]
                assert got == want, f"{op}: the adapter answered {got!r}, the oracle {want!r}"
                assert list(table.held().items()) == list(reference.held().items()), f"held view differs after {op}"
                steps += 1
                if isinstance(want, ClaimResult) and not want.ok:
                    refused += 1
                    multi += len(want.conflicts) >= 2
                    mixed += any(c.held.mode != c.requested.mode for c in want.conflicts)
                    both_modes += len({c.requested.mode for c in want.conflicts if c.requested.pattern == c.held.pattern}) == 2
            assert_never_fell_back(table, caplog)
    assert steps == 2400 and refused >= 300 and multi >= 100 and mixed >= 100 and both_modes >= 5, (steps, refused, multi, mixed, both_modes)


# ---- threads -------------------------------------------------------------------------------------------------------------------


def run_in_thread(fn: Callable[[], Any]) -> Any:
    box: list[Any] = []
    errors: list[BaseException] = []

    def target() -> None:
        try:
            box.append(fn())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the caller's thread
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(30)
    assert not thread.is_alive(), "a thread did not finish: the table deadlocked"
    if errors:
        raise errors[0]
    return box[0]


def test_a_table_made_in_one_thread_works_from_another_and_back(real: types.ModuleType, caplog: pytest.LogCaptureFixture) -> None:
    """The extension class is `frozen` (Send + Sync), not `unsendable`: no thread-affinity panic, in either direction."""
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        made_elsewhere = run_in_thread(make_claim_table)
        assert run_in_thread(lambda: made_elsewhere.acquire("A", [claim("a/**")])) == ClaimResult(True, ())
        assert made_elsewhere.acquire("B", [claim("a/b")]) == ClaimResult(False, (ClaimConflict("A", claim("a/**"), claim("a/b")),))
        assert run_in_thread(lambda: made_elsewhere.held()) == {"A": (claim("a/**"),)}
        assert run_in_thread(lambda: made_elsewhere.release("A")) == 1
        assert_never_fell_back(made_elsewhere, caplog)


def test_threads_contending_for_one_pattern_never_hold_it_together_and_leave_nothing_behind(
    real: types.ModuleType, caplog: pytest.LogCaptureFixture
) -> None:
    table = make_claim_table()
    assert type(table) is NativeClaimTable
    guard = threading.Lock()
    state = {"inside": 0, "most": 0, "won": 0, "lost": 0}
    failures: list[str] = []
    start = threading.Barrier(8)

    def worker(index: int) -> None:
        owner = f"T{index}"
        start.wait(10)
        for _ in range(40):
            taken = table.acquire(owner, [claim(f"own{index}/**"), claim("hot/**")])
            if not taken.ok:
                if not taken.conflicts or any(c.owner == owner or c.requested.pattern != "hot/**" or c.held.pattern != "hot/**" for c in taken.conflicts):
                    failures.append(f"{owner}: {taken}")
                with guard:
                    state["lost"] += 1
                time.sleep(0.0002)
                continue
            with guard:
                state["inside"] += 1
                state["won"] += 1
                state["most"] = max(state["most"], state["inside"])
            time.sleep(0.0005)
            with guard:
                state["inside"] -= 1
            assert table.release(owner) == 2

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not any(thread.is_alive() for thread in threads), "a worker did not finish: the table deadlocked"
        assert failures == []
        assert state["most"] == 1 and state["won"] > 0 and state["lost"] > 0, state
        assert table.held() == {}  # a refused acquire took nothing, a granted one was released
        assert_never_fell_back(table, caplog)


@pytest.mark.parametrize("backend", ["fake", "real"])
def test_the_journal_follows_the_order_the_native_table_saw_when_threads_interleave(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None, backend: str
) -> None:
    """A's native call returns, then A is slow before its journal entry; B must wait for A's turn instead of overtaking it. The held view
    after a native failure is rebuilt from the journal, so its owner order shows whether the two orders agree."""
    if backend == "real":
        module, why = _real_native()
        if module is None:
            pytest.skip(f"the extension {NATIVE} cannot be imported ({why}); build it (`maturin develop`) to run the native claim table legs")
        inner: Any = module.ClaimTable()
    else:
        inner = FakeNativeTable()
    native_saw_a = threading.Event()

    def slow() -> None:
        native_saw_a.set()
        time.sleep(0.3)

    observed = ObservedTable(inner, {"A": slow})
    table = NativeClaimTable(observed)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        first = threading.Thread(target=lambda: table.acquire("A", [claim("x/**")]))
        first.start()
        assert native_saw_a.wait(10)
        second = threading.Thread(target=lambda: table.acquire("B", [claim("y/**")]))
        second.start()
        first.join(30)
        second.join(30)
        assert not first.is_alive() and not second.is_alive()
        assert list(table.held()) == ["A", "B"]  # served by the native table
        observed.fail_held = True
        assert list(table.held()) == ["A", "B"]  # the native table failed: served by the Python table rebuilt from the journal
    assert len(warnings_of(caplog)) == 1
    assert table._python is not None and table._python.held() == {"A": (claim("x/**"),), "B": (claim("y/**"),)}


def test_a_native_failure_under_concurrent_use_keeps_every_granted_claim(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, native_class: Callable[[], Any]
) -> None:
    """Eight threads take disjoint claims while the native table starts failing: no granted claim is lost and none is granted twice."""
    inner = native_class()

    class Failing(ObservedTable):
        def __init__(self) -> None:
            super().__init__(inner)
            self.calls = 0

        def acquire(self, owner: str, claims: list[tuple[str, str]]) -> Any:
            self.calls += 1
            if self.calls == 25:
                raise RuntimeError("injected failure under load")
            return super().acquire(owner, claims)

    table = NativeClaimTable(Failing())
    start = threading.Barrier(8)
    granted: list[str] = []

    def worker(index: int) -> None:
        start.wait(10)
        for n in range(10):
            pattern = f"t{index}/f{n}"
            assert table.acquire(f"T{index}", [claim(pattern)]).ok
            granted.append(pattern)
            time.sleep(0.0002)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not any(thread.is_alive() for thread in threads)
    assert table._python is not None and len(warnings_of(caplog)) == 1
    held = {c.pattern for claims in table.held().values() for c in claims}
    assert held == set(granted) and len(granted) == 80
    assert table.acquire("late", [claim("t3/f4")]).conflicts[0].owner == "T3"
