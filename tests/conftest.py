"""Shared fixtures: isolated SWARM_DIR, script runner, stub claude and omp CLIs, omp workspace-install dirs."""
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

os.environ.setdefault("SWARM_PURE_PYTHON", "1")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from install_helpers import _ENV_KEYS, _yaml  # noqa: E402  (after ROOT is on sys.path, as the installer expects)


@pytest.fixture(autouse=True)
def ephemeral_signing_key(monkeypatch, tmp_path_factory):
    """Each test gets a throwaway HMAC key and isolated private-key discovery directories.

    The value is generated here and is not printed, logged, or written down. A test that needs
    another configuration sets or clears the variables itself. HOME/XDG remain test-owned even
    after a keyless test clears the raw credentials; the separate home fixture can still override HOME.
    Discovery directories live outside each test's tmp_path so installer filesystem assertions
    and rollback checks are not affected by this autouse fixture.
    """
    monkeypatch.delenv("SWARM_ALLOW_INSECURE_DEV_KEY", raising=False)
    monkeypatch.delenv("SWARM_ED25519_KEY", raising=False)
    monkeypatch.delenv("SWARM_ED25519_KEY_FILE", raising=False)
    monkeypatch.delenv("SWARM_REQUIRE_KEY", raising=False)
    monkeypatch.setenv("SWARM_SIGNING_KEY", secrets.token_hex(32))
    signing_home = tmp_path_factory.mktemp("signing-home")
    signing_config = tmp_path_factory.mktemp("signing-config")
    monkeypatch.setenv("HOME", str(signing_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(signing_config))


@pytest.fixture(autouse=True)
def _repo_swarm_state_is_untouched(monkeypatch, tmp_path_factory):
    """Keep tests off the checkout's own .swarm run state.

    call_tool appends a local-only audit row whenever a .swarm directory exists (or the tool writes),
    so a test that calls registry tools without SWARM_DIR would pollute <repo>/.swarm/events.jsonl,
    which is the live run log when a swarm runs in this checkout. When the process has no SWARM_DIR,
    point it at a fresh per-test directory (created lazily by the code under test, not here). Tests
    that set SWARM_DIR keep control; tests of the default resolution call monkeypatch.delenv, which
    runs after this fixture and wins.
    """
    if not os.environ.get("SWARM_DIR"):
        monkeypatch.setenv("SWARM_DIR", str(tmp_path_factory.mktemp("swarm") / ".swarm"))


@pytest.fixture(autouse=True)
def _repo_state_mirrors_are_untouched(monkeypatch, tmp_path):
    """Keep the STATE.md mirror and the .claude projection out of the checkout.

    SwarmStateStore and write_claude_state otherwise write <root>/.planning/STATE.md and
    <root>/.claude/swarm-state.json, which are tracked files. The STATE mirror is disabled (empty
    override) and the .claude projection goes to a per-test directory. A test that needs another
    location sets the variables itself.
    """
    monkeypatch.setenv("SWARM_GSD_STATE_FILE", "")
    monkeypatch.setenv("SWARM_CLAUDE_STATE_DIR", str(tmp_path / "claude-state"))


@pytest.fixture()
def swarm_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    # Every swarm* module is imported afresh inside the test. The modules the test files imported at collection time come back
    # afterwards: a later test that patches its own module-level reference (telemetry, substrate_tee, script_base) must patch the
    # object the code under test reaches through sys.modules, and a purge that outlived the test left the two apart.
    original = {name: module for name, module in sys.modules.items() if name.startswith("swarm")}
    for name in original:
        del sys.modules[name]
    yield tmp_path / ".swarm"
    for name in [name for name in sys.modules if name.startswith("swarm")]:
        del sys.modules[name]
    sys.modules.update(original)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return h


@pytest.fixture()
def ws(tmp_path):
    w = tmp_path / "ws"
    w.mkdir()
    return w.resolve()


@pytest.fixture(params=["pyyaml", "stdlib"])
def yaml_mode(request, monkeypatch):
    """Run with PyYAML validating, and on the canonical stdlib-only path (PyYAML import blocked)."""
    if request.param == "stdlib":
        monkeypatch.setitem(sys.modules, "yaml", None)
    elif _yaml is None:
        pytest.skip("PyYAML not installed")
    return request.param


_TREE = ("scripts", "swarm", "prompts", "agents.json", ".claude", ".grok", ".cursor", "omp", "skills", "hooks", "grokbot")


@pytest.fixture()
def tree(tmp_path):
    """A copy of the checkout the installer runs from; build_agents.py resolves ROOT from its own path."""
    t = tmp_path / "checkout"
    ignore = shutil.ignore_patterns("__pycache__", "node_modules")
    for name in _TREE:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, t / name, ignore=ignore, symlinks=True)
        else:
            t.mkdir(exist_ok=True)
            shutil.copy2(src, t / name)
    return t.resolve()


# Inheritable env for run_script children: platform vars the interpreter and
# tools need (PATH, HOME, tmp dirs, SYSTEMROOT-class Windows vars, locale and
# codec vars), plus SWARM_DIR and state-mirror routing and the fixture-managed signing-key vars.
# Every other parent var — SWARM_* session vars (AGENT_SESSION, TASK_ID,
# CORRELATION_ID, ...), SUBSTRATE_* tokens, ANTHROPIC_API_KEY, *_PROXY and any
# other host contamination — is scrubbed. Callers pass what the child needs
# explicitly via `env`, which is applied on top untouched. Host signing keys
# still cannot leak: the autouse ephemeral_signing_key fixture overwrites
# SWARM_SIGNING_KEY, clears private-file references, and isolates HOME/XDG_CONFIG_HOME for every test.
# Fail-closed tests can clear the raw key vars without discovering the real host fallback.
_BASE_ENV_ALLOW = frozenset({
    "PATH", "HOME", "XDG_CONFIG_HOME",
    "TMPDIR", "TEMP", "TMP",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "LANG", "LANGUAGE", "LC_ALL",
    "LC_MESSAGES", "LC_CTYPE", "LC_NUMERIC", "LC_TIME",
    "LC_COLLATE", "LC_MONETARY",
    "PYTHONIOENCODING", "PYTHONUTF8", "TZ",
    "SWARM_DIR", "SWARM_GSD_STATE_FILE", "SWARM_CLAUDE_STATE_DIR",
    "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY",
    "SWARM_ALLOW_INSECURE_DEV_KEY",
    "SWARM_PURE_PYTHON",
})


_ENGINES = ("python", "rust")


def _test_engine() -> str:
    """The scheduler engine the children run on: SWARM_TEST_ENGINE=python (default when unset) or rust.

    Any other value, including an empty one, is a usage error: a typo must not silently run the Python engine and report a
    green Rust leg. pytest_configure below raises it at session start; a direct call raises it too.
    """
    engine = os.environ.get("SWARM_TEST_ENGINE", "python")
    if engine not in _ENGINES:
        raise pytest.UsageError(f"SWARM_TEST_ENGINE must be one of {', '.join(_ENGINES)}; got {engine!r}")
    return engine


def pytest_configure(config):
    _test_engine()


def _scrubbed_base_env() -> dict:
    """The child environment. With SWARM_TEST_ENGINE=rust (children only; this process stays SWARM_PURE_PYTHON=1) it adds
    GSD_ENGINE=rust, SWARM_PURE_PYTHON=0 and, when the parent has it, GSD_ENGINE_BIN; with python or unset it is unchanged."""
    env = {k: v for k, v in os.environ.items() if k in _BASE_ENV_ALLOW or k.startswith("LC_")}
    if _test_engine() == "rust":
        env["GSD_ENGINE"] = "rust"
        env["SWARM_PURE_PYTHON"] = "0"
        if "GSD_ENGINE_BIN" in os.environ:
            env["GSD_ENGINE_BIN"] = os.environ["GSD_ENGINE_BIN"]
    return env


def run_script(name, *args, env=None):
    cmd = [sys.executable, str(ROOT / "scripts" / name), *args]
    child_env = _scrubbed_base_env()
    child_env.update(env or {})
    return subprocess.run(cmd, capture_output=True, text=True, env=child_env, cwd=ROOT)


def stub_claude(tmp_path, result: dict) -> Path:
    """Executable standing in for `claude`: `auth status` → logged in; `-p` → result in a fenced json block."""
    out = json.dumps({"result": "done\n```json\n" + json.dumps(result) + "\n```"})
    p = tmp_path / "claude-stub"
    p.write_text(f"""#!{sys.executable}
import sys
if sys.argv[1:3] == ["auth", "status"]:
    print('{{"loggedIn": true}}')
elif "-p" in sys.argv:
    sys.stdin.read()
    print({out!r})
""")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


_OMP_STUB = r'''#!@PY@
import json, os, re, subprocess, sys, time
prompt = sys.stdin.read()
with open(@CALLS@, "a") as fh:
    fh.write(json.dumps({"argv": sys.argv, "env": dict(os.environ), "cwd": os.getcwd(), "stdin": prompt}) + "\n")
if @LOAD_ERROR_ON@ is not None and @LOAD_ERROR_ON@ in prompt:
    # omp 18.3.1 when the -e package fails to load: this stderr line, then the session runs on without it (rc 0)
    ext = sys.argv[sys.argv.index("-e") + 1]
    print(f"Failed to load extension {ext}/src/index.ts: Failed to load extension: Failed to parse extension source "
          f"for dependency rewriting: {ext}/src/index.ts: Unexpected token (3:0)", file=sys.stderr, flush=True)
if @HANG@:
    # a tool process of the session, then a session that outlives any --task-timeout
    gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(@GRANDCHILD@, "w") as fh:
        fh.write(str(gc.pid))
    print(json.dumps({"type": "session", "version": 3, "id": "stub-session", "cwd": os.getcwd()}), flush=True)
    time.sleep(120)
res = dict(@RESULT@)
m = re.search(r'"task_id": "([^"]+)"', prompt)
if "task_id" not in res and m:
    res["task_id"] = m.group(1)
if @YIELD_ERROR@ is not None and @YIELD_ERROR_ON@ in prompt:
    # the final text still carries a result block, but the yield itself failed
    call = {"type": "toolCall", "id": "toolu_1", "name": "yield", "arguments": {"error": @YIELD_ERROR@}}
    text = "done\n```json\n" + json.dumps(res) + "\n```"
    tres = {"role": "toolResult", "toolCallId": "toolu_1", "toolName": "yield", "isError": False,
            "details": {"status": "aborted", "error": @YIELD_ERROR@}}
else:
    call = {"type": "toolCall", "id": "toolu_1", "name": "yield", "arguments": {"data": res}}
    text = "done"
    tres = {"role": "toolResult", "toolCallId": "toolu_1", "toolName": "yield", "isError": False,
            "details": {"data": res, "status": "success"}}
asst = {"role": "assistant", "content": [{"type": "text", "text": text}, call], "stopReason": "toolUse",
        "usage": {"cost": {"total": 0.01}}}
print("omp: stub banner (not json)")
for ev in ({"type": "session", "version": 3, "id": "stub-session", "cwd": os.getcwd()}, {"type": "agent_start"},
           {"type": "turn_end", "message": asst, "toolResults": [tres]},
           {"type": "agent_end", "messages": [{"role": "user", "content": prompt}, asst, tres],
            "isTerminal": True, "yielded": True}):
    print(json.dumps(ev))
'''


def stub_omp(tmp_path, result: dict, name: str = "omp", *, yield_error: str | None = None, yield_error_on: str = "",
             hang: bool = False, load_error_on: str | None = None) -> Path:
    """Executable `<tmp>/omp-bin/<name>` standing in for `omp -p --mode json`: appends {argv, env, cwd, stdin} to
    omp_calls(stub), then prints a JSONL stream whose terminal agent_end yields `result` (task_id taken from the
    prompt when `result` has none). Its directory holds no claude, so it can be the whole PATH.
    yield_error: sessions whose prompt contains `yield_error_on` put `result` in a fenced json block of the final
    text and yield {error} instead (status aborted). hang: the session starts a `sleep` grandchild (pid in
    omp_grandchild(stub)), prints a session event and sleeps 120 s. load_error_on: sessions whose prompt contains it
    print omp's `Failed to load extension …` stderr line for the -e package, then run normally."""
    d = tmp_path / "omp-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text(_OMP_STUB.replace("@PY@", sys.executable).replace("@CALLS@", repr(str(d / "calls.jsonl")))
                 .replace("@GRANDCHILD@", repr(str(d / "grandchild.pid"))).replace("@HANG@", repr(hang))
                 .replace("@YIELD_ERROR_ON@", repr(yield_error_on)).replace("@YIELD_ERROR@", repr(yield_error))
                 .replace("@LOAD_ERROR_ON@", repr(load_error_on))
                 .replace("@RESULT@", repr(result)))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def omp_grandchild(stub: Path) -> int | None:
    f = stub.parent / "grandchild.pid"
    return int(f.read_text()) if f.exists() else None


def omp_calls(stub: Path) -> list[dict]:
    f = stub.parent / "calls.jsonl"
    return [json.loads(ln) for ln in f.read_text().splitlines()] if f.exists() else []
