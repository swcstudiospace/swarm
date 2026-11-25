"""E-DEP exponential backoff caps at 60 s, matching plan/execution-waves.md (1 s to 60 s) and
crates/gsd-failure DEPENDENCY_BACKOFF (cap 60 s, jitter 0.3 s)."""
from swarm.errors import _exp_backoff_err

DEP_BACKOFF_CAP_S = 60.0
DEP_BACKOFF_JITTER_S = 0.3


def test_dependency_backoff_saturates_at_60s_not_30s():
    high_attempt = 10  # 2**10 s is far past the cap, so only the cap decides the nominal delay
    delay = _exp_backoff_err(high_attempt)
    assert DEP_BACKOFF_CAP_S <= delay <= DEP_BACKOFF_CAP_S + DEP_BACKOFF_JITTER_S
