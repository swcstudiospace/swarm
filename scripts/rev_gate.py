#!/usr/bin/env python3
"""A09 — mechanical review of a diff and emission of a signed review gate verdict.

Computes the change set against --diff-base (default: HEAD~1, else origin/main, else
the whole tree when the root is not a git checkout
untracked files always count as
added), produces per-file diff stats, runs rules-only checks (oversized files,
TODO/FIXME/XXX, debug prints, commented-out code, missing tests, secret-looking
strings, invisible/bidi control characters), merges optional LLM findings from
--findings-file (every target) and --per-target-findings (each gate target only its
own), and writes a signed `gate.verdict` (gate=review) envelope to
.swarm/verdicts/<task_id>.review.json (recorded in the Task Store when the task exists).
"""
from __future__ import annotations
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.script_base import AgentScript, sh, which, iter_files  # noqa: E402
from swarm.gates import make_finding  # noqa: E402
from swarm.verdicts import agent_finding, check_per_target_keys, issue_gate, load_per_target  # noqa: E402

CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h"}
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|_spec)\.[a-z]+$|(^|/)test_[^/]+\.py$")
TODO_RE = re.compile(r"\b(TODO|FIXME|XXX)\b")
DEBUG_RE = re.compile(r"console\.(log|debug|trace)\(|\bdebugger\b;?$|breakpoint\(\)|pdb\.set_trace\(|\bipdb\b|var_dump\(|\bdd\(")
PRINT_RE = re.compile(r"^\s*print\(")
CODE_LIKE_RE = re.compile(r"^\s*(#|//)\s*(if|for|while|return|import|from|def|class|const|let|var|function|else)\b.*|^\s*(#|//).*[;{}()]\s*$")
SECRET_RES = [re.compile(p) for p in (
    r"AKIA[0-9A-Z]{16}", r"\bgh[pousr]_[A-Za-z0-9]{36,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\bxox[baprs]-[0-9A-Za-z-]{10,}", r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\b\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]")]
PLACEHOLDER_RE = re.compile(r"(?i)example|placeholder|xxx|changeme|dummy|redacted|<[^>]*>|\$\{|\{\{|your[_-]|\.\.\.|…|\*{3}")
CONTROL_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff\x00-\x08\x0b\x0c\x0e-\x1f]")


def _entropy(s: str) -> float:
    return -sum((c / len(s)) * math.log2(c / len(s)) for c in {ch: s.count(ch) for ch in s}.values())


def resolve_base(root: Path, requested: str | None) -> str | None:
    if not which("git") or sh(["git", "rev-parse", "--is-inside-work-tree"], cwd=root).returncode != 0:
        return None
    for cand in ([requested] if requested else ["HEAD~1", "origin/main"]):
        if sh(["git", "rev-parse", "--verify", "-q", f"{cand}^{{commit}}"], cwd=root).returncode == 0:
            return cand
    return None


def collect_changes(root: Path, base: str | None) -> dict[str, dict]:
    """rel_path -> {added, deleted, lines: [(lineno, text)] for added lines}."""
    changes: dict[str, dict] = {}

    def whole(p: Path, rel: str):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            changes[rel] = {"added": 0, "deleted": 0, "lines": [], "binary": True}
            return
        lines = text.splitlines()
        changes[rel] = {"added": len(lines), "deleted": 0, "lines": list(enumerate(lines, 1)), "binary": False}

    if base is None:
        for p in iter_files(root):
            whole(p, p.relative_to(root).as_posix())
        return changes
    diff = sh(["git", "diff", "-U0", "--no-color", base, "--", "."], cwd=root).stdout
    cur, new_no = None, 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            rel = line[4:].removeprefix("b/").strip()
            cur = changes.setdefault(rel, {"added": 0, "deleted": 0, "lines": [], "binary": False}) if rel != "/dev/null" else None
        elif line.startswith("@@") and cur is not None:
            m = re.search(r"\+(\d+)", line)
            new_no = int(m.group(1)) if m else 0
        elif cur is not None and line.startswith("+") and not line.startswith("+++"):
            cur["added"] += 1
            cur["lines"].append((new_no, line[1:]))
            new_no += 1
        elif cur is not None and line.startswith("-") and not line.startswith("---"):
            cur["deleted"] += 1
        elif line.startswith("Binary files") and cur is not None:
            cur["binary"] = True
    skip = {".swarm", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
    # `-z`: names come back as raw bytes, not quoted octal; decoded with the filesystem encoding so the real path is read.
    listed = subprocess.run(["git", "ls-files", "-z", "--others", "--exclude-standard", "."], cwd=root, capture_output=True, timeout=600).stdout
    for raw in listed.split(b"\0"):
        rel = os.fsdecode(raw)
        if rel and not skip & set(Path(rel).parts):
            whole(root / rel, rel)
    return changes


def mechanical_checks(changes: dict[str, dict], max_lines: int, self_path: Path, root: Path) -> list[dict]:
    findings, n = [], [0]

    def add(sev, kind, summary, loc, evidence=""):
        n[0] += 1
        findings.append(make_finding(f"RF-{n[0]:03d}", sev, kind, summary, evidence=evidence, location=loc,
                                     owner_suggestion="A05"))

    src_changed, tests_changed = False, False
    for rel, ch in sorted(changes.items()):
        ext = Path(rel).suffix
        is_code, is_test = ext in CODE_EXTS, bool(TEST_RE.search(rel))
        src_changed |= is_code and not is_test
        tests_changed |= is_test
        if ch.get("binary"):
            add("info", "structure", "binary/undecodable file — structural checks only", rel)
            continue
        if ch["added"] + ch["deleted"] > max_lines:
            add("major", "size", f"{ch['added'] + ch['deleted']} changed lines exceeds {max_lines}; request a split", rel)
        run, self_file = 0, (root / rel).resolve() == self_path
        cli = ext != ".py" or any("__main__" in t or "argparse" in t for _, t in ch["lines"])
        for no, text in ch["lines"]:
            loc = f"{rel}:{no}"
            if CONTROL_RE.search(text):
                add("major", "encoding", "invisible/bidi control character (possible Trojan-source)", loc)
            if is_code and not self_file and TODO_RE.search(text):
                add("minor", "todo", "TODO/FIXME/XXX marker added", loc, text.strip()[:120])
            if is_code and not self_file and (DEBUG_RE.search(text) or (not cli and PRINT_RE.match(text))):
                add("minor", "debug", "debug statement left in code", loc, text.strip()[:120])
            run = run + 1 if (is_code and CODE_LIKE_RE.match(text)) else 0
            if run == 3:
                add("minor", "dead-code", "commented-out code block (>=3 lines)", loc)
            for rx in SECRET_RES if not self_file else ():
                m = rx.search(text)
                if m:
                    val = m.group(m.lastindex or 0)
                    if PLACEHOLDER_RE.search(val) or (m.lastindex and _entropy(val) < 3.2):
                        continue
                    add("major", "secret", "secret-looking string added (value redacted)", loc, f"value not recorded ({len(val)} chars)")
    if src_changed and not tests_changed:
        add("minor", "coverage", "source files changed but no test files changed", "tests/")
    return findings


def load_extra(path: str | None, offset: int) -> list[dict]:
    if not path:
        return []
    raw = json.loads(Path(path).read_text())
    raw = raw.get("findings", raw) if isinstance(raw, dict) else raw
    return [agent_finding(f, i, path) for i, f in enumerate(raw, offset + 1)]


def run(args, ctx) -> dict:
    if ctx.dry_run:
        stats = {"files": 1, "added": 12, "deleted": 3, "per_file": [{"file": "src/example.py", "added": 12, "deleted": 3}]}
        env, recorded = issue_gate(ctx, gate="review", agent_id="A09@dry", findings=[], expires_s=172800,
                                   runs={"rules": "pass", "semantic": "skipped:dry-run"}, simulate=True,
                                   extra={"mode": "rules-only", "diff_base": "HEAD~1", "stats": stats})
        return {"status": "ok", "verdict": "pass", "findings": [], "stats": stats, "dry_run": True, "envelope": env,
                "recorded": sorted(recorded),
                "summary": "dry-run: canned pass verdict"}
    base = resolve_base(ctx.root, args.diff_base)
    changes = collect_changes(ctx.root, base)
    per_file = [{"file": f, "added": c["added"], "deleted": c["deleted"]} for f, c in sorted(changes.items())]
    stats = {"files": len(changes), "added": sum(c["added"] for c in changes.values()),
             "deleted": sum(c["deleted"] for c in changes.values()), "per_file": per_file}
    mechanical = mechanical_checks(changes, args.max_lines, Path(__file__).resolve(), ctx.root)
    extra = load_extra(args.findings_file, len(mechanical))
    own = load_per_target(args.per_target_findings, len(mechanical) + len(extra))
    check_per_target_keys(own, ctx.task_id, ctx.root)
    semantic = extra + [f for items in own.values() for f in items]
    findings = mechanical + semantic
    llm = bool(args.findings_file or args.per_target_findings)
    blocking = ("major", "critical", "blocker")
    runs = {"rules": "fail" if any(f["severity"] in blocking for f in mechanical) else "pass",
            "semantic": ("fail" if any(f["severity"] in blocking for f in semantic) else "pass")
            if llm else "skipped:no-llm-findings"}
    # WR-14: a gate task's target gets the shared findings plus only the findings attributed to it
    per_target = {t: mechanical + extra + items for t, items in own.items()} if args.per_target_findings else None
    env, recorded = issue_gate(ctx, gate="review", agent_id="A09@local", findings=findings, runs=runs, expires_s=172800,
                               per_target=per_target,
                               extra={"mode": "rules+semantic" if llm else "rules-only",
                                      "diff_base": base or "whole-tree", "stats": {k: v for k, v in stats.items() if k != "per_file"}})
    verdict = env["payload"]["verdict"]
    return {"recorded": sorted(recorded), "status": "ok" if verdict == "pass" else "fail", "verdict": verdict, "findings": findings, "stats": stats,
            "runs": runs, "diff_base": base or "whole-tree", "envelope": env,
            "summary": f"review gate {verdict.upper()} — {stats['files']} files, +{stats['added']}/-{stats['deleted']}, "
                       f"{len(findings)} findings (base: {base or 'whole-tree'})"}


def add_args(p):
    p.add_argument("--diff-base", help="git ref to diff against (default: HEAD~1, then origin/main, else whole tree)")
    p.add_argument("--findings-file", help="JSON list of additional (LLM semantic) findings to merge into every target's verdict")
    p.add_argument("--per-target-findings",
                   help="JSON object {target task id: [findings]}: each gate target's verdict gets only its own findings "
                        "(targets it omits get every finding)")
    p.add_argument("--max-lines", type=int, default=800, help="changed-lines threshold per file for a major 'size' finding")


# Engine selector (swarm/engine_select.py). The default row stays Python. `GSD_ENGINE=rust` serves the grammar `gates::rev_gate`
# accepts (`--json`, not `--dry-run`) and leaves every other argv to this body. `ENGINE_ROUTES` is the seam a route registers in.
# The route writes (the binary records the review gate's verdict in issue_gate's transaction), so it is a write route: Python
# answers only a binary reply that says nothing was written. A route that commits a verdict reports a committed outcome
# as final, so Python is not re-run.
def _binary_env(route: str) -> dict[str, str]:
    from swarm.engine_select import BINARY_ENV_NAMES
    names = set(BINARY_ENV_NAMES) | {"XDG_CONFIG_HOME", "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE",
                                     "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION"}
    if route != "rev-gate":
        raise KeyError(route)
    return {key: value for key, value in os.environ.items() if key in names}


def _claims_rev(argv: list[str]) -> bool:
    return "--json" in argv and "--dry-run" not in argv and "--help" not in argv and "-h" not in argv


def _rev_commit(call, before, effects_path, reply, stdout):
    # The binary has committed the verdict and its reply is final; the gate routes write no effects manifest to replay.
    return reply, stdout


ENGINE_ROUTES: tuple = ()


if __name__ == "__main__":
    script = AgentScript("A09", "rev_gate", run, description=__doc__, add_args=add_args)
    answer = None
    if os.environ.get("GSD_ENGINE", "python") not in ("python", "") or ENGINE_ROUTES:
        from swarm import engine_select
        if ENGINE_ROUTES:
            answer = engine_select.select_engine(engine_select.Selector(script, ENGINE_ROUTES), sys.argv[1:])
        else:
            route = engine_select.Route(
                claims=_claims_rev, parse=lambda argv: argv if _claims_rev(argv) else None,
                default=lambda: "python", env_route=lambda argv, call: "rev-gate",
                write=engine_select.WriteFlow(
                    before=lambda call: None, commit=_rev_commit,
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
