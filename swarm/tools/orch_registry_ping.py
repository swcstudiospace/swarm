"""A01 pilot tool. Echoes a token and can be driven to every contract terminal state.

`probe` exists so generated contract tests can reach dependency, partial, and timeout
without a task-store side effect. Callers that omit it get SUCCESS.
"""
from __future__ import annotations

from collections.abc import Mapping

from swarm.tools.support import sleep_until_cancelled


def call(payload: Mapping[str, object]) -> dict[str, object]:
    probe = payload.get("probe")
    if probe == "sleep":
        sleep_until_cancelled(0.2)
        return {"state": "TIMEOUT", "message": "probe sleep", "token": payload.get("token", ""), "owner": "A01"}
    if probe == "dependency":
        raise OSError("pilot dependency is unavailable")
    if probe == "partial":
        return {"state": "PARTIAL_SUCCESS", "completed_fraction": 0.5, "token": payload["token"], "owner": "A01"}
    return {"state": "SUCCESS", "token": payload["token"], "owner": "A01"}
