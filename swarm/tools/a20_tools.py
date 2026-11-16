"""A20 FinOps & GreenOps tools: pure cost, budget and carbon analyses over supplied data.

A20 recommends and prices; it never buys commitments (L4), never resizes production (A11/A21 apply, L3) and never
deletes resources. Every handler is a deterministic READ-tier calculation: money and ratios are exact `Fraction`
arithmetic over the numbers as written (Decimal repr), rounded once for the output, so documented boundaries
(`>=` versus `>`) are exact. No price list is embedded: unit prices, rate cards, tier tables and grid intensities are
inputs, because they change by provider, region and contract.

Tools:
  finops_unit_cost            unit economics: cost per request/user/transaction, margin against revenue per unit
  finops_rightsize            CPU/memory request recommendation from p95/p99 usage with headroom, priced per month
  finops_commitment_coverage  savings-plan style coverage, utilization, break-even and baseline commitment
  finops_budget_burn          spend-to-date against a budget: run rate, projected total, overrun instant
  finops_spend_anomaly        modified z-score (MAD) anomaly detection over a daily spend series
  finops_tag_coverage         allocation-tag coverage, cost-weighted coverage and per-resource violations
  finops_idle_resources       trailing-run idle detection under a utilization threshold
  finops_storage_tiering      cheapest eligible storage tier per dataset net of transition cost
  finops_egress_cost          cross-AZ, cross-region, internet (tiered) and NAT processing transfer cost
  finops_spot_suitability     spot/preemptible fit: interruption overhead versus savings and fault tolerance
  finops_carbon_estimate      location-based operational emissions (energy x PUE x grid intensity), region comparison
  finops_showback             exact-to-the-cent shared-cost allocation: proportional, even or usage-based

Reference constants (public sources, checked 2026-10-20) and what is house policy:
  * 0.6745 and the cut-off |M| > 3.5 of the modified z-score: Iglewicz and Hoaglin (1993), restated in the NIST/SEMATECH
    e-Handbook of Statistical Methods, section 1.3.5.17. The fallback for MAD = 0, M = (x - median) / (1.253314 x
    MeanAD) with MeanAD the mean absolute deviation about the median, is IBM's convention (US patent 9,348,887; the IBM
    Cognos Analytics modified z-score page), not part of Iglewicz and Hoaglin; 1.253314 is sqrt(pi/2).
  * 730 hours per month: the AWS Pricing Calculator assumption (365 x 24 / 12, no leap years).
  * 15 % default CPU/memory headroom: the default safety margin of the Kubernetes Vertical Pod Autoscaler recommender
    (--recommendation-margin-fraction, 0.15 in vertical-pod-autoscaler/pkg/recommender/config/config.go).
  * PUE: total data-centre energy divided by IT equipment energy (ISO/IEC 30134-2; the calculated value is always
    greater than 1, the schema accepts 1 as its lower bound); emissions are location-based operational (use-phase)
    emissions in the sense of the GHG Protocol Scope 2 Guidance; embodied carbon is excluded.
  * House policy, not a provider rule or a standard: the 90 % / 70 % commitment utilization / coverage floors, the 95 %
    tag coverage floor, the 60 % unit-cost concentration note, the 25 % target overshoot that makes a unit-cost finding
    major, the 30 % spot savings floor, the 20 % spot interruption ceiling and on-demand floor, the 10 % carbon
    relocation threshold, the 90 % budget at_risk line and the 50 % dominant-category lines.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import Any, NoReturn, SupportsFloat

from swarm.tools.support import respond

HOURS_PER_MONTH = Fraction(730)
DEFAULT_HEADROOM_PCT = 15
DEFAULT_TOLERANCE_PCT = 10
MODIFIED_Z_FACTOR = Fraction("0.6745")
MEAN_AD_FACTOR = Fraction("1.253314")
DEFAULT_ANOMALY_THRESHOLD = 3.5
MAX_FINDINGS = 100
TOP_FLOWS = 10
DAY_SECONDS = 86400
MONTH_DAYS = 30  # storage retention is expressed in days; a billing month is 30 days for the payback arithmetic

RECOMMENDATION_BY_KIND = {
    "vm": "schedule_or_stop",
    "database": "snapshot_and_stop",
    "volume": "snapshot_and_review",
    "load_balancer": "review_targets",
    "ip_address": "review_attachment",
}


class _Bad(Exception):
    """A semantic input problem the schema cannot express; carries the offending top-level input field."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def _bad(field: str, message: str) -> NoReturn:
    raise _Bad(field, message)


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _guard(work: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    """Turn a `_bad` raised anywhere inside `work` into the INVALID_INPUT shape. A result too large for a float (a
    volume of 1e-300, say) is also an input problem, not a dependency failure."""

    def run() -> dict[str, Any]:
        try:
            return work()
        except _Bad as exc:
            return _invalid(exc.field, exc.message)
        except (OverflowError, ZeroDivisionError):
            return _invalid("payload", "a result is outside the representable range: check the magnitudes and units of the inputs")

    return run


def _fr(value: object) -> Fraction:
    """Exact value of the number as written: 0.1 is 1/10, not its binary neighbour."""
    return Fraction(Decimal(repr(value)))


def _r(value: SupportsFloat, digits: int = 4) -> float:
    return round(float(value), digits)


def _num(value: SupportsFloat) -> str:
    """Six significant figures, never exponent form: a money amount above 1e6 prints as digits, not 1e+06."""
    number = float(value)
    text = f"{number:.6g}"
    if "e" not in text:
        return text
    if abs(number) >= 1:
        return f"{number:.0f}"
    return format(Decimal(text), "f")


def _finding(fid: str, severity: str, summary: str) -> dict[str, Any]:
    return {"id": fid, "severity": severity, "summary": summary}


_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "info": 3}


def _cap(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At most MAX_FINDINGS findings, most severe first (stable within a severity). When some are dropped the last slot
    says how many, so a truncated list is never mistaken for a complete one."""
    ordered = sorted(findings, key=lambda item: _SEVERITY_ORDER[item["severity"]])
    if len(ordered) <= MAX_FINDINGS:
        return ordered
    kept = ordered[:MAX_FINDINGS - 1]
    kept.append(_finding("TRUNCATED", "info", f"{len(ordered) - len(kept)} further finding(s) omitted; the per-record fields hold every result"))
    return kept


def _blank(text: str) -> bool:
    return not text.strip()


def _seconds(delta: timedelta) -> Fraction:
    return Fraction(delta // timedelta(microseconds=1), 1_000_000)


def _instant(field: str, value: str) -> datetime:
    """The input text is never echoed back: only the field and the rule it broke."""
    text = value.strip()
    text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _bad(field, f"{field} must be an ISO-8601 timestamp with an explicit offset (for example 2026-10-01T00:00:00Z)")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return _bad(field, f"{field} must carry an explicit UTC offset (for example 2026-10-01T00:00:00Z)")
    try:
        return parsed.astimezone(timezone.utc)
    except OverflowError:
        return _bad(field, f"{field} is outside the supported calendar range once converted to UTC")


def _iso_utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(field: str, value: str) -> date:
    """The input text is never echoed back: only the field and the rule it broke."""
    shape_ok = len(value) == 10 and value[4] == "-" and value[7] == "-" and (value[:4] + value[5:7] + value[8:]).isdigit()
    if not shape_ok:
        return _bad(field, f"{field} must be a calendar date YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        return _bad(field, f"{field} must be a real calendar date YYYY-MM-DD")


def _median(values: list[Fraction]) -> Fraction:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def _unique(field: str, names: list[str], what: str) -> None:
    """Reject a repeated identifier by position, not by value."""
    seen: dict[str, int] = {}
    for index, name in enumerate(names):
        if name in seen:
            _bad(field, f"duplicate {what}: entry {index} repeats entry {seen[name]}")
        seen[name] = index


# --- unit economics --------------------------------------------------------------------------------------------


def unit_cost(payload: dict[str, Any]) -> dict[str, Any]:
    """Total cost over volume: unit cost, cost per 1000 units, dominant category and margin against revenue per unit.

    Verdict: loss_making (unit cost > revenue per unit), else over_target (unit cost > target), within_target
    (unit cost <= target) or no_target; the verdict uses the exact quotient, while `unit_cost` is printed rounded to
    8 decimals and `cost_per_1000_units` to 6. A target overshoot above 25 % is major, otherwise minor (house policy)."""

    def work() -> dict[str, Any]:
        totals: dict[str, Fraction] = {}
        for index, item in enumerate(payload["costs"]):
            category = item["category"].strip()
            if not category:
                _bad("costs", f"costs[{index}].category must not be blank")
            totals[category] = totals.get(category, Fraction(0)) + _fr(item["amount"])
        total = sum(totals.values(), Fraction(0))
        unit = payload["unit"]
        per_unit = total / _fr(payload["volume"])
        top_category, top_share = "", Fraction(0)
        if total > 0:
            top_category = min(totals, key=lambda name: (-totals[name], name))
            top_share = totals[top_category] / total
        findings: list[dict[str, Any]] = []
        verdict = "no_target"
        margin_known, margin = False, Fraction(0)
        if "revenue_per_unit" in payload:
            revenue = _fr(payload["revenue_per_unit"])
            margin_known, margin = True, (revenue - per_unit) / revenue * 100
            if per_unit > revenue:
                verdict = "loss_making"
                findings.append(_finding("UC-LOSS", "major", f"cost per {unit} {_num(per_unit)} exceeds revenue per {unit} {_num(revenue)}"))
        if "target_unit_cost" in payload:
            target = _fr(payload["target_unit_cost"])
            over = per_unit > target
            if verdict != "loss_making":
                verdict = "over_target" if over else "within_target"
            if over:
                severity = "major" if per_unit > target * Fraction(5, 4) else "minor"
                findings.append(_finding("UC-TARGET", severity, f"cost per {unit} {_num(per_unit)} is above the target {_num(target)}"))
        if total == 0:
            findings.append(_finding("UC-NO-SPEND", "info", "no spend supplied: unit cost is zero"))
        elif top_share >= Fraction(3, 5):
            findings.append(_finding("UC-CONCENTRATION", "info", f"category {top_category} is {_num(top_share * 100)}% of the cost"))
        return {
            "state": "SUCCESS",
            "total_cost": _r(total),
            "unit_cost": _r(per_unit, 8),
            "cost_per_1000_units": _r(per_unit * 1000, 6),
            "top_category": top_category,
            "top_category_share_pct": _r(top_share * 100),
            "margin_known": margin_known,
            "gross_margin_pct": _r(margin) if margin_known else 0.0,
            "verdict": verdict,
            "findings": findings,
        }

    return respond(payload, _guard(work))


# --- rightsizing -----------------------------------------------------------------------------------------------


def _resize_action(request: Fraction, recommended: Fraction, tolerance: Fraction) -> str:
    if recommended > request * (1 + tolerance):
        return "upsize"
    if recommended < request * (1 - tolerance):
        return "downsize"
    return "keep"


def rightsize(payload: dict[str, Any]) -> dict[str, Any]:
    """Recommend per-replica CPU and memory requests from observed usage.

    CPU = ceil(p95 or p99 x (1 + headroom)); memory = ceil(p99 x (1 + headroom)) because memory is not compressible
    (the kernel evicts or OOM-kills instead of throttling). A change inside +-tolerance (inclusive) is `keep`, except
    a memory p99 above its request, which is always an upsize. A CPU downsize is withheld while the p99 exceeds the
    current request. A dimension whose action is keep (inside the band
    or withheld) reports its current request, rounded up to a whole unit, as the recommendation, so applying every
    `*_recommended` field never applies a change the action does not name. There is no floor above 1 m / 1 MiB:
    callers apply their own platform minimums. Saving = replicas x hours x unit price x delta; positive for
    downsizing, negative for upsizing. Nothing is applied: A11/A21 apply the recommendation at L3."""

    def work() -> dict[str, Any]:
        cpu_basis = payload["cpu_basis"]
        cpu_headroom = 1 + _fr(payload.get("cpu_headroom_pct", DEFAULT_HEADROOM_PCT)) / 100
        mem_headroom = 1 + _fr(payload.get("mem_headroom_pct", DEFAULT_HEADROOM_PCT)) / 100
        tolerance = _fr(payload.get("tolerance_pct", DEFAULT_TOLERANCE_PCT)) / 100
        hours = _fr(payload.get("hours_per_month", 730))
        cpu_price, mem_price = _fr(payload["cpu_price_per_core_hour"]), _fr(payload["mem_price_per_gib_hour"])
        _unique("workloads", [w["name"] for w in payload["workloads"]], "workload name")
        records, findings = [], []
        counts = {"downsize": 0, "upsize": 0, "mixed": 0, "keep": 0}
        gross, extra = Fraction(0), Fraction(0)
        for index, w in enumerate(payload["workloads"]):
            name = w["name"]
            if w["cpu_p95_m"] > w["cpu_p99_m"]:
                _bad("workloads", f"workloads[{index}]: cpu_p95_m must not exceed cpu_p99_m")
            if w["mem_p95_mib"] > w["mem_p99_mib"]:
                _bad("workloads", f"workloads[{index}]: mem_p95_mib must not exceed mem_p99_mib")
            cpu_req, mem_req = _fr(w["cpu_request_m"]), _fr(w["mem_request_mib"])
            cpu_p99, mem_p99 = _fr(w["cpu_p99_m"]), _fr(w["mem_p99_mib"])
            cpu_usage = _fr(w["cpu_p95_m"] if cpu_basis == "p95" else w["cpu_p99_m"])
            cpu_rec = Fraction(max(1, math.ceil(cpu_usage * cpu_headroom)))
            mem_rec = Fraction(max(1, math.ceil(mem_p99 * mem_headroom)))
            cpu_action = _resize_action(cpu_req, cpu_rec, tolerance)
            mem_action = _resize_action(mem_req, mem_rec, tolerance)
            if mem_action == "keep" and mem_p99 > mem_req:
                mem_action = "upsize"
            if cpu_action == "downsize" and cpu_p99 > cpu_req:
                cpu_action = "keep"
                findings.append(_finding(f"RS-{name}-cpu-peak", "minor", f"{name}: cpu downsize withheld, p99 {_num(cpu_p99)}m is above the request {_num(cpu_req)}m"))
            elif cpu_p99 > cpu_req:
                findings.append(_finding(f"RS-{name}-cpu-peak", "minor", f"{name}: cpu p99 {_num(cpu_p99)}m is above the request {_num(cpu_req)}m (relies on burst capacity)"))
            if mem_p99 > mem_req:
                findings.append(_finding(f"RS-{name}-mem-peak", "major", f"{name}: memory p99 {_num(mem_p99)}MiB is above the request {_num(mem_req)}MiB (eviction candidate under node pressure)"))
            actions = {cpu_action, mem_action} - {"keep"}
            action = "keep" if not actions else (next(iter(actions)) if len(actions) == 1 else "mixed")
            saving = Fraction(0)
            replicas = Fraction(w["replicas"])
            deltas = (
                ("cpu", cpu_action, cpu_req, cpu_rec, "m", cpu_price / 1000),
                ("mem", mem_action, mem_req, mem_rec, "MiB", mem_price / 1024),
            )
            for resource, act, req, rec, unit_name, price_per_unit in deltas:
                if act == "keep":
                    continue
                delta = (req - rec) * price_per_unit * replicas * hours
                saving += delta
                if delta > 0:
                    gross += delta
                else:
                    extra -= delta
                severity = "minor" if act == "downsize" else "major"
                findings.append(_finding(f"RS-{name}-{resource}", severity, f"{name}: {resource} request {_num(req)}{unit_name} -> {_num(rec)}{unit_name} ({act})"))
            counts[action] += 1
            records.append({
                "name": name,
                "action": action,
                "cpu_request_m": _r(cpu_req),
                "cpu_recommended_m": int(cpu_rec if cpu_action != "keep" else math.ceil(cpu_req)),
                "mem_request_mib": _r(mem_req),
                "mem_recommended_mib": int(mem_rec if mem_action != "keep" else math.ceil(mem_req)),
                "monthly_saving": _r(saving, 2),
            })
        changed = len(records) - counts["keep"]
        return {
            "state": "SUCCESS",
            "workload_count": len(records),
            "downsize_count": counts["downsize"],
            "upsize_count": counts["upsize"],
            "mixed_count": counts["mixed"],
            "keep_count": counts["keep"],
            "monthly_saving_gross": _r(gross, 2),
            "monthly_extra_cost": _r(extra, 2),
            "monthly_saving_net": _r(gross - extra, 2),
            "verdict": "rightsizing_needed" if changed else "right_sized",
            "workloads": records,
            "findings": _cap(findings),
        }

    return respond(payload, _guard(work))


# --- commitments -----------------------------------------------------------------------------------------------


def commitment_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Savings-plan style commitment of `commitment` discounted spend per period at `discount_pct` off on-demand.

    A commitment covers up to cap = commitment / (1 - discount) of on-demand-equivalent usage per period.
    coverage = covered / total usage; utilization = covered / (cap x periods); net savings = covered - commitment x
    periods, which is zero exactly at utilization = 1 - discount (the break-even). Verdict: under_utilized (utilization
    < min_utilization_pct, default 90, or below the break-even whichever is higher: a commitment that costs more than
    on-demand is never within_band), else under_covered (coverage < min_coverage_pct, default 70), else within_band.
    The 90 / 70 floors are house policy defaults, not provider rules. The baseline suggestion is the nearest-rank
    10th percentile of usage x (1 - discount). A20 proposes;
    the purchase is an L4 human decision."""

    def work() -> dict[str, Any]:
        usage = [_fr(u) for u in payload["usage"]]
        commitment = _fr(payload["commitment"])
        retained = 1 - _fr(payload["discount_pct"]) / 100
        min_util = _fr(payload.get("min_utilization_pct", 90))
        min_cov = _fr(payload.get("min_coverage_pct", 70))
        periods = len(usage)
        cap = commitment / retained
        covered = sum((min(u, cap) for u in usage), Fraction(0))
        total = sum(usage, Fraction(0))
        coverage = covered / total * 100 if total > 0 else Fraction(0)
        utilization = covered / (cap * periods) * 100
        paid = commitment * periods
        net = covered - paid
        unused = paid - covered * retained
        baseline = sorted(usage)[math.ceil(Fraction(periods, 10)) - 1] * retained
        findings: list[dict[str, Any]] = []
        breakeven_pct = retained * 100
        if utilization < min_util:
            verdict = "under_utilized"
            findings.append(_finding("CC-UTIL", "major", f"utilization {_num(utilization)}% is below {_num(min_util)}%: {_num(unused)} of commitment unused"))
        elif utilization < breakeven_pct:
            verdict = "under_utilized"
        elif coverage < min_cov:
            verdict = "under_covered"
            findings.append(_finding("CC-COVERAGE", "minor", f"coverage {_num(coverage)}% is below {_num(min_cov)}%: more baseline usage could be committed"))
        else:
            verdict = "within_band"
        if utilization < breakeven_pct:
            findings.append(_finding("CC-BREAKEVEN", "blocker", f"utilization {_num(utilization)}% is below the break-even {_num(breakeven_pct)}%: the commitment costs more than on-demand"))
        return {
            "state": "SUCCESS",
            "period_count": periods,
            "coverage_pct": _r(coverage),
            "utilization_pct": _r(utilization),
            "breakeven_utilization_pct": _r(retained * 100),
            "net_savings": _r(net, 2),
            "unused_commitment": _r(unused, 2),
            "uncovered_on_demand": _r(total - covered, 2),
            "suggested_commitment": _r(baseline, 4),
            "verdict": verdict,
            "findings": findings,
        }

    return respond(payload, _guard(work))


# --- budget ----------------------------------------------------------------------------------------------------


def budget_burn(payload: dict[str, Any]) -> dict[str, Any]:
    """Spend-to-date against a budget for the period [period_start, period_end].

    Run rate per day = mean of `recent_daily_spend` when supplied, else spend_to_date / elapsed days. Projected total
    = spend + run rate x remaining days. Verdict: over_budget (spend >= budget), will_overrun (projected > budget),
    at_risk (projected >= 90 % of budget), else on_track. `overrun_at` is the UTC instant the cumulative spend reaches
    the budget at the run rate ("" when no overrun is projected or the budget is already spent);
    `days_until_overrun` is -1 when no overrun is projected and 0 when already over."""

    def work() -> dict[str, Any]:
        start = _instant("period_start", payload["period_start"])
        end = _instant("period_end", payload["period_end"])
        as_of = _instant("as_of", payload["as_of"])
        if end <= start:
            _bad("period_end", "period_end must be after period_start")
        if as_of <= start:
            _bad("as_of", "as_of must be after period_start")
        if as_of > end:
            _bad("as_of", "as_of must not be after period_end")
        budget, spend = _fr(payload["budget"]), _fr(payload["spend_to_date"])
        total_s, elapsed_s, remaining_s = _seconds(end - start), _seconds(as_of - start), _seconds(end - as_of)
        elapsed_days, remaining_days = elapsed_s / DAY_SECONDS, remaining_s / DAY_SECONDS
        recent = payload.get("recent_daily_spend")
        if recent is not None:
            rate, source = sum((_fr(v) for v in recent), Fraction(0)) / len(recent), "recent_daily_spend"
        else:
            rate, source = spend / elapsed_days, "period_average"
        projected = spend + rate * remaining_days
        used_pct, elapsed_pct = spend / budget * 100, elapsed_s / total_s * 100
        overrun_amount = max(Fraction(0), projected - budget)
        overrun_at, days_until = "", Fraction(-1)
        findings: list[dict[str, Any]] = []
        if spend >= budget:
            verdict, days_until = "over_budget", Fraction(0)
            findings.append(_finding("BB-OVER", "major", f"spend {_num(spend)} has reached or exceeded the budget {_num(budget)}"))
        elif projected > budget:
            verdict = "will_overrun"
            days_until = (budget - spend) / rate
            overrun_at = _iso_utc(as_of + timedelta(seconds=math.floor(days_until * DAY_SECONDS)))
            findings.append(_finding("BB-OVERRUN", "major", f"projected {_num(projected)} exceeds the budget {_num(budget)}; budget reached {overrun_at}"))
        elif projected * 10 >= budget * 9:
            verdict = "at_risk"
            findings.append(_finding("BB-RISK", "minor", f"projected {_num(projected)} is {_num(projected / budget * 100)}% of the budget"))
        else:
            verdict = "on_track"
        return {
            "state": "SUCCESS",
            "pct_of_budget_used": _r(used_pct),
            "pct_of_period_elapsed": _r(elapsed_pct),
            "burn_ratio": _r(used_pct / elapsed_pct),
            "run_rate_per_day": _r(rate, 4),
            "run_rate_source": source,
            "projected_total": _r(projected, 2),
            "projected_overrun_amount": _r(overrun_amount, 2),
            "overrun_at": overrun_at,
            "days_until_overrun": _r(days_until),
            "verdict": verdict,
            "findings": findings,
        }

    return respond(payload, _guard(work))


# --- anomalies -------------------------------------------------------------------------------------------------


def spend_anomaly(payload: dict[str, Any]) -> dict[str, Any]:
    """Modified z-score anomalies over a daily series: M = 0.6745 (x - median) / MAD, flagged when |M| > threshold
    (default 3.5, Iglewicz and Hoaglin). When the MAD is 0 (more than half the days are identical) the mean absolute
    deviation about the median is used instead, M = (x - median) / (1.253314 x meanAD): IBM's convention, `scale_method`
    says `mean_ad`; a constant series has no anomalies (`constant`). The series is one value per consecutive day: the
    dates are start_date + index. Excess spend = sum of (value - median) over the flagged upward anomalies only; days
    inside the threshold and downward days add nothing. Upward anomalies at
    |M| >= 2 x threshold are major, other upward ones minor and downward ones info."""

    def work() -> dict[str, Any]:
        start = _day("start_date", payload["start_date"])
        try:
            (start + timedelta(days=len(payload["daily_spend"]) - 1)).isoformat()
        except OverflowError:
            _bad("start_date", "start_date plus the series length must stay within the calendar range")
        threshold = _fr(payload.get("threshold", DEFAULT_ANOMALY_THRESHOLD))
        values = [_fr(v) for v in payload["daily_spend"]]
        median = _median(values)
        deviations = [abs(v - median) for v in values]
        mad = _median(deviations)
        if mad > 0:
            method, scale, factor = "mad", mad, MODIFIED_Z_FACTOR
        else:
            mean_ad = sum(deviations, Fraction(0)) / len(values)
            if mean_ad > 0:
                method, scale, factor = "mean_ad", mean_ad * MEAN_AD_FACTOR, Fraction(1)
            else:
                method, scale, factor = "constant", Fraction(0), Fraction(1)
        anomalies, findings = [], []
        excess = Fraction(0)
        if scale > 0:
            for index, value in enumerate(values):
                z = factor * (value - median) / scale
                if abs(z) > threshold:
                    up = value > median
                    when = (start + timedelta(days=index)).isoformat()
                    if up:
                        excess += value - median
                    severity = ("major" if abs(z) >= threshold * 2 else "minor") if up else "info"
                    findings.append(_finding(f"SA-{when}", severity, f"{when}: spend {_num(value)} is {'above' if up else 'below'} the median {_num(median)} (z={_num(z)})"))
                    anomalies.append({"index": index, "date": when, "value": _r(value, 4), "z_score": _r(z, 3), "direction": "up" if up else "down"})
        return {
            "state": "SUCCESS",
            "day_count": len(values),
            "median": _r(median),
            "mad": _r(mad),
            "scale_method": method,
            "anomaly_count": len(anomalies),
            "spike_count": sum(1 for a in anomalies if a["direction"] == "up"),
            "excess_spend": _r(excess, 2),
            "anomalies": anomalies,
            "verdict": "anomalous" if anomalies else "normal",
            "findings": findings,
        }

    return respond(payload, _guard(work))


# --- allocation tags -------------------------------------------------------------------------------------------

PROBLEM_TYPES = ("case_mismatch", "duplicate_key", "empty_value", "invalid_value", "missing")
BLOCKING_PROBLEMS = ("case_mismatch", "empty_value", "missing")


def _tag_problems(tags: list[dict[str, Any]], required: list[str], allowed: dict[str, list[str]]) -> list[tuple[str, str]]:
    """(problem, key) pairs for one resource. Tag keys are compared case-sensitively (the AWS rule; Azure tag names and
    GCP labels follow other rules, so a caller on those clouds must normalise first): `Owner` does not satisfy a
    required `owner` and is reported as case_mismatch. Only required keys are judged; a repeated key is judged on
    every one of its values, so a blank or invalid duplicate is not hidden behind a valid first value."""
    by_key: dict[str, list[str]] = {}
    for tag in tags:
        by_key.setdefault(tag["key"], []).append(tag["value"])
    folded = {key.lower() for key in by_key}
    problems: list[tuple[str, str]] = []
    for key in required:
        values = by_key.get(key)
        if values is None:
            problems.append(("case_mismatch" if key.lower() in folded else "missing", key))
            continue
        if len(values) > 1:
            problems.append(("duplicate_key", key))
        if any(_blank(value) for value in values):
            problems.append(("empty_value", key))
        if key in allowed and any(not _blank(value) and value not in allowed[key] for value in values):
            problems.append(("invalid_value", key))
    return problems


def tag_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Share of resources (and of their cost) carrying every required allocation tag with a valid value.

    A resource is compliant when each required key is present with that exact spelling, has a non-blank value and,
    where `allowed_values` lists the key, one of its values. Problems: missing, case_mismatch, empty_value,
    invalid_value, duplicate_key. Verdict `compliant` when coverage >= min_coverage_pct (default 95, a house policy
    default). One finding per non-compliant resource (highest cost first, at most 100): major when allocation is
    impossible (missing, case_mismatch, empty_value), else minor. A finding lists at most 5 problems (the
    `problem_counts` cover all of them). Tag values are never echoed."""

    def work() -> dict[str, Any]:
        resources = payload["resources"]
        _unique("resources", [r["id"] for r in resources], "resource id")
        required = payload["required_keys"]
        _unique("required_keys", required, "required key")
        allowed: dict[str, list[str]] = {}
        for index, rule in enumerate(payload.get("allowed_values", [])):
            if rule["key"] not in required:
                _bad("allowed_values", f"allowed_values[{index}].key is not one of required_keys")
            if rule["key"] in allowed:
                _bad("allowed_values", f"allowed_values[{index}].key repeats an earlier allowed_values key")
            allowed[rule["key"]] = list(rule["values"])
        min_cov = _fr(payload.get("min_coverage_pct", 95))
        total_cost = compliant_cost = Fraction(0)
        compliant = 0
        counts = {name: 0 for name in PROBLEM_TYPES}
        offenders = []
        for resource in resources:
            cost = _fr(resource["monthly_cost"])
            total_cost += cost
            problems = _tag_problems(resource["tags"], required, allowed)
            if not problems:
                compliant += 1
                compliant_cost += cost
                continue
            for problem, _ in problems:
                counts[problem] += 1
            offenders.append((cost, resource["id"], problems))
        count = len(resources)
        coverage = Fraction(compliant, count) * 100
        cost_coverage = compliant_cost / total_cost * 100 if total_cost > 0 else coverage
        offenders.sort(key=lambda item: (-item[0], item[1]))
        findings = []
        for cost, rid, problems in offenders[:MAX_FINDINGS]:
            severity = "major" if any(p in BLOCKING_PROBLEMS for p, _ in problems) else "minor"
            ordered = sorted(problems)
            detail = "; ".join(f"{p}: {k}" for p, k in ordered[:5]) + (f"; (+{len(ordered) - 5} more)" if len(ordered) > 5 else "")
            findings.append(_finding(rid, severity, f"{rid} ({_num(cost)}/month) {detail}"))
        return {
            "state": "SUCCESS",
            "resource_count": count,
            "compliant_count": compliant,
            "coverage_pct": _r(coverage),
            "cost_coverage_pct": _r(cost_coverage),
            "untagged_cost": _r(total_cost - compliant_cost, 2),
            "problem_counts": [{"problem": name, "count": counts[name]} for name in PROBLEM_TYPES if counts[name]],
            "verdict": "compliant" if coverage >= min_cov else "non_compliant",
            "findings_truncated": len(offenders) > MAX_FINDINGS,
            "findings": findings,
        }

    return respond(payload, _guard(work))


# --- idle resources --------------------------------------------------------------------------------------------


def idle_resources(payload: dict[str, Any]) -> dict[str, Any]:
    """Resources whose utilization has been strictly below `threshold_pct` for at least `min_idle_days` consecutive
    days ending with the most recent sample (one sample per day, oldest first). A series shorter than min_idle_days is
    reported as insufficient data, never as idle. The recommendation is owner review: A20 does not delete. Severity is
    major when every sample is idle, else minor."""

    def work() -> dict[str, Any]:
        threshold = _fr(payload["threshold_pct"])
        min_days = payload["min_idle_days"]
        _unique("resources", [r["id"] for r in payload["resources"]], "resource id")
        idle, insufficient, findings = [], [], []
        idle_cost = Fraction(0)
        for resource in payload["resources"]:
            series = resource["daily_utilization_pct"]
            if len(series) < min_days:
                insufficient.append(resource["id"])
                continue
            run = 0
            for sample in reversed(series):
                if _fr(sample) < threshold:
                    run += 1
                else:
                    break
            if run < min_days:
                continue
            cost = _fr(resource["monthly_cost"])
            idle_cost += cost
            kind = resource["kind"]
            idle.append({"id": resource["id"], "kind": kind, "idle_days": run, "monthly_cost": _r(cost, 2), "recommendation": RECOMMENDATION_BY_KIND[kind]})
            findings.append(_finding(resource["id"], "major" if run == len(series) else "minor",
                                     f"{resource['id']} ({kind}) idle for {run} day(s) below {_num(threshold)}%: {_num(cost)}/month"))
        idle.sort(key=lambda item: (-item["monthly_cost"], item["id"]))
        findings.sort(key=lambda item: (item["severity"] != "major", item["id"]))
        for rid in sorted(insufficient):
            findings.append(_finding(rid, "info", f"{rid}: fewer than {min_days} daily samples, idleness cannot be judged"))
        return {
            "state": "SUCCESS",
            "resource_count": len(payload["resources"]),
            "idle_count": len(idle),
            "insufficient_data_count": len(insufficient),
            "idle_monthly_cost": _r(idle_cost, 2),
            "idle": idle,
            "verdict": "idle_found" if idle else "clean",
            "findings": _cap(findings),
        }

    return respond(payload, _guard(work))


# --- storage tiering -------------------------------------------------------------------------------------------


def storage_tiering(payload: dict[str, Any]) -> dict[str, Any]:
    """Cheapest storage tier per dataset over its remaining retention, net of the one-off transition cost.

    Monthly cost of a tier = size x storage price + monthly reads x retrieval price. A tier is eligible only when the
    expected retention is >= its minimum storage duration (no early-deletion charge). Among eligible tiers other than
    the current one the best is the one with the largest net benefit = monthly saving x retention / 30 - size x
    transition_in price; the dataset moves only when that net benefit is > 0. Prices are inputs (one rate card per
    provider and region); payback_months = transition cost / monthly saving. Not modelled: an early-deletion charge for
    leaving the current tier (the dataset's age in it is not an input), object-size minimums and retrieval latency;
    a billing month is 30 days."""

    def work() -> dict[str, Any]:
        tiers = {t["name"]: t for t in payload["tiers"]}
        if len(tiers) != len(payload["tiers"]):
            _bad("tiers", "tier names must be unique")
        _unique("datasets", [d["name"] for d in payload["datasets"]], "dataset name")
        records, findings = [], []
        saving_total = transition_total = Fraction(0)
        moves = 0

        def monthly(tier: dict[str, Any], size: Fraction, reads: Fraction) -> Fraction:
            return size * _fr(tier["storage_per_gb_month"]) + reads * _fr(tier["retrieval_per_gb"])

        for dataset_index, dataset in enumerate(payload["datasets"]):
            current = tiers.get(dataset["current_tier"])
            if current is None:
                _bad("datasets", f"datasets[{dataset_index}].current_tier is not one of the listed tiers")
            size, reads = _fr(dataset["size_gb"]), _fr(dataset["monthly_read_gb"])
            months = Fraction(dataset["retention_days"], MONTH_DAYS)
            now_cost = monthly(current, size, reads)
            best = None
            for name in sorted(tiers):
                tier = tiers[name]
                if name == current["name"] or dataset["retention_days"] < tier["min_storage_days"]:
                    continue
                saving = now_cost - monthly(tier, size, reads)
                transition = size * _fr(tier["transition_in_per_gb"])
                net = saving * months - transition
                if best is None or net > best[0]:
                    best = (net, name, saving, transition)
            if best is not None and best[0] > 0:
                net, target, saving, transition = best
                moves += 1
                saving_total += saving
                transition_total += transition
                payback = transition / saving
                records.append({
                    "name": dataset["name"], "current_tier": current["name"], "recommended_tier": target,
                    "monthly_cost_now": _r(now_cost, 2), "monthly_cost_after": _r(now_cost - saving, 2),
                    "monthly_saving": _r(saving, 2), "transition_cost": _r(transition, 2),
                    "net_saving_over_retention": _r(net, 2), "payback_months": _r(payback),
                })
                findings.append(_finding(f"ST-{dataset['name']}", "minor", f"{dataset['name']}: move {current['name']} -> {target}, saves {_num(saving)}/month, payback {_num(payback)} month(s)"))
            else:
                records.append({
                    "name": dataset["name"], "current_tier": current["name"], "recommended_tier": current["name"],
                    "monthly_cost_now": _r(now_cost, 2), "monthly_cost_after": _r(now_cost, 2),
                    "monthly_saving": 0.0, "transition_cost": 0.0, "net_saving_over_retention": 0.0, "payback_months": 0.0,
                })
        return {
            "state": "SUCCESS",
            "dataset_count": len(records),
            "move_count": moves,
            "monthly_saving_total": _r(saving_total, 2),
            "transition_cost_total": _r(transition_total, 2),
            "datasets": records,
            "verdict": "tiering_available" if moves else "optimal",
            "findings": _cap(findings),
        }

    return respond(payload, _guard(work))


# --- network egress --------------------------------------------------------------------------------------------

EGRESS_ADVICE = {
    "cross_az": "keep chatty traffic zone-local (topology-aware routing, zonal replicas)",
    "cross_region": "replicate or cache data near its consumers",
    "internet": "front with a CDN, compress responses, or use a private path",
    "nat_processing": "route AWS-service traffic through gateway/interface VPC endpoints instead of the NAT gateway",
}


def egress_cost(payload: dict[str, Any]) -> dict[str, Any]:
    """Monthly data-transfer cost from flows and a rate card.

    cross_az and cross_region flows are billed at their per-GB rate (state the effective rate: where a provider bills
    both directions, pass the sum). internet flows are summed and priced through `internet_tiers`: tier i starts at
    starts_at_gb (cumulative monthly GB, first tier 0, strictly ascending) and applies until the next tier starts; the
    last tier is open-ended, so a free allowance is a leading tier with rate 0. Flows with via_nat add the NAT
    processing rate per GB on top. A flow's cost is its own rate; internet flows share the blended internet rate. A
    category at >= 50 % of the total cost yields a finding and is reported as dominant_category."""

    def work() -> dict[str, Any]:
        rates = payload["rates"]
        tiers = payload["internet_tiers"]
        previous = None
        for index, tier in enumerate(tiers):
            start = _fr(tier["starts_at_gb"])
            if index == 0 and start != 0:
                _bad("internet_tiers", "the first tier must start at 0 GB")
            if previous is not None and start <= previous:
                _bad("internet_tiers", "starts_at_gb must be strictly ascending")
            previous = start
        gb = {"cross_az": Fraction(0), "cross_region": Fraction(0), "internet": Fraction(0), "nat_processing": Fraction(0)}
        for flow in payload["flows"]:
            gb[flow["kind"]] += _fr(flow["monthly_gb"])
            if flow["via_nat"]:
                gb["nat_processing"] += _fr(flow["monthly_gb"])
        internet_cost = Fraction(0)
        for index, tier in enumerate(tiers):
            low = _fr(tier["starts_at_gb"])
            high = _fr(tiers[index + 1]["starts_at_gb"]) if index + 1 < len(tiers) else gb["internet"]
            internet_cost += _fr(tier["per_gb"]) * max(Fraction(0), min(gb["internet"], high) - low)
        cost = {
            "cross_az": gb["cross_az"] * _fr(rates["cross_az_per_gb"]),
            "cross_region": gb["cross_region"] * _fr(rates["cross_region_per_gb"]),
            "internet": internet_cost,
            "nat_processing": gb["nat_processing"] * _fr(rates["nat_processing_per_gb"]),
        }
        total = sum(cost.values(), Fraction(0))
        blended = internet_cost / gb["internet"] if gb["internet"] > 0 else Fraction(0)
        flow_rate = {"cross_az": _fr(rates["cross_az_per_gb"]), "cross_region": _fr(rates["cross_region_per_gb"]), "internet": blended}
        per_flow = []
        for flow in payload["flows"]:
            volume = _fr(flow["monthly_gb"])
            flow_cost = volume * flow_rate[flow["kind"]] + (volume * _fr(rates["nat_processing_per_gb"]) if flow["via_nat"] else 0)
            per_flow.append((flow_cost, flow["name"], flow["kind"], volume))
        per_flow.sort(key=lambda item: (-item[0], item[1]))
        dominant, findings = "", []
        if total > 0:
            for category in sorted(cost):
                if cost[category] * 2 >= total:
                    dominant = category
                    findings.append(_finding(f"EG-{category}", "minor", f"{category} is {_num(cost[category] / total * 100)}% of transfer cost: {EGRESS_ADVICE[category]}"))
                    break
        return {
            "state": "SUCCESS",
            "total_gb": _r(sum((gb[k] for k in ("cross_az", "cross_region", "internet")), Fraction(0))),
            "total_cost": _r(total, 2),
            "breakdown": [{"category": name, "gb": _r(gb[name]), "cost": _r(cost[name], 2)} for name in sorted(cost)],
            "top_flows": [{"name": n, "kind": k, "gb": _r(v), "cost": _r(c, 2)} for c, n, k, v in per_flow[:TOP_FLOWS]],
            "dominant_category": dominant,
            "findings": findings,
        }

    return respond(payload, _guard(work))


# --- spot / preemptible ----------------------------------------------------------------------------------------


def spot_suitability(payload: dict[str, Any]) -> dict[str, Any]:
    """Spot/preemptible fit per workload: interruption overhead versus price savings.

    Each instance is interrupted interruption_pct_month / 100 times a month (the provider advisor's monthly
    frequency read as an expected count, an approximation for the low rates it is used at; AWS defines it as the rate
    at which Spot reclaimed capacity in the trailing month); each interruption costs recovery_minutes plus, for `checkpointed` work,
    half the checkpoint interval of lost progress. effective price = spot / (1 - lost fraction of a 730 h month);
    savings = 1 - effective / on_demand. Verdicts in order: unsuitable (stateful, a single replica, or
    interruption_pct_month > max_interruption_pct, default 20), marginal (savings < min_savings_pct, default 30, or the
    on-demand floor leaves no replica to run on spot), else suitable. Suitable workloads keep
    ceil(replicas x on_demand_floor_pct / 100) (default 20 %, at least 1) replicas on demand; the rest may run on
    spot. The 30 / 20 / 20 defaults are house policy. Recommendation only: A11 applies node-pool changes (L3)."""

    def work() -> dict[str, Any]:
        max_interrupt = _fr(payload.get("max_interruption_pct", 20))
        min_savings = _fr(payload.get("min_savings_pct", 30))
        floor_pct = _fr(payload.get("on_demand_floor_pct", 20))
        _unique("workloads", [w["name"] for w in payload["workloads"]], "workload name")
        records, findings = [], []
        saving_total = Fraction(0)
        counts = {"suitable": 0, "marginal": 0, "unsuitable": 0}
        for index, w in enumerate(payload["workloads"]):
            tolerance = w["fault_tolerance"]
            if tolerance == "checkpointed" and "checkpoint_interval_minutes" not in w:
                _bad("workloads", f"workloads[{index}]: checkpointed work needs checkpoint_interval_minutes")
            lost_minutes = _fr(w["recovery_minutes"])
            if tolerance == "checkpointed":
                lost_minutes += _fr(w["checkpoint_interval_minutes"]) / 2
            lost_fraction = _fr(w["interruption_pct_month"]) / 100 * lost_minutes / (HOURS_PER_MONTH * 60)
            on_demand, spot = _fr(w["on_demand_hourly"]), _fr(w["spot_hourly"])
            effective = spot / (1 - lost_fraction)
            savings = (1 - effective / on_demand) * 100
            if tolerance == "stateful":
                verdict, reason = "unsuitable", "stateful-no-checkpoint"
            elif w["replicas"] < 2:
                verdict, reason = "unsuitable", "single-replica"
            elif _fr(w["interruption_pct_month"]) > max_interrupt:
                verdict, reason = "unsuitable", "interruption-rate-above-max"
            elif savings < min_savings:
                verdict, reason = "marginal", "savings-below-minimum"
            elif w["replicas"] - max(1, math.ceil(w["replicas"] * floor_pct / 100)) < 1:
                verdict, reason = "marginal", "on-demand-floor-leaves-no-spot-replicas"
            else:
                verdict, reason = "suitable", "ok"
            counts[verdict] += 1
            spot_replicas, saving = 0, Fraction(0)
            if verdict == "suitable":
                spot_replicas = w["replicas"] - max(1, math.ceil(w["replicas"] * floor_pct / 100))
                saving = spot_replicas * HOURS_PER_MONTH * (on_demand - effective)
                saving_total += saving
                findings.append(_finding(f"SP-{w['name']}", "minor", f"{w['name']}: run {spot_replicas} of {w['replicas']} replicas on spot, saves {_num(saving)}/month ({_num(savings)}% per instance-hour)"))
            else:
                findings.append(_finding(f"SP-{w['name']}", "info", f"{w['name']}: {verdict} ({reason})"))
            records.append({
                "name": w["name"], "verdict": verdict, "reason": reason, "effective_spot_hourly": _r(effective, 6),
                "savings_pct": _r(savings), "spot_replicas": spot_replicas, "monthly_saving": _r(saving, 2),
            })
        return {
            "state": "SUCCESS",
            "workload_count": len(records),
            "suitable_count": counts["suitable"],
            "marginal_count": counts["marginal"],
            "unsuitable_count": counts["unsuitable"],
            "monthly_saving_total": _r(saving_total, 2),
            "workloads": records,
            "verdict": "spot_candidates" if counts["suitable"] else "none",
            "findings": _cap(findings),
        }

    return respond(payload, _guard(work))


# --- carbon ----------------------------------------------------------------------------------------------------


def carbon_estimate(payload: dict[str, Any]) -> dict[str, Any]:
    """Operational emissions: kWh = average power W x hours / 1000 x PUE; kgCO2e = kWh x grid intensity (g/kWh) / 1000.

    Location-based method (the intensity of the grid where the workload runs). Every workload region must have a
    grid_intensity row. best_region is the lowest-intensity row flagged eligible (ties by name; "" when none is
    eligible); a workload's potential saving is its energy x (own intensity - best intensity), never negative.
    Verdict relocation_candidates when the total potential saving is >= min_saving_pct (default 10) of the total
    emissions, else no_action. Embodied emissions and network energy are out of scope."""

    def work() -> dict[str, Any]:
        grid = {}
        for grid_index, row in enumerate(payload["grid_intensity"]):
            if row["region"] in grid:
                _bad("grid_intensity", f"grid_intensity[{grid_index}] repeats an earlier region")
            grid[row["region"]] = row
        _unique("workloads", [w["name"] for w in payload["workloads"]], "workload name")
        pue = _fr(payload["pue"])
        min_saving = _fr(payload.get("min_saving_pct", 10))
        eligible = sorted((r for r in grid if grid[r]["eligible"]), key=lambda r: (_fr(grid[r]["g_co2e_per_kwh"]), r))
        best = eligible[0] if eligible else ""
        best_intensity = _fr(grid[best]["g_co2e_per_kwh"]) if best else Fraction(0)
        by_region: dict[str, Fraction] = {}
        records, findings = [], []
        energy_total = emissions_total = saving_total = Fraction(0)
        for index, w in enumerate(payload["workloads"]):
            if w["region"] not in grid:
                _bad("workloads", f"workloads[{index}]: its region has no grid_intensity row")
            intensity = _fr(grid[w["region"]]["g_co2e_per_kwh"])
            kwh = _fr(w["avg_power_w"]) * _fr(w["hours"]) / 1000 * pue
            kg = kwh * intensity / 1000
            saving = max(Fraction(0), kwh * (intensity - best_intensity) / 1000) if best else Fraction(0)
            energy_total += kwh
            emissions_total += kg
            saving_total += saving
            by_region[w["region"]] = by_region.get(w["region"], Fraction(0)) + kg
            records.append({"name": w["name"], "region": w["region"], "energy_kwh": _r(kwh), "kg_co2e": _r(kg), "potential_saving_kg": _r(saving)})
            if saving > 0:
                findings.append(_finding(f"CO-{w['name']}", "info", f"{w['name']}: moving {w['region']} -> {best} avoids {_num(saving)} kgCO2e"))
        saving_pct = saving_total / emissions_total * 100 if emissions_total > 0 else Fraction(0)
        return {
            "state": "SUCCESS",
            "total_energy_kwh": _r(energy_total),
            "total_kg_co2e": _r(emissions_total),
            "best_region": best,
            "potential_saving_kg": _r(saving_total),
            "potential_saving_pct": _r(saving_pct),
            "region_breakdown": [{"region": name, "kg_co2e": _r(by_region[name])} for name in sorted(by_region)],
            "workloads": records,
            "verdict": "relocation_candidates" if saving_total > 0 and saving_pct >= min_saving else "no_action",
            "findings": _cap(findings),
        }

    return respond(payload, _guard(work))


# --- showback --------------------------------------------------------------------------------------------------


def showback(payload: dict[str, Any]) -> dict[str, Any]:
    """Allocate a shared cost to consumers: proportional (by direct cost), even, or usage (by the usage metric).

    The shared cost must be a whole number of cents. Shares are exact fractions; each consumer gets the floor in
    cents and the leftover cents go to the largest fractional remainders (ties by team name), so the allocations sum
    to the shared cost to the cent. proportional needs a positive total direct cost and usage a positive total usage
    (else INVALID_INPUT on consumers). A consumer at >= 50 % of the allocation (with more than one consumer) is an
    info finding."""

    def work() -> dict[str, Any]:
        consumers = payload["consumers"]
        _unique("consumers", [c["team"] for c in consumers], "team")
        shared = Decimal(repr(payload["shared_cost"])) * 100
        if shared != shared.to_integral_value():
            _bad("shared_cost", "shared_cost must be a whole number of cents (at most 2 decimals)")
        cents = int(shared)
        method = payload["method"]
        if method == "even":
            weights = [Fraction(1)] * len(consumers)
        else:
            key = "direct_cost" if method == "proportional" else "usage"
            weights = [_fr(c[key]) for c in consumers]
            if sum(weights) <= 0:
                _bad("consumers", f"method {method} needs a positive total {key}")
        weight_total = sum(weights, Fraction(0))
        exact = [cents * w / weight_total for w in weights]
        floors = [math.floor(x) for x in exact]
        leftover = cents - sum(floors)
        order = sorted(range(len(consumers)), key=lambda i: (-(exact[i] - floors[i]), consumers[i]["team"]))
        for i in order[:leftover]:
            floors[i] += 1
        rows = []
        for i, c in enumerate(consumers):
            direct = _fr(c["direct_cost"])
            rows.append({
                "team": c["team"], "direct_cost": _r(direct, 4), "shared_allocation": _r(Fraction(floors[i], 100), 2),
                "total_cost": _r(direct + Fraction(floors[i], 100), 4),
                "share_pct": _r(Fraction(floors[i], cents) * 100) if cents else 0.0,
            })
        rows.sort(key=lambda row: row["team"])
        findings = []
        if len(consumers) > 1 and cents:
            for i in sorted(range(len(consumers)), key=lambda i: consumers[i]["team"]):
                if floors[i] * 2 >= cents:
                    findings.append(_finding(f"SB-{consumers[i]['team']}", "info", f"{consumers[i]['team']} carries {_num(Fraction(floors[i], cents) * 100)}% of the shared cost"))
        if method == "usage":
            for c in sorted(consumers, key=lambda c: c["team"]):
                if _fr(c["usage"]) == 0:
                    findings.append(_finding(f"SB-{c['team']}-idle", "info", f"{c['team']} reported no usage and receives no shared cost"))
        return {
            "state": "SUCCESS",
            "method": method,
            "allocated_total": _r(Fraction(sum(floors), 100), 2),
            "allocations": rows,
            "largest_consumer": consumers[min(range(len(consumers)), key=lambda i: (-floors[i], consumers[i]["team"]))]["team"],
            "findings": findings,
        }

    return respond(payload, _guard(work))
