#!/usr/bin/env python3
"""A08 — run the repository's test suites and emit a signed quality gate verdict.

Detects pytest / npm test / go test / cargo test, runs the tiers required by the
risk class, parses pass/fail, and writes a signed `gate.verdict` envelope to
.swarm/verdicts/<task_id>.quality.json. When --task-id is a quality gate task, one verdict
row is recorded on each target in its Task Store notes.gate_for (swarm.verdicts).
"""
from __future__ import annotations
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.script_base import AgentScript, sh, which  # noqa: E402
from swarm.gates import make_finding  # noqa: E402
from swarm.verdicts import check_per_target_keys, gate_risk_class, gate_verdict, issue_gate, load_per_target  # noqa: E402

TIERS_BY_RISK = {"low": ["unit"], "medium": ["unit", "integration"],
                 "high": ["unit", "integration", "e2e", "perf"]}


def detect_runners(root: Path) -> list[dict]:
    runners = []
    if (root / "pyproject.toml").exists() or (root / "pytest.ini").exists() or list(root.glob("tests/test_*.py")):
        # Same interpreter that runs `sys.executable -m pytest`. A pytest binary on PATH is not required.
        if importlib.util.find_spec("pytest") is not None:
            runners.append({"name": "pytest", "cmd": [sys.executable, "-m", "pytest", "-q", "--maxfail=50", "-p", "no:cacheprovider"],
                            "pass_re": r"(\d+) passed", "fail_re": r"(\d+) failed|(\d+) error"})
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        if "test" in scripts and which("npm"):
            runners.append({"name": "npm test", "cmd": ["npm", "test", "--silent", "--", "--ci"],
                            "pass_re": r"Tests:\s+(\d+) passed|(\d+) passing", "fail_re": r"(\d+) failed|(\d+) failing"})
    if (root / "go.mod").exists() and which("go"):
        runners.append({"name": "go test", "cmd": ["go", "test", "./..."], "pass_re": r"^ok", "fail_re": r"^FAIL|--- FAIL"})
    if (root / "Cargo.toml").exists() and which("cargo"):
        runners.append({"name": "cargo test", "cmd": ["cargo", "test", "-q"], "pass_re": r"(\d+) passed", "fail_re": r"(\d+) failed"})
    return runners


def _count(pattern: str, text: str) -> int:
    total = 0
    for m in re.finditer(pattern, text, re.M):
        groups = [g for g in m.groups() if g] if m.groups() else ["1"]
        total += int(groups[0]) if groups and groups[0].isdigit() else 1
    return total


def run(args, ctx) -> dict:
    # D-13: a gate task runs at the highest risk class of itself and its targets unless --risk-class overrides it
    risk = args.risk_class or gate_risk_class(ctx.task_id, ctx.root) or "medium"
    tiers = args.tier.split(",") if args.tier else TIERS_BY_RISK[risk]
    findings, runs, executed = [], {t: "skipped:not-selected" for t in ("unit", "integration", "e2e", "perf")}, []

    if ctx.dry_run:
        runs.update({t: "pass" for t in tiers})
        verdict_env, recorded = issue_gate(ctx, gate="quality", agent_id="A08@dry", findings=[], runs=runs, simulate=True)
        return {"status": "ok", "verdict": "pass", "runs": runs, "findings": [], "dry_run": True,
                "envelope": verdict_env, "recorded": sorted(recorded), "summary": "dry-run: canned pass verdict"}

    runners = detect_runners(ctx.root)
    if not runners:
        findings.append(make_finding("QF-000", "major" if risk != "low" else "minor", "coverage",
                                     "no test runner detected in repository", evidence=str(ctx.root),
                                     owner_suggestion="A05"))
        for t in tiers:
            runs[t] = "skipped:infra"
    else:
        for r in runners:
            proc = sh(r["cmd"], cwd=ctx.root, timeout=args.timeout)
            out = proc.stdout + proc.stderr
            passed, failed = _count(r["pass_re"], out), _count(r["fail_re"], out)
            ok = proc.returncode == 0 and failed == 0
            executed.append({"runner": r["name"], "returncode": proc.returncode, "passed": passed, "failed": failed,
                             "tail": out[-1500:]})
            if "unit" in tiers:
                runs["unit"] = "pass" if ok else "fail"
            if "integration" in tiers and runs["integration"].startswith("skipped"):
                runs["integration"] = "pass" if ok else "fail"
            if not ok:
                findings.append(make_finding(f"QF-{len(findings)+1:03d}", "major", "functional",
                                             f"{r['name']} failed ({failed} failing)", evidence=out[-800:],
                                             owner_suggestion="A05"))
        for t in ("e2e", "perf"):
            if t in tiers:
                runs[t] = "skipped:infra"
                if risk == "high":
                    findings.append(make_finding(f"QF-{len(findings)+1:03d}", "major", t,
                                                 f"{t} tier required for risk_class=high but no harness configured",
                                                 owner_suggestion="A11"))

    # optional coverage (pytest-cov) threshold
    if args.min_coverage and any(e["runner"] == "pytest" for e in executed):
        proc = sh([sys.executable, "-m", "pytest", "-q", "--cov", "--cov-report=term", "-p", "no:cacheprovider"], cwd=ctx.root, timeout=args.timeout)
        m = re.search(r"TOTAL.*?(\d+)%", proc.stdout)
        if m and int(m.group(1)) < args.min_coverage:
            findings.append(make_finding(f"QF-{len(findings)+1:03d}", "major", "coverage",
                                         f"coverage {m.group(1)}% < {args.min_coverage}%", owner_suggestion="A05"))

    # D-13: the agent's per-target findings join the script's own findings on their target only; a target the
    # agent omits gets the script's findings alone
    own = load_per_target(args.per_target_findings, len(findings), prefix="QF", owner="A05")
    check_per_target_keys(own, ctx.task_id, ctx.root)
    per_target = {t: findings + items for t, items in own.items()} if own else None
    env, recorded = issue_gate(ctx, gate="quality", agent_id="A08@local", findings=findings, runs=runs,
                               per_target=per_target)
    findings = findings + [f for items in own.values() for f in items]
    verdict = gate_verdict(env, recorded)
    return {"status": "ok" if verdict == "pass" else "fail", "verdict": verdict, "risk_class": risk, "runs": runs,
            "findings": findings, "executed": executed, "envelope": env, "recorded": sorted(recorded),
            "summary": f"quality gate {verdict.upper()} — runners: {[e['runner'] for e in executed] or 'none'}"}


def add_args(p):
    p.add_argument("--risk-class", choices=["low", "medium", "high"],
                   help="tier selection; default: the highest risk class of the gate task and its targets, else medium")
    p.add_argument("--tier", help="comma list overriding risk-based tier selection (unit,integration,e2e,perf)")
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--min-coverage", type=int, default=0, help="fail if pytest-cov TOTAL below this percent")
    p.add_argument("--per-target-findings",
                   help="JSON object {target task id: [findings]}: the agent's findings; each gate target's verdict "
                        "gets the script's findings plus only its own (targets it omits get the script's findings only)")


# Engine selector (swarm/engine_select.py): inert while ENGINE_ROUTES is empty. A unit that gives this script a route on the Rust engine
# registers its `engine_select.Route` here; until then GSD_ENGINE=python (or unset) costs nothing and `rust` is answered as an unserved route.
ENGINE_ROUTES: tuple = ()


if __name__ == "__main__":
    script = AgentScript("A08", "qa_gate", run, description=__doc__, add_args=add_args)
    answer = None
    if ENGINE_ROUTES or os.environ.get("GSD_ENGINE", "python") not in ("python", ""):
        from swarm import engine_select
        answer = engine_select.select_engine(engine_select.Selector(script, ENGINE_ROUTES), sys.argv[1:])
    sys.exit(answer if answer is not None else script.main())
