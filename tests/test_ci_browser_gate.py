"""The browser CI gate (Phase 14.2, criterion 5): Chrome discovery, the not-run exit, and the PASS/FAIL
accounting of the two acceptance scripts, driven with a fake node so no real browser is needed."""

import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "_ci_browser_gate.py"

# Prints FAKE_HEADERS_PASS or FAKE_SAB_PASS PASS lines for the script it is handed, then FAKE_FAIL FAIL
# lines, echoes the CHROME_PATH it was given, and exits with FAKE_EXIT.
FAKE_NODE = """#!/bin/sh
case "$1" in
  *browser_headers.mjs) count="${FAKE_HEADERS_PASS:-5}" ;;
  *browser_sab.mjs) count="${FAKE_SAB_PASS:-20}" ;;
  *) echo "unexpected script $1" >&2; exit 9 ;;
esac
echo "chrome-path=$CHROME_PATH"
i=0
while [ "$i" -lt "$count" ]; do echo "PASS case $i"; i=$((i + 1)); done
i=0
while [ "$i" -lt "${FAKE_FAIL:-0}" ]; do echo "FAIL case $i"; i=$((i + 1)); done
exit "${FAKE_EXIT:-0}"
"""
FAKE_CHROME = '#!/bin/sh\necho "Fake Chrome 1.2.3"\n'


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "bin"
    directory.mkdir()
    return directory


def _gate(bin_dir: Path, tmp_path: Path, **extra: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Runs the gate with PATH limited to bin_dir and a fake node; returns the result and the summary file."""
    node = _executable(tmp_path / "fake-node", FAKE_NODE)
    summary = tmp_path / "summary.md"
    env = {"PATH": str(bin_dir), **extra, "GITHUB_STEP_SUMMARY": str(summary)}
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--node", str(node)], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    return done, summary


def test_missing_chrome_exits_3_and_reports(bin_dir: Path, tmp_path: Path) -> None:
    done, summary = _gate(bin_dir, tmp_path)
    assert done.returncode == 3
    assert "browser acceptance NOT RUN: no Chrome" in done.stdout
    assert "::error::browser acceptance NOT RUN: no Chrome" in done.stdout
    assert "chrome-path=" not in done.stdout, "node must not run without Chrome"
    assert "browser acceptance NOT RUN: no Chrome" in summary.read_text(encoding="utf-8")


def test_explicit_bad_chrome_path_does_not_fall_back_to_path(bin_dir: Path, tmp_path: Path) -> None:
    _executable(bin_dir / "google-chrome", FAKE_CHROME)
    done, _ = _gate(bin_dir, tmp_path, CHROME_PATH=str(tmp_path / "nonexistent"))
    assert done.returncode == 3
    assert "NOT RUN: no Chrome" in done.stdout
    assert "chrome-path=" not in done.stdout


def test_chrome_path_that_is_not_executable_is_rejected(bin_dir: Path, tmp_path: Path) -> None:
    plain = tmp_path / "not-executable"
    plain.write_text(FAKE_CHROME, encoding="utf-8")
    plain.chmod(0o644)
    done, _ = _gate(bin_dir, tmp_path, CHROME_PATH=str(plain))
    assert done.returncode == 3


@pytest.mark.parametrize("names", [["chromium"], ["chromium-browser"], ["google-chrome-stable"], ["chromium", "google-chrome"]])
def test_path_discovery_picks_the_first_name_in_order(bin_dir: Path, tmp_path: Path, names: list[str]) -> None:
    for name in names:
        _executable(bin_dir / name, FAKE_CHROME)
    expected = bin_dir / ("google-chrome" if "google-chrome" in names else names[0])
    done, _ = _gate(bin_dir, tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"chrome-path={expected}" in done.stdout
    assert "Fake Chrome 1.2.3" in done.stdout


@pytest.mark.parametrize(
    ("extra", "expected_code", "needle"),
    [
        ({}, 0, "ok (PASS 20, FAIL 0, minimum PASS 20)"),
        ({"FAKE_HEADERS_PASS": "6", "FAKE_SAB_PASS": "21"}, 0, "ok (PASS 21, FAIL 0, minimum PASS 20)"),
        ({"FAKE_HEADERS_PASS": "4"}, 1, "only 4 PASS line(s), need at least 5"),
        ({"FAKE_SAB_PASS": "19"}, 1, "only 19 PASS line(s), need at least 20"),
        ({"FAKE_FAIL": "1"}, 1, "1 FAIL line(s)"),
        ({"FAKE_EXIT": "1"}, 1, "exit code 1"),
    ],
    ids=["success", "above-minimum", "headers-short", "sab-short", "fail-line", "non-zero-exit"],
)
def test_pass_fail_accounting(bin_dir: Path, tmp_path: Path, extra: dict[str, str], expected_code: int, needle: str) -> None:
    chrome = _executable(bin_dir / "google-chrome", FAKE_CHROME)
    done, summary = _gate(bin_dir, tmp_path, **extra)
    assert done.returncode == expected_code, done.stdout + done.stderr
    assert needle in done.stdout
    assert needle in summary.read_text(encoding="utf-8")
    assert f"chrome-path={chrome}" in done.stdout


def test_explicit_chrome_path_wins_over_path_and_is_passed_to_node(bin_dir: Path, tmp_path: Path) -> None:
    _executable(bin_dir / "google-chrome", FAKE_CHROME)
    chosen = _executable(tmp_path / "my-chrome", FAKE_CHROME)
    done, _ = _gate(bin_dir, tmp_path, CHROME_PATH=str(chosen))
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.count(f"chrome-path={chosen}") == 2


def test_both_scripts_run_even_when_the_first_fails(bin_dir: Path, tmp_path: Path) -> None:
    _executable(bin_dir / "google-chrome", FAKE_CHROME)
    done, summary = _gate(bin_dir, tmp_path, FAKE_HEADERS_PASS="0", FAKE_SAB_PASS="3")
    assert done.returncode == 1
    text = summary.read_text(encoding="utf-8")
    assert "browser_headers.mjs: FAIL" in text and "browser_sab.mjs: FAIL" in text


def test_missing_node_is_reported_not_run(bin_dir: Path, tmp_path: Path) -> None:
    _executable(bin_dir / "google-chrome", FAKE_CHROME)
    env = {"PATH": str(bin_dir), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--node", "no-such-node"], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 3
    assert "NOT RUN: no node" in done.stdout


def test_summary_is_optional(bin_dir: Path, tmp_path: Path) -> None:
    node = _executable(tmp_path / "fake-node", FAKE_NODE)
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--node", str(node)],
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 3
    assert "NOT RUN: no Chrome" in done.stdout
