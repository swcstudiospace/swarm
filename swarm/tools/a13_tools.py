"""A13 observability analysis tools: pure checks over supplied telemetry facts.

Tools: obs_multiwindow_alert (burn-rate paging), obs_budget_remaining (error budget),
obs_alert_fatigue (noise ratio of pages), obs_incident_mttd_mttr (detection and repair time),
obs_ingestion_lag_p95 (telemetry lag), obs_dashboard_staleness (dashboard freshness),
obs_runbook_link_coverage (alert-to-runbook links), obs_cardinality_estimate (series explosion),
obs_probe_availability (synthetic probe availability and failure streaks).
"""
from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from swarm.tools.support import respond

# Thresholds are decimal so that the documented boundaries (14.4x, 6x, 1x) are exact, not float-division accidents.
PAGE_BURN_1H = Decimal("14.4")
PAGE_BURN_6H = Decimal("6")
TICKET_BURN_3D = Decimal("1")
MAX_EVENT_COUNT = 10**15


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr), so 0.0144 is 0.0144 and not its binary neighbour."""
    return Decimal(repr(value))


def _ts(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp needs a timezone: {value!r}")
    return parsed


def multiwindow_alert(payload: dict[str, Any]) -> dict[str, Any]:
    """Multi-window burn rate: page when 1h >= 14.4x and 6h >= 6x; ticket when 3d >= 1x."""

    def work() -> dict[str, Any]:
        budget = Decimal(1) - _dec(payload["slo_target"])
        ratio_1h, ratio_6h, ratio_3d = (_dec(payload[key]) for key in ("error_ratio_1h", "error_ratio_6h", "error_ratio_3d"))
        burn_1h, burn_6h, burn_3d = float(ratio_1h / budget), float(ratio_6h / budget), float(ratio_3d / budget)
        # cross-multiplied: error_ratio >= factor * budget, exact in decimal arithmetic
        page = ratio_1h >= PAGE_BURN_1H * budget and ratio_6h >= PAGE_BURN_6H * budget
        ticket = ratio_3d >= TICKET_BURN_3D * budget
        severity = "page" if page else "ticket" if ticket else "none"
        return {
            "state": "SUCCESS",
            "burn_1h": round(burn_1h, 4),
            "burn_6h": round(burn_6h, 4),
            "burn_3d": round(burn_3d, 4),
            "page": page,
            "ticket": ticket,
            "severity": severity,
        }

    return respond(payload, work)


def budget_remaining(payload: dict[str, Any]) -> dict[str, Any]:
    """Error budget left in a window, from good and total event counts."""

    def work() -> dict[str, Any]:
        good, total = payload["good"], payload["total"]
        if total > MAX_EVENT_COUNT:
            return {"state": "INVALID_INPUT", "field": "total", "message": f"total must be at most {MAX_EVENT_COUNT}"}
        if good > total:
            return {"state": "INVALID_INPUT", "field": "good", "message": "good events exceed total events"}
        allowed = total * (Decimal(1) - _dec(payload["slo_target"]))
        bad = total - good
        remaining = allowed - bad
        return {
            "state": "SUCCESS",
            "allowed_bad_events": round(float(allowed), 2),
            "bad_events": bad,
            "remaining_events": round(float(remaining), 2),
            "remaining_fraction": round(float(remaining / allowed), 4),
            "exhausted": remaining <= 0,
        }

    return respond(payload, work)


def alert_fatigue(payload: dict[str, Any]) -> dict[str, Any]:
    """Share of pages that were not actionable, compared with the allowed noise ratio."""

    def work() -> dict[str, Any]:
        pages, actionable = payload["pages"], payload["actionable"]
        if actionable > pages:
            return {"state": "INVALID_INPUT", "field": "actionable", "message": "actionable pages exceed pages"}
        noise = pages - actionable
        noise_ratio = noise / pages if pages else 0.0
        return {
            "state": "SUCCESS",
            "noise_ratio": round(noise_ratio, 4),
            "actionable_count": actionable,
            "noise_count": noise,
            "fatigued": noise_ratio > payload["max_noise_ratio"],
        }

    return respond(payload, work)


def incident_timing(payload: dict[str, Any]) -> dict[str, Any]:
    """MTTD = detected - started; MTTR = resolved - started (time to restore); both averaged in seconds."""

    def work() -> dict[str, Any]:
        mttd: list[float] = []
        mttr: list[float] = []
        for index, item in enumerate(payload["incidents"]):
            try:
                started = _ts(item["started"])
                detected = _ts(item["detected"])
                resolved = _ts(item["resolved"])
            except ValueError as exc:
                return {"state": "INVALID_INPUT", "field": "incidents", "message": f"incident {index}: {exc}"}
            if not started <= detected <= resolved:
                return {
                    "state": "INVALID_INPUT",
                    "field": "incidents",
                    "message": f"incident {index}: expected started <= detected <= resolved",
                }
            mttd.append((detected - started).total_seconds())
            mttr.append((resolved - started).total_seconds())
        return {
            "state": "SUCCESS",
            "incident_count": len(mttd),
            "mttd_seconds": round(sum(mttd) / len(mttd), 1),
            "mttr_seconds": round(sum(mttr) / len(mttr), 1),
            "max_mttr_seconds": round(max(mttr), 1),
        }

    return respond(payload, work)


def ingestion_lag_p95(payload: dict[str, Any]) -> dict[str, Any]:
    """Nearest-rank P95 of telemetry ingestion lag, compared with a target."""

    def work() -> dict[str, Any]:
        lags = sorted(float(value) for value in payload["lags_seconds"])
        p95 = lags[math.ceil(0.95 * len(lags)) - 1]
        return {
            "state": "SUCCESS",
            "count": len(lags),
            "p95_seconds": p95,
            "max_seconds": lags[-1],
            "within_target": p95 <= payload["target_s"],
        }

    return respond(payload, work)


def dashboard_staleness(payload: dict[str, Any]) -> dict[str, Any]:
    """Age in hours of a dashboard's last refresh at a supplied check time."""

    def work() -> dict[str, Any]:
        try:
            updated = _ts(payload["last_updated_at"])
        except ValueError as exc:
            return {"state": "INVALID_INPUT", "field": "last_updated_at", "message": str(exc)}
        try:
            checked = _ts(payload["checked_at"])
        except ValueError as exc:
            return {"state": "INVALID_INPUT", "field": "checked_at", "message": str(exc)}
        if checked < updated:
            return {
                "state": "INVALID_INPUT",
                "field": "checked_at",
                "message": "checked_at precedes last_updated_at",
            }
        age_hours = (checked - updated).total_seconds() / 3600
        return {
            "state": "SUCCESS",
            "age_hours": round(age_hours, 2),
            "stale": age_hours > payload["max_age_hours"],
        }

    return respond(payload, work)


def runbook_link_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Share of alerts whose runbook reference resolves to a known runbook."""

    def work() -> dict[str, Any]:
        known = set(payload["known_runbooks"])
        alerts = payload["alerts"]
        unlinked = [alert["name"] for alert in alerts if alert["runbook"] not in known]
        total = len(alerts)
        linked = total - len(unlinked)
        return {
            "state": "SUCCESS",
            "alert_count": total,
            "linked_count": linked,
            "coverage_ratio": round(linked / total, 4),
            "unlinked_alerts": unlinked,
        }

    return respond(payload, work)


def cardinality_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    """Worst-case series count: product of distinct values per label."""

    def work() -> dict[str, Any]:
        labels = payload["labels"]
        names = [label["label"] for label in labels]
        if len(set(names)) != len(names):
            return {"state": "INVALID_INPUT", "field": "labels", "message": "duplicate label names"}
        series = math.prod(label["distinct_values"] for label in labels)
        top = max(labels, key=lambda label: label["distinct_values"])["label"]
        return {
            "state": "SUCCESS",
            "series_estimate": series,
            "exceeds_budget": series > payload["series_budget"],
            "highest_cardinality_label": top,
        }

    return respond(payload, work)


def probe_availability(payload: dict[str, Any]) -> dict[str, Any]:
    """Availability ratio of boolean probe results and the longest run of consecutive failures."""

    def work() -> dict[str, Any]:
        results = payload["results"]
        up = sum(1 for ok in results if ok)
        longest = 0
        run = 0
        for ok in results:
            run = 0 if ok else run + 1
            longest = max(longest, run)
        availability = up / len(results)
        return {
            "state": "SUCCESS",
            "probe_count": len(results),
            "availability": round(availability, 4),
            "longest_failure_run": longest,
            "meets_target": availability >= payload["min_availability"],
        }

    return respond(payload, work)


MAX_AUTO_MITIGATIONS = 3
ESCALATION_MINUTES = 10.0
# obs_slo numbers severities so that 1 is the worst: sev 1 (1h page) and sev 2 (6h page) are the incident levels.
INCIDENT_SEV_MAX = 2


def mitigation_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Allow an automatic mitigation only for a whitelisted playbook, below the per-incident cap, and not on full prod without A12."""

    def work() -> dict[str, Any]:
        max_auto = payload.get("max_auto", MAX_AUTO_MITIGATIONS)
        remaining = max(0, max_auto - payload["auto_count"])
        if payload["playbook"] not in payload["whitelist"]:
            reason = "not-whitelisted"
        elif payload["environment"] == "prod" and not payload.get("full_prod_with_a12", False):
            reason = "prod-needs-approval"
        elif remaining == 0:
            reason = "cap-reached"
        else:
            reason = "ok"
        return {"state": "SUCCESS", "allowed": reason == "ok", "reason": reason, "remaining_auto": remaining}

    return respond(payload, work)


def escalation_due(payload: dict[str, Any]) -> dict[str, Any]:
    """An incident at sev 1 or 2 escalates to humans once it has gone max_minutes (default 10) without stabilizing."""

    def work() -> dict[str, Any]:
        try:
            declared = _ts(payload["declared_at"])
        except ValueError as exc:
            return {"state": "INVALID_INPUT", "field": "declared_at", "message": str(exc)}
        try:
            now = _ts(payload["now"])
        except ValueError as exc:
            return {"state": "INVALID_INPUT", "field": "now", "message": str(exc)}
        if now < declared:
            return {"state": "INVALID_INPUT", "field": "now", "message": "now precedes declared_at"}
        stabilized = None
        if payload.get("stabilized_at") is not None:
            try:
                stabilized = _ts(payload["stabilized_at"])
            except ValueError as exc:
                return {"state": "INVALID_INPUT", "field": "stabilized_at", "message": str(exc)}
            if stabilized < declared:
                return {"state": "INVALID_INPUT", "field": "stabilized_at", "message": "stabilized_at precedes declared_at"}
            if stabilized > now:
                return {"state": "INVALID_INPUT", "field": "stabilized_at", "message": "stabilized_at is after now"}
        limit_s = payload.get("max_minutes", ESCALATION_MINUTES) * 60
        elapsed = ((stabilized or now) - declared).total_seconds()
        if payload["sev"] > INCIDENT_SEV_MAX:
            reason, due, remaining = "sev-below-threshold", False, 0.0
        elif stabilized is not None:
            reason, due, remaining = "stabilized", False, 0.0
        elif elapsed >= limit_s:
            reason, due, remaining = "escalate", True, 0.0
        else:
            reason, due, remaining = "waiting", False, limit_s - elapsed
        return {
            "state": "SUCCESS",
            "elapsed_s": round(elapsed, 3),
            "due": due,
            "remaining_s": round(remaining, 3),
            "reason": reason,
        }

    return respond(payload, work)
