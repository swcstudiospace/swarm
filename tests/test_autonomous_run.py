import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "hooks" / "autonomous_run.py"


def test_autonomous_dry_run(tmp_path):
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "--cwd",
            str(tmp_path),
            "--brief",
            "implement a billing feature",
            "--runtime",
            "auto",
            "--swarm-root",
            str(ROOT),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    log = json.loads((tmp_path / ".swarm" / "autonomous.log").read_text())
    assert log["plan_rc"] == 0
    assert log["run_rc"] == 0
    assert "DONE" in log["run_out"] or "complete" in log["run_out"].lower() or '"complete": true' in log["run_out"]


def test_autonomous_kicks_sdlc_prompt(tmp_path):
    """An SDLC-shaped brief passes the gate: plan and dry-run happen, and the log records both."""
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "add a /health endpoint", "--swarm-root", str(ROOT), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    log = json.loads((tmp_path / ".swarm" / "autonomous.log").read_text())
    assert log["plan_rc"] == 0
    assert log["run_rc"] == 0


def test_autonomous_skips_question_without_starting_a_run(tmp_path):
    """A question is non-sdlc: the gate exits 0 before any plan or run, so no child starts and no log is written."""
    import shutil
    root = tmp_path / "swarm-root"
    shutil.copytree(ROOT / "swarm", root / "swarm")
    (root / "scripts").mkdir()
    started = tmp_path / "started"
    for name in ("orch_plan.py", "swarm_run.py"):
        (root / "scripts" / name).write_text(f"open({str(started)!r}, 'a').write({name!r} + chr(10))\n")
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "what is a monad",
         "--swarm-root", str(root), "--dry-run"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert not started.exists()
    assert not (tmp_path / ".swarm" / "autonomous.log").exists()


def test_rejected_prompt_appends_no_hook_main_phase(tmp_path):
    """H-01: a prompt the gate rejects exits 0 and appends no phase.hook.main record to events.jsonl.
    The accepted-prompt control shows the same hook records phase.hook.main when the gate passes."""
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "what is a monad",
         "--swarm-root", str(ROOT), "--dry-run"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    events = tmp_path / ".swarm" / "events.jsonl"
    assert "phase.hook.main" not in (events.read_text() if events.exists() else "")

    root = _stub_root(tmp_path, "pass\n")
    accepted = {**os.environ, "SWARM_DIR": str(tmp_path / "accepted-swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "implement a billing feature",
         "--swarm-root", str(root), "--dry-run"],
        capture_output=True, text=True, env=accepted, timeout=60,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert "phase.hook.main" in (tmp_path / "accepted-swarm" / "events.jsonl").read_text()



def test_autonomous_skips_slash(tmp_path):
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "/uplift last", "--swarm-root", str(ROOT), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert r.returncode == 0
    assert not (tmp_path / ".swarm" / "autonomous.log").exists()


def test_autonomous_plans_from_uplifted_spec(tmp_path):
    """The brief classifies the kick; the uplifted spec (when given) is what A01 plans from."""
    spec = tmp_path / "spec.xml"
    spec.write_text("<task><goal>Add invoice PDF export</goal><acceptance>PDF matches fixture</acceptance></task>\n")
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "implement invoice export",
         "--spec", str(spec), "--swarm-root", str(ROOT), "--dry-run"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    log = json.loads((tmp_path / ".swarm" / "autonomous.log").read_text())
    assert log["plan_rc"] == 0
    assert log["spec"] == str(spec)
    corr = (tmp_path / ".swarm" / "latest_correlation").read_text().strip()
    plan = json.loads((tmp_path / ".swarm" / "plans" / f"{corr}.json").read_text())
    assert plan["brief"].strip() == spec.read_text().strip()


def test_autonomous_skips_run_on_plan_failure(tmp_path):
    import shutil
    root = tmp_path / "swarm-root"
    shutil.copytree(ROOT / "swarm", root / "swarm")
    (root / "scripts").mkdir()
    sentinel = tmp_path / "ran"
    (root / "scripts" / "orch_plan.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'status': 'error', 'error': {'code': 'E-CONTRACT', 'message': 'prefix taken'}}))\n"
        "sys.exit(2)\n")
    (root / "scripts" / "swarm_run.py").write_text(f"open({str(sentinel)!r}, 'w').write('x')\n")
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "implement a billing feature",
         "--swarm-root", str(root), "--dry-run"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    assert not sentinel.exists()
    log = json.loads((tmp_path / ".swarm" / "autonomous.log").read_text())
    assert log["plan_rc"] == 2 and log["skipped_run"] is True
    assert log["error"]["code"] == "E-CONTRACT"
    assert "run_rc" not in log


def test_autonomous_forwards_runtime_omp(tmp_path):
    import shutil
    root = tmp_path / "swarm-root"
    shutil.copytree(ROOT / "swarm", root / "swarm")
    (root / "scripts").mkdir()
    events = tmp_path / "events.jsonl"
    (root / "scripts" / "orch_plan.py").write_text(
        "import json, sys\n"
        f"open({str(events)!r}, 'a').write(json.dumps(['plan', sys.argv[1:]]) + '\\n')\n"
        "print(json.dumps({'status': 'ok', 'correlation_id': 'corr-omp-1'}))\n")
    (root / "scripts" / "swarm_run.py").write_text(
        "import json, sys\n"
        f"open({str(events)!r}, 'a').write(json.dumps(['run', sys.argv[1:]]) + '\\n')\n")
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm")}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "implement a billing feature",
         "--swarm-root", str(root), "--runtime", "omp", "--dry-run"],
        capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    calls = [json.loads(line) for line in events.read_text().splitlines()]
    assert [name for name, _ in calls] == ["plan", "run"]
    run_argv = calls[1][1]
    assert run_argv[run_argv.index("--runtime") + 1] == "omp"
    assert run_argv[run_argv.index("--correlation-id") + 1] == "corr-omp-1"
    assert "--dry-run" in run_argv
    log = json.loads((tmp_path / ".swarm" / "autonomous.log").read_text())
    assert log["plan_rc"] == 0 and log["run_rc"] == 0


def _load_hook():
    import importlib.util
    spec = importlib.util.spec_from_file_location("autonomous_run_under_test", RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_kickoff_lock_is_held_until_released_and_then_debounced(tmp_path):
    """T-06-18: a second kick cannot start while the first holds the lock, nor within 120s of the release mtime.

    main rewrites the fd on the way out. A release stamp blocks the next acquire immediately, even when the
    acquisition mtime is already older than 120s, and allows it once that release mtime is itself that old."""
    import os
    import time
    mod = _load_hook()
    lock = tmp_path / "kickoffs" / "same.lock"
    held = mod.acquire(lock)
    assert held is not None
    assert mod.acquire(lock) is None
    os.utime(lock, (time.time() - 121, time.time() - 121))
    mod._stamp_lock(held)  # what main's finally does before close: the debounce starts at release
    os.close(held)
    assert mod.acquire(lock) is None
    os.utime(lock, (time.time() - 121, time.time() - 121))
    again = mod.acquire(lock)
    assert again is not None
    os.close(again)


def test_env_seconds_falls_back_on_blank_or_non_positive(monkeypatch):
    mod = _load_hook()
    monkeypatch.delenv("SWARM_AUTONOMOUS_RUN_CAP_S", raising=False)
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 3600
    monkeypatch.setenv("SWARM_AUTONOMOUS_RUN_CAP_S", "  ")
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 3600
    monkeypatch.setenv("SWARM_AUTONOMOUS_RUN_CAP_S", "nope")
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 3600
    monkeypatch.setenv("SWARM_AUTONOMOUS_RUN_CAP_S", "0")
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 3600
    monkeypatch.setenv("SWARM_AUTONOMOUS_RUN_CAP_S", "-4")
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 3600
    monkeypatch.setenv("SWARM_AUTONOMOUS_RUN_CAP_S", "inf")
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 3600
    monkeypatch.setenv("SWARM_AUTONOMOUS_RUN_CAP_S", "1e309")
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 3600
    monkeypatch.setenv("SWARM_AUTONOMOUS_RUN_CAP_S", "12.5")
    assert mod._env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600) == 12.5


def _stub_root(tmp_path, body: str):
    import shutil
    root = tmp_path / "swarm-root"
    shutil.copytree(ROOT / "swarm", root / "swarm")
    (root / "scripts").mkdir()
    (root / "scripts" / "orch_plan.py").write_text(
        "import json, sys\n"
        "print(json.dumps({'status': 'ok', 'correlation_id': 'corr-cap'}))\n")
    (root / "scripts" / "swarm_run.py").write_text(body)
    return root


def test_run_cap_sigterms_the_runner_before_killing_it(tmp_path):
    """T-06-26: the cap must not SIGKILL a runner that still needs to end its sessions."""
    import time
    marker = tmp_path / "got-sigterm"
    root = _stub_root(tmp_path, f"""import json, signal, sys, time
def stop(signum, _frame):
    open({str(marker)!r}, "w").write(str(signum))
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print(json.dumps({{"status": "ok"}}))
sys.stdout.flush()
time.sleep(30)
""")
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm"),
           "SWARM_AUTONOMOUS_RUN_CAP_S": "1", "SWARM_AUTONOMOUS_RUN_GRACE_S": "5"}
    started = time.monotonic()
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "implement a billing feature",
         "--swarm-root", str(root)],
        capture_output=True, text=True, env=env, timeout=20,
    )
    elapsed = time.monotonic() - started
    assert r.returncode == 0, r.stderr + r.stdout
    assert marker.read_text() == str(signal.SIGTERM)
    assert elapsed < 8, elapsed
    log = json.loads((tmp_path / ".swarm" / "autonomous.log").read_text())
    assert log["run_rc"] == 0


def test_run_cap_sigkills_a_runner_that_ignores_sigterm(tmp_path):
    """The grace is finite: a runner that ignores SIGTERM is then SIGKILLed, and the log shows that signal."""
    import time
    root = _stub_root(tmp_path, """import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(30)
""")
    env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm"),
           "SWARM_AUTONOMOUS_RUN_CAP_S": "1", "SWARM_AUTONOMOUS_RUN_GRACE_S": "1"}
    started = time.monotonic()
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", "implement a billing feature",
         "--swarm-root", str(root)],
        capture_output=True, text=True, env=env, timeout=20,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 8, elapsed
    log = json.loads((tmp_path / ".swarm" / "autonomous.log").read_text())
    assert log["run_rc"] == -signal.SIGKILL
    assert r.returncode == 0  # the hook fails open; the runner's SIGKILL is recorded in the log above


def test_run_exports_its_grace_as_the_runners_shutdown_budget(tmp_path):
    """RV-A P1: the runner sized its own shutdown (10 s wait + 5 s kill) without knowing the supervisor's 15 s grace,
    so its sessions were SIGKILLed alongside it. The hook now tells the runner its grace."""
    seen = tmp_path / "budget"
    root = _stub_root(tmp_path, f"""import json, os
open({str(seen)!r}, "w").write(os.environ.get("SWARM_SHUTDOWN_BUDGET_S", "unset"))
print(json.dumps({{"status": "ok"}}))
""")
    briefs = iter(["implement a billing feature", "implement a billing report", "implement a billing export"])

    def kick(**extra):  # a different brief per kick: the same one is debounced for 120 s
        env = {**os.environ, "SWARM_DIR": str(tmp_path / ".swarm"), **extra}
        for name in ("SWARM_AUTONOMOUS_RUN_GRACE_S", "SWARM_SHUTDOWN_BUDGET_S"):
            if name not in extra:
                env.pop(name, None)
        subprocess.run([sys.executable, str(RUNNER), "--cwd", str(tmp_path), "--brief", next(briefs),
                        "--swarm-root", str(root)], capture_output=True, text=True, env=env, timeout=30)
        return seen.read_text()

    assert kick() == "15"
    assert kick(SWARM_AUTONOMOUS_RUN_GRACE_S="7.5") == "7.5"
    assert kick(SWARM_AUTONOMOUS_RUN_GRACE_S="nope", SWARM_SHUTDOWN_BUDGET_S="99") == "15"  # the supervisor's grace wins
