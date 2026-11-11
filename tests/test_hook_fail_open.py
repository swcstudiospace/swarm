"""Fail-open hooks: a broken swarm dependency or an unwritable state dir must end in exit 0, never a traceback."""
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "hooks" / "autonomous_run.py"
ON_A01 = ROOT / "hooks" / "on_a01_complete.py"
PROMPT_HOOK = ROOT / "hooks" / "user_prompt_submit.py"
GREPTILE_INGEST = ROOT / "scripts" / "rev_greptile_ingest.py"


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "SWARM_CHILD")}
    env["AIO_SWARM"] = "0"
    return env


def _run_hook_with_blocked_module(hook: Path, module: str, stdin_text: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run `hook` as __main__ in a child whose `module` import is poisoned: sys.modules[module] = None makes it raise ImportError."""
    probe = f"import runpy, sys\nsys.modules[{module!r}] = None\nrunpy.run_path({str(hook)!r}, run_name='__main__')\n"
    return subprocess.run([sys.executable, "-c", probe], input=stdin_text, capture_output=True, text=True,
                          cwd=cwd, env=_env(), timeout=120)


def _load_runner():
    spec = importlib.util.spec_from_file_location("autonomous_run_fail_open_under_test", RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_exits_zero_when_swarm_dir_unwritable(tmp_path):
    """SWARM_DIR names a regular file, so creating the state dir fails. The runner must still exit 0 with JSON on stdout."""
    blocker = tmp_path / "swarm-state"
    blocker.write_text("not a directory\n")
    workdir = tmp_path / "repo"
    workdir.mkdir()
    env = {**os.environ, "SWARM_DIR": str(blocker)}
    r = subprocess.run(
        [sys.executable, str(RUNNER), "--cwd", str(workdir), "--brief", "implement x",
         "--swarm-root", str(ROOT), "--dry-run"],
        cwd=workdir, capture_output=True, text=True, env=env, timeout=120,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    json.loads(r.stdout)


def test_on_a01_complete_exits_zero_when_hook_trigger_unimportable(tmp_path):
    """swarm.hook_trigger cannot be imported: the shim must print the empty result and exit 0."""
    r = _run_hook_with_blocked_module(ON_A01, "swarm.hook_trigger", "{}", tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "{}"


@pytest.mark.parametrize("wrapper", ["BUILD_PROMPT", "ultrathink"])
def test_fallback_stays_silent_on_uplift_wrapped_prompts(tmp_path, wrapper):
    """With the classifier unavailable, a question wrapped in an uplift tag must stay silent though the outer text says "Implement".
    The plain control runs first under the same poisoning and must inject, so silence cannot come from a dead fallback."""
    control = _run_hook_with_blocked_module(PROMPT_HOOK, "swarm.signal_detector",
                                            json.dumps({"prompt": "Implement the cache layer."}), tmp_path)
    assert control.returncode == 0, control.stderr
    assert "additionalContext" in control.stdout, control.stdout
    prompt = f"<{wrapper}>Implement the cache layer.<ORIGINAL>how does the cache work?</ORIGINAL></{wrapper}>"
    r = _run_hook_with_blocked_module(PROMPT_HOOK, "swarm.signal_detector", json.dumps({"prompt": prompt}), tmp_path)
    assert r.returncode == 0, r.stderr
    assert "additionalContext" not in r.stdout, r.stdout


def test_run_capped_accepts_cap_beyond_poll_range():
    """A cap of 1e7 seconds is valid config. It must run the command to completion and not overflow the poll timeout."""
    runner = _load_runner()
    r = runner.run_capped([sys.executable, "-c", "print('ok')"], cwd=".", env=os.environ.copy(), timeout=1e7, grace=1)
    assert r.returncode == 0
    assert r.stdout == "ok\n"


def _load_prompt_hook():
    spec = importlib.util.spec_from_file_location("user_prompt_submit_fail_open_under_test", PROMPT_HOOK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_outer_failure_traced(monkeypatch, capsys):
    """An exception outside the classifier still ends in `{}` with exit 0, and stderr names it so the failure is visible."""
    hook = _load_prompt_hook()

    def broken(payload):
        raise RuntimeError("extract-marker")

    monkeypatch.setattr(hook, "extract_prompt", broken)
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"prompt": "implement x"}'))
    assert hook.main() == 0
    captured = capsys.readouterr()
    assert captured.out.strip() == "{}"
    assert "RuntimeError" in captured.err and "extract-marker" in captured.err


def test_greptile_over_cap_input_is_e_input(monkeypatch, tmp_path):
    """An input file above MAX_INPUT_BYTES is refused with E_INPUT before it is parsed."""
    spec = importlib.util.spec_from_file_location("rev_greptile_ingest_fail_open_under_test", GREPTILE_INGEST)
    assert spec is not None and spec.loader is not None
    ingest = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingest)
    from swarm.errors import ErrorCode, SwarmError

    monkeypatch.setattr(ingest, "MAX_INPUT_BYTES", 16)
    big = tmp_path / "threads.json"
    big.write_text('{"data": {"padding": "' + "x" * 64 + '"}}')
    with pytest.raises(SwarmError) as info:
        ingest._load_file(str(big))
    assert info.value.code is ErrorCode.E_INPUT
