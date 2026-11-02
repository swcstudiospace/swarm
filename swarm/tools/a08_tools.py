"""Lane tools for A08 (Test Engineer): deterministic quality-gate arithmetic and classification.

Mission: decide flake quarantine, criteria coverage, mutation and escape thresholds, suite
runtime budget, skip and rerun budgets, and test-name conventions from data A08 already holds.

Tools: qa_flaky_classify, qa_criteria_coverage, qa_mutation_gate, qa_runtime_p95,
qa_skip_ratio, qa_defect_escape_rate, qa_retry_budget, qa_name_convention.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Hashable, Iterable
from typing import Any

from swarm.tools.support import respond

FLAKE_WINDOW = 10
FLAKE_QUARANTINE_FAILS = 3
MUTATION_THRESHOLD_PCT = 70.0
RUNTIME_BUDGET_S = 2700.0
DETECTION_MIN_PCT = 90.0
ESCAPE_MAX_PCT = 5.0
NAME_MAX_LENGTH = 80
_SNAKE_CASE = re.compile(r"[a-z0-9_]+")


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _first_duplicate(values: Iterable[Hashable]) -> Hashable | None:
    seen: set[Hashable] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 2)


def _flake_state(outcomes: list[str]) -> str:
    """Last FLAKE_WINDOW non-skipped runs. Fails with passes present are flaky, not broken."""
    runs = [outcome for outcome in outcomes if outcome != "skip"][-FLAKE_WINDOW:]
    if not runs:
        return "unobserved"
    fails = runs.count("fail")
    if fails == 0:
        return "stable"
    if "pass" not in runs:
        return "consistent_fail"
    return "quarantine" if fails >= FLAKE_QUARANTINE_FAILS else "watch"


def flaky_classify(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        histories = payload["histories"]
        duplicate = _first_duplicate(item["test"] for item in histories)
        if duplicate is not None:
            return _invalid("histories", f"duplicate test {duplicate}")
        buckets: dict[str, list[str]] = {
            "quarantine": [],
            "watch": [],
            "consistent_fail": [],
            "unobserved": [],
            "stable": [],
        }
        for item in histories:
            buckets[_flake_state(item["outcomes"])].append(item["test"])
        return {
            "state": "SUCCESS",
            "quarantine": buckets["quarantine"],
            "watch": buckets["watch"],
            "consistent_fail": buckets["consistent_fail"],
            "unobserved": buckets["unobserved"],
            "stable_count": len(buckets["stable"]),
            "evaluated": len(histories),
        }

    return respond(payload, work)


def criteria_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        criteria = payload["criteria"]
        tests = payload["tests"]
        duplicate = _first_duplicate(criteria)
        if duplicate is not None:
            return _invalid("criteria", f"duplicate criterion {duplicate}")
        duplicate = _first_duplicate(test["name"] for test in tests)
        if duplicate is not None:
            return _invalid("tests", f"duplicate test {duplicate}")
        known = set(criteria)
        covered = {ref for test in tests for ref in test["ac_refs"] if ref in known}
        return {
            "state": "SUCCESS",
            "criteria_count": len(criteria),
            "covered_count": len(covered),
            "coverage_pct": _pct(len(covered), len(criteria)),
            "uncovered": [criterion for criterion in criteria if criterion not in covered],
            "orphan_tests": [test["name"] for test in tests if not known.intersection(test["ac_refs"])],
        }

    return respond(payload, work)


def mutation_gate(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        killed = payload["killed"]
        survived = payload["survived"]
        timeout = payload.get("timeout", 0)
        scored = killed + timeout + survived
        if scored == 0:
            return _invalid("killed", "no scored mutants")
        threshold = payload.get("threshold_pct", MUTATION_THRESHOLD_PCT)
        # Timeouts count as detected (Stryker convention); uncovered mutants are reported as survived.
        # Floor (never round up) to 2 places in integer arithmetic, then compare that same reported value:
        # a gate must fail closed, so 69.996 % reports 69.99 and does not meet a 70 % threshold.
        score_pct = (10000 * (killed + timeout) // scored) / 100
        return {
            "state": "SUCCESS",
            "scored_mutants": scored,
            "score_pct": score_pct,
            "threshold_pct": float(threshold),
            "meets_threshold": score_pct >= threshold,
        }

    return respond(payload, work)


def runtime_p95(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        durations = sorted(payload["durations_s"])
        count = len(durations)
        rank = (95 * count + 99) // 100  # nearest-rank: ceil(0.95 * count), integer arithmetic
        p95 = durations[rank - 1]
        budget = payload.get("budget_s", RUNTIME_BUDGET_S)
        return {
            "state": "SUCCESS",
            "sample_count": count,
            "p95_s": float(p95),
            "mean_s": round(sum(durations) / count, 3),
            "max_s": float(durations[-1]),
            "budget_s": float(budget),
            "within_budget": p95 <= budget,
        }

    return respond(payload, work)


def skip_ratio(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        tests = payload["tests"]
        duplicate = _first_duplicate(test["name"] for test in tests)
        if duplicate is not None:
            return _invalid("tests", f"duplicate test {duplicate}")
        skipped = [test for test in tests if test["status"] == "skipped"]
        return {
            "state": "SUCCESS",
            "total": len(tests),
            "skipped": len(skipped),
            "skip_pct": _pct(len(skipped), len(tests)),
            "unexplained": [test["name"] for test in skipped if not test.get("reason", "").strip()],
            "within_limit": 100.0 * len(skipped) / len(tests) <= payload["max_skip_pct"],
        }

    return respond(payload, work)


def defect_escape_rate(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        defects = payload["defects"]
        duplicate = _first_duplicate(defect["id"] for defect in defects)
        if duplicate is not None:
            return _invalid("defects", f"duplicate defect {duplicate}")
        escaped = [defect["id"] for defect in defects if defect["stage"] == "production"]
        total = len(defects)
        pre_prod = total - len(escaped)
        detection = 100.0 * pre_prod / total
        escape = 100.0 * len(escaped) / total
        return {
            "state": "SUCCESS",
            "total": total,
            "pre_prod": pre_prod,
            "escaped": len(escaped),
            "detection_pct": round(detection, 2),
            "escape_pct": round(escape, 2),
            "escaped_ids": escaped,
            "meets_detection": detection >= payload.get("detection_min_pct", DETECTION_MIN_PCT),
            "meets_escape": escape < payload.get("escape_max_pct", ESCAPE_MAX_PCT),
        }

    return respond(payload, work)


def retry_budget(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        runs = payload["runs"]
        duplicate = _first_duplicate(run["test"] for run in runs)
        if duplicate is not None:
            return _invalid("runs", f"duplicate test {duplicate}")
        limit = payload["max_reruns"]
        return {
            "state": "SUCCESS",
            "evaluated": len(runs),
            "total_reruns": sum(len(run["attempts"]) - 1 for run in runs),
            "over_budget": [run["test"] for run in runs if len(run["attempts"]) - 1 > limit],
            "rescued": [run["test"] for run in runs if run["attempts"][0] == "fail" and "pass" in run["attempts"]],
            "unresolved": [run["test"] for run in runs if "pass" not in run["attempts"]],
        }

    return respond(payload, work)


def name_convention(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        prefix = payload["prefix"]
        limit = payload.get("max_length", NAME_MAX_LENGTH)
        violations: list[str] = []
        seen: set[str] = set()
        for name in payload["names"]:
            if name in seen:
                violations.append(f"{name}: duplicate name")
            seen.add(name)
            if not name.startswith(prefix):
                violations.append(f"{name}: missing prefix {prefix}")
            if _SNAKE_CASE.fullmatch(name) is None:
                violations.append(f"{name}: not snake_case")
            if len(name) > limit:
                violations.append(f"{name}: longer than {limit}")
        return {
            "state": "SUCCESS",
            "checked": len(payload["names"]),
            "violations": violations,
            "conforming": not violations,
        }

    return respond(payload, work)


# ---- v2.2 additions: tier plan, finding shape, defect dedupe -------------------------------------

TIER_ORDER = ("unit", "integration", "e2e", "perf")
# Mirrors scripts/qa_gate.py TIERS_BY_RISK (low unit; medium + integration; high + e2e, perf).
REQUIRED_TIERS = {
    "low": ("unit",),
    "medium": ("unit", "integration"),
    "high": ("unit", "integration", "e2e", "perf"),
}
SMOKE_TIERS = ("unit",)
FINDING_KINDS = ("functional", "perf", "flake", "coverage")
FINDING_SEVERITIES = ("minor", "major", "critical")
BLOCKING_SEVERITIES = ("major", "critical")
_FINDING_ID = re.compile(r"QF-[0-9]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DERIVED_KEY_HEX = 16


def _run_status(runs: dict[str, str], tier: str) -> str:
    return runs.get(tier) or "skipped:not-selected"


def tier_plan(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        risk = payload["risk_class"]
        required_by_risk = REQUIRED_TIERS.get(risk)
        if required_by_risk is None:
            return _invalid("risk_class", f"unknown risk class {risk}")
        runs = payload["runs"]
        unknown = [tier for tier in runs if tier not in TIER_ORDER]
        if unknown:
            return _invalid("runs", f"unknown tier {unknown[0]}")
        degraded = bool(payload.get("degraded", False))
        required = list(SMOKE_TIERS if degraded else required_by_risk)
        status = {tier: _run_status(runs, tier) for tier in TIER_ORDER}
        failed = [tier for tier in TIER_ORDER if status[tier] == "fail"]
        missing = [tier for tier in required if status[tier].startswith("skipped")]
        optional = [tier for tier in TIER_ORDER if tier not in required and status[tier].startswith("skipped")]
        return {
            "state": "SUCCESS",
            "mode": "smoke" if degraded else "full",
            "required": required,
            "missing_required": missing,
            "failed": failed,
            "skipped_optional": optional,
            "verdict": "fail" if failed or missing else "pass",
        }

    return respond(payload, work)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def finding_shape_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        findings = payload["findings"]
        violations: list[dict[str, Any]] = []
        seen: set[str] = set()

        def flag(index: int, fid: str, field: str, message: str) -> None:
            violations.append({"index": index, "id": fid, "field": field, "message": message})

        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                flag(index, "", "finding", "finding must be an object")
                continue
            fid = _text(finding.get("id"))
            if not _FINDING_ID.fullmatch(fid):
                flag(index, fid, "id", "id must look like QF-<number>")
            elif fid in seen:
                flag(index, fid, "id", f"duplicate id {fid}")
            else:
                seen.add(fid)
            severity = finding.get("severity")
            if severity not in FINDING_SEVERITIES:
                flag(index, fid, "severity", "severity must be one of " + ", ".join(FINDING_SEVERITIES))
            kind = finding.get("kind")
            if kind not in FINDING_KINDS:
                flag(index, fid, "kind", "kind must be one of " + ", ".join(FINDING_KINDS))
            if kind == "functional" and not _text(finding.get("ac_ref")):
                flag(index, fid, "ac_ref", "a functional finding needs an ac_ref")
            if severity in BLOCKING_SEVERITIES and not _text(finding.get("evidence")):
                flag(index, fid, "evidence", f"a {severity} finding needs evidence")
        return {
            "state": "SUCCESS",
            "checked": len(findings),
            "violations": violations,
            "conforming": not violations,
        }

    return respond(payload, work)


def _normalize_title(title: str) -> str:
    return _NON_ALNUM.sub(" ", title.lower()).strip()


def _derived_key(component: str, ac_ref: str, title: str) -> str:
    material = "\n".join((component.strip().lower(), ac_ref.strip().upper(), _normalize_title(title)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DERIVED_KEY_HEX]


def defect_dedupe(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        defects = payload["defects"]
        duplicate = _first_duplicate(defect["defect_id"] for defect in defects)
        if duplicate is not None:
            return _invalid("defects", f"duplicate defect_id {duplicate}")
        groups: dict[str, list[dict[str, Any]]] = {}
        for index, defect in enumerate(defects):
            if not _normalize_title(defect["title"]):
                return _invalid("defects", f"defect {index} has no title text")
            given = defect.get("dedupe_key")
            if given is not None and not given.strip():
                return _invalid("defects", f"defect {index} has a blank dedupe_key")
            key = given.strip() if given is not None else _derived_key(
                defect["suspected_component"], defect["ac_ref"], defect["title"]
            )
            groups.setdefault(key, []).append(defect)
        rank = {name: position for position, name in enumerate(FINDING_SEVERITIES)}
        out_groups = []
        duplicate_ids: list[str] = []
        for key, members in groups.items():
            canonical = max(members, key=lambda item: rank[item["severity"]])  # ties keep the first reported
            duplicate_ids.extend(item["defect_id"] for item in members if item is not canonical)
            out_groups.append({
                "key": key,
                "canonical": canonical["defect_id"],
                "members": [item["defect_id"] for item in members],
                "max_severity": canonical["severity"],
            })
        return {
            "state": "SUCCESS",
            "groups": out_groups,
            "duplicate_ids": duplicate_ids,
            "unique_count": len(out_groups),
        }

    return respond(payload, work)
