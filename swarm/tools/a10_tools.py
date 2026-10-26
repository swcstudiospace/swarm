"""A10 security lane: deterministic checks over payload data for the security gate.

Mission: give sec_gate triage and the verdict cross-check reproducible evidence.
Tools: sec_credential_scan, sec_cvss_band, sec_dependency_age, sec_license_risk,
sec_tls_min_version, sec_header_presence, sec_kev_match_count, sec_sbom_summary.
Pure logic: no I/O, clock, environment, or randomness. Credential values are never returned.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any

from swarm.tools.support import respond

# Heuristic detectors. The sec_gate scan remains the authority; these only report rule hits.
# Every detector is linear in the line length: each regex either has a bounded repetition or starts at a
# token boundary (lookbehind), so a start position inside a long identifier-like run fails in O(1).
# JWT: "eyJ" may start at any word boundary, including after '-' (a dash is a boundary but also a segment character).
# Only the first dash-separated token that starts with "eyJ" in a run can match: a later start has a shorter first
# segment and the same tail, so it fails whenever the first one failed. The lookbehind plus the token skip therefore
# try one start per run instead of one per "-eyJ".
_JWT = re.compile(
    r"(?<![0-9A-Za-z_-])(?:(?!eyJ)[0-9A-Za-z_]*-)*"
    r"eyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}\b"
)
# generic_secret_assignment is "<identifier containing a secret word> [:=] <8+ char value>". It is decided per
# maximal identifier run: a run is followed by the assignment, and only then is the keyword looked up inside it.
_ASSIGNMENT = re.compile(
    r"(?<![a-z0-9_-])([a-z0-9_-]+)(?=\s*[:=]\s*[\"']?[^\s\"'*<>{}$=]{8,})",
    re.IGNORECASE,
)
_SECRET_WORD = re.compile(r"password|passwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token", re.IGNORECASE)


def _generic_secret_assignment(line: str) -> bool:
    return any(_SECRET_WORD.search(match.group(1)) for match in _ASSIGNMENT.finditer(line))


# (rule name, predicate over one line); a compiled regex contributes its bound search().
_CREDENTIAL_RULES = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b").search),
    ("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,255}\b").search),
    ("slack_token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}\b").search),
    ("stripe_live_key", re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b").search),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b").search),
    ("jwt", _JWT.search),
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----").search),
    ("generic_secret_assignment", _generic_secret_assignment),
)
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_TLS_RANK = {"SSL2": 0, "SSL3": 1, "TLS1.0": 2, "TLS1.1": 3, "TLS1.2": 4, "TLS1.3": 5}
_CVE_ID = re.compile(r"CVE-\d{4}-\d{4,}", re.ASCII)
# Exact dotted-numeric version with optional pre-release and build metadata.
# Ranges, wildcards, tags such as "latest", and missing versions are not pinned.
_PINNED_VERSION = re.compile(r"v?\d+(?:\.\d+)*(?:-[0-9A-Za-z.]+)?(?:\+[0-9A-Za-z.]+)?", re.ASCII)


def credential_scan(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        # Match objects are discarded; only the rule name and 1-based line number leave this function.
        findings = [
            {"rule": name, "line": number}
            for number, line in enumerate(payload["lines"], start=1)
            for name, matches in _CREDENTIAL_RULES
            if matches(line)
        ]
        return {"state": "SUCCESS", "finding_count": len(findings), "findings": findings}

    return respond(payload, work)


def cvss_band(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        score = payload["score"]
        if score == 0:
            band = "none"
        elif score < 4.0:
            band = "low"
        elif score < 7.0:
            band = "medium"
        elif score < 9.0:
            band = "high"
        else:
            band = "critical"
        return {"state": "SUCCESS", "severity": band, "gate_blocking": score >= 7.0}

    return respond(payload, work)


def _day(text: str) -> dt.date | None:
    if not _ISO_DAY.fullmatch(text):
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def dependency_age(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        released = _day(payload["released"])
        if released is None:
            return {"state": "INVALID_INPUT", "field": "released", "message": "released must be a valid YYYY-MM-DD date"}
        as_of = _day(payload["as_of"])
        if as_of is None:
            return {"state": "INVALID_INPUT", "field": "as_of", "message": "as_of must be a valid YYYY-MM-DD date"}
        age = (as_of - released).days
        if age < 0:
            return {"state": "INVALID_INPUT", "field": "released", "message": "released is after as_of"}
        return {"state": "SUCCESS", "age_days": age, "stale": age > payload["max_age_days"]}

    return respond(payload, work)


def license_risk(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        licenses = {value.strip() for value in payload["licenses"]}
        allowed = {value.strip().lower() for value in payload["allowlist"]}
        if "" in licenses:
            return {"state": "INVALID_INPUT", "field": "licenses", "message": "license identifiers must not be blank"}
        if "" in allowed:
            return {"state": "INVALID_INPUT", "field": "allowlist", "message": "allowlist entries must not be blank"}
        unapproved = sorted(item for item in licenses if item.lower() not in allowed)
        return {
            "state": "SUCCESS",
            "license_ok": not unapproved,
            "unapproved": unapproved,
            "unapproved_count": len(unapproved),
        }

    return respond(payload, work)


def _tls_key(text: str) -> str:
    """Canonical key: 'TLSv1.2', 'TLS 1.2' and 'tls1.2' all map to 'TLS1.2'."""
    key = re.sub(r"[^0-9A-Z.]", "", text.upper())
    return key.replace("TLSV", "TLS").replace("SSLV", "SSL")


def tls_min_version(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        floor = _TLS_RANK[_tls_key(payload["minimum_version"])]
        below: set[str] = set()
        unrecognized: set[str] = set()
        for raw in payload["protocols"]:
            if not raw.strip():
                return {"state": "INVALID_INPUT", "field": "protocols", "message": "protocol names must not be blank"}
            key = _tls_key(raw)
            if key not in _TLS_RANK:
                unrecognized.add(raw.strip())
            elif _TLS_RANK[key] < floor:
                below.add(key)
        return {
            "state": "SUCCESS",
            "compliant": not below and not unrecognized,
            "below_minimum": sorted(below),
            "unrecognized": sorted(unrecognized),
        }

    return respond(payload, work)


def header_presence(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        observed = {value.strip().lower() for value in payload["headers"]}
        required = {value.strip().lower() for value in payload["required_headers"]}
        if "" in observed:
            return {"state": "INVALID_INPUT", "field": "headers", "message": "header names must not be blank"}
        if "" in required:
            return {"state": "INVALID_INPUT", "field": "required_headers", "message": "header names must not be blank"}
        missing = sorted(required - observed)
        return {"state": "SUCCESS", "complete": not missing, "missing": missing, "missing_count": len(missing)}

    return respond(payload, work)


def _cve_set(values: list[str]) -> set[str] | None:
    cves = {value.strip().upper() for value in values}
    if not all(_CVE_ID.fullmatch(cve) for cve in cves):
        return None
    return cves


def kev_match_count(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        checked = _cve_set(payload["cve_ids"])
        if checked is None:
            return {"state": "INVALID_INPUT", "field": "cve_ids", "message": "entries must be CVE-YYYY-NNNN identifiers"}
        kev = _cve_set(payload["kev_ids"])
        if kev is None:
            return {"state": "INVALID_INPUT", "field": "kev_ids", "message": "entries must be CVE-YYYY-NNNN identifiers"}
        matched = sorted(checked & kev)
        return {
            "state": "SUCCESS",
            "checked_count": len(checked),
            "matched": matched,
            "matched_count": len(matched),
            "blocking": bool(matched),
        }

    return respond(payload, work)


def sbom_summary(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        unpinned_names: set[str] = set()
        unpinned_count = 0
        for component in payload["components"]:
            name = component["name"].strip()
            if not name:
                return {"state": "INVALID_INPUT", "field": "components", "message": "component names must not be blank"}
            version = component.get("version")
            if not (isinstance(version, str) and _PINNED_VERSION.fullmatch(version.strip())):
                unpinned_count += 1
                unpinned_names.add(name)
        return {
            "state": "SUCCESS",
            "component_count": len(payload["components"]),
            "unpinned_count": unpinned_count,
            "unpinned": sorted(unpinned_names),
            "all_pinned": unpinned_count == 0,
        }

    return respond(payload, work)


# --- v2.2 additions: scanner dedup, allow-list check, SBOM diff ---------------------------------
# Severity scale of the gate (swarm.gates.SEVERITIES); the order is the rank used for "severity = max".
_GATE_SEVERITIES = ("info", "minor", "major", "critical", "blocker")


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _finding_key(finding: dict[str, Any], group_by: str) -> str:
    """Group key. Findings missing a CWE or a location can never be proven the same issue: unique key."""
    if group_by == "rule":
        rule: str = finding["rule"].strip()
        return rule
    cwe = str(finding.get("cwe") or "").strip().upper()
    location = str(finding.get("location") or "").strip()
    if cwe and location:
        return f"{cwe}@{location}"
    return f"unkeyed:{finding['id'].strip()}"


def finding_dedup(payload: dict[str, Any]) -> dict[str, Any]:
    """Merge findings of several scanners; severity of a group is the maximum, differing severities are conflicts."""

    def work() -> dict[str, Any]:
        group_by = payload.get("group_by", "cwe_location")
        if group_by not in ("cwe_location", "rule"):
            return _invalid("group_by", "group_by must be cwe_location or rule")
        seen_ids: set[str] = set()
        groups: dict[str, dict[str, Any]] = {}
        for index, finding in enumerate(payload["findings"]):
            fid = finding["id"].strip()
            if not fid or not finding["tool"].strip() or not finding["rule"].strip():
                return _invalid("findings", f"findings[{index}]: id, tool and rule must not be blank")
            if finding["severity"] not in _GATE_SEVERITIES:
                return _invalid("findings", f"findings[{index}]: severity must be one of {', '.join(_GATE_SEVERITIES)}")
            if fid in seen_ids:
                return _invalid("findings", f"findings[{index}]: duplicate finding id {fid}")
            seen_ids.add(fid)
            group = groups.setdefault(_finding_key(finding, group_by), {"ids": [], "by_tool": {}})
            group["ids"].append(fid)
            tool = finding["tool"].strip()
            rank = _GATE_SEVERITIES.index(finding["severity"])
            group["by_tool"][tool] = max(group["by_tool"].get(tool, rank), rank)
        out_groups: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for key, group in groups.items():
            ranks = group["by_tool"].values()
            severity = _GATE_SEVERITIES[max(ranks)]
            out_groups.append({
                "key": key,
                "severity": severity,
                "tools": sorted(group["by_tool"]),
                "ids": sorted(group["ids"]),
            })
            if len(set(ranks)) > 1:
                conflicts.append({
                    "key": key,
                    "severity": severity,
                    "by_tool": [{"tool": t, "severity": _GATE_SEVERITIES[r]} for t, r in sorted(group["by_tool"].items())],
                })
        out_groups.sort(key=lambda g: (-_GATE_SEVERITIES.index(g["severity"]), g["key"]))
        conflicts.sort(key=lambda c: c["key"])
        return {
            "state": "SUCCESS",
            "group_by": group_by,
            "finding_count": len(seen_ids),
            "group_count": len(out_groups),
            "groups": out_groups,
            "conflicts": conflicts,
        }

    return respond(payload, work)


def allowlist_check(payload: dict[str, Any]) -> dict[str, Any]:
    """The sec_gate allow-list rules over supplied data: justification required, expired on or after `expires`."""

    def work() -> dict[str, Any]:
        as_of = _day(payload["as_of"])
        if as_of is None:
            return _invalid("as_of", "as_of must be an ISO date YYYY-MM-DD")
        seen: set[str] = set()
        active: list[str] = []
        expired: list[str] = []
        unjustified: list[str] = []
        no_expiry: list[str] = []
        for index, entry in enumerate(payload["entries"]):
            entry_id = entry["id"].strip()
            if not entry_id:
                return _invalid("entries", f"entries[{index}]: id must not be blank")
            if entry_id in seen:
                return _invalid("entries", f"entries[{index}]: duplicate allow-list id {entry_id}")
            seen.add(entry_id)
            if not str(entry.get("justification", "")).strip():
                unjustified.append(entry_id)
                continue
            expires = str(entry.get("expires", "")).strip()
            if not expires:
                active.append(entry_id)
                no_expiry.append(entry_id)
                continue
            expiry_day = _day(expires)
            if expiry_day is None:
                return _invalid("entries", f"entries[{index}]: expires must be an ISO date YYYY-MM-DD")
            # sec_gate compares the expiry at 00:00 with the current time: the entry stops applying on its expiry day.
            (expired if expiry_day <= as_of else active).append(entry_id)
        findings = {fid.strip() for fid in payload["finding_ids"]}
        suppressed = sorted(set(active) & findings)
        return {
            "state": "SUCCESS",
            "valid": not unjustified,
            "as_of": as_of.isoformat(),
            "entry_count": len(payload["entries"]),
            "active": sorted(active),
            "expired": sorted(expired),
            "unjustified": sorted(unjustified),
            "no_expiry": sorted(no_expiry),
            "suppressed_ids": suppressed,
            "unused": sorted(set(active) - findings),
        }

    return respond(payload, work)


def _component_map(components: list[dict[str, Any]], field: str) -> tuple[dict[str, str] | None, dict[str, Any] | None]:
    """name -> stripped version ("" when absent). A blank or repeated name is an error naming `field`."""
    mapping: dict[str, str] = {}
    for index, component in enumerate(components):
        name = component["name"].strip()
        if not name:
            return None, _invalid(field, f"{field}[{index}]: component names must not be blank")
        if name in mapping:
            return None, _invalid(field, f"{field}[{index}]: duplicate component name {name}")
        mapping[name] = str(component.get("version") or "").strip()
    return mapping, None


def _is_pinned(version: str) -> bool:
    return bool(_PINNED_VERSION.fullmatch(version))


def sbom_diff(payload: dict[str, Any]) -> dict[str, Any]:
    """Compare two component lists: added, removed, version-changed, and components that became unpinned."""

    def work() -> dict[str, Any]:
        before, problem = _component_map(payload["before"], "before")
        if problem:
            return problem
        after, problem = _component_map(payload["after"], "after")
        if problem:
            return problem
        assert before is not None and after is not None  # _component_map returns a mapping whenever it reports no problem
        added = [{"name": n, "version": after[n]} for n in sorted(after.keys() - before.keys())]
        removed = [{"name": n, "version": before[n]} for n in sorted(before.keys() - after.keys())]
        changed = [
            {"name": n, "from": before[n], "to": after[n]}
            for n in sorted(before.keys() & after.keys())
            if before[n] != after[n]
        ]
        newly_unpinned = sorted(
            n for n, version in after.items() if not _is_pinned(version) and (n not in before or _is_pinned(before[n]))
        )
        return {
            "state": "SUCCESS",
            "before_count": len(before),
            "after_count": len(after),
            "added": added,
            "removed": removed,
            "changed": changed,
            "newly_unpinned": newly_unpinned,
            "drift": bool(added or removed or changed or newly_unpinned),
        }

    return respond(payload, work)
