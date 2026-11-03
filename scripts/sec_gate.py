#!/usr/bin/env python3
"""A10 — secrets / dependency / SAST / IaC scan and signed security gate verdict.

Runs four scanners over the repository at --root: (1) secrets regexes (AWS, GitHub,
Slack, Stripe, Google, private keys, JWTs, generic api_key= with placeholder and
entropy filters), (2) dependency audit via pip-audit / npm audit / cargo audit /
govulncheck when installed (else `skipped:tool-missing`), (3) dangerous-pattern SAST
greps (eval/exec, shell=True + f-string, pickle, yaml.load, innerHTML, SQL string
building), (4) IaC checks on *.tf / *.yaml (0.0.0.0/0 ingress, privileged, hostNetwork).
Finding ids are stable hashes so --allow-list can suppress them with a justification.
Writes a signed `gate.verdict` (gate=security) to .swarm/verdicts/<task_id>.security.json.
"""
from __future__ import annotations
import codecs
from collections import Counter
import hashlib
import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.script_base import AgentScript, sh, which, iter_files  # noqa: E402
from swarm.gates import make_finding  # noqa: E402
from swarm.verdicts import check_per_target_keys, gate_verdict, issue_gate, load_per_target  # noqa: E402
from swarm.errors import SwarmError, ErrorCode  # noqa: E402

CODE_EXTS = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".vue", ".svelte"}
IAC_EXTS = {".tf", ".yaml", ".yml"}
SECRET_RULES = [(n, re.compile(p)) for n, p in (
    ("aws-access-key", r"\bAKIA[0-9A-Z]{16}\b"), ("github-token", r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    ("slack-token", r"\bxox[baprs]-[0-9A-Za-z-]{10,}"), ("stripe-live-key", r"\bsk_live_[0-9a-zA-Z]{24,}"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}\b"), ("private-key", r"-----BEGIN (RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY( BLOCK)?-----"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    ("aws-secret-key", r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})"),
    ("generic-credential", r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd)\b\s*[:=]\s*['\"]([^'\"\s]{16,})['\"]"))]
PLACEHOLDER_RE = re.compile(r"(?i)example|placeholder|xxx|changeme|dummy|redacted|<[^>]*>|\$\{|\{\{|your[_-]|\.\.\.|…|\*{3}|sample|fake")
SAST_RULES = [(n, re.compile(p), exts, sev) for n, p, exts, sev in (
    ("py.eval-exec", r"\b(eval|exec)\(\s*[^'\")\s]", {".py"}, "major"),
    ("py.shell-injection", r"(subprocess\.\w+|os\.system|os\.popen)\(.*(f['\"]|%\s*\(|\.format\(|\+\s*\w).*shell\s*=\s*True|(os\.system|os\.popen)\(\s*(f['\"]|\w+\s*\+)", {".py"}, "major"),
    ("py.pickle-load", r"\bpickle\.loads?\(", {".py"}, "major"),
    ("py.yaml-unsafe-load", r"\byaml\.load\((?![^)]*Loader\s*=)", {".py"}, "major"),
    ("js.eval", r"\beval\(\s*[^'\")\s]|new Function\(", {".js", ".ts", ".tsx", ".jsx", ".vue", ".svelte"}, "major"),
    ("js.inner-html", r"\.innerHTML\s*=|dangerouslySetInnerHTML\s*=", {".js", ".ts", ".tsx", ".jsx", ".vue", ".svelte"}, "major"),
    ("sql.string-build", r"(?i)(f['\"][^'\"]*\b(select|insert|update|delete)\b[^'\"]*\{|['\"]\s*\b(select|insert|update|delete)\b[^'\"]*['\"]\s*(\+|%|\|\|)\s*\w|\.(execute|query|raw)\(\s*(f['\"]|`[^`]*\$\{|['\"][^'\"]*['\"]\s*(\+|%)))", CODE_EXTS, "major"))]
IAC_RULES = [(n, re.compile(p), sev) for n, p, sev in (
    ("iac.open-ingress", r"0\.0\.0\.0/0|::/0", "major"), ("iac.privileged", r"privileged\s*[:=]\s*true", "major"),
    ("iac.host-network", r"host(Network|PID|IPC)\s*[:=]\s*true", "major"),
    ("iac.priv-escalation", r"allowPrivilegeEscalation\s*:\s*true|runAsUser\s*:\s*0\b", "minor"))]
COMMENT_RE = re.compile(r"^\s*(#|//|/\*|\*|--)")
MAX_SCAN_BYTES = 2_000_000  # an in-scope text file strictly larger than this is not read; it is scan-incomplete
PREFIX_BYTES = 8192  # bytes read from a file's head to decide it binary before the size check
AUDITS = [  # (name, manifest globs, binary, command)
    ("pip-audit", ("requirements*.txt", "pyproject.toml"), "pip-audit", ["pip-audit", "--format", "json", "--progress-spinner", "off"]),
    ("npm-audit", ("package-lock.json", "package.json"), "npm", ["npm", "audit", "--json"]),
    ("cargo-audit", ("Cargo.lock",), "cargo-audit", ["cargo", "audit", "--json"]),
    ("govulncheck", ("go.mod",), "govulncheck", ["govulncheck", "-json", "./..."])]


def _entropy(s: str) -> float:
    return -sum((c / len(s)) * math.log2(c / len(s)) for c in Counter(s).values())


def fid(*parts: str) -> str:
    return "SEC-" + hashlib.sha1("|".join(parts).encode()).hexdigest()[:8]


def _binary_prefix(path: Path) -> bool:
    """True when the first PREFIX_BYTES of a file are not UTF-8 with an error that is not an incomplete sequence at the
    prefix end, which the prefix may have cut. A prefix that cannot be read proves nothing, so the file is not binary."""
    try:
        with path.open("rb") as fh:
            prefix = fh.read(PREFIX_BYTES)
    except OSError:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(prefix, final=False)
    except UnicodeDecodeError:
        return True
    return False


def scan_files(root: Path, self_path: Path) -> tuple[list[dict], dict]:
    findings, seen = [], {"secrets": 0, "sast": 0, "iac": 0}

    def add(rule, sev, kind, summary, loc, evidence="", cwe=None):
        seen[kind] += 1
        findings.append(make_finding(fid(kind, rule, loc), sev, kind, f"{rule}: {summary}", evidence=evidence,
                                     location=loc, ac_ref=cwe, owner_suggestion="A05"))

    for p in iter_files(root):
        if p.resolve() == self_path:
            continue
        if _binary_prefix(p):
            continue
        rel, ext = p.relative_to(root).as_posix(), p.suffix
        size = p.stat().st_size
        if size > MAX_SCAN_BYTES:
            # An in-scope text file over the cap is not read: it is one major scan-incomplete finding (AGENTS.md doctrine).
            findings.append(make_finding(fid("secrets", "scan-incomplete", rel), "major", "secrets",
                                         f"scan-incomplete: scan incomplete, status is fail until every in-scope input is analysed: {rel} is {size} bytes, over the {MAX_SCAN_BYTES}-byte scan cap",
                                         evidence=f"{size} bytes exceeds the {MAX_SCAN_BYTES}-byte scan cap; not analysed",
                                         location=rel))
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            loc = f"{rel}:{no}"
            for name, rx in SECRET_RULES:
                m = rx.search(line)
                if not m:
                    continue
                val = m.group(m.lastindex) if name in ("generic-credential", "aws-secret-key") else m.group(0)
                # The placeholder test runs on the captured value. A placeholder word elsewhere on the line, such as a
                # comment that says `example`, does not make a real token a placeholder.
                if PLACEHOLDER_RE.search(val):
                    continue
                if name == "generic-credential" and (_entropy(val) < 3.2 or val.isalpha()):
                    continue
                add(name, "critical", "secrets", "credential material committed (value redacted)", loc,
                    evidence=f"value not recorded ({len(val)} chars)", cwe="CWE-798")
            if ext in CODE_EXTS and not COMMENT_RE.match(line):
                for name, rx, exts, sev in SAST_RULES:
                    if ext in exts and rx.search(line):
                        if name == "sql.string-build" and ("?" in line or "%s" in line):
                            sev = "minor"  # parameterised values, dynamic identifiers only
                        add(name, sev, "sast", "dangerous pattern", loc, evidence=line.strip()[:160],
                            cwe={"sql.string-build": "CWE-89", "js.inner-html": "CWE-79", "py.pickle-load": "CWE-502",
                                 "py.yaml-unsafe-load": "CWE-502"}.get(name, "CWE-94"))
            if ext in IAC_EXTS and not COMMENT_RE.match(line):
                for name, rx, sev in IAC_RULES:
                    if rx.search(line):
                        add(name, sev, "iac", "insecure infrastructure setting", loc, evidence=line.strip()[:160], cwe="CWE-284")
    return findings, seen


def dep_audit(root: Path, timeout: int) -> tuple[list[dict], dict]:
    findings, runs = [], {}
    for name, globs, binary, cmd in AUDITS:
        if not any(list(root.glob(g)) for g in globs):
            continue
        if not which(binary):
            runs[name] = "skipped:tool-missing"
            continue
        try:
            proc = sh(cmd, cwd=root, timeout=timeout)
        except Exception as e:  # noqa: BLE001 - timeout etc. ⇒ fail-closed advisory
            runs[name] = "fail"
            findings.append(make_finding(fid("deps", name, "run"), "major", "deps", f"{name} did not complete: {e}"))
            continue
        crit, high, other, kev, out = 0, 0, 0, 0, proc.stdout
        try:
            data = json.loads(out) if out.strip().startswith(("{", "[")) else None
        except json.JSONDecodeError:
            data = None
        if name == "npm-audit" and data:
            v = data.get("metadata", {}).get("vulnerabilities", {})
            crit, high, other = v.get("critical", 0), v.get("high", 0), v.get("moderate", 0) + v.get("low", 0)
        elif name == "pip-audit" and data:
            for d in (data.get("dependencies", data) if isinstance(data, dict) else data):
                for vuln in d.get("vulns", []):
                    high += 1
                    kev += int("kev" in json.dumps(vuln).lower() or "exploited" in json.dumps(vuln).lower())
        elif name == "cargo-audit" and data:
            crit = data.get("vulnerabilities", {}).get("count", 0)
        elif name == "govulncheck":
            high = sum(1 for ln in out.splitlines() if '"finding"' in ln and '"trace"' in ln)
        if proc.returncode != 0 and not (crit or high or other):
            # P4-01/P4-02: a non-zero exit that counts no vulnerability did not complete the audit (npm's {"error": ...}
            # JSON, a govulncheck load failure with no finding lines): fail closed, as an unparseable run does.
            high = 1
        runs[name] = "pass" if not (crit or high or other) else "fail"
        if kev or crit:
            findings.append(make_finding(fid("deps", name, "critical"), "blocker" if kev else "critical", "deps",
                                         f"{name}: {crit} critical{' / KEV-listed' if kev else ''} vulnerabilities", evidence=out[-600:]))
        elif high:
            findings.append(make_finding(fid("deps", name, "high"), "major", "deps", f"{name}: {high} high vulnerabilities", evidence=out[-600:]))
        elif other:
            findings.append(make_finding(fid("deps", name, "other"), "minor", "deps", f"{name}: {other} moderate/low advisories", evidence=out[-600:]))
    return findings, runs


def apply_allow_list(findings: list[dict], path: str | None) -> tuple[list[dict], list[dict]]:
    if not path:
        return findings, []
    raw = json.loads(Path(path).read_text())
    entries = raw.get("allow", raw) if isinstance(raw, dict) else raw
    allow = {}
    for e in entries:
        e = {"id": e} if isinstance(e, str) else e
        if not e.get("justification"):
            raise SwarmError(ErrorCode.E_POLICY, f"allow-list entry {e.get('id')} lacks a justification")
        if e.get("expires") and time.strptime(e["expires"], "%Y-%m-%d") < time.gmtime():
            continue
        allow[e["id"]] = e["justification"]
    kept, suppressed = [], []
    for f in findings:
        (suppressed if f["id"] in allow else kept).append({**f, "justification": allow[f["id"]]} if f["id"] in allow else f)
    return kept, suppressed


def run(args, ctx) -> dict:
    if ctx.dry_run:
        runs = {"secrets": "pass", "sast": "pass", "iac": "skipped:no-iac", "pip-audit": "skipped:dry-run"}
        env, recorded = issue_gate(ctx, gate="security", agent_id="A10@dry", findings=[], runs=runs, simulate=True,
                                   extra={"scan_digest": "sha256:" + "0" * 64, "suppressed": []})
        return {"status": "ok", "verdict": "pass", "findings": [], "runs": runs, "dry_run": True, "envelope": env,
                "recorded": sorted(recorded),
                "summary": "dry-run: canned pass verdict"}
    findings, counts = scan_files(ctx.root, Path(__file__).resolve())
    dep_findings, runs = dep_audit(ctx.root, args.timeout)
    findings += dep_findings
    if args.strict:
        for name, state in runs.items():
            if state.startswith("skipped"):
                findings.append(make_finding(fid("deps", name, "missing"), "major", "deps", f"{name} unavailable ({state}); fail-closed under --strict"))
    findings, suppressed = apply_allow_list(findings, args.allow_list)
    blocking = {"major", "critical", "blocker"}
    for kind in ("secrets", "sast", "iac"):
        hits = [f for f in findings if f["kind"] == kind and f["severity"] in blocking]
        runs[kind] = "fail" if hits else ("pass" if counts[kind] or kind != "iac" else "skipped:no-iac")
    digest = "sha256:" + hashlib.sha256(json.dumps(sorted(f["id"] for f in findings + suppressed)).encode()).hexdigest()
    # D-13: the agent's per-target findings join the script's own findings on their target only; a target the
    # agent omits gets the script's findings alone
    own = load_per_target(args.per_target_findings, len(findings), prefix="SF", owner="A10")
    check_per_target_keys(own, ctx.task_id, ctx.root)
    per_target = {t: findings + items for t, items in own.items()} if own else None
    env, recorded = issue_gate(ctx, gate="security", agent_id="A10@local", findings=findings, runs=runs,
                               per_target=per_target, extra={"scan_digest": digest, "suppressed": suppressed})
    findings = findings + [f for items in own.values() for f in items]
    verdict = gate_verdict(env, recorded)
    return {"recorded": sorted(recorded), "status": "ok" if verdict == "pass" else "fail", "verdict": verdict, "findings": findings, "runs": runs,
            "suppressed": suppressed, "scan_digest": digest, "envelope": env,
            "summary": f"security gate {verdict.upper()} — {len(findings)} findings, {len(suppressed)} suppressed; runs: {runs}"}


def add_args(p):
    p.add_argument("--allow-list", help="JSON file: [{id, justification, expires?}] of finding ids to suppress")
    p.add_argument("--strict", action="store_true", help="fail-closed: missing dependency-audit tools become major findings")
    p.add_argument("--timeout", type=int, default=600, help="per-audit-tool timeout in seconds")
    p.add_argument("--per-target-findings",
                   help="JSON object {target task id: [findings]}: the agent's findings; each gate target's verdict "
                        "gets the scan's findings plus only its own (targets it omits get the script's findings only)")


# Engine selector (swarm/engine_select.py). The default row stays Python. `GSD_ENGINE=rust` serves the grammar `gates::sec_gate`
# accepts (`--json`, not `--dry-run`) and leaves every other argv to this body. `ENGINE_ROUTES` is the seam a route registers in.
# The route writes (the binary records the security gate's verdict in issue_gate's transaction), so it is a write route: Python
# answers only a binary reply that says nothing was written. A route that commits a verdict reports a committed outcome
# as final, so Python is not re-run.
def _binary_env(route: str) -> dict[str, str]:
    from swarm.engine_select import BINARY_ENV_NAMES
    names = set(BINARY_ENV_NAMES) | {"XDG_CONFIG_HOME", "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE",
                                     "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION"}
    if route != "sec-gate":
        raise KeyError(route)
    return {key: value for key, value in os.environ.items() if key in names}


def _claims_sec(argv: list[str]) -> bool:
    return "--json" in argv and "--dry-run" not in argv and "--help" not in argv and "-h" not in argv


def _sec_commit(call, before, effects_path, reply, stdout):
    # The binary has committed the verdict and its reply is final; the gate routes write no effects manifest to replay.
    return reply, stdout


ENGINE_ROUTES: tuple = ()


if __name__ == "__main__":
    script = AgentScript("A10", "sec_gate", run, description=__doc__, add_args=add_args)
    answer = None
    if os.environ.get("GSD_ENGINE", "python") not in ("python", "") or ENGINE_ROUTES:
        from swarm import engine_select
        if ENGINE_ROUTES:
            answer = engine_select.select_engine(engine_select.Selector(script, ENGINE_ROUTES), sys.argv[1:])
        else:
            route = engine_select.Route(
                claims=_claims_sec, parse=lambda argv: argv if _claims_sec(argv) else None,
                default=lambda: "python", env_route=lambda argv, call: "sec-gate",
                write=engine_select.WriteFlow(
                    before=lambda call: None, commit=_sec_commit,
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
