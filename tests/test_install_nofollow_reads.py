"""Installer reads open with O_NOFOLLOW, and staged installer files get the umask mode of a normal write."""
import ast
import errno
import importlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _function_source(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(text, filename=str(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    raise AssertionError(f"{path.name} defines no {name}()")


@pytest.mark.parametrize(("module", "name"), [
    ("_install_cursor.py", "_open_nofollow_read"),
    ("build_agents.py", "_load_hook_config"),
])
def test_read_helper_opens_with_o_nofollow(module, name):
    src = _function_source(SCRIPTS / module, name)
    assert "os.open(" in src and "O_NOFOLLOW" in src, src


@pytest.mark.parametrize(("module", "name"), [
    ("_install_cursor.py", "_read_stamp"),
    ("_install_grokbot.py", "_read_stamp"),
    ("_install_grokbot.py", "_same_text"),
])
def test_stamp_and_text_reads_use_nofollow_helper(module, name):
    assert "_open_nofollow_read(" in _function_source(SCRIPTS / module, name)


def test_staged_install_file_mode_matches_write_text(tmp_path):
    target = tmp_path / "repo"
    target.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    r = subprocess.run(
        [sys.executable, str(SCRIPTS / "build_agents.py"), "--install-cursor", str(target)],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("x\n", encoding="utf-8")
    want = stat.S_IMODE(baseline.stat().st_mode)
    installed = target / ".cursor" / "rules" / "agent-swarm.mdc"
    assert stat.S_IMODE(installed.stat().st_mode) == want


def _mod(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


ANCESTORS = [".cursor", ".grok"]


def _workspace_with_outside_ancestor(tmp_path: Path, ancestor: str) -> tuple[Path, Path]:
    """A workspace whose ``ancestor`` directory is a symlink to a directory outside it: (workspace, outside)."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / ancestor).symlink_to(outside, target_is_directory=True)
    return ws, outside


@pytest.mark.parametrize("ancestor", ANCESTORS)
def test_read_refuses_symlinked_ancestor(tmp_path, ancestor):
    cursor = _mod("_install_cursor")
    ws, outside = _workspace_with_outside_ancestor(tmp_path, ancestor)
    victim = outside / ".agent-swarm-cursor.json"
    victim.write_text('{"installer": "x"}\n', encoding="utf-8")
    with pytest.raises(cursor.UnsafeDestination, match="symlink"):
        cursor._open_nofollow_read(ws / ancestor / ".agent-swarm-cursor.json", ws)
    assert victim.read_text(encoding="utf-8") == '{"installer": "x"}\n'


def test_grokbot_stamp_read_refuses_symlinked_ancestor(tmp_path):
    cursor = _mod("_install_cursor")
    grokbot = _mod("_install_grokbot")
    ws, outside = _workspace_with_outside_ancestor(tmp_path, "grokbot")
    (outside / ".agent-swarm-grokbot.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(cursor.UnsafeDestination, match="symlink"):
        grokbot._read_stamp(ws / "grokbot" / ".agent-swarm-grokbot.json", ws)


@pytest.mark.parametrize("ancestor", ANCESTORS)
def test_stage_text_refuses_symlinked_ancestor(tmp_path, ancestor):
    cursor = _mod("_install_cursor")
    ws, outside = _workspace_with_outside_ancestor(tmp_path, ancestor)
    before = _tree(outside)
    with pytest.raises(cursor.UnsafeDestination, match="symlink"):
        cursor._stage_text(ws / ancestor / "rules" / "agent-swarm.mdc", "x\n", ws)
    assert _tree(outside) == before


@pytest.mark.parametrize("ancestor", ANCESTORS)
def test_stage_bytes_refuses_symlinked_ancestor(tmp_path, ancestor):
    cursor = _mod("_install_cursor")
    ws, outside = _workspace_with_outside_ancestor(tmp_path, ancestor)
    before = _tree(outside)
    with pytest.raises(cursor.UnsafeDestination, match="symlink"):
        cursor._stage_bytes(ws / ancestor / "agents", ".x.", b"x", ws)
    assert _tree(outside) == before


def test_cursor_apply_refuses_symlinked_ancestor_and_writes_nothing_outside(tmp_path):
    cursor = _mod("_install_cursor")
    ws, outside = _workspace_with_outside_ancestor(tmp_path, ".cursor")
    before = _tree(outside)
    with pytest.raises(cursor.UnsafeDestination, match="symlink"):
        cursor._apply(ws, {".cursor/rules/agent-swarm.mdc": "x\n"}, [])
    assert _tree(outside) == before


def test_grokbot_apply_refuses_symlinked_ancestor_and_writes_nothing_outside(tmp_path):
    cursor = _mod("_install_cursor")
    grokbot = _mod("_install_grokbot")
    ws, outside = _workspace_with_outside_ancestor(tmp_path, "grokbot")
    before = _tree(outside)
    with pytest.raises(cursor.UnsafeDestination, match="symlink"):
        grokbot._apply(ws, {"grokbot/swarm/seat-map.json": "{}\n"}, [])
    assert _tree(outside) == before


@pytest.mark.parametrize("with_writer", [False, True], ids=["no-writer", "writer"])
def test_read_refuses_fifo(tmp_path, with_writer):
    if not hasattr(os, "mkfifo"):
        pytest.skip("os.mkfifo is unavailable on this platform")
    cursor = _mod("_install_cursor")
    ws = tmp_path / "workspace"
    (ws / ".cursor").mkdir(parents=True)
    fifo = ws / ".cursor" / ".agent-swarm-cursor.json"
    os.mkfifo(fifo)
    held: list[int] = []
    try:
        if with_writer:
            # The test's reader keeps the FIFO open, so a non-blocking writer can open it; the installer's read then meets EAGAIN.
            held.append(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
            held.append(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        with pytest.raises(cursor.UnsafeDestination, match="not a regular file"):
            cursor._open_nofollow_read(fifo, ws)
        with pytest.raises(cursor.UnsafeDestination, match="not a regular file"):
            cursor._read_stamp(fifo, ws)
    finally:
        for fd in held:
            os.close(fd)


def test_read_turns_blocking_read_into_refusal(tmp_path, monkeypatch):
    cursor = _mod("_install_cursor")
    ws = tmp_path / "workspace"
    (ws / ".cursor").mkdir(parents=True)
    stamp = ws / ".cursor" / ".agent-swarm-cursor.json"
    stamp.write_text("{}\n", encoding="utf-8")

    def blocked(fd, n):
        raise BlockingIOError(errno.EAGAIN, "write could not complete without blocking")

    monkeypatch.setattr(cursor.os, "read", blocked)
    with pytest.raises(cursor.UnsafeDestination, match="cannot be read"):
        cursor._open_nofollow_read(stamp, ws)
