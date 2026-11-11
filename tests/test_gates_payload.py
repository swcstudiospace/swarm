"""P1-06: `make_verdict(extra=...)` cannot override the signed verdict fields; non-signed extra keys survive."""

from __future__ import annotations

import pytest  # type: ignore[import-not-found, unused-ignore]

from swarm.gates import make_finding, make_verdict


def test_extra_cannot_override_signed_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_SIGNING_KEY", "k")
    major = make_finding("F-1", "major", "functional", "broken")
    env = make_verdict(gate="review", task_id="T-1", agent_id="A09", findings=[major], correlation_id="C-1", audit=False,
                       extra={"gate": "release", "task_id": "T-OTHER", "verdict": "pass", "findings": [],
                              "expires_s": 999999, "waived_by": "alice", "gate_task": "G-1"})
    payload = env["payload"]
    assert payload["gate"] == "review"
    assert payload["task_id"] == "T-1"
    assert payload["verdict"] == "fail"
    assert payload["findings"] == [major]
    assert payload["expires_s"] == 86400
    assert payload["gate_task"] == "G-1"
    assert payload["waived_by"] == "alice"


def test_waived_by_survives_in_waive_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_SIGNING_KEY", "k")
    env = make_verdict(gate="security", task_id="T-2", agent_id="A10", verdict="waive", correlation_id="C-2", audit=False,
                       extra={"waived_by": "human-lead"})
    assert env["payload"]["verdict"] == "waive"
    assert env["payload"]["waived_by"] == "human-lead"
