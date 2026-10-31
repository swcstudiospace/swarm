"""SWARM_TEST_ENGINE selector (Phase 14.4, unit S1.3): the engine the black-box scheduler children run on.

`python` (or unset) leaves every child environment exactly as the allow-list produces it; `rust` adds the engine
variables for children only; anything else is a usage error. tests/test_scheduler_continuous.py needs no change because
Rig.run and Rig.spawn already build `{**_scrubbed_base_env(), **self.env}`."""
import json
import os
import subprocess
import sys

import pytest

from conftest import ROOT, _BASE_ENV_ALLOW, _scrubbed_base_env, pytest_configure

_PARENT = {  # what a polluted host looks like: allow-listed vars, a locale var, and vars the scrub must drop
    "SWARM_DIR": "/parent/swarm", "SWARM_PURE_PYTHON": "1", "LC_ALL": "C.UTF-8", "LC_FOO": "bar",
    "SWARM_TASK_ID": "T-parent", "HTTPS_PROXY": "http://proxy.invalid:3128", "GSD_ENGINE": "python",
}
_BIN = "/opt/gsd/gsd_engine"


@pytest.fixture()
def parent_env(monkeypatch):
    """A fully known parent environment: only _PARENT plus PATH and HOME, and no engine selector."""
    for name in list(os.environ):
        if name not in ("PATH", "HOME", "SWARM_SIGNING_KEY", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(name)
    for name, value in _PARENT.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def _pre_change_env() -> dict:
    """The environment before the selector existed: the parent's allow-listed and LC_* variables, nothing else."""
    return {k: v for k, v in os.environ.items() if k in _BASE_ENV_ALLOW or k.startswith("LC_")}


@pytest.mark.parametrize("selector,bin_in_parent,extra", [
    (None, False, {}),
    ("python", False, {}),
    ("python", True, {}),
    ("rust", False, {"GSD_ENGINE": "rust", "SWARM_PURE_PYTHON": "0"}),
    ("rust", True, {"GSD_ENGINE": "rust", "SWARM_PURE_PYTHON": "0", "GSD_ENGINE_BIN": _BIN}),
], ids=["unset", "python", "python-bin-not-forwarded", "rust", "rust-forwards-bin"])
def test_child_environment_per_engine(parent_env, selector, bin_in_parent, extra):
    if selector is not None:
        parent_env.setenv("SWARM_TEST_ENGINE", selector)
    if bin_in_parent:
        parent_env.setenv("GSD_ENGINE_BIN", _BIN)
    expected = {**_pre_change_env(), **extra}
    assert _scrubbed_base_env() == expected
    assert expected["SWARM_DIR"] == "/parent/swarm" and expected["LC_FOO"] == "bar"
    assert "SWARM_TASK_ID" not in expected and "HTTPS_PROXY" not in expected
    # the selector itself and the host's own engine variables never leak through the allow-list
    assert "SWARM_TEST_ENGINE" not in expected
    assert expected.get("GSD_ENGINE") == extra.get("GSD_ENGINE")
    assert expected.get("SWARM_PURE_PYTHON") == extra.get("SWARM_PURE_PYTHON", "1")


@pytest.mark.parametrize("selector", ["Rust", "rust ", "", "bogus", "rounds"])
def test_an_invalid_engine_fails_loudly_instead_of_running_python(parent_env, selector):
    parent_env.setenv("SWARM_TEST_ENGINE", selector)
    with pytest.raises(pytest.UsageError, match="SWARM_TEST_ENGINE") as err:
        _scrubbed_base_env()
    assert repr(selector) in str(err.value)
    with pytest.raises(pytest.UsageError, match="SWARM_TEST_ENGINE"):
        pytest_configure(None)  # the session-start check that aborts the whole run


@pytest.mark.parametrize("selector", [None, "python", "rust"])
def test_the_session_start_check_accepts_the_valid_values(parent_env, selector):
    if selector is not None:
        parent_env.setenv("SWARM_TEST_ENGINE", selector)
    assert pytest_configure(None) is None


def test_the_rust_selector_changes_children_only(parent_env):
    parent_env.setenv("SWARM_TEST_ENGINE", "rust")
    child = _scrubbed_base_env()
    assert child["SWARM_PURE_PYTHON"] == "0" and child["GSD_ENGINE"] == "rust"
    assert os.environ["SWARM_PURE_PYTHON"] == "1" and os.environ["GSD_ENGINE"] == "python"  # the pytest process keeps pure Python
    assert "GSD_ENGINE_BIN" not in child  # absent in the parent, so absent in the child


_PROBE = "import json, os; print(json.dumps({k: os.environ.get(k) for k in ('GSD_ENGINE', 'SWARM_PURE_PYTHON', 'SWARM_TASK_ID')}))"


@pytest.mark.parametrize("selector,override,seen", [
    ("rust", {}, {"GSD_ENGINE": "rust", "SWARM_PURE_PYTHON": "0", "SWARM_TASK_ID": None}),
    ("rust", {"GSD_ENGINE": "python"}, {"GSD_ENGINE": "python", "SWARM_PURE_PYTHON": "0", "SWARM_TASK_ID": None}),
    ("rust", {"SWARM_PURE_PYTHON": "1", "SWARM_TASK_ID": "T-own"}, {"GSD_ENGINE": "rust", "SWARM_PURE_PYTHON": "1", "SWARM_TASK_ID": "T-own"}),
    ("python", {"GSD_ENGINE": "rust"}, {"GSD_ENGINE": "rust", "SWARM_PURE_PYTHON": "1", "SWARM_TASK_ID": None}),
    (None, {}, {"GSD_ENGINE": None, "SWARM_PURE_PYTHON": "1", "SWARM_TASK_ID": None}),
], ids=["rust", "rust-test-selects-python", "rust-test-pins-pure-python", "python-test-selects-rust", "unset"])
def test_a_tests_own_env_override_wins_over_the_forwarded_values(parent_env, selector, override, seen):
    """A real child built the way Rig.run builds it, `{**_scrubbed_base_env(), **env}`, reports what it was given."""
    if selector is not None:
        parent_env.setenv("SWARM_TEST_ENGINE", selector)
    done = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=30,
                          env={**_scrubbed_base_env(), **override})
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == seen


@pytest.mark.parametrize("selector", [None, "python"], ids=["unset", "python"])
def test_python_child_start_reuses_original_controller_monitor_helper(parent_env, selector):
    """Helper extraction leaves the Python child on its original controller and pool."""
    if selector is not None:
        parent_env.setenv("SWARM_TEST_ENGINE", selector)
    code = f"""
import importlib.util, json, sys, threading
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location("swarm_run_rig_helper", {str(ROOT / "scripts" / "swarm_run.py")!r})
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
controller = mod.Scheduler.__new__(mod.Scheduler)
controller.mgr = SimpleNamespace(max_concurrent=1)
controller._monitor_stop = threading.Event()
seen = []
real = mod.Scheduler._start_monitor
def start_monitor(self):
    seen.append((self, self.pool))
    return real(self)
mod.Scheduler._start_monitor = start_monitor
mod.Scheduler._watch = lambda self: self._monitor_stop.wait(5)
controller.start()
try:
    print(json.dumps({{
        "same_controller": len(seen) == 1 and seen[0][0] is controller,
        "same_pool": len(seen) == 1 and seen[0][1] is controller.pool,
        "python_pool": isinstance(controller.pool, mod.WorkerPool),
        "monitor_daemon": controller._monitor.daemon,
    }}))
finally:
    controller._monitor_stop.set()
    controller._monitor.join(5)
    controller.pool.shutdown()
"""
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                          cwd=ROOT, env=_scrubbed_base_env())
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "same_controller": True, "same_pool": True, "python_pool": True, "monitor_daemon": True,
    }
