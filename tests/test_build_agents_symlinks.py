"""build_agents.py refuses to generate or prune through a symlinked export directory."""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _run(tree):
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    return subprocess.run(
        [sys.executable, str(tree / "scripts" / "build_agents.py")],
        cwd=tree, env=env, capture_output=True, text=True,
    )


def _names(path):
    return sorted(p.name for p in path.iterdir())


@pytest.mark.parametrize("link_rel", [".cursor/agents", ".cursor"], ids=["agents", "parent"])
def test_cursor_export_symlink_refused(tree, tmp_path, link_rel):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "custom.md").write_text("keep\n", encoding="utf-8")
    before = _names(victim)
    link = tree / link_rel
    if link.is_dir() and not link.is_symlink():
        shutil.rmtree(link)
    else:
        link.unlink(missing_ok=True)
    link.symlink_to(victim, target_is_directory=True)
    r = _run(tree)
    assert r.returncode == 1, r.stdout + r.stderr
    err_lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(err_lines) == 1, r.stderr
    assert "symlink" in err_lines[0]
    assert "Traceback" not in r.stderr and "Traceback" not in r.stdout
    assert (victim / "custom.md").read_text(encoding="utf-8") == "keep\n"
    assert _names(victim) == before


_INSTALL_WORKSPACE = (
    "import sys; from pathlib import Path; sys.path.insert(0, 'scripts'); "
    "import build_agents; build_agents.install_workspace(Path(sys.argv[1]))"
)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_install_workspace_refuses_symlinked_settings(tree, tmp_path, kind):
    victim = tmp_path / "victim.json"
    victim.write_text('{"keep": true}\n', encoding="utf-8")
    ws = tmp_path / "workspace"
    (ws / ".claude").mkdir(parents=True)
    settings = ws / ".claude" / "settings.json"
    if kind == "symlink":
        settings.symlink_to(victim)
    else:
        os.link(victim, settings)
    before = victim.read_bytes()
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    r = subprocess.run(
        [sys.executable, "-c", _INSTALL_WORKSPACE, str(ws)],
        cwd=tree, env=env, capture_output=True, text=True,
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert re.search("symlink|hardlink", r.stderr), r.stderr
    assert victim.read_bytes() == before


def test_manifest_read_is_utf8_under_c_locale(tree):
    manifest = tree / "agents.json"
    text = manifest.read_text(encoding="utf-8")
    old = '"name": "Backend Engineer",'
    assert text.count(old) == 1
    manifest.write_text(text.replace(old, '"name": "Backend Engineer \u00e9",'), encoding="utf-8")
    drop = ("SWARM_AGENTS_FILE", "PYTHONUTF8", "PYTHONCOERCECLOCALE", "LC_ALL", "LANG", "LC_CTYPE")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update(PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C")
    r = subprocess.run(
        [sys.executable, "scripts/build_agents.py", "--check"],
        cwd=tree, env=env, capture_output=True,
    )
    assert b"UnicodeDecodeError" not in r.stderr, r.stderr.decode("utf-8", "replace")


def _build_agents():
    spec = importlib.util.spec_from_file_location("build_agents_nofollow", ROOT / "scripts" / "build_agents.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def _workspace_with_outside_ancestor(tmp_path: Path, ancestor: str) -> tuple[Path, Path]:
    """A workspace whose ``ancestor`` directory is a symlink to a directory outside it: (workspace, outside)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / ancestor).symlink_to(outside, target_is_directory=True)
    return ws, outside


@pytest.mark.parametrize(("ancestor", "rel"), [
    (".grok", "agents/agent-swarm.md"),
    (".cursor", "rules/agent-swarm.mdc"),
    (".claude", "settings.json"),
], ids=["grok", "cursor", "claude"])
def test_write_text_nofollow_refuses_symlinked_ancestor(tmp_path, ancestor, rel):
    mod = _build_agents()
    ws, outside = _workspace_with_outside_ancestor(tmp_path, ancestor)
    before = _tree(outside)
    with pytest.raises(mod._install_omp.UnsafeDestination, match="symlink"):
        mod._write_text_nofollow(ws / ancestor / rel, "x\n", ws)
    assert _tree(outside) == before


@pytest.mark.parametrize("ancestor", [".grok", ".cursor", ".claude"])
def test_target_refusal_reports_symlinked_ancestor(tmp_path, ancestor):
    mod = _build_agents()
    ws, _outside = _workspace_with_outside_ancestor(tmp_path, ancestor)
    reason = mod._target_refusal(ws / ancestor / "agents" / "x.md", ws)
    assert reason is not None and "symlink" in reason


def test_target_refusal_allows_a_missing_chain(tmp_path):
    mod = _build_agents()
    ws = tmp_path / "workspace"
    ws.mkdir()
    assert mod._target_refusal(ws / ".grok" / "agents" / "x.md", ws) is None


def test_install_workspace_refuses_symlinked_grok_ancestor(tree, tmp_path):
    ws, outside = _workspace_with_outside_ancestor(tmp_path, ".grok")
    before = _tree(outside)
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    r = subprocess.run(
        [sys.executable, "-c", _INSTALL_WORKSPACE, str(ws)],
        cwd=tree, env=env, capture_output=True, text=True,
    )
    assert r.returncode != 0, r.stdout + r.stderr
    assert "symlink" in r.stderr, r.stderr
    assert _tree(outside) == before


def test_load_hook_config_refuses_fifo(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable on this platform")
    mod = _build_agents()
    fifo = tmp_path / "settings.json"
    os.mkfifo(fifo)
    with pytest.raises(mod.HookConfigError, match="not a regular file"):
        mod._load_hook_config(fifo)
