"""fire_if_ready must not detach hooks/autonomous_run.py unless a real signing key,
SWARM_ALLOW_AUTONOMOUS=1 and no cloud-agent marker are all present.

These tests patch subprocess.Popen. They never start the runner.
"""
import json
import subprocess
from pathlib import Path

import pytest

from swarm.envelope import insecure_dev_key
from swarm.hook_trigger import fire_if_ready, is_opt_in
from swarm.subagent_lifecycle import get_manager, reset_manager

_COMPLETE = "a01-orchestrator task.result state IN_REVIEW"
_CLOUD_MARKERS = ("CURSOR_AGENT", "CURSOR_CLOUD_AGENT", "CLOUD_AGENT")
_KEY_VARS = (
    "SWARM_SIGNING_KEY",
    "SWARM_ED25519_KEY",
    "SWARM_REQUIRE_KEY",
    "SWARM_ALLOW_INSECURE_DEV_KEY",
)


class _NotAProcess:
    """Stand-in for Popen. It does not start a process."""

    pid = 0


@pytest.fixture()
def state(tmp_path, monkeypatch):
    path = tmp_path / "swarm-state"
    monkeypatch.setenv("SWARM_DIR", str(path))
    monkeypatch.setenv("AIO_SWARM_AFTER_ORCH", "1")
    monkeypatch.delenv("SWARM_AFTER_ORCH", raising=False)
    return path


def _clear_cloud(monkeypatch):
    for name in _CLOUD_MARKERS:
        monkeypatch.delenv(name, raising=False)


def _clear_keys(monkeypatch):
    for name in _KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def _record_popen(monkeypatch):
    calls = []

    def _fake(argv, **kwargs):
        calls.append(argv)
        return _NotAProcess()

    monkeypatch.setattr(subprocess, "Popen", _fake)
    return calls


def _hook_payloads(state: Path) -> list[dict]:
    log = state / "events.jsonl"
    if not log.exists():
        return []
    payloads = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "hook-fired":
            payloads.append(rec.get("payload") or {})
    return payloads


def _reasons(state: Path) -> list[str]:
    return [p["reason"] for p in _hook_payloads(state) if p.get("reason")]


@pytest.mark.parametrize("name,value", [
    ("AIO_SWARM_AFTER_ORCH", "1"),
    ("AIO_SWARM_AFTER_ORCH", "true"),
    ("AIO_SWARM_AFTER_ORCH", "yes"),
    ("AIO_SWARM_AFTER_ORCH", "on"),
    ("AIO_SWARM_AFTER_ORCH", " TRUE "),
    ("SWARM_AFTER_ORCH", "1"),
    ("SWARM_AFTER_ORCH", "yes"),
])
def test_opt_in_accepts_either_name_and_the_true_words(monkeypatch, name, value):
    monkeypatch.delenv("AIO_SWARM_AFTER_ORCH", raising=False)
    monkeypatch.delenv("SWARM_AFTER_ORCH", raising=False)
    assert is_opt_in() is False
    monkeypatch.setenv(name, value)
    assert is_opt_in() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_opt_in_rejects_other_values(monkeypatch, value):
    monkeypatch.setenv("AIO_SWARM_AFTER_ORCH", value)
    monkeypatch.delenv("SWARM_AFTER_ORCH", raising=False)
    assert is_opt_in() is False


def test_opt_in_off_and_no_signal_write_no_event(state, monkeypatch):
    monkeypatch.setenv("AIO_SWARM_AFTER_ORCH", "0")
    assert fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)["reason"] == "opt-in-off"
    assert _hook_payloads(state) == []
    monkeypatch.setenv("AIO_SWARM_AFTER_ORCH", "1")
    assert fire_if_ready("what is a monad", session_id="s", corr="c", root=state)["reason"] == "no-signal"
    assert _hook_payloads(state) == []


def test_no_real_key_does_not_spawn(state, monkeypatch):
    _clear_keys(monkeypatch)
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")
    calls = _record_popen(monkeypatch)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert calls == []
    assert result["fired"] is False
    assert result["reason"] == "no-signing-key"
    assert _reasons(state) == ["no-signing-key"]


def test_public_dev_key_is_not_a_real_key(state, monkeypatch):
    _clear_keys(monkeypatch)
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
    monkeypatch.setenv("SWARM_SIGNING_KEY", insecure_dev_key())
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")
    calls = _record_popen(monkeypatch)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert calls == []
    assert result["reason"] == "no-signing-key"
    assert _reasons(state) == ["no-signing-key"]


def test_without_second_opt_in_does_not_spawn(state, monkeypatch):
    _clear_cloud(monkeypatch)
    monkeypatch.delenv("SWARM_ALLOW_AUTONOMOUS", raising=False)
    calls = _record_popen(monkeypatch)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert calls == []
    assert result["fired"] is False
    assert result["reason"] == "autonomous-not-allowed"
    assert _reasons(state) == ["autonomous-not-allowed"]


@pytest.mark.parametrize("marker", _CLOUD_MARKERS)
def test_cloud_agent_marker_does_not_spawn(state, monkeypatch, marker):
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")
    monkeypatch.setenv(marker, "1")
    calls = _record_popen(monkeypatch)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert calls == []
    assert result["fired"] is False
    assert result["reason"] == "cloud-agent"
    assert result["marker"] == marker
    assert _reasons(state) == ["cloud-agent"]
    refused = [p for p in _hook_payloads(state) if p.get("reason") == "cloud-agent"]
    assert refused == [{"hook": "a01_complete", "deduped": False, "reason": "cloud-agent", "marker": marker}]


def test_spawn_oserror_is_recorded(state, monkeypatch):
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")

    def _boom(*_args, **_kwargs):
        raise OSError("spawn refused by test")

    monkeypatch.setattr(subprocess, "Popen", _boom)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert result["fired"] is False
    assert result["reason"] == "spawn-failed"
    failed = [p for p in _hook_payloads(state) if p.get("reason") == "spawn-failed"]
    assert len(failed) == 1
    assert failed[0]["error"] == "OSError"


def test_real_key_and_second_opt_in_reaches_spawn(state, monkeypatch):
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")
    calls = _record_popen(monkeypatch)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert result["fired"] is True
    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "omp"
    assert "-p" in argv
    assert argv[-1].startswith("/gsd-autonomous workflowz ultrathink orchestrate after a01")


def test_follow_on_env_has_no_signing_material(state, monkeypatch):
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")
    monkeypatch.setenv("SWARM_SIGNING_KEY", "a" * 64)
    monkeypatch.setenv("SWARM_ED25519_KEY", "b" * 64)
    monkeypatch.setenv("SWARM_ED25519_KEY_FILE", "/tmp/key-ref")
    monkeypatch.setenv("SWARM_REQUIRE_KEY", "1")
    monkeypatch.setenv("SWARM_TASK_ID", "T-1")
    monkeypatch.setenv("SWARM_CORRELATION_ID", "corr-1")
    monkeypatch.setenv("SWARM_KEEP_ME", "kept")
    envs = []

    def _fake(argv, **kwargs):
        envs.append(kwargs["env"])
        return _NotAProcess()

    monkeypatch.setattr(subprocess, "Popen", _fake)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert result["fired"] is True
    assert len(envs) == 1
    env = envs[0]
    for name in ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE",
                 "SWARM_REQUIRE_KEY", "SWARM_TASK_ID", "SWARM_CORRELATION_ID"):
        assert name not in env
    assert env["SWARM_KEEP_ME"] == "kept"
    assert env["SWARM_CHILD"] == "0"
    assert env["SWARM_AGENT"] == "a01-orchestrator"
    assert "SWARM_SIGNING_KEY" not in env


def test_failed_spawn_releases_its_slot(state, monkeypatch):
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")
    reset_manager()

    def _boom(*_args, **_kwargs):
        raise OSError("spawn refused by test")

    monkeypatch.setattr(subprocess, "Popen", _boom)

    result = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert result["reason"] == "spawn-failed"
    assert get_manager().active_ids() == []


def test_failed_spawn_does_not_suppress_retry(state, monkeypatch):
    """hook_trigger inserts the trigger row before Popen and never removes it on spawn failure, so an identical retry is
    deduped and the completion trigger is lost. Expected to fail today: the retry returns deduped True, fired False."""
    _clear_cloud(monkeypatch)
    monkeypatch.setenv("SWARM_ALLOW_AUTONOMOUS", "1")
    reset_manager()
    attempts = []

    def _flaky(argv, **kwargs):
        attempts.append(argv)
        if len(attempts) == 1:
            raise OSError("spawn refused by test")
        return _NotAProcess()

    monkeypatch.setattr(subprocess, "Popen", _flaky)

    first = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)
    assert first["fired"] is False and first["reason"] == "spawn-failed"

    retry = fire_if_ready(_COMPLETE, session_id="s", corr="c", root=state)

    assert len(attempts) == 2
    assert retry["fired"] is True
    assert retry["deduped"] is False
