"""CORE-04: one absolute Swarm state dir, resolved lazily (D-09..D-11)."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import ROOT

ONE_TASK = {"tasks": [{"id": "one", "capability": "code.backend", "agent": "A05", "title": "one", "depends_on": [], "gates": []}]}


def _env_without_swarm_dir():
    return {k: v for k, v in os.environ.items() if k != "SWARM_DIR"}


def _run(script, *args, cwd, env):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *args],
                          capture_output=True, text=True, cwd=cwd, env=env)


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def test_swarm_dir_same_from_any_cwd(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = _env_without_swarm_dir()
    brief = tmp_path / "brief.md"
    brief.write_text("build it")
    a = _run("orch_plan.py", "--root", str(repo), "--brief", str(brief), "--prefix", "A", "--json", cwd=repo, env=env)
    b = _run("orch_plan.py", "--root", str(repo), "--brief", str(brief), "--prefix", "B", "--json", cwd=elsewhere, env=env)
    assert a.returncode == 0, a.stdout + a.stderr
    assert b.returncode == 0, b.stdout + b.stderr
    sdir = (repo / ".swarm").resolve()
    assert json.loads(a.stdout)["plan_file"].startswith(str(sdir))
    assert json.loads(b.stdout)["plan_file"].startswith(str(sdir))
    assert (sdir / "tasks.db").exists() and not (elsewhere / ".swarm").exists()
    st = _run("orch_status.py", "--root", str(repo), "--history", "A-be", "--json", cwd=elsewhere, env=env)
    assert st.returncode == 0 and json.loads(st.stdout)["history"], st.stdout + st.stderr
    st = _run("orch_status.py", "--root", str(repo), "--history", "B-be", "--json", cwd=repo, env=env)
    assert st.returncode == 0 and json.loads(st.stdout)["history"], st.stdout + st.stderr
    assert (sdir / ".gitignore").read_text() == "*"
    assert not (repo / ".gitignore").exists()


def test_orch_status_db_same_from_any_cwd(tmp_path):
    """D-04: orch_status --json names the absolute tasks.db, identical from any cwd (empty and list output)."""
    repo = _git_repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    env = _env_without_swarm_dir()
    db = str((repo / ".swarm" / "tasks.db").resolve())

    def status_from_both_cwds():
        outs = []
        for cwd in (repo, elsewhere):
            r = _run("orch_status.py", "--root", str(repo), "--json", cwd=cwd, env=env)
            assert r.returncode == 0, r.stdout + r.stderr
            outs.append(json.loads(r.stdout))
        return outs

    empty = status_from_both_cwds()
    assert all(o["tasks"] == [] for o in empty)
    p = _run("orch_plan.py", "--root", str(repo), "--brief-text", "fix it", "--pattern", "hotfix",
             "--prefix", "D", "--json", cwd=elsewhere, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    listed = status_from_both_cwds()
    assert all(o["tasks"] for o in listed)
    for o in empty + listed:
        assert o["db"] == db and o["swarm_dir"] == str(Path(db).parent), o
    assert not (elsewhere / ".swarm").exists()


def test_env_relative_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SWARM_DIR", "rel/.swarm")
    from swarm.paths import swarm_dir
    d = swarm_dir()
    assert d.is_absolute() and d == (tmp_path / "rel" / ".swarm").resolve()


def test_no_import_time_resolution(tmp_path, monkeypatch):
    import swarm.runlog as runlog
    assert not hasattr(runlog, "SWARM_DIR") and not hasattr(runlog, "LOG_FILE")
    target = tmp_path / "late"
    monkeypatch.setenv("SWARM_DIR", str(target))
    runlog.emit("x.test", {}, source="t")
    assert (target / "events.jsonl").exists()


def test_no_git_fallback(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    monkeypatch.delenv("SWARM_DIR", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    from swarm.paths import swarm_dir
    assert swarm_dir() == plain.resolve() / ".swarm"


def test_gitignore_not_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    (tmp_path / ".swarm").mkdir()
    (tmp_path / ".swarm" / ".gitignore").write_text("custom")
    from swarm.paths import swarm_dir
    first, second = swarm_dir(create=True), swarm_dir(create=True)
    assert first == second
    assert (tmp_path / ".swarm" / ".gitignore").read_text() == "custom"


def test_create_adds_gitignore_to_an_existing_dir_without_one(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    (tmp_path / ".swarm").mkdir()
    from swarm.paths import swarm_dir
    assert swarm_dir(create=True) == (tmp_path / ".swarm").resolve()
    assert (tmp_path / ".swarm" / ".gitignore").read_text() == "*"


def test_create_leaves_a_dangling_gitignore_link_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    (tmp_path / ".swarm").mkdir()
    link = tmp_path / ".swarm" / ".gitignore"
    link.symlink_to(tmp_path / "nowhere")
    from swarm.paths import swarm_dir
    swarm_dir(create=True)
    assert link.is_symlink() and not (tmp_path / "nowhere").exists()


def test_create_still_fails_when_the_state_path_is_a_file(tmp_path, monkeypatch):
    blocker = tmp_path / ".swarm"
    blocker.write_text("not a directory")
    monkeypatch.setenv("SWARM_DIR", str(blocker))
    from swarm.paths import swarm_dir
    with pytest.raises(FileExistsError):
        swarm_dir(create=True)


def test_swarm_run_children_get_absolute_dir(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(ONE_TASK))
    env = {**_env_without_swarm_dir(), "SWARM_DIR": "rel/.swarm"}
    p = _run("orch_plan.py", "--plan", str(plan), "--json", cwd=work, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    r = _run("swarm_run.py", "--dry-run", "--repo", str(tmp_path), "--json", cwd=work, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    sdir = (work / "rel" / ".swarm").resolve()
    assert (sdir / "events.jsonl").exists() and (sdir / "results").is_dir()
    logs = [q for q in tmp_path.rglob("events.jsonl")]
    assert logs == [sdir / "events.jsonl"]
    assert json.loads(r.stdout)["complete"] is True
