"""Regression test for a runtime defect fixed while typing swarm/tools/a01_tools.py.

``budget_guard`` validated usage with ``math.isfinite``, which raises ``OverflowError`` for an integer beyond float
range, and then divided ``used / limit``, which raises the same error for a huge integer over a small limit. The
function's own "too large relative to max_... to compare" INVALID_INPUT branch was therefore unreachable for integers:
a 400-digit token count crashed the call instead of returning that response.
"""
from __future__ import annotations

from swarm.tools import a01_tools


def test_budget_guard_reports_integer_beyond_float_range_as_invalid_input() -> None:
    out = a01_tools.budget_guard({
        "budget": {"max_tokens": 1, "max_wall_s": 1, "max_cost_usd": 1},
        "usage": {"tokens": 10**400, "wall_s": 0, "cost_usd": 0},
        "prior_breaches": 0,
    })

    assert out == {"state": "INVALID_INPUT", "field": "usage",
                   "message": "tokens is too large relative to max_tokens to compare"}
