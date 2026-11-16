"""A12 release-manager lane tools: canary decisions, rollback triggers, record checks, delivery metrics.

Mission: decide canary promotion and rollback from supplied telemetry, verify release-record
completeness and promotion-gate order, and compute release-health metrics. Pure logic only;
every input comes from the payload.

Tools:
- rel_canary_verdict: promote, hold, or roll back one canary step.
- rel_rollback_trigger: find the first sustained SLO burn-rate breach.
- rel_release_record_check: list required release-record fields that are missing or empty.
- rel_change_failure_rate: failed-change share of supplied change outcomes.
- rel_deploy_frequency: deployments per week from supplied ISO deploy dates.
- rel_lead_time_p95: median and nearest-rank P95 of commit-to-deploy durations in hours.
- rel_gate_order_check: missing or out-of-order promotion gates.
- rel_gate_freshness: gate verdicts older than their maximum age, or waived without an approval id.
- rel_freeze_decision: deploy freeze from incident alerts, and whether an unfreeze request comes from the authority.
- rel_resume_point: next action after an interrupted canary promotion.
- rel_guardrail_breaches: guardrails one canary sample breaches, and metrics it is missing.
"""

import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from swarm.tools.support import respond


def canary_verdict(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        canary = payload["canary_error_rate"]
        regression = round(canary - payload["baseline_error_rate"], 6)
        if canary > payload["error_rate_max"]:
            decision, reason = "rollback", "error_rate_over_ceiling"
        elif regression > payload["max_regression"]:
            decision, reason = "rollback", "regressed_over_baseline"
        elif payload["soak_observed_min"] < payload["soak_required_min"]:
            decision, reason = "hold", "soak_incomplete"
        else:
            decision, reason = "promote", "within_guardrails"
        return {"state": "SUCCESS", "decision": decision, "reason": reason, "regression": regression}

    return respond(payload, work)


def rollback_trigger(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        limit = payload["slo_burn_max"]
        needed = payload["consecutive_required"]
        run = 0
        breach = -1
        for index, rate in enumerate(payload["burn_rates"]):
            run = run + 1 if rate > limit else 0
            if run >= needed:
                breach = index
                break
        return {
            "state": "SUCCESS",
            "triggered": breach >= 0,
            "breach_index": breach,
            "max_burn": max(payload["burn_rates"]),
        }

    return respond(payload, work)


def _is_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def release_record_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        record = payload["record"]
        fields = list(dict.fromkeys(payload["required_fields"]))
        missing = [name for name in fields if name not in record or _is_empty(record[name])]
        return {
            "state": "SUCCESS",
            "complete": not missing,
            "missing": ",".join(missing),
            "checked": len(fields),
        }

    return respond(payload, work)


def change_failure_rate(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        outcomes = payload["outcomes"]
        total = len(outcomes)
        failed = sum(1 for flag in outcomes if flag)
        return {"state": "SUCCESS", "total": total, "failed": failed, "rate": round(failed / total, 4)}

    return respond(payload, work)


def deploy_frequency(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        try:
            days = sorted(date.fromisoformat(text) for text in payload["deploy_days"])
        except ValueError as exc:
            return {
                "state": "INVALID_INPUT",
                "field": "deploy_days",
                "message": f"every entry must be an ISO date YYYY-MM-DD ({exc})",
            }
        span = (days[-1] - days[0]).days + 1
        return {
            "state": "SUCCESS",
            "deploys": len(days),
            "active_days": len(set(days)),
            "span_days": span,
            "per_week": round(len(days) / span * 7, 2),
        }

    return respond(payload, work)


def lead_time_p95(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        ordered = sorted(payload["durations_hours"])
        count = len(ordered)
        # Nearest-rank percentile: the value at rank ceil(p * n / 100), 1-based.
        median_rank = (50 * count + 99) // 100
        p95_rank = (95 * count + 99) // 100
        return {
            "state": "SUCCESS",
            "count": count,
            "median_hours": ordered[median_rank - 1],
            "p95_hours": ordered[p95_rank - 1],
        }

    return respond(payload, work)


def gate_order_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        steps = payload["steps"]
        wanted = payload["required_order"]
        if len(set(wanted)) != len(wanted):
            return {"state": "INVALID_INPUT", "field": "required_order", "message": "gate names must be unique"}
        for field, names in (("steps", steps), ("required_order", wanted)):
            if any("," in name for name in names):
                return {"state": "INVALID_INPUT", "field": field, "message": "gate names must not contain commas"}
        first: dict[str, int] = {}
        for index, name in enumerate(steps):
            first.setdefault(name, index)
        missing = [name for name in wanted if name not in first]
        out_of_order = []
        last = -1
        for name in wanted:
            if name not in first:
                continue
            if first[name] < last:
                out_of_order.append(name)
            last = max(last, first[name])
        return {
            "state": "SUCCESS",
            "ordered": not missing and not out_of_order,
            "missing": ",".join(missing),
            "out_of_order": ",".join(out_of_order),
        }

    return respond(payload, work)


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


_EPOCH = re.compile(r"[0-9]{9,11}(?:\.[0-9]+)?\Z")


def _instant(text: str) -> datetime | None:
    """Parse a verdict timestamp: ISO 8601 with a UTC offset (Z allowed), or epoch seconds as a string.

    Epoch seconds are what the signed gate envelopes carry in ``payload.issued_at``.
    """
    value = text.strip()
    if _EPOCH.match(value):
        return datetime.fromtimestamp(float(value), timezone.utc)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


_MAX_AGE_HOURS = 87600
_CLOCK_SKEW = timedelta(seconds=1)


def gate_freshness(payload: dict[str, Any]) -> dict[str, Any]:
    """Gates whose signed verdict is older than its maximum age, waived without an approval id, or failed."""

    def work() -> dict[str, Any]:
        now = _instant(payload["now"])
        if now is None:
            return _invalid("now", "now must be an ISO 8601 timestamp with a UTC offset (2026-08-29T09:41:00Z) or epoch seconds as a string")
        limits = payload["max_age_h"]
        for gate, hours in limits.items():
            if not _finite(hours) or hours <= 0 or hours > _MAX_AGE_HOURS:
                return _invalid("max_age_h", f"max age for gate {gate!r} must be a number of hours above 0 and at most {_MAX_AGE_HOURS}")
        approvals: dict[str, str] = {}
        for index, waiver in enumerate(payload.get("waivers", [])):
            approval = waiver["approval_id"].strip()
            if not approval:
                return _invalid("waivers", f"waiver {index} has a blank approval_id")
            approvals[waiver["gate"]] = approval
        seen: set[str] = set()
        fresh: list[str] = []
        expired: list[str] = []
        unwaived: list[str] = []
        failed: list[str] = []
        for index, entry in enumerate(payload["verdicts"]):
            gate = entry["gate"]
            if "," in gate:
                return _invalid("verdicts", f"verdict {index}: gate names must not contain commas")
            if gate in seen:
                return _invalid("verdicts", f"gate {gate!r} appears twice; pass only its latest verdict")
            seen.add(gate)
            if gate not in limits:
                return _invalid("max_age_h", f"no maximum age supplied for gate {gate!r}")
            issued = _instant(entry["issued_at"])
            if issued is None:
                return _invalid("verdicts", f"verdict {index}: issued_at must be an ISO 8601 timestamp with a UTC offset or epoch seconds as a string")
            # `now` may come from a whole-second clock while issued_at has a fraction: allow one second.
            if issued > now + _CLOCK_SKEW:
                return _invalid("verdicts", f"verdict {index}: issued_at is after now")
            # Exactly at the limit still counts as fresh (a security verdict must be at most 24 h old).
            (expired if now - issued > timedelta(hours=limits[gate]) else fresh).append(gate)
            if entry["verdict"] == "waive" and gate not in approvals:
                unwaived.append(gate)
            if entry["verdict"] == "fail":
                failed.append(gate)
        return {
            "state": "SUCCESS",
            "fresh": ",".join(fresh),
            "expired": ",".join(expired),
            "unwaived": ",".join(unwaived),
            "failed": ",".join(failed),
            "ok": not (expired or unwaived or failed),
        }

    return respond(payload, work)


def _actor(identity: str) -> str:
    """Strip an instance suffix: A12@local is A12."""
    return identity.strip().partition("@")[0]


def _is_authority(identity: str) -> bool:
    actor = _actor(identity)
    return actor == "A13" or (actor.startswith("human:") and len(actor) > len("human:"))


def freeze_decision(payload: dict[str, Any]) -> dict[str, Any]:
    """Freeze from incident alerts (severity 2 or worse, sev1 is worst) and judge an unfreeze request."""

    def work() -> dict[str, Any]:
        release_id = payload["release_id"]
        unfreeze_by = payload.get("unfreeze_by")
        frozen_by = payload.get("frozen_by")
        if unfreeze_by is not None and frozen_by is None:
            return _invalid("frozen_by", "frozen_by is required to judge an unfreeze request")
        for index, alert in enumerate(payload["alerts"]):
            if "," in alert["id"]:
                return _invalid("alerts", f"alert {index}: ids must not contain commas")
        severities = [alert["severity"] for alert in payload["alerts"]]
        worst = min(severities) if severities else 0
        freezing = [alert for alert in payload["alerts"] if alert["severity"] <= 2]
        matched = [a["id"] for a in freezing if a.get("release_id", release_id) in ("", release_id)]
        reasons: list[str] = []
        if not freezing:
            reasons.append("no alert at severity 2 or worse")
        elif matched:
            reasons.append(f"sev<=2 alert(s) {','.join(matched)} name {release_id} or the whole swarm")
        else:
            others = ",".join(a["id"] for a in freezing)
            reasons.append(f"swarm-wide freeze: sev<=2 alert(s) {others} name other releases")
        allowed = False
        if unfreeze_by is None:
            reasons.append("no unfreeze requested")
        elif not _is_authority(unfreeze_by):
            reasons.append(f"{_actor(unfreeze_by)} is not a declaring authority (A13 or human:<id>)")
        elif _actor(unfreeze_by).startswith("human:"):
            allowed = True
            reasons.append("a human may lift any freeze")
        elif frozen_by is not None and _is_authority(frozen_by) and _actor(frozen_by) != _actor(unfreeze_by):
            reasons.append(f"the freeze was declared by {_actor(frozen_by)}; only that authority may lift it")
        else:
            allowed = True
            reasons.append(f"{_actor(unfreeze_by)} is the declaring authority")
        return {
            "state": "SUCCESS",
            "freeze": bool(freezing),
            "worst_sev": worst,
            "matched": ",".join(matched),
            "unfreeze_allowed": allowed,
            "reason": "; ".join(reasons),
        }

    return respond(payload, work)


def resume_point(payload: dict[str, Any]) -> dict[str, Any]:
    """Next action for an interrupted canary: resume, roll back, or complete."""

    def work() -> dict[str, Any]:
        steps = payload["steps_pct"]
        if any(later <= earlier for earlier, later in zip(steps, steps[1:])):
            return _invalid("steps_pct", "steps must be strictly increasing")
        if steps[-1] != 100:
            return _invalid("steps_pct", "the last step must be 100")
        confirmed = payload["confirmed"]
        in_flight = payload.get("in_flight")
        if payload["breach"]:
            return {"state": "SUCCESS", "action": "rollback", "next_pct": 0, "reason": "guardrail breach observed: roll back, never resume"}
        if confirmed != steps[: len(confirmed)]:
            return {
                "state": "SUCCESS",
                "action": "rollback",
                "next_pct": 0,
                "reason": "confirmed steps are not an in-order prefix of the plan: roll back cleanly",
            }
        if len(confirmed) == len(steps):
            if in_flight is not None:
                return {"state": "SUCCESS", "action": "rollback", "next_pct": 0, "reason": f"step {in_flight} is in flight but every step is confirmed: roll back cleanly"}
            return {"state": "SUCCESS", "action": "complete", "next_pct": 100, "reason": "every step is confirmed"}
        following = steps[len(confirmed)]
        if in_flight is not None and in_flight != following:
            return {
                "state": "SUCCESS",
                "action": "rollback",
                "next_pct": 0,
                "reason": f"step {in_flight} is in flight but the next unconfirmed step is {following}: roll back cleanly",
            }
        if in_flight is not None:
            reason = f"step {following} was in flight and is unconfirmed: re-run it"
        elif confirmed:
            reason = f"last confirmed step is {confirmed[-1]}: continue at {following}"
        else:
            reason = f"no step is confirmed: start at {following}"
        return {"state": "SUCCESS", "action": "resume", "next_pct": following, "reason": reason}

    return respond(payload, work)


_GUARDRAIL_PAIRS = (("error_rate", "error_rate_max"), ("p95_ms", "p95_ms_max"), ("slo_burn", "slo_burn_max"))


def guardrail_breaches(payload: dict[str, Any]) -> dict[str, Any]:
    """Guardrails one canary sample breaches (strictly above its limit), and metrics the sample lacks."""

    def work() -> dict[str, Any]:
        observed = payload["observed"]
        limits = payload["limits"]
        for name, value in [*observed.items(), *limits.items()]:
            if not _finite(value) or value < 0:
                return _invalid("observed" if name in observed else "limits", f"{name} must be a finite number of at least 0")
        breaches = [metric for metric, limit in _GUARDRAIL_PAIRS if metric in observed and observed[metric] > limits[limit]]
        missing = [metric for metric, _limit in _GUARDRAIL_PAIRS if metric not in observed]
        return {
            "state": "SUCCESS",
            "breaches": ",".join(breaches),
            "breached": bool(breaches),
            "missing_metrics": ",".join(missing),
        }

    return respond(payload, work)
