#!/usr/bin/env python3
"""Run the two Chrome acceptance scripts as one CI gate (Phase 14.2, criterion 5).

Runs `node tests/acceptance/browser_headers.mjs` and `browser_sab.mjs` from the repository root with
CHROME_PATH set, 300 s each, and counts the stdout lines that start `PASS ` and `FAIL `. The minimums
are 5 PASS (headers: 1 isolated page, 3 header checks, 1 control) and 20 PASS (sab: 2 contexts, each
1 mode check plus the 9 parity vectors). They are minimums so that added vectors never break the gate.

Chrome discovery: when CHROME_PATH is set and non-empty it is the only candidate and must be an
executable file (no fallback to PATH, so a typo cannot silently test another browser). Otherwise the
first of google-chrome, google-chrome-stable, chromium, chromium-browser found on PATH is used.

The job runs as an unprivileged user with no Docker and no sudo: nothing here needs either, and the
only writes are to $GITHUB_STEP_SUMMARY when that is set. Test hooks: --node and --scripts-dir.

Exit codes: 0 both scripts pass, 1 a script failed (non-zero exit, a FAIL line, too few PASS lines,
or a timeout), 3 not run (no Chrome, or no node): the missing tool is reported, never skipped silently.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TIMEOUT_SECONDS = 300
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
MINIMUM_PASS = {"browser_headers.mjs": 5, "browser_sab.mjs": 20}


def find_chrome(env: dict[str, str]) -> str | None:
    explicit = env.get("CHROME_PATH", "")
    if explicit:
        return explicit if os.path.isfile(explicit) and os.access(explicit, os.X_OK) else None
    for name in CHROME_NAMES:
        found = shutil.which(name, path=env.get("PATH"))
        if found:
            return found
    return None


def report(message: str, summary: Path | None) -> None:
    print(message, flush=True)
    if summary is not None:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write(message + "\n")


def run_script(node: str, script: Path, env: dict[str, str]) -> tuple[int, int, int | None, str]:
    """Returns (pass count, fail count, exit code or None on timeout, captured stdout)."""
    try:
        done = subprocess.run(
            [node, str(script)], cwd=ROOT, env=env, capture_output=True, timeout=TIMEOUT_SECONDS, check=False
        )
    except subprocess.TimeoutExpired:
        return 0, 0, None, ""
    stdout = done.stdout.decode("utf-8", errors="replace")
    lines = stdout.splitlines()
    passes = sum(1 for line in lines if line.startswith("PASS "))
    fails = sum(1 for line in lines if line.startswith("FAIL "))
    if done.returncode != 0:
        sys.stderr.write(done.stderr.decode("utf-8", errors="replace"))
    return passes, fails, done.returncode, stdout


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the browser acceptance scripts as a CI gate.")
    parser.add_argument("--node", default="node", help="node executable (test hook)")
    parser.add_argument("--scripts-dir", type=Path, default=ROOT / "tests" / "acceptance", help="acceptance scripts (test hook)")
    args = parser.parse_args(argv)
    env = dict(os.environ if env is None else env)
    summary_path = env.get("GITHUB_STEP_SUMMARY")
    summary = Path(summary_path) if summary_path else None

    chrome = find_chrome(env)
    if chrome is None:
        print("::error::browser acceptance NOT RUN: no Chrome", flush=True)
        report("browser acceptance NOT RUN: no Chrome", summary)
        return 3
    if shutil.which(args.node, path=env.get("PATH")) is None:
        print("::error::browser acceptance NOT RUN: no node", flush=True)
        report("browser acceptance NOT RUN: no node", summary)
        return 3
    env["CHROME_PATH"] = chrome

    try:
        version = subprocess.run([chrome, "--version"], capture_output=True, text=True, timeout=30, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = "version unavailable"
    print(f"chrome: {chrome} ({version or 'version unavailable'})", flush=True)

    failed = False
    for name, minimum in MINIMUM_PASS.items():
        passes, fails, code, stdout = run_script(args.node, args.scripts_dir / name, env)
        sys.stdout.write(stdout)
        if code is None:
            problem = f"timed out after {TIMEOUT_SECONDS} s"
        elif code != 0:
            problem = f"exit code {code}"
        elif fails > 0:
            problem = f"{fails} FAIL line(s)"
        elif passes < minimum:
            problem = f"only {passes} PASS line(s), need at least {minimum}"
        else:
            problem = ""
        if problem:
            failed = True
            print(f"::error::{name}: {problem}", flush=True)
        counts = f"PASS {passes}, FAIL {fails}, minimum PASS {minimum}"
        report(f"{name}: FAIL ({problem}; {counts})" if problem else f"{name}: ok ({counts})", summary)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
