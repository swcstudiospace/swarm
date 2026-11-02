#!/usr/bin/env python3
"""A08 — run the repository's test suites and emit a signed quality gate verdict.

Detects pytest / npm test / go test / cargo test / .NET, runs the tiers required by the
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
from scripts._dotnet_quality import project_files, run_dotnet  # noqa: E402

TIERS_BY_RISK = {"low": ["unit"], "medium": ["unit", "integration"],
                 "high": ["unit", "integration", "e2e", "perf"]}
_SHELL_HEADS = frozenset(
    "! . : [ [[ ]] { } case do done elif else esac fi for function if in select then time until while "
    "coproc break continue return exit exec eval command builtin env sudo doas xargs source bash sh "
    "dash zsh fish nohup timeout nice setsid".split()
)
_NODE_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_NODE_METADATA = re.compile(
    r"(?P<style>#|ℹ) (?:(?P<count>tests|suites|pass|fail|cancelled|skipped|todo) (?P<n>[0-9]+)"
    r"|duration_ms (?P<duration_ms>[0-9]+(?:\.[0-9]+)?)"
    r"|duration (?P<duration>[0-9]+(?:\.[0-9]+)?(?:ms|s))"
    r"|(?P<stamp>start|end|time) (?P<stamp_value>.+))"
)


def _literal_script_commands(script: str) -> list[list[str]] | None:
    """Decode only literal SIMPLE (&& SIMPLE)*, never interpret or execute shell text."""
    if any(c in script for c in "\0\r\n"):
        return None
    commands, words, word = [], [], []
    quote, started, i = "", False, 0
    while i < len(script):
        c = script[i]
        if quote == "'":
            if c == "'":
                quote = ""
            else:
                word.append(c)
        elif c == "\\":
            if i + 1 == len(script):
                return None
            following = script[i + 1]
            if quote == '"' and following not in '$`"\\':
                word.append(c)
            else:
                i += 1
                word.append(following)
            started = True
        elif quote == '"':
            if c == '"':
                quote = ""
            elif c in "$`":
                return None
            else:
                word.append(c)
        elif c in "'\"":
            quote, started = c, True
        elif c in " \t":
            if started:
                words.append("".join(word))
                word, started = [], False
        elif c == "&" and script[i:i + 2] == "&&":
            if started:
                words.append("".join(word))
                word, started = [], False
            if not words:
                return None
            commands.append(words)
            words = []
            i += 1
        elif c in ";|&<>()$`#~*?[]{}" or ord(c) < 32 or 127 <= ord(c) <= 159:
            return None
        else:
            word.append(c)
            started = True
        i += 1
    if quote:
        return None
    if started:
        words.append("".join(word))
    if not words:
        return None
    commands.append(words)
    if any(not cmd[0] or "=" in cmd[0] or cmd[0].rsplit("/", 1)[-1] in _SHELL_HEADS for cmd in commands):
        return None
    return commands


def _is_node_test_script(script: object) -> bool:
    if not isinstance(script, str):
        return False
    commands = _literal_script_commands(script)
    if not commands or commands[-1][0] != "node":
        return False
    args = commands[-1][1:]
    test, reporter, filenames = False, False, False
    i = 0
    while i < len(args):
        arg = args[i]
        if not filenames and arg == "--test" and not test:
            test = True
        elif not filenames and not reporter and arg in ("--test-reporter=tap", "--test-reporter=spec"):
            reporter = True
        elif not filenames and not reporter and arg == "--test-reporter":
            i += 1
            if i == len(args) or args[i] not in ("tap", "spec"):
                return False
            reporter = True
        elif arg.startswith("-"):
            return False
        else:
            if not test:
                return False
            filenames = True
        i += 1
    return test


def _node_stream_evidence(text: str) -> dict | None:
    """The last top-level metadata block wins, including an incomplete one."""
    candidate, active = None, False
    for line in _NODE_ANSI.sub("", text).splitlines():
        match = _NODE_METADATA.fullmatch(line)
        if match is None:
            active = False
            continue
        style = match["style"]
        key = match["count"] or ("duration_ms" if match["duration_ms"] is not None else
                                 "duration" if match["duration"] is not None else match["stamp"])
        if not active or candidate["style"] != style or key in candidate["seen"]:
            candidate = {"style": style, "seen": set(), "passed": 0, "failed": 0}
        active = True
        candidate["seen"].add(key)
        if key in ("pass", "fail"):
            candidate["passed" if key == "pass" else "failed"] = int(match["n"])
    return candidate


def _node_test_evidence(stdout: str, stderr: str) -> tuple[int, int, bool]:
    out, err = _node_stream_evidence(stdout), _node_stream_evidence(stderr)
    candidate = err if err is not None else out
    if candidate is None:
        return 0, 0, False
    complete = {"pass", "fail"} <= candidate["seen"] and not (out is not None and err is not None)
    return candidate["passed"], candidate["failed"], complete




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
            node_test = _is_node_test_script(scripts["test"])
            runner = {"name": "npm test", "cmd": ["npm", "test", "--silent"] + ([] if node_test else ["--", "--ci"]),
                      "pass_re": r"Tests:\s+(\d+) passed|(\d+) passing", "fail_re": r"(\d+) failed|(\d+) failing"}
            if node_test:
                runner["node_test"] = True
            runners.append(runner)
    if (root / "go.mod").exists() and which("go"):
        runners.append({"name": "go test", "cmd": ["go", "test", "./..."], "pass_re": r"^ok", "fail_re": r"^FAIL|--- FAIL"})
    if (root / "Cargo.toml").exists() and which("cargo"):
        runners.append({"name": "cargo test", "cmd": ["cargo", "test", "-q"], "pass_re": r"(\d+) passed", "fail_re": r"(\d+) failed"})
    if project_files(root):
        # Missing dotnet is a required infrastructure failure, not an invisible workload.
        runners.append({"name": "dotnet"})
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
            if r["name"] == "dotnet":
                execution = run_dotnet(
                    ctx.root, target=getattr(args, "dotnet_target", None),
                    configuration=getattr(args, "dotnet_configuration", "Release"),
                    runtime_version=getattr(args, "dotnet_runtime_version", None),
                    required_modules=getattr(args, "dotnet_required_test_module", []),
                    timeout=args.timeout,
                )
                ok = execution["ok"]
                executed.append(execution)
                if not ok:
                    findings.append(make_finding(
                        f"QF-{len(findings)+1:03d}", "major",
                        "infrastructure" if execution["returncode"] is None else "functional",
                        f"dotnet {execution['stage']} failed: {execution['reason']}",
                        evidence=execution["tail"][-800:], owner_suggestion="A05"))
            else:
                proc = sh(r["cmd"], cwd=ctx.root, timeout=args.timeout)
                out = proc.stdout + proc.stderr
                missing_node_evidence = False
                if r.get("node_test"):
                    passed, failed, complete = _node_test_evidence(proc.stdout, proc.stderr)
                    missing_node_evidence = not complete or passed == 0
                    ok = proc.returncode == 0 and complete and passed > 0 and failed == 0
                else:
                    passed, failed = _count(r["pass_re"], out), _count(r["fail_re"], out)
                    ok = proc.returncode == 0 and failed == 0
                execution = {"runner": r["name"], "returncode": proc.returncode, "passed": passed, "failed": failed,
                             "tail": out[-1500:]}
                if r.get("node_test"):
                    execution["evidence_complete"] = complete
                executed.append(execution)
                if not ok:
                    summary = ("npm test missing Node test evidence (complete pass/fail aggregate with passed > 0 required)"
                               if missing_node_evidence and proc.returncode == 0 else f"{r['name']} failed ({failed} failing)")
                    findings.append(make_finding(f"QF-{len(findings)+1:03d}", "major", "functional",
                                                 summary, evidence=out[-800:],
                                                 owner_suggestion="A05"))
            # A later passing runner must never overwrite an earlier tier failure.
            for tier in ("unit", "integration"):
                if tier in tiers:
                    runs[tier] = "pass" if ok and runs[tier] != "fail" else "fail"
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
    p.add_argument("--dotnet-target", help="explicit repository-relative project/solution/filter; required when ambiguous")
    p.add_argument("--dotnet-runtime-version", help="runtime pin passed to locked restore/build (no SDK installation)")
    p.add_argument("--dotnet-configuration", default="Release", help=".NET build/test configuration (default: Release)")
    p.add_argument("--dotnet-required-test-module", action="append", default=[],
                   help="repeatable required assembly stem; each needs positive fresh TRX execution; "
                        "SDK10 Microsoft.Testing.Platform required in global.json; "
                        "--timeout is shared across .NET restore/build/test")
    p.add_argument("--per-target-findings",
                   help="JSON object {target task id: [findings]}: the agent's findings; each gate target's verdict "
                        "gets the script's findings plus only its own (targets it omits get the script's findings only)")


# Engine selector. The default row stays Python. `GSD_ENGINE=rust` serves the grammar `gates::qa_gate` accepts and
# leaves every other argv to this body. `GSD_QA_PYTHON` is this interpreter, so runner detection matches `sys.executable`.
# The route writes (the binary records the verdict in issue_gate's transaction), so it is a write route: Python answers only a
# binary reply that says nothing was written. A route that commits a verdict reports a committed outcome
# as final, so Python is not re-run.
def _binary_env(route: str) -> dict[str, str]:
    from swarm.engine_select import BINARY_ENV_NAMES
    names = set(BINARY_ENV_NAMES) | {"GSD_QA_PYTHON", "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE",
                                     "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION", "XDG_CONFIG_HOME",
                                     "SWARM_DRYRUN_FAIL"}
    if route != "qa-gate":
        raise KeyError(route)
    return {key: value for key, value in os.environ.items() if key in names}


def _claims_qa(argv: list[str]) -> bool:
    return "--json" in argv and "--dry-run" not in argv and "--help" not in argv and "-h" not in argv


def _qa_commit(call, before, effects_path, reply, stdout):
    # The binary has committed the verdict and its reply is final; the gate routes write no effects manifest to replay.
    return reply, stdout


ENGINE_ROUTES: tuple = ()


if __name__ == "__main__":
    script = AgentScript("A08", "qa_gate", run, description=__doc__, add_args=add_args)
    os.environ["GSD_QA_PYTHON"] = sys.executable
    answer = None
    if os.environ.get("GSD_ENGINE", "python") not in ("python", "") or ENGINE_ROUTES:
        from swarm import engine_select
        if ENGINE_ROUTES:
            answer = engine_select.select_engine(engine_select.Selector(script, ENGINE_ROUTES), sys.argv[1:])
        else:
            route = engine_select.Route(
                claims=_claims_qa, parse=lambda argv: argv if _claims_qa(argv) else None,
                default=lambda: "python", env_route=lambda argv, call: "qa-gate",
                write=engine_select.WriteFlow(
                    before=lambda call: None, commit=_qa_commit,
                    unknown=lambda call: ("the verdict may or may not have been recorded; check the gate's verdict rows and "
                                          "verdicts/ before running the gate again", {}),
                    preexec=lambda route: engine_select.no_core_dumps,
                ),
            )
            answer = engine_select.select_engine(
                engine_select.Selector(script, (route,), binary_env=_binary_env,
                                       env_keys=("SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT_SESSION")),
                sys.argv[1:],
            )
    sys.exit(answer if answer is not None else script.main())
