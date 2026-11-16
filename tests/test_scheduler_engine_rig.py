"""SWARM_TEST_ENGINE selector (Phase 14.4, unit S1.3): the engine the black-box scheduler children run on.

`python` (or unset) leaves every child environment exactly as the allow-list produces it; `rust` adds the engine
variables for children only; anything else is a usage error. tests/test_scheduler_continuous.py needs no change because
Rig.run and Rig.spawn already build `{**_scrubbed_base_env(), **self.env}`."""
import importlib.util
import json
import os
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest

from conftest import ROOT, _BASE_ENV_ALLOW, _scrubbed_base_env, pytest_configure, run_script

from swarm.errors import ErrorCode, SwarmError
from swarm.taskstore import TaskStore

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


# ------------------------------------------------------------------------------------------ GSD_ENGINE selection in run()
_RUNNER = ROOT / "scripts" / "swarm_run.py"
# the named refusals of validate_runtime_approval: (name, the option, the code the python engine keeps)
_UNSUPPORTED = [
    ("grok-transport", {"runtime": "grok"}, ErrorCode.E_DEP),
    ("omp-approval", {"runtime": "omp", "approval_mode": "yolo"}, ErrorCode.E_INPUT),
    ("claude-bypass", {"permission_mode": "bypassPermissions"}, ErrorCode.E_INPUT),
]


def _runner():
    """The runner as its own module, loaded the way the black-box suite loads it."""
    spec = importlib.util.spec_from_file_location("swarm_run_engine_rig", _RUNNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _planned(tmp_path):
    """A git workspace with one planned correlation, planned as the black-box dry-run smoke test plans one."""
    work, swarm = tmp_path / "work", tmp_path / ".swarm"
    work.mkdir()
    env = {"SWARM_DIR": str(swarm), "SWARM_PURE_PYTHON": "1"}  # setup only: planning needs no built extension
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    plan = run_script("orch_plan.py", "--repo", str(work), "--brief-text", "smoke", "--pattern", "hotfix", "--json", env=env)
    assert plan.returncode == 0, plan.stdout + plan.stderr
    corr = {t["correlation_id"] for t in TaskStore(swarm / "tasks.db").list()}
    assert len(corr) == 1
    return work, swarm, env, corr.pop()


def _args(work, **overrides):
    base = {"repo": str(work), "dry_run": True, "runtime": "claude", "claude_bin": "claude", "grok_bin": "grok",
            "omp_bin": "omp", "approval_mode": "write", "max_parallel": 2, "max_rounds": 10, "once": False,
            "max_turns": 1, "task_timeout": 30, "permission_mode": "default", "model": None, "allowed_tools": ""}
    return SimpleNamespace(**{**base, **overrides})


def _ctx(work, corr):
    return SimpleNamespace(correlation_id=corr, emit=lambda task, payload, **kw: None, root=work)


def _store_state(swarm, corr):
    """Task states and the store's transition count: a claim or a dispatch moves one or both."""
    store = TaskStore(swarm / "tasks.db")
    states = {t["task_id"]: t["state"] for t in store.list(correlation_id=corr)}
    return states, store.conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0]


def _native_surface(monkeypatch, *, run=True, fence=True):
    """A stand-in swarm._native with the names the runner checks. A call to its run_scheduler is recorded, never run."""
    module = ModuleType("swarm._native")
    calls = []
    if run:
        module.run_scheduler = lambda *args, **kwargs: calls.append((args, kwargs))
    if fence:
        module.NativeCleanupFence = lambda: object()
    monkeypatch.setitem(sys.modules, "swarm._native", module)
    return calls


def _lease_calls(monkeypatch, mod):
    """Every lease_bridge call: the runner takes its leases there, so an empty list means no lease was reached."""
    calls = []
    monkeypatch.setattr(mod, "lease_bridge", lambda *args, **kwargs: calls.append(args))
    return calls


@pytest.mark.usefixtures("native_extension")
def test_the_default_engine_runs_the_python_scheduler(tmp_path, scheduler_engine_env):
    # in-process: the selector reads the test's own environment, with SWARM_PURE_PYTHON unset and the package already imported
    work, swarm, _env, corr = _planned(tmp_path)
    scheduler_engine_env.setenv("SWARM_DIR", str(swarm))
    out = _runner().run(_args(work), _ctx(work, corr))
    assert out["complete"] is True and out["scheduler"] == "continuous"
    assert [line for line in out["log"] if line.startswith("scheduler_engine:")] == ["scheduler_engine: python"]


def test_swarm_pure_python_wins_over_rust_with_one_warning(tmp_path, scheduler_engine_env):
    work, _swarm, env, _corr = _planned(tmp_path)
    r = run_script("swarm_run.py", "--repo", str(work), "--dry-run", "--json",
                   env={**env, "GSD_ENGINE": "rust", "SWARM_PURE_PYTHON": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    out = json.loads(r.stdout)
    assert out["complete"] is True
    assert [line for line in out["log"] if line.startswith("scheduler_engine:")] == ["scheduler_engine: python"]
    assert len([line for line in r.stderr.splitlines() if "SWARM_PURE_PYTHON" in line]) == 1


@pytest.mark.parametrize("selector", ["bogus", "Rust", "rust ", "rounds"])
def test_an_unknown_engine_is_an_input_error_before_any_claim(tmp_path, scheduler_engine_env, selector):
    work, swarm, _env, corr = _planned(tmp_path)
    scheduler_engine_env.setenv("SWARM_DIR", str(swarm))
    scheduler_engine_env.setenv("GSD_ENGINE", selector)
    mod = _runner()
    leases = _lease_calls(scheduler_engine_env, mod)
    before = _store_state(swarm, corr)
    with pytest.raises(SwarmError) as err:
        mod.run(_args(work), _ctx(work, corr))
    assert err.value.code is ErrorCode.E_INPUT
    assert repr(selector) not in str(err.value)  # the value is never echoed
    assert leases == [] and _store_state(swarm, corr) == before


@pytest.mark.parametrize("surface", ["absent", "no-run_scheduler", "no-NativeCleanupFence"])
def test_rust_without_the_complete_native_surface_refuses_before_any_claim(tmp_path, scheduler_engine_env, surface):
    work, swarm, _env, corr = _planned(tmp_path)
    scheduler_engine_env.setenv("SWARM_DIR", str(swarm))
    scheduler_engine_env.setenv("GSD_ENGINE", "rust")
    if surface == "absent":
        scheduler_engine_env.setitem(sys.modules, "swarm._native", None)  # an import of it fails, as a missing build does
    else:
        _native_surface(scheduler_engine_env, run=surface != "no-run_scheduler", fence=surface != "no-NativeCleanupFence")
    mod = _runner()
    leases = _lease_calls(scheduler_engine_env, mod)
    before = _store_state(swarm, corr)
    with pytest.raises(SwarmError) as err:
        mod.run(_args(work), _ctx(work, corr))
    assert err.value.code is ErrorCode.E_DEP
    assert leases == [] and _store_state(swarm, corr) == before


@pytest.mark.parametrize("option", [option for _, option, _ in _UNSUPPORTED], ids=[name for name, _, _ in _UNSUPPORTED])
def test_rust_refuses_an_unsupported_approval_option_before_any_claim(tmp_path, scheduler_engine_env, option):
    work, swarm, _env, corr = _planned(tmp_path)
    scheduler_engine_env.setenv("SWARM_DIR", str(swarm))
    scheduler_engine_env.setenv("GSD_ENGINE", "rust")
    native_calls = _native_surface(scheduler_engine_env)  # complete, so the refusal is what stops the run
    mod = _runner()
    leases = _lease_calls(scheduler_engine_env, mod)
    before = _store_state(swarm, corr)
    with pytest.raises(SwarmError) as err:
        mod.run(_args(work, **option), _ctx(work, corr))
    assert err.value.code is ErrorCode.E_POLICY
    assert leases == [] and native_calls == [] and _store_state(swarm, corr) == before


@pytest.mark.parametrize("option,code", [(option, code) for _, option, code in _UNSUPPORTED],
                         ids=[name for name, _, _ in _UNSUPPORTED])
def test_the_python_engine_keeps_its_approval_refusal_codes(tmp_path, scheduler_engine_env, option, code):
    work, swarm, _env, corr = _planned(tmp_path)
    scheduler_engine_env.setenv("SWARM_DIR", str(swarm))
    mod = _runner()
    leases = _lease_calls(scheduler_engine_env, mod)
    before = _store_state(swarm, corr)
    with pytest.raises(SwarmError) as err:
        mod.run(_args(work, **option), _ctx(work, corr))
    assert err.value.code is code
    assert leases == [] and _store_state(swarm, corr) == before
