"""A git worktree of a repo reads its primary checkout's agent env file, so a session started in any worktree keeps its token."""
import subprocess

import pytest

from swarm import workspace

A05_TOKEN = "wt-a05-own-token-0123456789"


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.delenv("SUBSTRATE_TOKEN", raising=False)
    monkeypatch.delenv("SUBSTRATE_TOKEN_SWARM_A05_BE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    primary = tmp_path / "primary"
    primary.mkdir()
    _git("init", "-q", cwd=primary)
    (primary / "README.md").write_text("x\n")
    _git("add", ".", cwd=primary)
    _git("commit", "-qm", "init", cwd=primary)
    return primary


def test_worktree_reads_primary_checkout_token(repo, tmp_path):
    workspace.write_token(workspace.env_file(repo, "a05-backend"), A05_TOKEN)
    wt = tmp_path / "wt-a05"
    _git("worktree", "add", "-q", str(wt), cwd=repo)
    token, source, problem = workspace.credential("A05", wt, {"XDG_CONFIG_HOME": str(tmp_path / "config")})
    assert token == A05_TOKEN, problem
    assert source == str(workspace.env_file(repo, "a05-backend"))


def test_worktree_own_env_file_wins_over_primary(repo, tmp_path):
    workspace.write_token(workspace.env_file(repo, "a05-backend"), A05_TOKEN)
    wt = tmp_path / "wt-a05"
    _git("worktree", "add", "-q", str(wt), cwd=repo)
    workspace.write_token(workspace.env_file(wt, "a05-backend"), "wt-own-token-0123456789abcdef")
    token = workspace.credential("A05", wt, {"XDG_CONFIG_HOME": str(tmp_path / "config")})[0]
    assert token == "wt-own-token-0123456789abcdef"


def test_worktree_without_primary_token_still_refuses(repo, tmp_path):
    wt = tmp_path / "wt-a05"
    _git("worktree", "add", "-q", str(wt), cwd=repo)
    token, _source, problem = workspace.credential("A05", wt, {"XDG_CONFIG_HOME": str(tmp_path / "config")})
    assert token is None
    assert "does not exist" in problem


def test_primary_checkout_is_none_for_the_primary_and_for_a_plain_dir(repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert workspace.primary_checkout(repo) is None
    assert workspace.primary_checkout(plain) is None
