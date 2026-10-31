"""P4-09: run_capped must return after SIGKILL even when a grandchild keeps the stdout/stderr pipes open."""
import os
import signal
import subprocess
import sys
from pathlib import Path

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
