"""AIMD concurrency limiter and rate-limit detection for the bounded parallel runtime.

`AimdLimiter` is the adaptive cap on concurrent runs: a rate-limit signal halves the cap (never below `min_n`) and
starts a cooldown; once the cooldown has elapsed each success adds one slot back, never above `max_n`. Time comes
from an injected `Clock`, so tests drive the cooldown with `FakeClock` instead of sleeping.
"""
from __future__ import annotations

import math
import os
import re
import threading

from .clock import SYSTEM_CLOCK, Clock

DEFAULT_COOLDOWN_S = 60.0
DEFAULT_RETRY_CAP_S = 120.0  # Retry-After hold cap, not the backoff cap (swarm.retry.DEFAULT_CAP_S, 5 s, follows the gsd-failure contract)

_UNIT_SECONDS = {
    "": 1.0, "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
    "ms": 0.001, "msec": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
}
_NUM = r"(\d+(?:\.\d+)?)"
_RETRY_AFTER_HEADER = re.compile(r"retry-after\s*[:=]\s*" + _NUM, re.IGNORECASE)
_RETRY_AFTER_TEXT = re.compile(r"retry\s+(?:after|in)\s+" + _NUM + r"\s*([a-z]*)", re.IGNORECASE)
_MARKERS = re.compile(
    r"(?:\bhttp[/\d. ]*|\bstatus(?:\s+code)?\s*[:=]?\s*|\berror\s*[:=]?\s*)429\b"
    r"|\b429\s+too\s+many\s+requests\b"
    r"|\btoo\s+many\s+requests\b"
    r"|rate[\s_-]?limit"
    r"|overloaded"
    r"|\bquota\b"
    r"|retry-after",
    re.IGNORECASE,
)


def detect_rate_limit(text: str | None) -> float | None:
    """Seconds to wait if `text` reports a rate limit or overload, else None.

    Returns the advertised delay (`Retry-After: N`, `retry after N s`) when there is one and 0.0 when only a marker
    is present (HTTP 429, "rate limit", "overloaded_error", "overloaded", "quota", a bare Retry-After header).
    Ordinary text, including a stray "429" that is not an HTTP status, returns None.
    """
    if not text:
        return None
    match = _RETRY_AFTER_HEADER.search(text)
    if match:
        return float(match.group(1))
    match = _RETRY_AFTER_TEXT.search(text)
    if match and match.group(2).lower() in _UNIT_SECONDS:
        return float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
    if _MARKERS.search(text):
        return 0.0
    return None


def env_seconds(name: str, default: float, *, zero_ok: bool = False) -> float:
    """The number of seconds in environment variable `name`, or `default`.

    A finite number above zero is taken; zero is taken only with `zero_ok` (a hold or grace where none is a real
    setting). Unset, blank, non-numeric, NaN, infinite and negative values all fall back to `default`.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value):
        return default
    if value > 0 or (zero_ok and value == 0):
        return value
    return default


def _env_cooldown() -> float:
    return env_seconds("SWARM_RATE_COOLDOWN_S", DEFAULT_COOLDOWN_S, zero_ok=True)


def _retry_cap_s() -> float:
    """`SWARM_RETRY_CAP_S` (default 120): the longest a server-supplied Retry-After may hold growth back."""
    return env_seconds("SWARM_RETRY_CAP_S", DEFAULT_RETRY_CAP_S)


class AimdLimiter:
    """Additive-increase / multiplicative-decrease cap on concurrent runs. Thread-safe."""

    def __init__(self, max_n: int, *, min_n: int = 1, cooldown_s: float | None = None, clock: Clock = SYSTEM_CLOCK) -> None:
        if isinstance(max_n, bool) or isinstance(min_n, bool) or not isinstance(max_n, int) or not isinstance(min_n, int):
            raise ValueError("max_n and min_n must be integers")
        if min_n < 1 or max_n < min_n:
            raise ValueError(f"need 1 <= min_n <= max_n, got min_n={min_n} max_n={max_n}")
        cooldown = _env_cooldown() if cooldown_s is None else float(cooldown_s)
        if not cooldown >= 0 or cooldown == float("inf"):
            raise ValueError(f"cooldown_s must be a finite number >= 0, got {cooldown_s!r}")
        self._max = max_n
        self._min = min_n
        self._cooldown_s = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._current = max_n
        self._cooldown_until: float | None = None

    def current(self) -> int:
        """The cap on concurrent runs right now."""
        with self._lock:
            return self._current

    def on_success(self) -> None:
        """Add one slot, but only once the cooldown from the last rate limit has elapsed."""
        with self._lock:
            if self._cooldown_until is not None and self._clock.monotonic() < self._cooldown_until:
                return
            if self._current < self._max:
                self._current += 1

    def on_rate_limit(self, retry_after_s: float | None = None) -> None:
        """Halve the cap (floor `min_n`) and hold growth for max(cooldown, retry_after_s), where `retry_after_s` is
        clamped to `SWARM_RETRY_CAP_S` (default 120): a server asking for a day must not freeze growth for a day."""
        hold = self._cooldown_s
        if retry_after_s is not None and retry_after_s == retry_after_s:  # NaN ignored; +inf clamps to the cap
            hold = max(hold, min(float(retry_after_s), _retry_cap_s()))
        with self._lock:
            self._current = max(self._min, self._current // 2)
            self._cooldown_until = max(self._cooldown_until or 0.0, self._clock.monotonic() + hold)
