"""Gate oracles (2026-11): secret evasion by trailing comment, scan completeness, untracked path decoding, coverage floor.

Red today: a trailing "example" comment hides a committed credential (sec), an oversized in-scope text file with a
credential is skipped silently (sec), an untracked non-ASCII path is octal-escaped by git and dropped (rev), and
--min-coverage passes silently when the coverage report has no TOTAL line (qa). Green control: an oversized binary is
not a scan-incomplete finding.
"""
import importlib.util
import json
import shutil
import stat
import string
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import _scrubbed_base_env, run_script

ROOT = Path(__file__).resolve().parent.parent
GITHUB_TOKEN = "ghp_" + string.digits + string.ascii_lowercase  # "ghp_" + 36 alphanumerics
CREDENTIAL = 'password = "Abcd0123456789efghIJKL"'
OVERSIZED = 2_000_000  # sec_gate skips text files strictly larger than this


def _load(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_oracles_2026_11_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sec_scan(root):
    sec = _load("sec_gate")
    findings, _ = sec.scan_files(root, Path(sec.__file__).resolve())
    return findings


def _mentions_incomplete(finding):
    return "incomplete" in json.dumps(finding).lower()


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=gate-test", "-c", "user.email=gate-test@example.invalid",
                    "-c", "commit.gpgsign=false", *args], cwd=root, check=True, capture_output=True, text=True)


# Fake interpreter: the pytest run prints a pass line; the --cov run prints a report with no TOTAL line.
_FAKE_PYTHON = """#!/bin/sh
case " $* " in
  *" --cov "*) echo 'Name    Stmts   Miss  Cover' ;;
  *) echo '1 passed' ;;
esac
exit 0
"""
# qa_gate runs `sys.executable -m pytest ...`; the driver points sys.executable at the fake and runs the real script.
_DRIVER = "import runpy, sys; sys.executable = sys.argv[1]; sys.argv = sys.argv[2:]; runpy.run_path(sys.argv[0], run_name='__main__')"


def test_sec_credential_with_trailing_example_comment_is_still_a_secret(tmp_path):
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    (root / "app" / "config.py").write_text(f'TOKEN = "{GITHUB_TOKEN}"  # example\n')

    secrets = [f for f in _sec_scan(root) if f["kind"] == "secrets"]

    assert any(f["severity"] in ("critical", "major") and f["location"].startswith("app/config.py:1")
               for f in secrets), secrets


def test_sec_oversized_text_file_with_credential_reports_scan_incomplete(tmp_path):
    root = tmp_path / "repo"
    (root / "logs").mkdir(parents=True)
    big = root / "logs" / "dump.txt"
    big.write_text(CREDENTIAL + "\n" + "log line padding\n" * 130_000, encoding="utf-8")
    assert big.stat().st_size > OVERSIZED

    incomplete = [f for f in _sec_scan(root) if _mentions_incomplete(f)]

    assert any(f["severity"] == "major" and "logs/dump.txt" in json.dumps(f) for f in incomplete), incomplete


def test_sec_oversized_binary_is_not_reported_as_scan_incomplete(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "blob.bin").write_bytes(b"\xff" * (OVERSIZED + 1))

    assert [f for f in _sec_scan(root) if _mentions_incomplete(f)] == []


def test_rev_untracked_non_ascii_secret_file_is_scanned(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("rev_gate's untracked-file path requires git")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "README.md").write_text("v1\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "first")
    (root / "README.md").write_text("v2\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "second")  # HEAD~1 exists, so the diff-base path lists untracked files
    secret = "secrets_é.env"
    (root / secret).write_text(f'GITHUB_TOKEN = "{GITHUB_TOKEN}"\n', encoding="utf-8")

    r = run_script("rev_gate.py", "--root", str(root), "--json", env={"SWARM_DIR": str(tmp_path / ".swarm")})
    out = json.loads(r.stdout)

    hits = [f for f in out["findings"] if f["kind"] == "secret" and (f["location"] or "").startswith(secret)]
    assert hits and hits[0]["severity"] == "major", out["findings"]


def test_qa_min_coverage_without_total_line_is_a_major_coverage_finding(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    fake = tmp_path / "bin" / "fake-python"
    fake.parent.mkdir(parents=True)
    fake.write_text(_FAKE_PYTHON)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    child_env = _scrubbed_base_env()
    child_env.pop("GSD_ENGINE", None)
    child_env["SWARM_DIR"] = str(tmp_path / ".swarm")

    proc = subprocess.run([sys.executable, "-c", _DRIVER, str(fake), str(ROOT / "scripts" / "qa_gate.py"),
                           "--root", str(root), "--risk-class", "low", "--min-coverage", "80", "--json"],
                          capture_output=True, text=True, env=child_env, cwd=ROOT, timeout=120)

    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    body = json.loads(proc.stdout)
    assert [f for f in body["findings"] if f["kind"] == "coverage" and f["severity"] == "major"], body["findings"]
