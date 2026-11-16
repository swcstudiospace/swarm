"""A09 review-gate tools: pure checks over data the reviewer already holds.

Mission: produce measurable review evidence (restricted paths, auto-approve
thresholds, latency, severities, escape rate, comment density, test pairing,
open threads) for the signed review verdict. Tools:
  rev_sensitive_paths      restricted-path categories and co-sign need for changed paths
  rev_auto_approve_check   L2 auto-approve thresholds from decision_logic
  rev_review_latency_p95   nearest-rank P95 of review durations against target
  rev_severity_histogram   severity counts, blocking count and highest severity
  rev_false_approve_rate   defects later found in approved code, against target
  rev_comment_density      comment-line ratio of added source lines
  rev_test_pairing         test-to-source ratio and source files without a test
  rev_unresolved_threads   open review threads, including open blocking threads
  rev_risk_tier            risk_tier low/medium/high from categories, diff size, blocking count and top severity
  rev_verdict_shape        validate the final review json block and normalize its verdict to pass or fail
  rev_rule_dispute_flag    rules one producer disputed on at least `threshold` pull requests
"""
from __future__ import annotations

import os
import re
from typing import Any

from swarm.tools.support import respond

_SEVERITIES = ("info", "minor", "major", "critical", "blocker")
_BLOCKING_FROM = _SEVERITIES.index("major")  # same threshold as swarm.gates.BLOCKING_SEVERITY

# Restricted-path categories, matched on whole word tokens of the path.
_CATEGORY_WORDS = {
    "contracts": {"openapi", "asyncapi", "swagger", "proto", "graphql", "contract", "contracts"},
    "auth": {"auth", "authn", "authz", "oauth", "oidc", "login", "logout", "session", "sessions", "jwt",
             "rbac", "acl", "permission", "permissions", "password", "passwords", "credential",
             "credentials", "sso", "saml", "mfa"},
    "payments": {"payment", "payments", "billing", "invoice", "invoices", "checkout", "stripe", "ledger",
                 "refund", "refunds"},
    "migrations": {"migration", "migrations", "migrate", "alembic", "flyway", "liquibase"},
    "pii": {"pii", "gdpr", "privacy", "ssn"},
    "iac": {"terraform", "tf", "tfvars", "helm", "k8s", "kubernetes", "ansible", "pulumi", "cloudformation",
            "dockerfile"},
}
_CO_SIGN_CATEGORIES = frozenset({"auth", "payments", "pii", "iac"})  # high-risk: A10 co-sign required
_TOKEN = re.compile(r"[a-z0-9]+")

_CODE_EXTENSIONS = frozenset({".py", ".go", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt",
                              ".rb", ".rs", ".cs", ".php", ".swift", ".scala", ".c", ".h", ".cc", ".cpp",
                              ".hpp", ".ex", ".exs"})
_TEST_DIRS = frozenset({"test", "tests", "__tests__", "spec", "specs"})
_TEST_SUFFIXES = ("_test", ".test", "_spec", ".spec")
# CamelCase xUnit/JVM/.NET names (FooTest, FooTests, FooSpec, TestFoo); matched on the original-case stem so
# lookalikes such as latest/contest/Attestation stay sources.
_CAMEL_TEST_SUFFIX = re.compile(r"^(?P<base>.*[a-z0-9])(?:Tests?|Spec)$")
_CAMEL_TEST_PREFIX = re.compile(r"^Tests?(?P<base>[A-Z].*)$")

_COMMENT_PREFIXES = ("#", "//", "/*", "*/", "* ", "--", "<!--")
_DIRECTIVES = ("#include", "#define", "#if", "#elif", "#else", "#endif", "#pragma", "#undef", "#error",
               "#import", "#!")


def _bad(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _categories(path: str) -> set[str]:
    tokens = set(_TOKEN.findall(path.replace("\\", "/").lower()))
    return {name for name, words in _CATEGORY_WORDS.items() if tokens & words}


def sensitive_paths(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        flagged: list[str] = []
        categories: set[str] = set()
        for path in dict.fromkeys(payload["paths"]):
            hits = _categories(path)
            if hits:
                flagged.append(path)
                categories |= hits
        return {
            "state": "SUCCESS",
            "sensitive_count": len(flagged),
            "categories": sorted(categories),
            "flagged_paths": flagged,
            "co_sign_required": bool(categories & _CO_SIGN_CATEGORIES),
        }

    return respond(payload, work)


def auto_approve_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        diff_lines = payload["added"] + payload["deleted"]
        failed: list[str] = []
        if diff_lines >= 100:
            failed.append("diff_size")
        # Any restricted category (contracts, auth, payments, migrations, PII, IaC) blocks auto-approve.
        if any(_categories(path) for path in payload["changed_paths"]):
            failed.append("restricted_paths")
        if payload["sast_findings"] != 0:
            failed.append("sast")
        if not payload["tests_present"]:
            failed.append("tests")
        if payload["first_pass_pct"] <= 90:
            failed.append("first_pass_rate")
        auto = not failed
        return {
            "state": "SUCCESS",
            "auto_approve": auto,
            "route": "auto_approve" if auto else "full_semantic_review",
            "failed_criteria": failed,
            "diff_lines": diff_lines,
        }

    return respond(payload, work)


def review_latency_p95(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        ordered = sorted(payload["durations_min"])
        rank = (95 * len(ordered) + 99) // 100  # nearest-rank: ceil(0.95 * n), 1-based
        p95 = float(ordered[rank - 1])
        return {
            "state": "SUCCESS",
            "sample_count": len(ordered),
            "p95_min": round(p95, 2),
            "meets_target": p95 < payload["target_min"],
        }

    return respond(payload, work)


def severity_histogram(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        counts = {name: 0 for name in _SEVERITIES}
        for severity in payload["severities"]:
            counts[severity] += 1
        present = [name for name in _SEVERITIES if counts[name]]
        return {
            "state": "SUCCESS",
            "total": len(payload["severities"]),
            **counts,
            "blocking_count": sum(counts[name] for name in _SEVERITIES[_BLOCKING_FROM:]),
            "max_severity": present[-1] if present else "none",
        }

    return respond(payload, work)


def false_approve_rate(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        approved = payload["approved_count"]
        defects = payload["defects_found_later"]
        if defects > approved:
            return _bad("defects_found_later", "cannot exceed approved_count")
        raw_rate = defects * 100 / approved
        return {
            "state": "SUCCESS",
            "rate_pct": round(raw_rate, 2),
            "within_target": raw_rate < payload["target_pct"],
        }

    return respond(payload, work)


def _is_comment(line: str) -> bool:
    if line.startswith(_DIRECTIVES):
        return False
    return line.startswith(_COMMENT_PREFIXES) or line == "*"


def comment_density(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        nonblank = 0
        comments = 0
        for raw in payload["added_lines"]:
            line = raw.strip()
            if not line:
                continue
            nonblank += 1
            if _is_comment(line):
                comments += 1
        density = comments / nonblank if nonblank else 0.0
        return {
            "state": "SUCCESS",
            "nonblank_lines": nonblank,
            "comment_lines": comments,
            "density": round(density, 4),
        }

    return respond(payload, work)


def _split(path: str) -> tuple[list[str], str, str]:
    *dirs, base = path.replace("\\", "/").split("/")
    stem, ext = os.path.splitext(base)
    return [part.lower() for part in dirs], stem, ext.lower()


def _test_base(dirs: list[str], stem: str) -> str | None:
    """Lowercased source stem a test file covers, or None when the file is not a test.

    `stem` keeps its original case so CamelCase test names can be recognised.
    """
    lowered = stem.lower()
    for suffix in _TEST_SUFFIXES:
        if lowered.endswith(suffix):
            return lowered[: -len(suffix)]
    if lowered.startswith("test_"):
        return lowered[len("test_"):]
    for pattern in (_CAMEL_TEST_SUFFIX, _CAMEL_TEST_PREFIX):
        match = pattern.match(stem)
        if match:
            return match["base"].lower()
    if any(part in _TEST_DIRS for part in dirs):
        return lowered
    return None


def test_pairing(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        sources: list[tuple[str, str]] = []
        covered: set[str] = set()
        test_count = 0
        for path in dict.fromkeys(payload["changed_paths"]):
            dirs, stem, ext = _split(path)
            if ext not in _CODE_EXTENSIONS:
                continue
            base = _test_base(dirs, stem)
            if base is None:
                sources.append((path, stem.lower()))
            else:
                test_count += 1
                covered.add(base)
        untested = [path for path, stem in sources if stem not in covered]
        ratio = test_count / len(sources) if sources else 0.0
        return {
            "state": "SUCCESS",
            "source_files": len(sources),
            "test_files": test_count,
            "test_to_source_ratio": round(ratio, 4),
            "untested_sources": untested,
            "all_sources_paired": not untested,
        }

    return respond(payload, work)


def unresolved_threads(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        threads = payload["threads"]
        ids = [thread["id"] for thread in threads]
        if len(set(ids)) != len(ids):
            return _bad("threads", "thread ids must be unique")
        open_threads = [thread for thread in threads if not thread["resolved"]]
        open_blocking = [thread for thread in open_threads if thread["blocking"]]
        return {
            "state": "SUCCESS",
            "total_threads": len(threads),
            "unresolved_count": len(open_threads),
            "unresolved_blocking_count": len(open_blocking),
            "unresolved_ids": [thread["id"] for thread in open_threads],
            "blocking_clear": not open_blocking,
        }

    return respond(payload, work)


# ---- v2.2 additions: risk tier, verdict block shape, producer rule disputes --------------------------------------
_RISK_SEVERITIES = ("none", *_SEVERITIES)
_RISK_TIERS = ("low", "medium", "high")
_MEDIUM_DIFF_LINES = 100  # same diff-size threshold as auto_approve_check (decision_logic: diff < 100 lines)
_MODES = ("rules+semantic", "rules-only")
_MAX_EXPIRES_S = 172800  # 48 h verdict expiry (output_format expires_s)
_VERDICT_ALIASES = {
    "pass": "pass", "approve": "pass",
    "fail": "fail", "request_changes": "fail", "block": "fail",
}
_MAX_BLOCK_ITEMS = 2000
_DEFAULT_DISPUTE_THRESHOLD = 2


def risk_tier(payload: dict[str, Any]) -> dict[str, Any]:
    """Map restricted categories, diff size and top severity to risk_tier low/medium/high.

    high   co_sign_required, or max_severity critical or blocker
    medium any restricted category, diff_lines >= 100 (the auto_approve_check size threshold), or blocking_count > 0
    low    none of the above
    """

    def work() -> dict[str, Any]:
        categories = list(dict.fromkeys(payload["categories"]))
        unknown = [name for name in categories if name not in _CATEGORY_WORDS]
        if unknown:
            return _bad("categories", f"unknown category {unknown[0]!r}; expected one of {sorted(_CATEGORY_WORDS)}")
        severity = payload["max_severity"]
        blocking_count = payload["blocking_count"]
        blocking_severity = _RISK_SEVERITIES.index(severity) >= _RISK_SEVERITIES.index("major")
        if blocking_severity and blocking_count == 0:
            return _bad("blocking_count", f"max_severity {severity} is blocking, so blocking_count must be at least 1")
        if not blocking_severity and blocking_count > 0:
            return _bad("max_severity", f"blocking_count {blocking_count} needs max_severity major or higher")
        needs_cosign = sorted(set(categories) & _CO_SIGN_CATEGORIES)
        if needs_cosign and not payload["co_sign_required"]:
            return _bad("co_sign_required", f"category {needs_cosign[0]} requires co_sign_required true")

        high: list[str] = []
        if payload["co_sign_required"]:
            high.append("co_sign_required")
        if severity in ("critical", "blocker"):
            high.append(f"max_severity:{severity}")
        medium = [f"category:{name}" for name in categories]
        if payload["diff_lines"] >= _MEDIUM_DIFF_LINES:
            medium.append(f"diff_lines:{payload['diff_lines']}>={_MEDIUM_DIFF_LINES}")
        if blocking_count > 0:
            medium.append(f"blocking_count:{blocking_count}>0")
        tier = "high" if high else "medium" if medium else "low"
        return {"state": "SUCCESS", "risk_tier": tier, "reasons": [*high, *medium] or ["no_risk_trigger"]}

    return respond(payload, work)


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _line_ok(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _verdict_block_errors(block: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """(errors, normalized verdict or "none") for the final review json block (prompt output_format)."""
    errors: list[dict[str, Any]] = []

    def err(field: str, reason: str) -> None:
        errors.append({"field": field, "reason": reason})

    def need(field: str, ok: bool, expected: str) -> None:
        if field not in block:
            err(field, "missing")
        elif not ok:
            err(field, f"expected {expected}")

    need("gate", block.get("gate") == "review", "'review'")
    need("task_id", _nonblank(block.get("task_id")), "a non-empty string")
    need("correlation_id", _nonblank(block.get("correlation_id")), "a non-empty string")
    if "pr" in block and not _nonblank(block["pr"]):
        err("pr", "expected a non-empty string")

    verdict = block.get("verdict")
    normalized = _VERDICT_ALIASES.get(verdict, "none") if isinstance(verdict, str) else "none"
    need("verdict", normalized != "none", f"one of {', '.join(_VERDICT_ALIASES)}")

    blocking = block.get("blocking")
    need("blocking", isinstance(blocking, list), "a list")
    if isinstance(blocking, list):
        for index, item in enumerate(blocking):
            where = f"blocking[{index}]"
            if not isinstance(item, dict):
                err(where, "expected an object")
                continue
            for key in ("file", "rule", "md"):
                if key not in item:
                    err(f"{where}.{key}", "missing")
                elif not _nonblank(item[key]):
                    err(f"{where}.{key}", "expected a non-empty string")
            if "line" not in item:
                err(f"{where}.line", "missing")
            elif not _line_ok(item["line"]):
                err(f"{where}.line", "expected an integer of at least 1")
    non_blocking = block.get("non_blocking")
    need("non_blocking", isinstance(non_blocking, list), "a list")
    if isinstance(non_blocking, list):
        for index, item in enumerate(non_blocking):
            where = f"non_blocking[{index}]"
            if not isinstance(item, dict):
                err(where, "expected an object")
                continue
            for key in ("rule", "suggestion_md"):
                if key not in item:
                    err(f"{where}.{key}", "missing")
                elif not _nonblank(item[key]):
                    err(f"{where}.{key}", "expected a non-empty string")

    need("risk_tier", block.get("risk_tier") in _RISK_TIERS, f"one of {', '.join(_RISK_TIERS)}")
    need("mode", block.get("mode") in _MODES, f"one of {', '.join(_MODES)}")
    need("co_sign_required", isinstance(block.get("co_sign_required"), bool), "a boolean")
    expires = block.get("expires_s")
    need("expires_s", isinstance(expires, int) and not isinstance(expires, bool) and 1 <= expires <= _MAX_EXPIRES_S,
         f"an integer from 1 to {_MAX_EXPIRES_S}")

    # verdict ladder: pass means no blocking finding; a fail carries the finding that explains it (fail closed)
    if isinstance(blocking, list) and normalized == "pass" and blocking:
        err("verdict", "pass cannot carry blocking items")
    if isinstance(blocking, list) and normalized == "fail" and not blocking:
        err("blocking", "a fail verdict needs at least one blocking item")
    return errors, normalized


def verdict_shape(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        block = payload["block"]
        for key in ("blocking", "non_blocking"):
            if isinstance(block.get(key), list) and len(block[key]) > _MAX_BLOCK_ITEMS:
                return _bad(f"block.{key}", f"more than {_MAX_BLOCK_ITEMS} items")
        errors, normalized = _verdict_block_errors(block)
        return {"state": "SUCCESS", "valid": not errors, "errors": errors, "normalized_verdict": normalized}

    return respond(payload, work)


def rule_dispute_flag(payload: dict[str, Any]) -> dict[str, Any]:
    """Flag rules that one producer disputed on at least `threshold` distinct pull requests (default 2).

    Counts are per (rule, producer): a rule disputed once each by two producers is not merged into one count, and a
    repeated (rule, pr, producer) dispute is counted once.
    """

    def work() -> dict[str, Any]:
        threshold = payload.get("threshold", _DEFAULT_DISPUTE_THRESHOLD)
        seen: set[tuple[str, str, str]] = set()
        for index, item in enumerate(payload["disputes"]):
            for key in ("rule", "pr", "producer"):
                if not item[key].strip():
                    return _bad("disputes", f"disputes[{index}].{key} must not be blank")
            seen.add((item["rule"], item["pr"], item["producer"]))
        per_pair: dict[tuple[str, str], list[str]] = {}
        for rule, pr, producer in sorted(seen):
            per_pair.setdefault((rule, producer), []).append(pr)
        flagged = [
            {"rule": rule, "producer": producer, "dispute_count": len(prs), "prs": prs}
            for (rule, producer), prs in sorted(per_pair.items())
            if len(prs) >= threshold
        ]
        return {
            "state": "SUCCESS",
            "flagged_rules": flagged,
            "flagged_count": len(flagged),
            "max_disputes": max((len(prs) for prs in per_pair.values()), default=0),
        }

    return respond(payload, work)
