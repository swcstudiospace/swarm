"""Retry policy: capped exponential backoff with bounded jitter, and the transient-error classifier."""
from __future__ import annotations

import random
import re
from typing import Protocol

from .limiter import detect_rate_limit, env_seconds

# The gsd-failure default (crates/gsd-failure/src/isolation.rs): the Rust contract (Phase 11 U5).
DEFAULT_BASE_S = 0.1
DEFAULT_CAP_S = 5.0

TRANSIENT_CODES = frozenset({"E-TIMEOUT", "E-DEP", "E-CAPACITY", "E-CONTRACT"})
PERMANENT_CODES = frozenset({"E-INPUT", "E-POLICY"})
_CODE_PREFIX = re.compile(r"^\s*(E-[A-Z]+)\b")


class _Uniform(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


def backoff_s(attempt: int, *, base: float | None = None, cap: float | None = None, jitter: float = 0.25, rng: _Uniform = random) -> float:
    """Delay before retry number `attempt` (1-based): min(cap, base * 2**(attempt-1)) scaled by 1 +/- jitter, then clamped to cap.

    `base`/`cap` default to SWARM_RETRY_BASE_S / SWARM_RETRY_CAP_S (0.1 / 5). `rng` needs `uniform(a, b)`; pass a
    seeded `random.Random` for deterministic delays.
    """
    if not 0 <= jitter <= 1:
        raise ValueError(f"jitter must be within [0, 1], got {jitter!r}")
    base_s = env_seconds("SWARM_RETRY_BASE_S", DEFAULT_BASE_S) if base is None else float(base)
    cap_s = env_seconds("SWARM_RETRY_CAP_S", DEFAULT_CAP_S) if cap is None else float(cap)
    if base_s <= 0 or cap_s <= 0:
        raise ValueError("base and cap must be > 0")
    exponent = min(max(int(attempt), 1) - 1, 62)
    nominal = min(cap_s, base_s * 2.0 ** exponent)
    if jitter == 0:
        return nominal
    return max(0.0, min(cap_s, nominal * (1.0 + rng.uniform(-jitter, jitter))))


def is_transient(error_code: object, message: str | None = None) -> bool:
    """True when a failure is worth retrying.

    E-TIMEOUT, E-DEP, E-CAPACITY and E-CONTRACT are transient; E-INPUT and E-POLICY never are, whatever the message
    says. For any other code (E-INTERNAL, unknown, None) the message decides: rate-limit and overload markers are
    transient. A missing code is read from an `E-XXX:` message prefix.
    """
    text = message or ""
    code = str(getattr(error_code, "value", error_code) or "").strip().upper()
    if not code:
        match = _CODE_PREFIX.match(text)
        code = match.group(1) if match else ""
    if code in PERMANENT_CODES:
        return False
    if code in TRANSIENT_CODES:
        return True
    return detect_rate_limit(text) is not None
