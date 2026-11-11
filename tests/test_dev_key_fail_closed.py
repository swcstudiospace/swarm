"""Keyless gate runs are advisory: the public dev key is never implicit.

These tests clear SWARM_SIGNING_KEY, SWARM_ED25519_KEY, SWARM_REQUIRE_KEY,
SWARM_ALLOW_INSECURE_DEV_KEY and SWARM_AGENT_SESSION. The dev key signs and
verifies only while SWARM_ALLOW_INSECURE_DEV_KEY=1, and never when
SWARM_REQUIRE_KEY=1 or SWARM_ED25519_KEY is set.
"""
import json
import logging
import os
import sqlite3
import subprocess
import sys

import pytest

from conftest import ROOT, run_script

_KEY_VARS = (
    "SWARM_SIGNING_KEY",
    "SWARM_ED25519_KEY",
    "SWARM_REQUIRE_KEY",
    "SWARM_ALLOW_INSECURE_DEV_KEY",
    "SWARM_AGENT_SESSION",
)
ED_SEED = "11" * 32
KEY_REASON = "fail-closed: no signing key configured"


@pytest.fixture(autouse=True)
def _clear_signing_env(monkeypatch, ephemeral_signing_key):
    for name in _KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def _rows(swarm, task_id=None):
    con = sqlite3.connect(swarm / "tasks.db")
    con.row_factory = sqlite3.Row
    q, a = (
        ("SELECT * FROM verdicts WHERE task_id=? ORDER BY id", (task_id,))
        if task_id
        else ("SELECT * FROM verdicts ORDER BY id", ())
    )
    rows = [dict(r) for r in con.execute(q, a)]
    con.close()
    return rows


def _events(swarm, etype):
    p = swarm / "events.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text().splitlines() if f'"{etype}"' in ln]


def _plan(env, prefix="X"):
    r = run_script("orch_plan.py", "--brief-text", "x", "--pattern", "feature", "--prefix", prefix, env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def _lease(ts, tid, **notes):
    steps = ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS")
    state = ts.get(tid)["state"]
    for step in steps[steps.index(state) + 1 if state in steps else 0:]:
        ts.transition(tid, step)
    if notes:
        ts.set_notes(tid, **notes)


def _in_review(ts, tid, gates=("review",)):
    ts.create(task_id=tid, correlation_id="c", capability="code.backend", notes={"gates": list(gates)})
    for step in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW"):
        ts.transition(tid, step)


def _child_env(swarm_dir, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_DIR", *_KEY_VARS)}
    env["SWARM_DIR"] = str(swarm_dir)
    env.update(extra)
    return env


def test_keyless_leased_gate_records_no_rows(swarm_dir):
    """A keyless gate run on a leased gate task records no verdict rows."""
    from swarm.taskstore import TaskStore

    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    _lease(TaskStore(), "X-qa", dry_run=True)
    r = run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(swarm_dir) == []
    reasons = [e["payload"].get("reason", "") for e in _events(swarm_dir, "gate.verdict.unrecorded")]
    assert any(KEY_REASON in reason and "SWARM_SIGNING_KEY" in reason for reason in reasons), reasons
    envelope = json.loads((swarm_dir / "verdicts" / "X-qa.quality.json").read_text())
    assert envelope["payload"].get("advisory") is True


def test_record_gate_verdicts_keyless_records_nothing(swarm_dir):
    from swarm.taskstore import TaskStore
    from swarm.verdicts import record_gate_verdicts

    env = {"SWARM_DIR": str(swarm_dir)}
    _plan(env)
    ts = TaskStore()
    _lease(ts, "X-qa", dry_run=True)
    seen = []
    recorded = record_gate_verdicts(
        ts, gate_task_id="X-qa", gate="quality", agent_id="A08@local", findings=[], runs={},
        correlation_id=None, expires_s=60, emit=lambda et, payload, **_k: seen.append((et, payload)),
    )
    assert recorded == {}
    assert _rows(swarm_dir) == []
    assert seen and seen[-1][0] == "gate.verdict.unrecorded"
    assert KEY_REASON in seen[-1][1]["reason"] and "SWARM_ED25519_KEY" in seen[-1][1]["reason"]


def test_dev_key_pass_cannot_reach_approved(swarm_dir, monkeypatch):
    """A dev-key passing verdict does not verify once the opt-in is unset, and APPROVED fails closed."""
    from swarm.envelope import verify_envelope
    from swarm.errors import ErrorCode, SwarmError
    from swarm.gates import make_verdict
    from swarm.taskstore import TaskStore

    ts = TaskStore()
    _in_review(ts, "K-1")
    monkeypatch.setenv("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
    signed = make_verdict(gate="review", task_id="K-1", agent_id="A09", correlation_id="c")
    assert signed["sig"].startswith("hmac:")
    assert verify_envelope(signed) is True
    ts.record_verdict("K-1", signed)
    monkeypatch.delenv("SWARM_ALLOW_INSECURE_DEV_KEY", raising=False)
    assert verify_envelope(signed) is False
    with pytest.raises(SwarmError, match="fail-closed: no signing key configured") as ei:
        ts.transition("K-1", "APPROVED")
    assert ei.value.code is ErrorCode.E_POLICY
    assert "SWARM_REQUIRE_KEY=1" not in str(ei.value)
    assert ts.get("K-1")["state"] == "IN_REVIEW"


def test_dev_key_event_requires_opt_in_and_stays_forbidden(swarm_dir, monkeypatch, tmp_path):
    """No security.dev_key event without the opt-in. With it, today's event returns, and
    SWARM_REQUIRE_KEY=1 or SWARM_ED25519_KEY still forbid the dev key."""
    from swarm.envelope import verify_envelope
    from swarm.errors import SwarmError
    from swarm.gates import make_verdict

    other = tmp_path / "other"
    other.mkdir()
    env = _child_env(swarm_dir)
    cmd = [sys.executable, str(ROOT / "scripts" / "rev_gate.py"), "--root", str(swarm_dir.parent),
           "--dry-run", "--task-id", "R-rev", "--json"]
    # swarm_dir fixture points SWARM_DIR at swarm_dir itself; --root must be its parent
    # so the script resolves the same store. Pass SWARM_DIR explicitly.
    env["SWARM_DIR"] = str(swarm_dir)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=other, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _events(swarm_dir, "security.dev_key") == []

    env["SWARM_ALLOW_INSECURE_DEV_KEY"] = "1"
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=other, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _events(swarm_dir, "security.dev_key"), "opt-in dev-key signing must emit security.dev_key"

    monkeypatch.setenv("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
    signed = make_verdict(gate="review", task_id="K-opt", agent_id="A09", correlation_id="c")
    assert verify_envelope(signed) is True
    monkeypatch.setenv("SWARM_REQUIRE_KEY", "1")
    with pytest.raises(SwarmError, match="SWARM_REQUIRE_KEY=1 but no signing key configured"):
        make_verdict(gate="review", task_id="K-req", agent_id="A09", correlation_id="c")
    assert verify_envelope(signed) is False
    monkeypatch.delenv("SWARM_REQUIRE_KEY")
    monkeypatch.setenv("SWARM_ED25519_KEY", ED_SEED)
    assert verify_envelope(signed) is False
    monkeypatch.setenv("SWARM_ED25519_KEY", "zz")
    assert verify_envelope(signed) is False


def test_agent_session_preview_stays_unrecorded(swarm_dir):
    """SWARM_AGENT_SESSION=1 still records nothing and does not emit security.dev_key."""
    from swarm.envelope import verify_envelope
    from swarm.taskstore import TaskStore

    env = {"SWARM_DIR": str(swarm_dir), "SWARM_AGENT_SESSION": "1"}
    _plan(env)
    _lease(TaskStore(), "X-qa", dry_run=True)
    r = run_script("qa_gate.py", "--dry-run", "--task-id", "X-qa", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _rows(swarm_dir) == []
    reasons = [e["payload"].get("reason", "") for e in _events(swarm_dir, "gate.verdict.unrecorded")]
    assert any("agent session" in reason for reason in reasons), reasons
    assert _events(swarm_dir, "security.dev_key") == []
    envelope = json.loads((swarm_dir / "verdicts" / "X-qa.quality.json").read_text())
    assert envelope["payload"].get("advisory") is True
    assert verify_envelope(envelope) is False



@pytest.mark.parametrize("script,gate,gate_id", [
    ("qa_gate.py", "quality", "K-qa"),
    ("sec_gate.py", "security", "K-sec"),
])
def test_keyless_major_per_target_finding_fails_with_no_rows(tmp_path, swarm_dir, script, gate, gate_id):
    """A keyless quality or security gate whose own checks are clean still fails on one major
    per-target finding, writes no verdict rows, and leaves an unsigned advisory envelope."""
    from swarm.envelope import verify_envelope
    from swarm.taskstore import TaskStore

    work = tmp_path / "empty"
    work.mkdir()
    ts = TaskStore()
    ts.create(task_id="K-be", correlation_id="c", capability="code.backend", risk_class="low",
              notes={"gates": [gate]})
    ts.create(task_id=gate_id, correlation_id="c", capability=f"gate.{gate}", risk_class="low",
              notes={"gate": gate, "gate_for": ["K-be"], "gates": []})
    _lease(ts, gate_id)
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({
        "K-be": [{"severity": "major", "kind": "functional", "summary": "blocking per-target finding"}],
    }))
    extra = ["--risk-class", "low"] if script == "qa_gate.py" else []
    r = run_script(
        script, "--task-id", gate_id, "--correlation-id", "c", "--root", str(work),
        "--per-target-findings", str(findings), *extra, "--json",
        env={"SWARM_DIR": str(swarm_dir)},
    )
    assert r.returncode != 0, r.stdout + r.stderr
    body = json.loads(r.stdout)
    assert body["verdict"] == "fail"
    assert _rows(swarm_dir) == []
    envelope = json.loads((swarm_dir / "verdicts" / f"{gate_id}.{gate}.json").read_text())
    assert envelope["payload"].get("advisory") is True
    assert envelope["payload"]["verdict"] == "fail"
    assert verify_envelope(envelope) is False


def test_dev_key_audit_failure_is_logged(swarm_dir, monkeypatch, caplog):
    """A failing security.dev_key audit write is logged; signing itself stays non-fatal."""
    from swarm import runlog
    from swarm.envelope import sign_envelope

    def _sink_down(*_args, **_kwargs):
        raise RuntimeError("audit sink down")

    monkeypatch.setattr(runlog, "emit", _sink_down)
    monkeypatch.setenv("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
    env = {"type": "gate.verdict", "source": "A09", "correlation_id": "c", "payload": {}}
    signed = sign_envelope(env, root=swarm_dir)
    assert signed["sig"].startswith("hmac:")
    logged = [r for r in caplog.records if r.name == "swarm.envelope" and r.levelno >= logging.WARNING]
    assert any("RuntimeError" in r.getMessage() for r in logged), caplog.text


def test_lifecycle_messages_reach_logging(swarm_dir, capsys, caplog):
    """Operator lifecycle text goes through logging and still reaches stderr unchanged."""
    from swarm.envelope import perform_signing_key_lifecycle

    caplog.set_level(logging.INFO, logger="swarm.envelope")
    result = perform_signing_key_lifecycle(root=swarm_dir)
    msg = result["message"]
    logged = [r.getMessage() for r in caplog.records if r.name == "swarm.envelope"]
    assert logged == [msg]
    assert capsys.readouterr().err == msg + "\n"


def test_key_lifecycle_message_via_logging(swarm_dir, monkeypatch, caplog):
    """The enterprise-required failure is logged on swarm.envelope at ERROR before it raises."""
    from swarm.envelope import perform_signing_key_lifecycle
    from swarm.errors import SwarmError

    monkeypatch.setenv("SWARM_REQUIRE_KEY", "1")
    caplog.set_level(logging.INFO, logger="swarm.envelope")
    with pytest.raises(SwarmError):
        perform_signing_key_lifecycle(root=swarm_dir)
    recs = [r for r in caplog.records if r.name == "swarm.envelope"]
    assert recs and recs[-1].levelno == logging.ERROR
    assert recs[-1].getMessage().startswith(
        "Signing key required for enterprise audit/integrity of parallel swarm. "
        "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured")


def test_bad_ed25519_key_names_cause_not_value(monkeypatch):
    """A malformed SWARM_ED25519_KEY is reported by variable name and cause, never by its value."""
    from swarm.envelope import signing_config_error

    monkeypatch.setenv("SWARM_ED25519_KEY", "zz")
    err = signing_config_error()
    assert err is not None and "SWARM_ED25519_KEY" in err
    assert "zz" not in err
