"""Lane tools for A14 (Maintenance Engineer): patch priority and deadlines, semver
bump classification, EOL and staleness ages, unpinned-spec counts, tech-debt
scoring, license-change review, and changelog validation.

Tools (all pure; dates, versions, and thresholds come from the payload):
  maint_patch_priority   CVSS/KEV/exploit/blast/usage priority score and tier
  maint_patch_deadline   24 h for KEV or critical findings, otherwise the supplied SLA
  maint_semver_bump      major/minor/patch/prerelease/downgrade kind, breaking flag, autonomy
  maint_eol_distance     days to platform EOL, status, upgrade-epic and compliance flags
  maint_unpinned_count   pinned / wildcard / unpinned counts over dependency specifiers
  maint_stale_age        days a dependency release trails the latest release
  maint_debt_priority    impact x recurrence x effort tech-debt score and iteration
  maint_license_change   SPDX identifier diff, allowlist check, copyleft introduction
  maint_changelog_check  Keep a Changelog release entry validation
  maint_hotfix_decision  hotfix / rollback / defer rule for a sev 1-2 incident (sev 1 = worst)
  maint_version_affected is a version inside an advisory range (`<`, `<=`, `>=`, `>`, comma AND)

maint_unpinned_count classifies each specifier with scripts/maint_deps.py `_pin_state` (the
reference, also behind maint_spec_pin): one exact version, bare or `v`-prefixed or `==`/`===`/`=`
prefixed (whitespace after the operator allowed: `== 1.2.3`), counts as pinned; `*`, `latest`, an
empty spec, `x`, `any`, `>=0` and a bare or wildcard-only operator (`==`, `==*`) count as wildcard;
everything else (`1.x`, `1.2.x`, `^1.2`, `>=1,<2`) is a range and counts as unpinned; a wildcard
component (`x`, `X`, `*`) in the numeric core is a range even behind `==` (`== 1.2.*`), a compound
spec is a range even when it starts with an exact pin (`==1.2.3,<2`, `==1.2.3 || ^2`), while build and
prerelease tails (`1.2.3-rc.1`, `1.2.3+build5`) stay pinned.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from typing import Any, cast

from swarm.tools.support import respond, script

_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_UTC_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_PRE_IDENT = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"  # semver 2.0.0 section 9: non-empty, no leading zero when numeric
_BUILD_IDENT = r"[0-9A-Za-z-]+"  # section 10: non-empty
_SEMVER = re.compile(
    rf"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-({_PRE_IDENT}(?:\.{_PRE_IDENT})*))?(?:\+{_BUILD_IDENT}(?:\.{_BUILD_IDENT})*)?"
)
_LICENSE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+-]*")
_COPYLEFT_PREFIXES = ("GPL", "AGPL", "LGPL", "MPL", "EPL", "CDDL", "EUPL")
_CHANGELOG_HEAD = re.compile(r"## \[([^\]]+)\](?: - (\S+))?")
_CHANGELOG_CATEGORIES = frozenset({"Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"})
_EXPLOIT_POINTS = {"none": 0, "poc": 10, "functional": 15, "weaponized": 20}
_BLAST_POINTS = {"isolated": 0, "service": 5, "platform": 10, "internet-facing": 15}
_URGENT_WINDOW_H = 24
_EOL_EPIC_DAYS = 90


def _bad(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _date(text: str) -> dt.date | None:
    if not _ISO_DATE.fullmatch(text):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _utc(text: str) -> dt.datetime | None:
    if not _UTC_STAMP.fullmatch(text):
        return None
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def patch_priority(payload: dict[str, Any]) -> dict[str, Any]:
    """Score = 4*CVSS + exploit points + blast points + 0.15*usage% + 10 if KEV, capped at 100."""

    def work() -> dict[str, Any]:
        kev = payload["kev"]
        cvss = payload["cvss"]
        raw = (
            cvss * 4
            + _EXPLOIT_POINTS[payload["exploit_maturity"]]
            + _BLAST_POINTS[payload["blast_radius"]]
            + payload["usage_pct"] * 0.15
            + (10 if kev else 0)
        )
        score = min(100, round(raw))
        immediate = kev or cvss >= 9.0
        if immediate:
            tier = "immediate"
        elif score >= 70:
            tier = "high"
        elif score >= 40:
            tier = "normal"
        else:
            tier = "low"
        return {"state": "SUCCESS", "score": score, "tier": tier, "immediate": immediate}

    return respond(payload, work)


def patch_deadline(payload: dict[str, Any]) -> dict[str, Any]:
    """KEV or critical severity gets a 24 h window from detection; anything else uses sla_hours."""

    def work() -> dict[str, Any]:
        detected = _utc(payload["detected_at"])
        if detected is None:
            return _bad("detected_at", "detected_at must be YYYY-MM-DDTHH:MM:SSZ")
        if payload["kev"] or payload["severity"] == "critical":
            window: Any = _URGENT_WINDOW_H
            basis = "kev" if payload["kev"] else "critical"
        else:
            window = payload.get("sla_hours")
            if window is None:
                return _bad("sla_hours", "sla_hours is required unless the finding is KEV or critical")
            basis = "sla"
        deadline = detected + dt.timedelta(hours=window)
        return {
            "state": "SUCCESS",
            "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_hours": window,
            "basis": basis,
        }

    return respond(payload, work)


def _parse_semver(text: str) -> tuple[tuple[int, int, int], str | None] | None:
    match = _SEMVER.fullmatch(text)
    if match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3])), match[4]


def _pre_key(pre: str | None) -> tuple[Any, ...]:
    """Semver precedence: a release outranks every prerelease of the same core version."""
    if pre is None:
        return (1,)
    idents = []
    for ident in pre.split("."):
        idents.append((0, int(ident), "") if ident.isdigit() else (1, 0, ident))
    return (0, tuple(idents))


def semver_bump(payload: dict[str, Any]) -> dict[str, Any]:
    """Classify from -> to. Breaking means a major change, or a minor change while major is 0."""

    def work() -> dict[str, Any]:
        old = _parse_semver(payload["from"])
        if old is None:
            return _bad("from", "from must be a semver version")
        new = _parse_semver(payload["to"])
        if new is None:
            return _bad("to", "to must be a semver version")
        (old_core, old_pre), (new_core, new_pre) = old, new
        old_key = (old_core, _pre_key(old_pre))
        new_key = (new_core, _pre_key(new_pre))
        if new_key < old_key:
            kind = "downgrade"
        elif new_key == old_key:
            kind = "none"
        elif new_core[0] != old_core[0]:
            kind = "major"
        elif new_core[1] != old_core[1]:
            kind = "minor"
        elif new_core[2] != old_core[2]:
            kind = "patch"
        else:
            kind = "prerelease"
        breaking = kind == "major" or (kind == "minor" and old_core[0] == 0)
        if kind in ("downgrade", "none"):
            autonomy = "none"
        else:
            autonomy = "L3" if breaking else "L2"
        return {"state": "SUCCESS", "kind": kind, "breaking": breaking, "autonomy": autonomy}

    return respond(payload, work)


def eol_distance(payload: dict[str, Any]) -> dict[str, Any]:
    """Days from today to EOL. Negative means passed; an epic is due at 90 days or less."""

    def work() -> dict[str, Any]:
        today = _date(payload["today"])
        if today is None:
            return _bad("today", "today must be YYYY-MM-DD")
        eol = _date(payload["eol_date"])
        if eol is None:
            return _bad("eol_date", "eol_date must be YYYY-MM-DD")
        days = (eol - today).days
        if days < 0:
            status = "passed"
        elif days <= _EOL_EPIC_DAYS:
            status = "within_90_days"
        else:
            status = "supported"
        return {
            "state": "SUCCESS",
            "days_to_eol": days,
            "status": status,
            "upgrade_epic": days <= _EOL_EPIC_DAYS,
            "compliance_finding": days < 0 and payload["in_production"],
        }

    return respond(payload, work)


def _spec_class(spec: str) -> str:
    """Pin state of one specifier, taken from the maint_deps script so the two cannot drift."""
    return cast(str, script("maint_deps")._pin_state(spec))  # maint_deps._pin_state is annotated str; script() module attrs are Any


def unpinned_count(payload: dict[str, Any]) -> dict[str, Any]:
    """Count specifiers that are not a single exact pin. Empty and wildcard entries are unpinned too."""

    def work() -> dict[str, Any]:
        classes = [_spec_class(spec) for spec in payload["specs"]]
        total = len(classes)
        if total == 0:
            return _bad("specs", "specs must contain at least one entry")
        pinned = classes.count("pinned")
        unpinned = total - pinned
        return {
            "state": "SUCCESS",
            "total": total,
            "pinned": pinned,
            "unpinned": unpinned,
            "wildcard": classes.count("wildcard"),
            "unpinned_pct": round(100 * unpinned / total, 1),
        }

    return respond(payload, work)


def stale_age(payload: dict[str, Any]) -> dict[str, Any]:
    """Days between the installed release date and the latest release date, against a threshold."""

    def work() -> dict[str, Any]:
        installed = _date(payload["installed_released"])
        if installed is None:
            return _bad("installed_released", "installed_released must be YYYY-MM-DD")
        latest = _date(payload["latest_released"])
        if latest is None:
            return _bad("latest_released", "latest_released must be YYYY-MM-DD")
        age = (latest - installed).days
        if age < 0:
            return _bad("latest_released", "latest_released must not precede installed_released")
        threshold = payload["threshold_days"]
        return {
            "state": "SUCCESS",
            "age_days": age,
            "stale": age > threshold,
            "days_over_threshold": max(0, age - threshold),
        }

    return respond(payload, work)


def debt_priority(payload: dict[str, Any]) -> dict[str, Any]:
    """score = 100 * (impact*recurrence/25) * 8/(8+effort_h), capped at 100.

    Iteration: score >= 70 -> I+1, >= 40 -> I+2, otherwise I+3.
    """

    def work() -> dict[str, Any]:
        benefit = payload["impact"] * payload["recurrence"]
        score = min(100, round(100 * (benefit / 25) * (8 / (8 + payload["effort_h"]))))
        if score >= 70:
            iteration = "I+1"
        elif score >= 40:
            iteration = "I+2"
        else:
            iteration = "I+3"
        return {"state": "SUCCESS", "score": score, "proposed_iteration": iteration}

    return respond(payload, work)


def _license_ids(expression: str) -> set[str]:
    """SPDX identifiers in an expression. AND/OR drop out; the exception after WITH is skipped."""
    ids: set[str] = set()
    skip_next = False
    for token in _LICENSE_TOKEN.findall(expression):
        upper = token.upper()
        if skip_next:
            skip_next = False
        elif upper == "WITH":
            skip_next = True
        elif upper not in ("AND", "OR"):
            ids.add(token.lower())
    return ids


def _has_copyleft(ids: set[str]) -> bool:
    return any(ident.upper().startswith(_COPYLEFT_PREFIXES) for ident in ids)


def license_change(payload: dict[str, Any]) -> dict[str, Any]:
    """Compare SPDX identifier sets between two license records."""

    def work() -> dict[str, Any]:
        before = _license_ids(payload["previous_license"])
        if not before:
            return _bad("previous_license", "no SPDX identifier found")
        after = _license_ids(payload["current_license"])
        if not after:
            return _bad("current_license", "no SPDX identifier found")
        allowed_ids = {item.strip().lower() for item in payload["allowlist"]} - {""}
        changed = before != after
        allowed = after <= allowed_ids
        return {
            "state": "SUCCESS",
            "changed": changed,
            "allowed": allowed,
            "copyleft_introduced": _has_copyleft(after) and not _has_copyleft(before),
            "requires_review": changed or not allowed,
        }

    return respond(payload, work)


def _changelog_problems(entry: str, version: str) -> list[str]:
    lines = [line.rstrip() for line in entry.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    if not lines:
        return ["entry is empty"]
    problems: list[str] = []
    head = _CHANGELOG_HEAD.fullmatch(lines[0])
    if head is None:
        problems.append("first line must be '## [version] - YYYY-MM-DD' or '## [Unreleased]'")
    else:
        heading_version, stamp = head.group(1), head.group(2)
        if heading_version != version:
            problems.append(f"heading version {heading_version!r} does not match {version!r}")
        if heading_version == "Unreleased":
            if stamp is not None:
                problems.append("Unreleased heading must not carry a date")
        elif stamp is None:
            problems.append("release heading is missing its date")
        elif _date(stamp) is None:
            problems.append(f"release date {stamp!r} is not a valid YYYY-MM-DD date")
    categories = 0
    entries = 0
    in_category = False
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("## "):
            problems.append("unexpected '## ' heading after the release heading")
        elif line.startswith("### "):
            name = line[4:].strip()
            if name not in _CHANGELOG_CATEGORIES:
                problems.append(f"unknown category {name!r}")
            categories += 1
            in_category = True
        elif line == "-" or line.startswith("- "):
            entries += 1
            if not in_category:
                problems.append("entry appears before any category heading")
            elif not line[1:].strip():
                problems.append("empty entry")
        elif line.startswith("  ") and entries:
            continue
        else:
            problems.append(f"unrecognized line {line[:40]!r}")
    if categories == 0:
        problems.append("no category heading")
    if entries == 0:
        problems.append("no entries")
    return problems


def changelog_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one Keep a Changelog release section against the expected version."""

    def work() -> dict[str, Any]:
        problems = _changelog_problems(payload["entry"], payload["version"])
        return {
            "state": "SUCCESS",
            "valid": not problems,
            "problem_count": len(problems),
            "first_problem": problems[0] if problems else "",
        }

    return respond(payload, work)


_HOTFIX_MAX_SEVERITY = 2
_HOTFIX_MAX_FIX_H = 4
_RANGE_OPERATORS = ("<=", ">=", "<", ">")
_RANGE_CLAUSE = re.compile(r"(<=|>=|<|>)\s*(\S+)")
_MAX_RANGE_CLAUSES = 8


def hotfix_decision(payload: dict[str, Any]) -> dict[str, Any]:
    """Hotfix-vs-rollback rule: sev 1 or 2 (1 = worst, as in A13's incident.alert) primes the hotfix
    path; a low-risk rollback is preferred; a hotfix is chosen only when the root-cause fix is
    estimated under 4 h."""

    def work() -> dict[str, Any]:
        severity = payload["severity"]
        if isinstance(severity, bool) or not isinstance(severity, int) or not 1 <= severity <= 5:
            return _bad("severity", "severity must be an integer from 1 to 5")
        fix_est_h = payload["fix_est_h"]
        if isinstance(fix_est_h, bool) or not isinstance(fix_est_h, (int, float)) or not math.isfinite(fix_est_h) or fix_est_h < 0:
            return _bad("fix_est_h", "fix_est_h must be a finite number of hours, 0 or more")
        rollback_available = payload["rollback_available"]
        if not isinstance(rollback_available, bool):
            return _bad("rollback_available", "rollback_available must be a boolean")
        rollback_risk = payload["rollback_risk"]
        if rollback_risk not in ("low", "high"):
            return _bad("rollback_risk", "rollback_risk must be low or high")
        primes = severity <= _HOTFIX_MAX_SEVERITY
        fast_fix = fix_est_h < _HOTFIX_MAX_FIX_H
        if not primes:
            path, reason = "defer", "severity 3 or above (sev 1 is the worst) does not prime the hotfix path; use the normal patch.task flow"
        elif rollback_available and rollback_risk == "low":
            path, reason = "rollback", "a low-risk rollback is preferred over a hotfix"
        elif fast_fix:
            path = "hotfix"
            reason = (
                "the rollback is high risk and the root-cause fix is under 4 h"
                if rollback_available
                else "no rollback is available and the root-cause fix is under 4 h"
            )
        elif rollback_available:
            path, reason = "rollback", "the root-cause fix needs 4 h or more, so only the high-risk rollback remains; escalate for approval"
        else:
            path, reason = "defer", "no rollback is available and the root-cause fix needs 4 h or more; escalate to the incident owner"
        return {"state": "SUCCESS", "path": path, "primes_hotfix": primes, "reason": reason}

    return respond(payload, work)


def _range_clauses(text: str) -> list[tuple[str, str, tuple[Any, ...]]] | None:
    """Parse a comma-separated AND range into (operator, version text, sort key); None when malformed."""
    parts = [part.strip() for part in text.split(",")]
    if not parts or len(parts) > _MAX_RANGE_CLAUSES:
        return None
    clauses = []
    for part in parts:
        match = _RANGE_CLAUSE.fullmatch(part)
        if match is None:
            return None
        parsed = _parse_semver(match[2])
        if parsed is None:
            return None
        core, pre = parsed
        clauses.append((match[1], match[2], (core, _pre_key(pre))))
    return clauses


def version_affected(payload: dict[str, Any]) -> dict[str, Any]:
    """True when the version satisfies every clause of an advisory range such as ``>=1.0.0,<1.4.5``.

    matched_clause is the normalised range (clauses joined by a comma) when affected, else empty."""

    def work() -> dict[str, Any]:
        parsed = _parse_semver(payload["version"].strip())
        if parsed is None:
            return _bad("version", "version must be a semver version such as 1.2.3 or 1.2.3-rc.1")
        key = (parsed[0], _pre_key(parsed[1]))
        clauses = _range_clauses(payload["affected"])
        if clauses is None:
            return _bad(
                "affected",
                f"affected must be 1 to {_MAX_RANGE_CLAUSES} comma-separated clauses of "
                f"{', '.join(_RANGE_OPERATORS)} followed by a semver version",
            )
        tests = {
            "<": lambda a, b: a < b,
            "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b,
            ">=": lambda a, b: a >= b,
        }
        affected = all(tests[op](key, bound) for op, _text, bound in clauses)
        matched = ",".join(f"{op}{text}" for op, text, _bound in clauses) if affected else ""
        return {"state": "SUCCESS", "affected": affected, "matched_clause": matched}

    return respond(payload, work)
