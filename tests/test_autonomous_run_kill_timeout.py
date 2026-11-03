"""P4-09: run_capped must return after SIGKILL even when a grandchild keeps the stdout/stderr pipes open."""
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "hooks" / "autonomous_run.py"


_HARNESS = r"""
import importlib.util
import os
import signal
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("autonomous_run_kill_timeout", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
pid_file = Path(sys.argv[2])
# Only the shell is killed by run_capped. The sleep keeps both captured pipes open.
script = 'trap "" TERM; sleep 1000 & echo $! > "$1"; wait'
result = mod.run_capped(
    ["sh", "-c", script, "run-capped-child", str(pid_file)],
    cwd=str(pid_file.parent), env=dict(os.environ), timeout=1, grace=1,
)
assert result.returncode == -signal.SIGKILL, result
grandchild_pid = int(pid_file.read_text().strip())
os.kill(grandchild_pid, 0)
print("RETURNED_WITH_OPEN_PIPES", flush=True)
"""


def test_run_capped_returns_when_a_grandchild_outlives_the_sigkill(tmp_path):
    # Isolate the probe and all descendants so even a regressed, blocked run_capped
    # can be killed and reaped without leaving a worker thread in the test process.
    proc = subprocess.Popen(
        [sys.executable, "-c", _HARNESS, str(RUNNER), str(tmp_path / "grandchild.pid")],
        cwd=tmp_path, env=dict(os.environ), start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError(
                "run_capped blocked on pipes held by a grandchild after SIGKILL"
            ) from exc
        assert proc.returncode == 0, err + out
        assert "RETURNED_WITH_OPEN_PIPES" in out, err + out
    finally:
        # Kill the entire owned group even if the harness already exited, since
        # its grandchild intentionally remains alive after the behavioral probe.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=5)


requires_proc = pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads process state from /proc")
KICK_BRIEF = "implement a billing feature"
GRACE_S = 3


def _proc_fields(pid):
    """Fields after the command name in /proc/<pid>/stat: state, ppid, pgrp, ... None once the process is gone."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (OSError, IndexError):
        return None


def _alive(pid):
    fields = _proc_fields(pid)
    return fields is not None and fields[0] != "Z"


def _group_members(pgid):
    """Live (non-zombie) pids in process group pgid."""
    members = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            fields = _proc_fields(int(entry.name))
            if fields and fields[0] != "Z" and int(fields[2]) == pgid:
                members.append(int(entry.name))
    return members


def _kick_env(tmp_path):
    return {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm"),
            "SWARM_AUTONOMOUS_RUN_GRACE_S": str(GRACE_S), "SWARM_AUTONOMOUS_RUN_CAP_S": "60"}


def _kick_argv(tmp_path, root):
    return [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", KICK_BRIEF, "--swarm-root", str(root)]


def _kill_root(tmp_path):
    """A swarm root whose planner succeeds at once and whose runner writes its pid, then sleeps 60 s."""
    import shutil
    root = tmp_path / "swarm-root"
    shutil.copytree(ROOT / "swarm", root / "swarm")
    (root / "scripts").mkdir()
    (root / "scripts" / "orch_plan.py").write_text(
        "import json\n"
        "print(json.dumps({'status': 'ok', 'correlation_id': 'corr-kill'}))\n")
    pidfile = tmp_path / "runner.pid"
    staged = tmp_path / "runner.pid.tmp"
    (root / "scripts" / "swarm_run.py").write_text(
        "import os, time\n"
        f"open({str(staged)!r}, 'w').write(str(os.getpid()))\n"
        f"os.replace({str(staged)!r}, {str(pidfile)!r})\n"
        "time.sleep(60)\n")
    return root, pidfile


def _start_hook(tmp_path, root):
    return subprocess.Popen(_kick_argv(tmp_path, root), cwd=tmp_path, env=_kick_env(tmp_path),
                            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _await_runner_pid(hook, pidfile):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if pidfile.exists():
            return int(pidfile.read_text())
        if hook.poll() is not None:
            raise AssertionError(f"hook exited with {hook.returncode} before starting its runner")
        time.sleep(0.05)
    raise AssertionError("runner never started")


def _reap(hook, runner_pid):
    """Kill what a failed test left behind: the hook's group and the runner's group."""
    for pgid in (hook.pid, runner_pid):
        if pgid is None:
            continue
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    hook.wait(timeout=10)


@requires_proc
def test_sigterm_to_the_hook_ends_the_runner_group_within_grace(tmp_path):
    """H-03: SIGTERM to the hook must end the runner's process group within SWARM_AUTONOMOUS_RUN_GRACE_S."""
    root, pidfile = _kill_root(tmp_path)
    hook = _start_hook(tmp_path, root)
    runner_pid = None
    try:
        runner_pid = _await_runner_pid(hook, pidfile)
        os.kill(hook.pid, signal.SIGTERM)
        deadline = time.monotonic() + GRACE_S + 2
        while _group_members(runner_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _group_members(runner_pid) == [], f"runner group {runner_pid} outlived SIGTERM to the hook"
    finally:
        _reap(hook, runner_pid)


@requires_proc
def test_sigkill_to_the_hook_keeps_the_runner_lock_and_a_second_kick_is_a_duplicate(tmp_path):
    """H-03: SIGKILL cannot be handled, so the runner outlives the hook. The runner must keep the kickoff flock, and an
    immediate second kick must log 'skip duplicate'."""
    root, pidfile = _kill_root(tmp_path)
    hook = _start_hook(tmp_path, root)
    runner_pid = None
    try:
        runner_pid = _await_runner_pid(hook, pidfile)
        os.kill(hook.pid, signal.SIGKILL)
        hook.wait(timeout=10)
        assert _alive(runner_pid), "runner died with the hook"
        [lock] = list((tmp_path / ".swarm" / "kickoffs").glob("*.lock"))
        probe = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        second = subprocess.run(_kick_argv(tmp_path, root), capture_output=True, text=True,
                                env=_kick_env(tmp_path), timeout=30)
        assert second.returncode == 0, second.stderr + second.stdout
        assert "skip duplicate" in (tmp_path / ".swarm" / "autonomous.log").read_text()
    finally:
        _reap(hook, runner_pid)
