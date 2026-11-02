"""P4-01/P4-02/P4-04/P4-05: audit errors, secret evidence, and release no-task regressions.

An audit tool that exits non-zero without a countable result fails the run (P4-01 npm, P4-02 govulncheck). Secret values
never reach evidence (P4-04). A release run that evaluated no task signs no pass (P4-05).
"""
import importlib.util
import json
import stat
from pathlib import Path

from conftest import run_script

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
