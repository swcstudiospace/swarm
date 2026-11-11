"""A10 security gate (scripts/sec_gate.py) and the shared `sh` runner in swarm/script_base.py, as the gate scripts run them.

Rows proven here: P4-17 (malformed allow-list is E-INPUT), P4-19 (runner timeout kills the process group; the gate-side
outcome, E-INTERNAL with no verdict, is in test_qa_gate_detection.py), P4-26 (an expired suppression is reported as an info finding; the Rust mirror reports the same
finding), P4-27 (both gate scripts hand their engine step to the one helper in swarm/engine_select.py). P4-22 (the dependency
audit child receives no signing key) is proven here: `sh` takes an `env` argument and `dep_audit` passes it an allow-listed
environment, so no SWARM_* signing variable reaches the audit tool."""
from __future__ import annotations

import importlib.util
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest

from swarm import engine_select
from swarm.gates import make_finding
from swarm.errors import ErrorCode, SwarmError
from swarm.script_base import sh

ROOT = Path(__file__).resolve().parent.parent


def _load_sec_gate():
    spec = importlib.util.spec_from_file_location("sec_gate_under_test", ROOT / "scripts" / "sec_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"  # a zombie has exited; only its reaping is pending


# --- P4-17: a malformed allow-list is an E-INPUT error, not a traceback --------------------------------------------------------------

@pytest.mark.parametrize("content", [
    "{not json",
    "[42]",
    '[{"justification": "reviewed"}]',
    '[{"id": "SEC-1", "justification": "reviewed", "expires": "tomorrow"}]',
    '[{"id": "SEC-1", "justification": "reviewed", "expires": 5}]',
], ids=["invalid-json", "entry-not-object", "entry-without-id", "unparseable-expiry", "non-string-expiry"])
def test_malformed_allow_list_is_e_input(tmp_path, content):
    path = tmp_path / "allow.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SwarmError) as exc:
        _load_sec_gate().apply_allow_list([], str(path))
    assert exc.value.code is ErrorCode.E_INPUT


# --- P4-19 (runner side): a timed-out command takes its whole process group with it -------------------------------------------------

def test_sh_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        sh(f"sleep 60 & echo $! > '{pidfile}'; wait", timeout=1)
    pid = int(pidfile.read_text(encoding="utf-8"))
    # SIGKILL is delivered asynchronously: the grandchild must be gone within a bounded window, not at the instant sh returns
    deadline = time.monotonic() + 10
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(pid)


# --- P4-27: the gate scripts' engine step is the shared helper's, with their own route name --------------------------------------

@pytest.mark.parametrize(("name", "route"), [("sec_gate", "sec-gate"), ("qa_gate", "qa-gate")])
def test_gate_main_hands_its_engine_step_to_the_shared_helper(name, route, tmp_path, monkeypatch):
    calls = []

    def shared_helper(script, engine_routes, argv, **kwargs):
        calls.append((script.name, engine_routes, list(argv), kwargs["route_name"]))
        return 0

    argv = ["--json", "--root", str(tmp_path)]
    monkeypatch.setattr(engine_select, "run_gate_engine", shared_helper)
    monkeypatch.setattr(engine_select, "RELEASE_BINARY", tmp_path / "no-release")
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(tmp_path / "absent"))
    monkeypatch.setenv("GSD_QA_PYTHON", "")  # restored at teardown: the qa body sets it in os.environ
    monkeypatch.setattr(sys, "argv", [name, *argv])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(ROOT / "scripts" / f"{name}.py"), run_name="__main__")
    assert exit_info.value.code == 0
    assert calls == [(name, (), argv, route)]


# --- P4-22: the dependency audit child gets no signing material from the caller's environment -------------------------------------

SENTINEL_KEY_VALUE = "SWARM_TEST_SENTINEL"


def test_dep_audit_env_has_no_signing_key(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    captured = tmp_path / "audit-env.json"
    fake = bin_dir / "pip-audit"
    # A fake pip-audit that records the environment it was started with and reports no vulnerability
    fake.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"open({str(captured)!r}, 'w', encoding='utf-8').write(json.dumps(dict(os.environ)))\n"
        "print('[]')\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "requirements.txt").write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SWARM_SIGNING_KEY", SENTINEL_KEY_VALUE)
    monkeypatch.setenv("SWARM_ED25519_KEY", SENTINEL_KEY_VALUE)
    _, runs = _load_sec_gate().dep_audit(root, 30)
    assert runs == {"pip-audit": "pass"}
    child_env = json.loads(captured.read_text(encoding="utf-8"))
    # names and keys only: a failure must not echo the rest of the child's environment
    leaked_names = sorted(name for name in child_env if name.startswith("SWARM_") and name.endswith("KEY"))
    leaked_values = sorted(name for name, value in child_env.items() if SENTINEL_KEY_VALUE in value)
    assert leaked_names == []
    assert leaked_values == []


# --- P4-26: an expired suppression is reported as info; the finding it named stays reported --------------------------------------

def _allow_file(tmp_path, entry):
    path = tmp_path / "allow.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")
    return str(path)


def test_expired_suppression_emits_info_finding(tmp_path):
    finding = make_finding("SEC-1", "major", "secrets", "aws access key")
    path = _allow_file(tmp_path, {"id": "SEC-1", "justification": "old waiver", "expires": "2000-01-01"})
    kept, suppressed = _load_sec_gate().apply_allow_list([finding], path)
    assert suppressed == []
    assert [f["id"] for f in kept if f["severity"] != "info"] == ["SEC-1"]
    info = [f for f in kept if f["severity"] == "info"]
    assert len(info) == 1
    assert info[0]["kind"] == "allow-list"
    assert "SEC-1" in info[0]["summary"] and "2000-01-01" in info[0]["summary"]


def test_unexpired_suppression_emits_no_info_finding(tmp_path):
    finding = make_finding("SEC-1", "major", "secrets", "aws access key")
    path = _allow_file(tmp_path, {"id": "SEC-1", "justification": "fixture key", "expires": "2999-12-31"})
    kept, suppressed = _load_sec_gate().apply_allow_list([finding], path)
    assert kept == []
    assert [f["id"] for f in suppressed] == ["SEC-1"]
