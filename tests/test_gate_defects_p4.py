"""P4-01/P4-02/P4-04/P4-05: audit errors, secret evidence, and release no-task regressions.

An audit tool that exits non-zero without a countable result fails the run (P4-01 npm, P4-02 govulncheck). Secret values
never reach evidence (P4-04). A release run that evaluated no task signs no pass (P4-05).

Diff parsing and quoted names (P4-15, P4-16), the malformed findings file (P4-17, review half), the release freeze record (P4-21)
and the shared gate helper call (P4-27) are pinned at the end of this file.
"""
import importlib.util
import json
import runpy
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import run_script
from swarm import engine_select
from swarm.errors import ErrorCode, SwarmError

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_p4_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_tool(bin_dir, name, body):
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_npm_audit_error_json_with_exit_1_fails_closed(tmp_path, monkeypatch):
    sec = _load("sec_gate")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package-lock.json").write_text("{}")
    _fake_tool(tmp_path / "bin", "npm", "echo '{\"error\":{\"code\":\"ENOAUDIT\"}}'\nexit 1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    findings, runs = sec.dep_audit(root, 30)

    assert runs["npm-audit"] == "fail"
    assert [f["severity"] for f in findings] == ["major"]


def test_npm_audit_clean_exit_0_still_passes(tmp_path, monkeypatch):
    sec = _load("sec_gate")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package-lock.json").write_text("{}")
    _fake_tool(tmp_path / "bin", "npm",
               "echo '{\"metadata\":{\"vulnerabilities\":{\"info\":0,\"low\":0,\"moderate\":0,\"high\":0,\"critical\":0}}}'\nexit 0")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    findings, runs = sec.dep_audit(root, 30)

    assert runs["npm-audit"] == "pass"
    assert findings == []


def test_govulncheck_load_failure_with_no_finding_lines_fails_closed(tmp_path, monkeypatch):
    sec = _load("sec_gate")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "go.mod").write_text("module x\n")
    _fake_tool(tmp_path / "bin", "govulncheck", "echo 'go: cannot load module' >&2\nexit 1")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    _, runs = sec.dep_audit(root, 30)

    assert runs["govulncheck"] == "fail"


def test_govulncheck_clean_exit_0_still_passes(tmp_path, monkeypatch):
    sec = _load("sec_gate")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "go.mod").write_text("module x\n")
    _fake_tool(tmp_path / "bin", "govulncheck", "echo '{\"config\": {}}'\nexit 0")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))

    findings, runs = sec.dep_audit(root, 30)

    assert runs["govulncheck"] == "pass"
    assert findings == []


def test_secret_evidence_carries_no_value_prefix(tmp_path):
    sec = _load("sec_gate")
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    value = "Abcd0123456789efghIJKL"
    (root / "app" / "settings.py").write_text(f'password = "{value}"\n')

    findings, _ = sec.scan_files(root, Path(sec.__file__).resolve())

    secrets = [f for f in findings if f["kind"] == "secrets"]
    assert secrets, findings
    for finding in secrets:
        assert value not in json.dumps(finding)
        assert "Abcd" not in json.dumps(finding["evidence"])


def test_sec_gate_non_blocking_agent_finding_without_a_task_keeps_the_verdict(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    findings = tmp_path / "findings.json"
    findings.write_text(json.dumps({"T-target-1": [{"severity": "minor", "summary": "agent says"}]}))

    r = run_script("sec_gate.py", "--root", str(root), "--json", "--per-target-findings", str(findings),
                   env={"SWARM_DIR": str(tmp_path / ".swarm")})
    out = json.loads(r.stdout)

    assert (r.returncode, out["status"], out["verdict"]) == (0, "ok", "pass")


def test_rel_plan_with_no_task_signs_no_pass_for_any_gate(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    r = run_script("rel_plan.py", "--root", str(root), "--json", env={"SWARM_DIR": str(tmp_path / ".swarm")})
    out = json.loads(r.stdout)

    runs = out["envelope"]["payload"]["runs"]
    gate_states = {g: s for g, s in runs.items() if g != "freeze"}
    assert out["verdict"] == "fail" and out["status"] == "fail"
    assert gate_states and set(gate_states.values()) == {"skipped:no-task"}, runs


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
                   cwd=repo, check=True, capture_output=True)


def _committed_file(tmp_path, name, text):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", "base")
    return repo


@pytest.mark.xfail(strict=True, reason="BLOCKED P4-15: Rust mirror crates/gsd-adapter/src/gates/rev_gate.rs parse_diff must take the same header rule (RS-02)")
def test_added_line_starting_with_plusplus(tmp_path):
    repo = _committed_file(tmp_path, "a.py", "x = 1\n")
    (repo / "a.py").write_text("x = 1\n++ X\n", encoding="utf-8")
    changes = _load("rev_gate").collect_changes(repo, "HEAD")
    assert changes["a.py"]["lines"] == [(2, "++ X")]
    assert "X" not in changes


@pytest.mark.xfail(strict=True, reason="BLOCKED P4-16: Rust mirror crates/gsd-adapter/src/gates/rev_gate.rs parse_diff must unquote the header path (RS-02)")
def test_quoted_utf8_path(tmp_path):
    repo = _committed_file(tmp_path, "café.py", "x = 1\n")
    (repo / "café.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    changes = _load("rev_gate").collect_changes(repo, "HEAD")
    assert set(changes) == {"café.py"}
    assert changes["café.py"]["lines"] == [(2, "y = 2")]


def test_malformed_allow_list_is_e_input(tmp_path):
    findings = tmp_path / "findings.json"
    findings.write_text('[{"severity": ')
    with pytest.raises(SwarmError) as exc:
        _load("rev_gate").load_extra(str(findings), 0)
    assert exc.value.code is ErrorCode.E_INPUT


@pytest.mark.xfail(strict=True, reason="BLOCKED P4-21: Rust mirror crates/gsd-adapter/src/gates/rel_plan.rs read_freeze must reject a non-object record (RS-02)")
@pytest.mark.parametrize("body", ["[]", "7"])
def test_release_freeze_non_dict_is_unreadable(body, tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    rel = _load("rel_plan")
    rel.freeze_file(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    rel.freeze_file(tmp_path).write_text(body)
    assert rel.read_freeze(tmp_path) == {"reason": "unreadable freeze file", "frozen_at": None, "by": "unknown"}


@pytest.mark.parametrize(("name", "route"), [("rev_gate", "rev-gate"), ("rel_plan", "rel-plan")])
def test_gate_block_calls_the_shared_gate_helper(name, route, tmp_path, monkeypatch):
    served = []

    def helper(script, engine_routes, argv, **options):
        served.append((script.name, tuple(engine_routes), list(argv), options["route_name"]))
        return 0

    monkeypatch.setattr(engine_select, "run_gate_engine", helper)
    monkeypatch.setenv("GSD_ENGINE", "rust")
    monkeypatch.setenv("GSD_ENGINE_BIN", str(tmp_path / "no-engine"))
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    monkeypatch.setattr(sys, "argv", [name, "--json"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(str(ROOT / "scripts" / f"{name}.py"), run_name="__main__")
    assert exit_info.value.code == 0
    assert served == [(name, (), ["--json"], route)]
