"""INST-03 on the runner's side (ADR 0001 S4 criteria 4-5): each agent session starts with its own SUBSTRATE_TOKEN and no
other, and supported runtimes reach the projected `substrate` MCP entry without bypassing approval."""
import importlib.util
import json
import os
import shlex
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import ROOT, omp_calls, run_script, stub_omp
from swarm import workspace

A05_TOKEN = "p14-a05-own-token-0123456789"
RUNNER_TOKENS = {"SUBSTRATE_TOKEN": "p14-runner-a01-token-0123456789",
                 "SUBSTRATE_TOKEN_SWARM_A05_BE": "p14-runner-copy-of-a05-0123456789",
                 "SUBSTRATE_TOKEN_SWARM_A09_REV": "p14-runner-a09-token-0123456789",
                 "SUBSTRATE_OPERATOR_TOKENS": "p14-operator-token-0123456789"}
ONE_TASK = {"tasks": [{"id": "one", "capability": "code.backend", "agent": "A05", "title": "one", "depends_on": [], "gates": []}]}
RESULT = {"task_id": "T-one", "state": "IN_REVIEW", "outputs": [{"kind": "code.backend", "uri": "file://x", "version": "1",
                                                                 "digest": ""}], "metrics": {}, "summary_md": "ok"}
A05 = {"slug": "a05-backend", "id": "A05"}


@pytest.fixture()
def sr(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    spec = importlib.util.spec_from_file_location("swarm_run_tokens_under_test", ROOT / "scripts" / "swarm_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def ws(tmp_path, monkeypatch):
    """A workspace whose A05 env file holds A05's token, run by a runner that holds several other tokens."""
    monkeypatch.delenv("SUBSTRATE_URL", raising=False)  # off: these tests are about the session, not the lease
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for k, v in RUNNER_TOKENS.items():
        monkeypatch.setenv(k, v)
    w = tmp_path / "work"
    w.mkdir()
    workspace.write_token(workspace.env_file(w, "a05-backend"), A05_TOKEN)
    return w


def _wire(ws):
    """What the install leaves in the workspace for the runtimes to read."""
    (ws / ".mcp.json").write_text(json.dumps({"mcpServers": {"substrate": workspace.SPEC["json"]}}))
    (ws / ".grok").mkdir()
    (ws / ".grok" / "config.toml").write_text(workspace.SPEC["toml"]["block"])


def _args(**kw):
    return SimpleNamespace(**{"claude_bin": "claude", "grok_bin": "grok", "omp_bin": "omp", "model": "",
                              "permission_mode": "acceptEdits", "max_turns": 5, "task_timeout": 90,
                              "allowed_tools": "Read,Grep", **kw})


def _tokens(env):
    return {k: v for k, v in env.items() if workspace.carries_token(k)}


@pytest.mark.parametrize("runtime", ["claude", "omp"])
def test_a_session_holds_its_own_token_and_no_other(sr, ws, tmp_path, runtime):
    _, env, _ = sr.headless_command(runtime, A05, ws, tmp_path / ".swarm", _args())
    assert _tokens(env) == {"SUBSTRATE_TOKEN": A05_TOKEN}


def test_without_an_env_file_or_its_variable_a_session_gets_no_token_at_all(sr, ws, tmp_path):
    # A10 has neither: not the runner's SUBSTRATE_TOKEN, since a borrowed token would let the agent act, and be
    # recorded, as another surface
    _, env, _ = sr.headless_command("claude", {"slug": "a10-security", "id": "A10"}, ws, tmp_path / ".swarm", _args())
    assert _tokens(env) == {}


def test_without_an_env_file_a_session_gets_its_own_server_side_variable(sr, ws, tmp_path):
    # A09 has no env file but the runner holds SUBSTRATE_TOKEN_SWARM_A09_REV: the same secret, so the session gets it
    # as SUBSTRATE_TOKEN, the lookup the lease and the tee use too
    _, env, _ = sr.headless_command("claude", {"slug": "a09-reviewer", "id": "A09"}, ws, tmp_path / ".swarm", _args())
    assert _tokens(env) == {"SUBSTRATE_TOKEN": RUNNER_TOKENS["SUBSTRATE_TOKEN_SWARM_A09_REV"]}


def test_claude_gets_the_workspace_mcp_config_and_the_substrate_tools(sr, ws, tmp_path):
    _wire(ws)
    argv, _, cwd = sr.headless_command("claude", A05, ws, tmp_path / ".swarm", _args())
    assert cwd == ROOT  # where .claude/agents resolves, and where a workspace .mcp.json is never read
    assert argv[argv.index("--mcp-config") + 1] == str(ws / ".mcp.json") and "--strict-mcp-config" in argv
    allowed = argv[argv.index("--allowedTools") + 1: argv.index("--add-dir", argv.index("--allowedTools"))]
    assert allowed == ["Read", "Grep", "mcp__substrate"]


def test_an_unwired_workspace_gets_no_mcp_flags(sr, ws, tmp_path):
    argv, _, _ = sr.headless_command("claude", A05, ws, tmp_path / ".swarm", _args())
    assert "--mcp-config" not in argv and "mcp__substrate" not in argv


@pytest.mark.parametrize("wired", [False, True])
def test_grok_assignment_file_is_removed_when_the_spawn_fails(sr, ws, tmp_path, monkeypatch, wired):
    if wired:
        _wire(ws)
    spawned = {}

    def refuse_spawn(argv, *args, **kwargs):
        spawned["argv"] = list(argv)
        raise OSError("grok not installed")

    monkeypatch.setattr(sr.subprocess, "Popen", refuse_spawn)
    with pytest.raises(sr.SwarmError) as e:
        sr.run_agent_headless(A05, "test-owned assignment", ws, _args(runtime="grok"))
    assert e.value.code.value == "E-DEP"
    prompt_file = Path(spawned["argv"][spawned["argv"].index("--prompt-file") + 1])
    assert prompt_file.parent.name == "prompts" and not prompt_file.exists()


@pytest.mark.parametrize("config", [
    'model = "grok-4"\n',
    '[mcp_servers.other]\nurl = "https://example.invalid/mcp"\n',
    '[mcp_servers.substrate]\nurl = "https://example.invalid/mcp"\n',
    workspace.SPEC["toml"]["block"].replace("${SUBSTRATE_TOKEN}", "operator-token"),
    workspace.SPEC["toml"]["block"].replace("${SUBSTRATE_URL}", "https://example.invalid"),
    '[mcp_servers.substrate\n',
    'mcp_servers = "not a table"\n',
])
def test_grok_projection_rejects_unrelated_malformed_or_nonprojected_configs(ws, config):
    path = workspace.mcp_config(ws, "grok")
    path.parent.mkdir()
    path.write_text(config)
    assert not workspace.projected_mcp(ws, "grok")


def test_grok_projection_accepts_exact_entry_among_other_settings(ws):
    path = workspace.mcp_config(ws, "grok")
    path.parent.mkdir()
    path.write_text('model = "grok-4"\n' + workspace.SPEC["toml"]["block"] +
                    '\n[mcp_servers.other]\nurl = "https://example.invalid/mcp"\n')
    assert workspace.projected_mcp(ws, "grok")


def test_projected_config_through_a_symlink_is_refused(ws, tmp_path):
    other = tmp_path / "outside-config.toml"
    other.write_text(workspace.SPEC["toml"]["block"])
    path = workspace.mcp_config(ws, "grok")
    path.parent.mkdir()
    path.symlink_to(other)
    assert not workspace.projected_mcp(ws, "grok")


def test_omp_runs_where_the_entry_is_and_its_tools_list_names_only_builtins(sr, ws, tmp_path):
    # omp reads <cwd>/.mcp.json; MCP tools are not filtered by --tools (they mount as xd:// devices), so nothing in
    # the argv may restrict them either
    _wire(ws)
    argv, _, cwd = sr.headless_command("omp", A05, ws, tmp_path / ".swarm", _args())
    assert argv[argv.index("--cwd") + 1] == str(ws) and cwd == ws
    assert "--no-tools" not in argv and all(not t.startswith("mcp") for t in argv[argv.index("--tools") + 1].split(","))


@pytest.mark.parametrize("entry", ["projected", "absent", "operator-token", "invalid-json"])
def test_omp_guard_scope_requires_the_projected_own_token_entry(sr, ws, tmp_path, monkeypatch, entry):
    monkeypatch.setenv("SWARM_SUBSTRATE_AGENT", "a09-reviewer")
    if entry != "absent":
        _wire(ws)
    if entry == "operator-token":
        config = json.loads((ws / ".mcp.json").read_text())
        config["mcpServers"]["substrate"]["headers"]["Authorization"] = "Bearer operator-token"
        (ws / ".mcp.json").write_text(json.dumps(config))
    elif entry == "invalid-json":
        (ws / ".mcp.json").write_text("{")
    _, env, _ = sr.headless_command("omp", A05, ws, tmp_path / ".swarm", _args())
    assert env.get("SWARM_SUBSTRATE_AGENT") == ("a05-backend" if entry == "projected" else None)
    # Even a projected entry never unlocks a session lacking that agent's own credential.
    _, env, _ = sr.headless_command("omp", {"slug": "a10-security", "id": "A10"}, ws, tmp_path / ".swarm", _args())
    assert "SWARM_SUBSTRATE_AGENT" not in env


def _planned(tmp_path, env):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(ONE_TASK))
    r = run_script("orch_plan.py", "--plan", str(plan), "--prefix", "T", "--repo", str(tmp_path / "work"), "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr


def _runner_env(tmp_path):
    base = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_", "SUBSTRATE_")) and k != "ANTHROPIC_API_KEY"}
    return {**base, **RUNNER_TOKENS, "SWARM_DIR": str(tmp_path / ".swarm"), "XDG_CONFIG_HOME": str(tmp_path / "config")}


def test_the_live_child_gets_only_its_own_token(ws, tmp_path):
    env = _runner_env(tmp_path)
    _planned(tmp_path, env)
    stub = stub_omp(tmp_path, RESULT)
    r = run_script("swarm_run.py", "--runtime", "omp", "--omp-bin", str(stub), "--once", "--repo", str(ws), "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    (call,) = omp_calls(stub)
    assert _tokens(call["env"]) == {"SUBSTRATE_TOKEN": A05_TOKEN}


def test_dry_run_names_what_is_stripped_and_where_the_token_comes_from_never_its_value(ws, tmp_path):
    env = _runner_env(tmp_path)
    _planned(tmp_path, env)
    r = run_script("swarm_run.py", "--runtime", "claude", "--dry-run", "--once", "--repo", str(ws), "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    line = next(ln for ln in r.stderr.splitlines() if ln.startswith("dry-run T-one [A05]: "))
    tokens = shlex.split(line.removeprefix("dry-run T-one [A05]: "))
    unset = {tokens[i + 1] for i, t in enumerate(tokens) if t == "-u"}
    # The runner's foreign tokens are stripped; the child's own SUBSTRATE_TOKEN is kept in replay
    expected_stripped = {k for k in RUNNER_TOKENS if k != "SUBSTRATE_TOKEN"}
    assert expected_stripped <= unset
    assert "SUBSTRATE_TOKEN" not in unset
    assert f"dry-run T-one [A05] SUBSTRATE_TOKEN: from {workspace.env_file(ws, 'a05-backend')}" in r.stderr.splitlines()
    assert not any(v in r.stdout + r.stderr for v in (A05_TOKEN, *RUNNER_TOKENS.values()))


def test_valid_0600_token_in_nonprivate_directory_refuses_without_environment_fallback(sr, ws, tmp_path):
    path = workspace.env_file(ws, A05["slug"])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.parent.chmod(0o777)
    with pytest.raises(workspace.EnvFileProblem, match="private directory"):
        workspace.read_token(path)
    token, _, problem = workspace.credential("A05", ws)
    assert token is None and "0700" in problem
    _, env, _ = sr.headless_command("omp", A05, ws, tmp_path / ".swarm", _args())
    assert _tokens(env) == {}
    assert "SWARM_SUBSTRATE_AGENT" not in env
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o777


def test_token_staging_is_private_before_any_byte_and_replaces_atomically(tmp_path, monkeypatch):
    path = tmp_path / "private" / "a05-backend.env"
    path.parent.mkdir(mode=0o700)
    path.write_text("SUBSTRATE_TOKEN=old-token\n")
    path.chmod(0o600)
    fdopen = os.fdopen
    observed_modes = []

    def inspect_before_write(fd, mode, *args, **kwargs):
        if mode == "wb":
            observed_modes.append(stat.S_IMODE(os.fstat(fd).st_mode))
            assert path.read_text() == "SUBSTRATE_TOKEN=old-token\n"
        return fdopen(fd, mode, *args, **kwargs)

    monkeypatch.setattr(workspace.os, "fdopen", inspect_before_write)
    workspace.write_token(path, A05_TOKEN)
    assert observed_modes and set(observed_modes) == {0o600}
    assert path.read_text() == f"SUBSTRATE_TOKEN={A05_TOKEN}\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("config", [
    "{}",
    '{"mcpServers": {"other": {"type": "http", "url": "https://example.invalid/mcp"}}}',
    '{"mcpServers": {"substrate": {"type": "http", "url": "https://example.invalid/mcp"}}}',
    json.dumps({"mcpServers": {workspace.SERVER: workspace.SPEC["json"]}}).replace("${SUBSTRATE_TOKEN}", "operator-token"),
    '{"mcpServers": "not an object"}',
    "{",
    "[]",
])
def test_claude_does_not_unlock_substrate_flags_for_unrelated_or_invalid_configs(sr, ws, tmp_path, config):
    workspace.mcp_config(ws, "claude").write_text(config)
    argv, _, _ = sr.headless_command("claude", A05, ws, tmp_path / ".swarm", _args())
    assert "--mcp-config" not in argv
    assert "--strict-mcp-config" not in argv
    assert workspace.CLAUDE_TOOLS not in argv


@pytest.mark.parametrize("runtime", ["claude", "omp"])
def test_substrate_disabled_turns_off_child_mcp_and_strips_token(sr, ws, tmp_path, monkeypatch, runtime):
    _wire(ws)
    monkeypatch.setenv("SUBSTRATE_DISABLED", "1")
    argv, env, _ = sr.headless_command(runtime, A05, ws, tmp_path / ".swarm", _args())
    assert _tokens(env) == {}
    if runtime == "claude":
        assert "--mcp-config" not in argv
        assert workspace.CLAUDE_TOOLS not in argv
    elif runtime == "omp":
        assert "SWARM_SUBSTRATE_AGENT" not in env


def test_credential_in_child_session_resolves_own_session_token():
    session_env = {"SWARM_AGENT_SESSION": "1", "SWARM_AGENT": "a05-backend", "SUBSTRATE_TOKEN": "child-tok"}
    tok, src, prob = workspace.credential("A05", None, session_env)
    assert tok == "child-tok" and src == "$SUBSTRATE_TOKEN" and not prob

    # Another agent identity cannot claim this session's token
    tok, src, prob = workspace.credential("A09", None, session_env)
    assert tok is None and not src and "unset" in prob

    # Outside an agent session (e.g. runner), SUBSTRATE_TOKEN is never used as fallback
    tok, src, prob = workspace.credential("A05", None, {"SUBSTRATE_TOKEN": "child-tok"})
    assert tok is None and not src and "unset" in prob


def test_dry_run_invocation_wired_workspace(ws, tmp_path, capsys, monkeypatch):
    _wire(ws)
    secret = "secret-super-tok-12345"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    ef = workspace.env_file(ws, "a05-backend", {"HOME": str(home)})
    ef.parent.mkdir(parents=True, exist_ok=True)
    ef.write_text(f"SUBSTRATE_TOKEN={secret}\n")

    task = {"task_id": "T-two", "attempt": 1}
    agent = {"slug": "a10-security", "id": "A10"}
    sdir = tmp_path / ".swarm"
    (sdir / "assignments").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SUBSTRATE_TOKEN", "runner-tok")
    monkeypatch.setenv("SUBSTRATE_TOKEN_SWARM_A05_BE", "inherited-secret")

    spec = importlib.util.spec_from_file_location("sr_mod", ROOT / "scripts" / "swarm_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # 1. Runner export of SUBSTRATE_TOKEN is unset with -u when agent has no credential
    #    Inherited sibling token SUBSTRATE_TOKEN_SWARM_A05_BE IS unset with -u
    #    A10 has no token -> SWARM_SUBSTRATE_AGENT not in deltas
    capsys.readouterr()
    mod.dry_run_invocation(task, agent, ws, sdir, _args(runtime="omp"))
    err = capsys.readouterr().err
    tokens_a10 = shlex.split(err.splitlines()[0].split(": ", 1)[1])
    unset_a10 = {tokens_a10[i + 1] for i, t in enumerate(tokens_a10) if t == "-u"}
    assert "SUBSTRATE_TOKEN" in unset_a10
    assert "SUBSTRATE_TOKEN_SWARM_A05_BE" in unset_a10
    assert "SWARM_SUBSTRATE_AGENT" not in err

    # 2. A05 gets token from env file in wired workspace -> SWARM_SUBSTRATE_AGENT in deltas for omp
    #    Secret token value is NEVER printed in replay command or stderr
    mod.dry_run_invocation({"task_id": "T-one", "attempt": 1}, A05, ws, sdir, _args(runtime="omp"))
    err = capsys.readouterr().err
    tokens_a05 = shlex.split(err.splitlines()[0].split(": ", 1)[1])
    unset_a05 = {tokens_a05[i + 1] for i, t in enumerate(tokens_a05) if t == "-u"}
    assert "SUBSTRATE_TOKEN" not in unset_a05
    assert "SUBSTRATE_TOKEN_SWARM_A05_BE" in unset_a05
    assert "SWARM_SUBSTRATE_AGENT=a05-backend" in err
    assert secret not in err
    assert "inherited-secret" not in err
    assert "from " in err

    # 3. Agent with token in os.environ (SUBSTRATE_TOKEN_<SURFACE>) -> SUBSTRATE_TOKEN not unset, SWARM_SUBSTRATE_AGENT set
    monkeypatch.setenv("SUBSTRATE_TOKEN_SWARM_A10_SEC", "a10-secret-tok")
    capsys.readouterr()
    mod.dry_run_invocation(task, agent, ws, sdir, _args(runtime="omp"))
    err = capsys.readouterr().err
    tokens_a10_env = shlex.split(err.splitlines()[0].split(": ", 1)[1])
    unset_a10_env = {tokens_a10_env[i + 1] for i, t in enumerate(tokens_a10_env) if t == "-u"}
    assert "SUBSTRATE_TOKEN" not in unset_a10_env
    assert "SUBSTRATE_TOKEN_SWARM_A10_SEC" in unset_a10_env
    assert "SWARM_SUBSTRATE_AGENT=a10-security" in err
    assert "a10-secret-tok" not in err
    assert "from $SUBSTRATE_TOKEN_SWARM_A10_SEC" in err


