"""`SWARM_REDACT_ENGINE`: the opt-in Rust span engine behind `swarm.redact.redact_spans`, proved with fake native modules.

The oracle (pure Python) is the default; `rust` only changes who computes the spans, and every failure of the Rust path
degrades to the oracle for that call. None of these tests needs the real extension: a fake `swarm._native` module is
placed in `sys.modules`, the place `swarm._native_loader.load_native` imports from.
"""
from __future__ import annotations

import inspect
import logging
import sys
import types
from collections.abc import Callable

import pytest

from swarm import redact as R
from swarm.redact import redact_spans, redact_text

# Built at runtime so no secret-shaped literal sits in the repo.
SECRET = "curl -H \"Authorization: Bearer " + "abcdefghij" + "klmnop1234\" https://u:pw12345@h/x password=" + "hunter2" * 2 + " end"
SECRET_SPANS = [(31, 51), (61, 70), (84, 98)]  # taken from the pure-Python oracle
SECRET_MASKED = "curl -H \"Authorization: Bearer [REDACTED]\" https://[REDACTED]@h/x password=[REDACTED] end"
SURROGATE = "\ud800 x password=" + "hunter2" * 2  # a lone surrogate: cannot be encoded as UTF-8
SURROGATE_SPANS = [(13, 27)]  # taken from the pure-Python oracle

NATIVE = "swarm._native"
LOGGER = "swarm.redact"


class FakeNative(types.ModuleType):
    """A stand-in for `swarm._native` whose `redact_spans` records its calls and delegates to `impl`."""

    def __init__(self, impl: Callable[[str], object]) -> None:
        super().__init__(NATIVE)
        self.calls: list[str] = []
        self._impl = impl

    def redact_spans(self, text: str) -> object:
        self.calls.append(text)
        return self._impl(text)


@pytest.fixture(autouse=True)
def _clean_engine_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with the default engine, no extension and no warning reported yet."""
    monkeypatch.delenv(R._ENGINE_ENV, raising=False)
    monkeypatch.setattr(R, "_ENGINE_WARNED", set())
    monkeypatch.setitem(sys.modules, NATIVE, None)  # import of swarm._native fails unless a test installs a fake


@pytest.fixture
def rust(monkeypatch: pytest.MonkeyPatch) -> None:
    """`SWARM_REDACT_ENGINE=rust` with the suite's `SWARM_PURE_PYTHON=1` lifted so the selector reaches the extension."""
    monkeypatch.setenv(R._ENGINE_ENV, "rust")
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)


def install(monkeypatch: pytest.MonkeyPatch, impl: Callable[[str], object]) -> FakeNative:
    fake = FakeNative(impl)
    monkeypatch.setitem(sys.modules, NATIVE, fake)
    return fake


def oracle(text: str) -> list[tuple[int, int]]:
    """The pure-Python answer: the engine variable is unset in these tests whenever this is called."""
    return redact_spans(text)


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


def test_oracle_vectors_are_what_the_tests_assume() -> None:
    assert oracle(SECRET) == SECRET_SPANS
    assert redact_text(SECRET) == SECRET_MASKED
    assert oracle(SURROGATE) == SURROGATE_SPANS


@pytest.mark.parametrize("value", [None, "", "python"])
def test_default_engines_never_touch_native(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str | None) -> None:
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    if value is not None:
        monkeypatch.setenv(R._ENGINE_ENV, value)
    fake = install(monkeypatch, lambda text: [(0, 1)])

    def boom() -> object:
        raise AssertionError("the default engine must not load the extension")

    monkeypatch.setattr(R, "load_native", boom)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert redact_spans(SECRET) == SECRET_SPANS
    assert fake.calls == []
    assert warnings_of(caplog) == []


def test_rust_calls_native_and_returns_its_list_verbatim_as_tuples(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    fake = install(monkeypatch, lambda text: [(0, 3), (7, 9)])  # deliberately not the oracle's answer
    result = redact_spans(SECRET)
    assert fake.calls == [SECRET]
    assert result == [(0, 3), (7, 9)]
    assert all(type(span) is tuple for span in result)


def test_rust_converts_list_spans_to_tuples(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    install(monkeypatch, lambda text: [[2, 5]])
    result = redact_spans(SECRET)
    assert result == [(2, 5)]
    assert type(result[0]) is tuple


def test_an_empty_native_answer_is_trusted_not_recomputed(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    install(monkeypatch, lambda text: [])
    assert redact_spans(SECRET) == []  # the oracle would find three spans; empty from native is an answer, not a failure


@pytest.mark.parametrize("text", ["", "a", "12345", "token"])
def test_short_inputs_return_before_the_engine_dispatch(monkeypatch: pytest.MonkeyPatch, rust: None, text: str) -> None:
    fake = install(monkeypatch, lambda t: [(0, 1)])
    assert redact_spans(text) == []
    assert fake.calls == []


def test_a_six_character_input_reaches_native(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    fake = install(monkeypatch, lambda t: [(1, 2)])
    assert redact_spans("abcdef") == [(1, 2)]
    assert fake.calls == ["abcdef"]


def test_a_non_string_never_reaches_native(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    fake = install(monkeypatch, lambda t: [(0, 1)])
    assert redact_spans(b"password=hunter2hunter2") == []  # type: ignore[arg-type]
    assert fake.calls == []


def test_redact_text_under_rust_equals_python_when_native_returns_the_oracle_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    python_masked = redact_text(SECRET)
    oracle_spans = oracle(SECRET)
    fake = install(monkeypatch, lambda text: oracle_spans)
    monkeypatch.setenv(R._ENGINE_ENV, "rust")
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    assert redact_text(SECRET) == python_masked == SECRET_MASKED
    assert fake.calls == [SECRET]


def test_redact_text_masks_exactly_the_spans_native_returned(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    install(monkeypatch, lambda text: [(0, 4)])
    assert redact_text("abcdefgh") == "[REDACTED]efgh"


def test_the_container_walker_uses_the_selected_engine(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    fake = install(monkeypatch, lambda text: [(0, 4)])
    assert R.redact({"note": "abcdefgh", "n": 3}) == {"note": "[REDACTED]efgh", "n": 3}
    assert fake.calls == ["abcdefgh"]


def test_missing_extension_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert redact_spans(SECRET) == SECRET_SPANS
        assert redact_spans(SECRET) == SECRET_SPANS
    messages = warnings_of(caplog)
    assert len(messages) == 1
    assert "swarm._native is unavailable" in messages[0]
    assert R._ENGINE_ENV in messages[0]


def test_stale_extension_without_redact_spans_falls_back_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    monkeypatch.setitem(sys.modules, NATIVE, types.ModuleType(NATIVE))  # a module with the older five names only
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert redact_spans(SECRET) == SECRET_SPANS
        assert redact_spans(SECRET) == SECRET_SPANS
    messages = warnings_of(caplog)
    assert len(messages) == 1
    assert "no redact_spans" in messages[0]


def test_a_native_exception_falls_back_per_call_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    def fail(text: str) -> object:
        raise ValueError("E-INTERNAL: redact_spans failed: internal panic")

    fake = install(monkeypatch, fail)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert redact_spans(SECRET) == SECRET_SPANS
        assert redact_spans(SECRET) == SECRET_SPANS
    assert len(fake.calls) == 2  # an error is not cached: each call tries native again
    messages = warnings_of(caplog)
    assert len(messages) == 1
    assert "E-INTERNAL" in messages[0]
    assert SECRET not in messages[0]


def test_a_native_exception_does_not_stop_a_later_good_call(monkeypatch: pytest.MonkeyPatch, rust: None) -> None:
    outcomes: list[object] = [RuntimeError("boom"), [(0, 3)]]

    def impl(text: str) -> object:
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    install(monkeypatch, impl)
    assert redact_spans(SECRET) == SECRET_SPANS
    assert redact_spans(SECRET) == [(0, 3)]


def test_a_malformed_native_result_falls_back(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None) -> None:
    install(monkeypatch, lambda text: [(1, 2, 3)])  # three-element span: unpacking fails inside the adapter
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert redact_spans(SECRET) == SECRET_SPANS
    assert len(warnings_of(caplog)) == 1


@pytest.mark.parametrize("encode_in_native", [True, False])
def test_a_lone_surrogate_falls_back_silently_with_the_oracle_answer(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None, encode_in_native: bool
) -> None:
    def encode(text: str) -> object:
        text.encode("utf-8")  # raises the same UnicodeEncodeError PyO3 raises converting a lone surrogate to &str
        return [(0, 1)]

    def raise_it(text: str) -> object:
        raise UnicodeEncodeError("utf-8", text, 0, 1, "surrogates not allowed")

    fake = install(monkeypatch, encode if encode_in_native else raise_it)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert redact_spans(SURROGATE) == SURROGATE_SPANS
        assert redact_spans(SURROGATE) == SURROGATE_SPANS
    assert len(fake.calls) == 2
    assert warnings_of(caplog) == []
    # the engine is still on afterwards: a normal string goes to native
    fake._impl = lambda text: [(0, 3)]
    assert redact_spans(SECRET) == [(0, 3)]


def test_pure_mode_overrides_rust_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(R._ENGINE_ENV, "rust")
    monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
    fake = install(monkeypatch, lambda text: [(0, 1)])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert redact_spans(SECRET) == SECRET_SPANS
        assert redact_spans(SECRET) == SECRET_SPANS
    assert fake.calls == []
    messages = warnings_of(caplog)
    assert len(messages) == 1
    assert "SWARM_PURE_PYTHON=1" in messages[0]


@pytest.mark.parametrize("value", ["Rust", "RUST", "rust ", "fast", "1", "true"])
def test_an_unknown_value_uses_python_with_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, value: str
) -> None:
    monkeypatch.setenv(R._ENGINE_ENV, value)
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    fake = install(monkeypatch, lambda text: [(0, 1)])
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert redact_spans(SECRET) == SECRET_SPANS
        assert redact_spans(SECRET) == SECRET_SPANS
    assert fake.calls == []
    messages = warnings_of(caplog)
    assert len(messages) == 1
    assert repr(value) in messages[0]


def test_each_reason_warns_once_and_reasons_are_independent(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        monkeypatch.setenv(R._ENGINE_ENV, "bogus")
        redact_spans(SECRET)
        redact_spans(SECRET)
        monkeypatch.setenv(R._ENGINE_ENV, "rust")  # the extension is missing (autouse fixture)
        redact_spans(SECRET)
        redact_spans(SECRET)
        monkeypatch.setenv("SWARM_PURE_PYTHON", "1")
        redact_spans(SECRET)
        redact_spans(SECRET)
    messages = warnings_of(caplog)
    assert len(messages) == 3
    assert "unknown value" in messages[0]
    assert "unavailable" in messages[1]
    assert "SWARM_PURE_PYTHON=1" in messages[2]


def test_the_engine_is_read_per_call_so_a_test_can_switch_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWARM_PURE_PYTHON", raising=False)
    fake = install(monkeypatch, lambda text: [(0, 3)])
    monkeypatch.setenv(R._ENGINE_ENV, "rust")
    assert redact_spans(SECRET) == [(0, 3)]
    monkeypatch.setenv(R._ENGINE_ENV, "python")
    assert redact_spans(SECRET) == SECRET_SPANS
    monkeypatch.delenv(R._ENGINE_ENV)
    assert redact_spans(SECRET) == SECRET_SPANS
    assert fake.calls == [SECRET]


def test_no_secret_text_reaches_the_warning_on_a_native_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, rust: None
) -> None:
    def fail(text: str) -> object:
        raise RuntimeError("E-INTERNAL: redact_spans failed: internal panic")

    install(monkeypatch, fail)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        redact_spans(SECRET)
    assert all("hunter2" not in message for message in warnings_of(caplog))


def test_engine_warn_once_holds_lock() -> None:
    assert "with _engine_warn_lock" in inspect.getsource(R._engine_warn_once)
