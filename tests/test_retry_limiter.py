"""Backoff, transient-error classification and the AIMD limiter (swarm/retry.py, swarm/limiter.py)."""
import os
import random

import pytest

from swarm.clock import FakeClock
from swarm.errors import ErrorCode
from swarm.limiter import AimdLimiter, detect_rate_limit
from swarm.retry import backoff_s, is_transient


def test_backoff_without_jitter_doubles_then_caps():
    delays = [backoff_s(n, base=2, cap=120, jitter=0) for n in range(1, 10)]
    assert delays == [2, 4, 8, 16, 32, 64, 120, 120, 120]


def test_backoff_seeded_jitter_within_bounds_and_monotone_nominal():
    rng = random.Random(1234)
    base, cap, jitter = 2.0, 60.0, 0.25
    prev_nominal = 0.0
    for attempt in range(1, 12):
        nominal = min(cap, base * 2 ** (attempt - 1))
        assert nominal >= prev_nominal
        prev_nominal = nominal
        for _ in range(50):
            d = backoff_s(attempt, base=base, cap=cap, jitter=jitter, rng=rng)
            assert nominal * (1 - jitter) - 1e-9 <= d <= min(cap, nominal * (1 + jitter)) + 1e-9
    assert prev_nominal == cap


def test_backoff_is_deterministic_for_seed_and_never_exceeds_cap():
    a = [backoff_s(n, base=1, cap=10, rng=random.Random(7)) for n in range(1, 8)]
    b = [backoff_s(n, base=1, cap=10, rng=random.Random(7)) for n in range(1, 8)]
    assert a == b
    assert max(backoff_s(50, base=1, cap=10, rng=random.Random(s)) for s in range(100)) <= 10


def test_backoff_attempt_below_one_and_huge_attempt_are_safe():
    assert backoff_s(0, base=2, cap=100, jitter=0) == 2
    assert backoff_s(-5, base=2, cap=100, jitter=0) == 2
    assert backoff_s(10_000, base=2, cap=100, jitter=0) == 100


def test_backoff_env_defaults(monkeypatch):
    monkeypatch.delenv("SWARM_RETRY_BASE_S", raising=False)
    monkeypatch.delenv("SWARM_RETRY_CAP_S", raising=False)
    assert backoff_s(1, jitter=0) == 0.1
    assert backoff_s(20, jitter=0) == 5
    monkeypatch.setenv("SWARM_RETRY_BASE_S", "5")
    monkeypatch.setenv("SWARM_RETRY_CAP_S", "30")
    assert backoff_s(1, jitter=0) == 5
    assert backoff_s(5, jitter=0) == 30
    monkeypatch.setenv("SWARM_RETRY_BASE_S", "junk")
    assert backoff_s(1, jitter=0) == 0.1


def test_backoff_rejects_bad_args():
    with pytest.raises(ValueError):
        backoff_s(1, jitter=1.5)
    with pytest.raises(ValueError):
        backoff_s(1, base=0)


@pytest.mark.parametrize("code", ["E-TIMEOUT", "E-DEP", "E-CAPACITY", ErrorCode.E_TIMEOUT, "e-dep"])
def test_transient_codes(code):
    assert is_transient(code, "")


@pytest.mark.parametrize("code", ["E-INPUT", "E-POLICY", ErrorCode.E_POLICY])
def test_permanent_codes_never_transient_even_with_markers(code):
    assert not is_transient(code, "429 rate limit overloaded retry after 5 s")


def test_other_codes_decided_by_message_markers():
    assert is_transient("E-INTERNAL", "HTTP 429 from upstream")
    assert is_transient(None, "overloaded_error: try later")
    assert is_transient("", "Retry-After: 3")
    assert not is_transient("E-INTERNAL", "KeyError: 'x'")
    assert not is_transient(None, "")
    assert not is_transient(None, None)


def test_code_read_from_message_prefix():
    assert is_transient(None, "E-TIMEOUT: task_timeout exceeded")
    assert not is_transient(None, "E-POLICY: blocked, rate limit")


@pytest.mark.parametrize("text,expected", [
    ("Retry-After: 30", 30.0),
    ("retry-after: 2.5", 2.5),
    ("please retry after 12 s", 12.0),
    ("Retry after 7 seconds", 7.0),
    ("retry after 500 ms", 0.5),
    ("retry after 2 minutes", 120.0),
    ("HTTP 429 Too Many Requests", 0.0),
    ("HTTP/1.1 429", 0.0),
    ("status code: 429", 0.0),
    ("429 Too Many Requests", 0.0),
    ("You hit the RATE LIMIT", 0.0),
    ("rate_limit_error", 0.0),
    ("API Error: overloaded_error", 0.0),
    ("The server is Overloaded", 0.0),
    ("monthly QUOTA exceeded", 0.0),
    ("Retry-After header present", 0.0),
    ("HTTP 429, Retry-After: 9", 9.0),
])
def test_detect_rate_limit_positive(text, expected):
    assert detect_rate_limit(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "all tests passed", "line 429 of foo.py", "processed 4290 items", "ran 429 tests",
    "retry after the build", "quotation marks", "limit the rate of change of ... ",
])
def test_detect_rate_limit_negative(text):
    assert detect_rate_limit(text) is None


def test_limiter_sequence_with_fake_clock():
    clock = FakeClock()
    lim = AimdLimiter(8, min_n=1, cooldown_s=60, clock=clock)
    assert lim.current() == 8
    lim.on_success()
    assert lim.current() == 8  # never above max
    lim.on_rate_limit()
    assert lim.current() == 4
    lim.on_success()
    assert lim.current() == 4  # no growth before cooldown
    clock.advance(59.9)
    lim.on_success()
    assert lim.current() == 4
    clock.advance(0.2)
    lim.on_success()
    assert lim.current() == 5  # +1 per success after cooldown
    lim.on_success()
    lim.on_success()
    assert lim.current() == 7
    lim.on_rate_limit()
    assert lim.current() == 3
    lim.on_rate_limit()
    assert lim.current() == 1
    lim.on_rate_limit()
    assert lim.current() == 1  # floor
    clock.advance(61)
    for _ in range(20):
        lim.on_success()
    assert lim.current() == 8  # ceiling


def test_limiter_floor_is_min_n_and_retry_after_extends_cooldown():
    clock = FakeClock()
    lim = AimdLimiter(10, min_n=3, cooldown_s=10, clock=clock)
    for _ in range(5):
        lim.on_rate_limit()
    assert lim.current() == 3
    lim = AimdLimiter(4, min_n=1, cooldown_s=10, clock=clock)
    lim.on_rate_limit(retry_after_s=100)
    clock.advance(50)
    lim.on_success()
    assert lim.current() == 2  # retry-after longer than the cooldown still blocks growth
    clock.advance(51)
    lim.on_success()
    assert lim.current() == 3


def test_limiter_cooldown_never_shortens_and_env_default(monkeypatch):
    clock = FakeClock()
    lim = AimdLimiter(4, cooldown_s=100, clock=clock)
    lim.on_rate_limit()
    lim.on_rate_limit(retry_after_s=0)  # a later short hold must not shorten the earlier long one
    clock.advance(99)
    lim.on_success()
    assert lim.current() == 1
    monkeypatch.setenv("SWARM_RATE_COOLDOWN_S", "5")
    lim = AimdLimiter(4, clock=clock)
    lim.on_rate_limit()
    clock.advance(6)
    lim.on_success()
    assert lim.current() == 3


@pytest.mark.parametrize("kw", [{"max_n": 0}, {"max_n": 2, "min_n": 3}, {"max_n": 2, "min_n": 0}, {"max_n": 2, "cooldown_s": -1}])
def test_limiter_rejects_bad_config(kw):
    with pytest.raises(ValueError):
        AimdLimiter(**kw)


# --- W1 review R1 fix-up (P3-9): Retry-After is clamped to SWARM_RETRY_CAP_S ------------------------------------------
@pytest.mark.parametrize("huge", [86_400, 1e12, float("inf")])
def test_huge_retry_after_is_clamped_to_the_retry_cap(monkeypatch, huge):
    monkeypatch.delenv("SWARM_RETRY_CAP_S", raising=False)
    clock = FakeClock()
    lim = AimdLimiter(4, min_n=1, cooldown_s=10, clock=clock)
    lim.on_rate_limit(retry_after_s=huge)
    assert lim.current() == 2
    clock.advance(119)
    lim.on_success()
    assert lim.current() == 2  # still inside the (capped) 120 s hold
    clock.advance(2)
    lim.on_success()
    assert lim.current() == 3  # growth resumes after the cap, not after a day


def test_retry_after_clamp_follows_the_env_cap_and_never_shortens_the_cooldown(monkeypatch):
    monkeypatch.setenv("SWARM_RETRY_CAP_S", "30")
    clock = FakeClock()
    lim = AimdLimiter(4, min_n=1, cooldown_s=10, clock=clock)
    lim.on_rate_limit(retry_after_s=9999)
    clock.advance(31)
    lim.on_success()
    assert lim.current() == 3
    lim = AimdLimiter(4, min_n=1, cooldown_s=100, clock=clock)  # cooldown longer than the cap still wins
    lim.on_rate_limit(retry_after_s=9999)
    clock.advance(99)
    lim.on_success()
    assert lim.current() == 2


def test_transient_classification_follows_the_rust_contract():
    assert is_transient("E-CONTRACT")
    assert is_transient("E-TIMEOUT") and is_transient("E-DEP") and is_transient("E-CAPACITY")
    assert not is_transient("E-INPUT")
    assert not is_transient("E-POLICY")


# --- PY-08: lock waits follow the injected clock (P5-15) ---------------------------------------------------------------
def test_live_lock_wait_follows_the_injected_clock(tmp_path):
    import fcntl

    from swarm import telemetry

    clock = FakeClock()
    target = tmp_path / "live.json"
    holder = open(f"{target}.lock", "a")  # noqa: SIM115 - held for the whole wait, closed below
    fcntl.flock(holder, fcntl.LOCK_EX)
    start = clock.monotonic()
    try:
        with telemetry._live_lock(target, timeout=2.0, clock=clock, sleep=clock.advance) as held:
            assert held is False
    finally:
        holder.close()
    assert clock.monotonic() - start == pytest.approx(2.0, abs=0.01)


def test_scratch_lock_wait_follows_the_injected_clock(tmp_path):
    import fcntl

    from swarm.tools import shared_runtime as sr

    clock = FakeClock()
    directory = tmp_path / "scratch"
    directory.mkdir()
    holder = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    start = clock.monotonic()
    try:
        with pytest.raises(OSError, match="busy"), sr._scratch_lock(directory, clock=clock, sleep=clock.advance):
            pass
    finally:
        os.close(holder)
    assert clock.monotonic() - start == pytest.approx(sr.SCRATCH_LOCK_WAIT_S, abs=0.01)


# --- PY-08: one env_seconds helper for the retry and limiter knobs (P5-18) --------------------------------------------
@pytest.mark.parametrize("raw", ["0", "nan", "inf", "-1"])
def test_env_seconds_knobs_agree(monkeypatch, raw):
    from swarm.limiter import DEFAULT_COOLDOWN_S, DEFAULT_RETRY_CAP_S, _env_cooldown, _retry_cap_s, env_seconds
    from swarm.retry import DEFAULT_BASE_S, DEFAULT_CAP_S, backoff_s

    for name in ("SWARM_RATE_COOLDOWN_S", "SWARM_RETRY_BASE_S", "SWARM_RETRY_CAP_S"):
        monkeypatch.setenv(name, raw)
    # Retry base, backoff cap and the Retry-After cap need a positive number: every value here is the default.
    assert env_seconds("SWARM_RETRY_BASE_S", DEFAULT_BASE_S) == DEFAULT_BASE_S
    assert env_seconds("SWARM_RETRY_CAP_S", DEFAULT_CAP_S) == DEFAULT_CAP_S
    assert _retry_cap_s() == DEFAULT_RETRY_CAP_S
    assert backoff_s(1, jitter=0) == DEFAULT_BASE_S
    # The cooldown is a hold, so an explicit 0 means no hold (the Rust contract); the other bad values are the default.
    assert _env_cooldown() == (0.0 if raw == "0" else DEFAULT_COOLDOWN_S)
