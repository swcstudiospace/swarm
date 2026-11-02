"""A25 Resilience & Chaos Engineer tools: pure analyses over supplied manifests, schedules and measurements.

Boundary: A13 owns SLOs, alerts and incidents; A25 owns resilience evidence and experiments (PDBs, spread, probes,
backups and restore drills, chaos plans, failure modes). Nothing here talks to a cluster: callers pass parsed
manifest fields and measurements, handlers return counts, a ``status`` (pass | warn | fail) and ``findings``
of {id, severity, subject, summary} with severities blocker > major > minor > info (``fail`` on any major or worse).

Tools:
  resil_pdb_check          PodDisruptionBudget versus replicas: blocking, unsatisfiable, missing and permissive budgets.
  resil_spread_check       topology spread and anti-affinity versus zone count: pending pods and zone-loss survivors.
  resil_probe_check        startup, liveness and readiness probe sanity per container.
  resil_backup_coverage    volumes, etcd and databases versus backup schedule, retention, freshness and restore tests.
  resil_restore_rto        restore time estimate from data size and throughput versus the RTO target.
  resil_chaos_spec_check   chaos experiment spec: steady state, blast radius, abort conditions, freeze windows, gates.
  resil_fmea_rpn           FMEA risk priority numbers (severity x occurrence x detection) ranked with a Pareto cut.
  resil_chaos_budget_gate  go / no-go of a chaos experiment against the remaining error budget.
  resil_shutdown_check     terminationGracePeriodSeconds versus preStop and in-flight drain time.
  resil_drill_cadence      DR drill cadence, evidence and measured RTO/RPO versus requirements.
  resil_load_shed_check    load-shedding policy: Little's-law demand, priorities, reservations and queue bounds.
  resil_quorum_safety      StatefulSet quorum safety across zones with its PodDisruptionBudget.

Kubernetes references (public sources):
  * PodDisruptionBudget: policy/v1 (GA 1.21; policy/v1beta1 is no longer served from 1.25, see the Deprecated API
    Migration Guide https://kubernetes.io/docs/reference/using-api/deprecation-guide/). minAvailable and
    maxUnavailable are mutually exclusive; percentages are rounded UP for both
    (pkg/controller/disruption, intstr.GetScaledValueFromIntOrPercent with roundUp=true); disruptions allowed =
    currently healthy - desired healthy, never below 0. https://kubernetes.io/docs/tasks/run-application/configure-pdb/
  * unhealthyPodEvictionPolicy IfHealthyBudget (default) | AlwaysAllow, stable in 1.31.
  * Pod topology spread constraints: skew = pods in a domain - global minimum; minDomains (stable 1.30) only with
    whenUnsatisfiable=DoNotSchedule, and while fewer domains than minDomains exist the global minimum counts as 0.
    https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/
  * Well-known labels: topology.kubernetes.io/zone; failure-domain.beta.kubernetes.io/zone is deprecated.
  * Probes: defaults initialDelaySeconds 0, periodSeconds 10, timeoutSeconds 1, successThreshold 1, failureThreshold 3;
    successThreshold must be 1 for liveness and startup; liveness and readiness start only after the startup probe
    succeeds. https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
  * Pod termination: terminationGracePeriodSeconds counts down from the deletion and includes the preStop hook;
    0 means kill immediately; a preStop hook that outlives the grace period gets one 2 s extension.
    https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination
  * Quorum: etcd FAQ, quorum = floor(n/2) + 1, so an even member count adds no fault tolerance.
    https://etcd.io/docs/v3.5/faq/
  * Error budget: burn rate 1 spends exactly the whole budget by the end of the SLO window (Google SRE Workbook,
    "Alerting on SLOs"). FMEA: RPN = S x O x D is the classic AIAG ranking; AIAG-VDA 2019 replaced it with Action
    Priority tables.

House policy, NOT Kubernetes, RFC or standards facts (each rule below that applies one says so; callers can read the
numbers in the finding text): liveness failure window under 10 s, startup headroom 1.25x, shutdown margin
max(5 s, 10%), backup staleness at 1x and 2x the RPO and a backup interval equal to the RPO accepted, restore
headroom 10%, a burn rate of 1 or more blocking a production experiment, FMEA severity >= 9 always needing action,
the 80% Pareto cut, "more than half" for a permissive PodDisruptionBudget. Blast radius, duration, reserve and cost
limits, retention and restore-test age are caller inputs, never constants here.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Any, SupportsFloat

from swarm.tools.support import respond

Report = dict[str, Any]  # a tool result: JSON-shaped, its values are validated where they are read
Finding = dict[str, str]  # one finding: {id, severity, subject, summary}

SEVERITIES = ("blocker", "major", "minor", "info")
_RANK = {name: index for index, name in enumerate(SEVERITIES)}

ZONE_KEY = "topology.kubernetes.io/zone"
LEGACY_ZONE_KEY = "failure-domain.beta.kubernetes.io/zone"

PROBE_DEFAULTS = {"initial_delay_seconds": 0, "period_seconds": 10, "timeout_seconds": 1, "success_threshold": 1,
                  "failure_threshold": 3}
LIVENESS_MIN_WINDOW_S = 10  # house policy: liveness failure_threshold x period below this restarts on one slow response
STARTUP_HEADROOM = Decimal("1.25")  # a startup budget under 1.25x the measured startup time has no safety margin
SHUTDOWN_MIN_MARGIN_S = 5
SHUTDOWN_MARGIN_FRACTION = Decimal("0.1")
FMEA_ALWAYS_ACT_SEVERITY = 9  # house policy modelled on AIAG 4th-edition practice (the handbook is paywalled: not checked against it)
PARETO_SHARE = Decimal("0.8")
DAY_SECONDS = 86400


class _Invalid(Exception):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def _guard(work: Callable[[], Report]) -> Callable[[], Report]:
    """Turn a semantic input problem raised inside ``work`` into the registry's INVALID_INPUT shape."""

    def guarded() -> Report:
        try:
            return work()
        except _Invalid as exc:
            return {"state": "INVALID_INPUT", "field": exc.field, "message": exc.message}

    return guarded


def _reply(payload: dict[str, Any], work: Callable[[], Report]) -> Report:
    """`support.respond` is annotated to return Any; this keeps every handler's return type explicit."""
    result: Report = respond(payload, work)
    return result


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written, so 0.1 is 0.1 and not its binary neighbour."""
    return Decimal(repr(value))


def _round(value: SupportsFloat, digits: int = 4) -> float:
    return round(float(value), digits)


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _finding(rule: str, severity: str, subject: str, summary: str) -> Finding:
    return {"id": rule, "severity": severity, "subject": subject, "summary": summary}


def _ordered(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (_RANK[f["severity"]], f["id"], f["subject"], f["summary"]))


def _status(findings: list[Finding]) -> str:
    severities = {f["severity"] for f in findings}
    if severities & {"blocker", "major"}:
        return "fail"
    return "warn" if "minor" in severities else "pass"


def _ts(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _Invalid(field, f"{field}: not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _Invalid(field, f"{field}: a timestamp needs an explicit UTC offset such as +00:00 or Z")
    return parsed


def _seconds(delta: timedelta) -> Decimal:
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(10**6)


def _after(when: datetime, delta: timedelta, field: str) -> datetime:
    """``when + delta``; a result beyond year 9999 is an input problem, not a handler crash."""
    try:
        return when + delta
    except OverflowError:
        raise _Invalid(field, f"{field}: {delta} after {when.isoformat()} is beyond the supported calendar (year 9999)") from None


def _unique(names: list[str], field: str, what: str) -> None:
    if len(set(names)) != len(names):
        raise _Invalid(field, f"{field}: {what} names must be unique")


def _fmt(value: SupportsFloat) -> str:
    """Number for finding text: no exponent, no 6-digit rounding that makes an exceeded limit look equal to the limit."""
    number = float(value)
    if number != 0 and abs(number) < 1e-4:
        return f"{number:g}"
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


# --- PodDisruptionBudget arithmetic (shared by resil_pdb_check and resil_quorum_safety) ---------------------------


def _scaled(spec: str, total: int, field: str, label: str, what: str) -> int:
    """intstr.GetScaledValueFromIntOrPercent(spec, total, roundUp=true) for '3' or '50%'.

    A rejected value is never echoed (a mistyped field can hold anything, including a secret): only its kind is named.
    """
    percent = spec.endswith("%")
    digits = spec[:-1] if percent else spec
    if not (digits.isascii() and digits.isdigit()):
        raise _Invalid(field, f"{label}: {what} is not a non-negative integer or a percentage such as '50%'")
    value = int(digits)
    if not percent:
        return value
    if value > 100:
        raise _Invalid(field, f"{label}: {what} is above 100%")
    return -(-value * total // 100)


def _desired_healthy(replicas: int, min_spec: str | None, max_spec: str | None, label: str, fields: tuple[str, str]) -> int | None:
    """Desired healthy pods the budget demands, or None when no budget is declared; fields = (min, max) input names."""
    if min_spec is not None and max_spec is not None:
        raise _Invalid(fields[0], f"{label}: min_available and max_unavailable are mutually exclusive in a PodDisruptionBudget")
    if max_spec is not None:
        return max(0, replicas - _scaled(max_spec, replicas, fields[1], label, "max_unavailable"))
    if min_spec is not None:
        return _scaled(min_spec, replicas, fields[0], label, "min_available")
    return None


# --- 1. PodDisruptionBudget versus replicas ---------------------------------------------------------------------


def pdb_check(payload: dict[str, Any]) -> Report:
    """Per workload: is a PodDisruptionBudget present, satisfiable, non-blocking and protective?

    desired healthy = minAvailable, or replicas - maxUnavailable (percentages rounded up); disruptions allowed
    (steady state, all replicas ready) = replicas - desired healthy. Rules (replicas = 0 is skipped):
      pdb-missing (major, replicas >= 2), single-replica (minor, replicas = 1: no budget, or a budget that still lets
      the one pod be evicted; a budget that refuses the eviction is pdb-blocks-eviction instead),
      pdb-unsatisfiable (blocker, desired healthy > replicas: no eviction can ever succeed),
      pdb-blocks-eviction (major, 0 disruptions allowed: node drains and upgrades stall),
      pdb-no-protection (major, replicas >= 2 and every pod may be evicted at once),
      pdb-permissive (minor, replicas >= 3 and more than half may be evicted at once: house policy),
      pdb-stuck-unhealthy (major, ready < desired healthy while the policy is IfHealthyBudget: running but
      not-ready pods cannot be evicted, so a drain cannot remove the broken pod; AlwaysAllow lifts this).
    """

    def work() -> Report:
        items = payload["workloads"]
        _unique([w["name"] for w in items], "workloads", "workload")
        findings: list[Finding] = []
        budgets: list[dict[str, Any]] = []
        without = blocking = unsafe = 0
        for w in sorted(items, key=lambda w: w["name"]):
            name, replicas = w["name"], w["replicas"]
            ready = w.get("ready_replicas", replicas)
            policy = w.get("unhealthy_pod_eviction_policy", "IfHealthyBudget")
            desired = _desired_healthy(replicas, w.get("min_available"), w.get("max_unavailable"), f"workload {name}",
                                       ("workloads", "workloads"))
            if desired is None:
                without += 1
                budgets.append({"workload": name, "has_pdb": False, "desired_healthy": 0, "disruptions_allowed": replicas})
                if replicas >= 2:
                    findings.append(_finding("pdb-missing", "major", name,
                                             f"{replicas} replicas and no PodDisruptionBudget: a node drain may evict them all at once"))
                elif replicas == 1:
                    findings.append(_finding("single-replica", "minor", name,
                                             "one replica: any eviction is downtime and a PodDisruptionBudget cannot prevent it"))
                continue
            allowed = max(0, replicas - desired)
            budgets.append({"workload": name, "has_pdb": True, "desired_healthy": desired, "disruptions_allowed": allowed})
            if replicas == 0:
                continue
            unsatisfiable = desired > replicas
            if unsatisfiable:
                blocking += 1
                findings.append(_finding("pdb-unsatisfiable", "blocker", name,
                                         f"budget demands {desired} healthy pods but only {replicas} replicas exist: every eviction is refused"))
            elif allowed == 0:
                blocking += 1
                findings.append(_finding("pdb-blocks-eviction", "major", name,
                                         f"budget allows 0 voluntary disruptions of {replicas} replicas: node drains and upgrades stall"))
            elif replicas >= 2 and allowed >= replicas:
                unsafe += 1
                findings.append(_finding("pdb-no-protection", "major", name,
                                         f"budget allows all {replicas} replicas to be evicted at once"))
            elif replicas >= 3 and allowed * 2 > replicas:
                unsafe += 1
                findings.append(_finding("pdb-permissive", "minor", name,
                                         f"budget allows {allowed} of {replicas} replicas to be evicted at once (more than half)"))
            elif replicas == 1:
                findings.append(_finding("single-replica", "minor", name,
                                         "one replica: the budget allows its eviction, so any drain is downtime and the budget protects nothing"))
            if not unsatisfiable and ready < desired and policy == "IfHealthyBudget":
                findings.append(_finding("pdb-stuck-unhealthy", "major", name,
                                         f"{ready} ready of {desired} desired healthy: with IfHealthyBudget the not-ready pods cannot be "
                                         "evicted; set unhealthyPodEvictionPolicy AlwaysAllow"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "workloads_checked": len(items), "without_pdb": without, "blocking": blocking,
                "unsafe": unsafe, "budgets": budgets, "status": _status(findings), "findings": findings}

    return _reply(payload, _guard(work))


# --- 2. topology spread and anti-affinity versus zone count ------------------------------------------------------


def spread_check(payload: dict[str, Any]) -> Report:
    """Spread constraints and anti-affinity of one workload versus its failure domains.

    Worst case pods in one zone: hard spread (DoNotSchedule, maxSkew s over D domains) bounds it by
    floor((replicas + (D-1) * s) / D); while fewer domains than minDomains exist the global minimum counts as 0, so
    a domain holds at most s pods and replicas above D * s stay Pending. Required anti-affinity allows one pod per
    domain (replicas above D stay Pending). ScheduleAnyway and preferred rules guarantee nothing. The zone-loss
    survivors are replicas minus that worst case; pending counts are a lower bound over all rules.
    Rules: single-zone, zone-spread-missing (major), zone-spread-soft-only (minor), zone-loss-breaches-minimum
    (major), unschedulable-replicas (blocker), replicas-below-zone-count (info), deprecated-zone-label (minor).
    """

    def work() -> Report:
        workload, replicas = payload["workload"], payload["replicas"]
        if payload["min_available_replicas"] > replicas:
            raise _Invalid("min_available_replicas", "min_available_replicas cannot exceed replicas")
        domains: dict[str, int] = {}
        for domain in payload["topology_domains"]:
            if domain["key"] in domains:
                raise _Invalid("topology_domains", "topology_domains lists one topology key twice")
            domains[domain["key"]] = domain["count"]
        zone_key = ZONE_KEY if ZONE_KEY in domains else LEGACY_ZONE_KEY if LEGACY_ZONE_KEY in domains else None
        if zone_key is None:
            raise _Invalid("topology_domains", f"topology_domains must include {ZONE_KEY}")
        zones = domains[zone_key]
        zone_keys = {ZONE_KEY, LEGACY_ZONE_KEY}
        findings: list[Finding] = []
        guarantees: list[int] = []
        hard_zone = soft_zone = legacy = False
        pending = 0
        for constraint in payload["spread_constraints"]:
            key = constraint["topology_key"]
            if key not in domains:
                raise _Invalid("spread_constraints", "a spread constraint names a topology key with no entry in topology_domains")
            hard = constraint["when_unsatisfiable"] == "DoNotSchedule"
            min_domains = constraint.get("min_domains")
            if min_domains is not None and not hard:
                raise _Invalid("spread_constraints", "min_domains is only valid with whenUnsatisfiable=DoNotSchedule")
            count, skew = domains[key], constraint["max_skew"]
            legacy = legacy or key == LEGACY_ZONE_KEY
            if not hard:
                soft_zone = soft_zone or key in zone_keys
                continue
            worst = min(replicas, (replicas + (count - 1) * skew) // count)
            if min_domains is not None and min_domains > count:
                worst = min(worst, skew)
                pending = max(pending, replicas - count * skew)
            if key in zone_keys:
                hard_zone = True
                guarantees.append(worst)
        for rule in payload["anti_affinity"]:
            key = rule["topology_key"]
            if key not in domains:
                raise _Invalid("anti_affinity", "an anti-affinity rule names a topology key with no entry in topology_domains")
            legacy = legacy or key == LEGACY_ZONE_KEY
            if rule["mode"] == "required":
                pending = max(pending, replicas - domains[key])
                if key in zone_keys:
                    hard_zone = True
                    guarantees.append(min(1, replicas))
            elif key in zone_keys:
                soft_zone = True
        pending = max(0, pending)
        worst_zone = min(guarantees) if guarantees else replicas
        survivors = replicas - worst_zone
        if pending:
            findings.append(_finding("unschedulable-replicas", "blocker", workload,
                                     f"at least {pending} replica(s) can never be scheduled with these rules"))
        if replicas >= 2 and zones == 1:
            findings.append(_finding("single-zone", "major", workload, f"all {replicas} replicas share the only zone"))
        elif replicas >= 2 and not hard_zone:
            if soft_zone:
                findings.append(_finding("zone-spread-soft-only", "minor", workload,
                                         "zone spread is only preferred (ScheduleAnyway or preferred anti-affinity): "
                                         "all replicas may still land in one zone"))
            else:
                findings.append(_finding("zone-spread-missing", "major", workload,
                                         f"no zone spread or anti-affinity across {zones} zones"))
        if survivors < payload["min_available_replicas"]:
            findings.append(_finding("zone-loss-breaches-minimum", "major", workload,
                                     f"losing the fullest zone can remove {worst_zone} of {replicas} replicas, leaving {survivors} "
                                     f"< required {payload['min_available_replicas']}"))
        if 2 <= replicas < zones:
            findings.append(_finding("replicas-below-zone-count", "info", workload,
                                     f"{replicas} replicas cover at most {replicas} of {zones} zones"))
        if legacy or zone_key == LEGACY_ZONE_KEY:
            findings.append(_finding("deprecated-zone-label", "minor", workload,
                                     f"{LEGACY_ZONE_KEY} is deprecated; use {ZONE_KEY}"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "replicas": replicas, "zone_count": zones, "worst_case_pods_per_zone": worst_zone,
                "pods_surviving_zone_loss": survivors, "unschedulable_replicas": pending, "status": _status(findings),
                "findings": findings}

    return _reply(payload, _guard(work))


# --- 3. probe sanity ---------------------------------------------------------------------------------------------


def _probe(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    if spec is None:
        return None
    return {**{key: spec.get(key, default) for key, default in PROBE_DEFAULTS.items()}, "kind": spec["kind"],
            "endpoint": spec.get("endpoint", "")}


def probe_check(payload: dict[str, Any]) -> Report:
    """Startup, liveness and readiness probes of one workload's containers.

    Budget = initialDelaySeconds + failureThreshold x periodSeconds (the Kubernetes docs size a startup probe as
    failureThreshold x periodSeconds; treat it as an approximation of the time before the kubelet acts).
    Rules: readiness-missing (major, serves traffic), liveness-too-aggressive (major, failureThreshold x period
    < 10 s: house policy), startup-budget-too-small and liveness-kills-slow-start (blocker, budget below the measured
    startup time), thin-startup-budget (minor, budget < 1.25x startup time: house policy), liveness-equals-readiness
    (minor, same kind and non-empty endpoint; the endpoint itself is never echoed: it may be a URL or an exec command
    carrying a credential), liveness-missing (info). successThreshold other than 1 on liveness or startup is
    rejected by the API and answered with INVALID_INPUT.
    """

    def work() -> Report:
        workload, serves = payload["workload"], payload["serves_traffic"]
        expected = payload.get("expected_startup_seconds")
        containers = payload["containers"]
        _unique([c["name"] for c in containers], "containers", "container")
        findings: list[Finding] = []
        missing_readiness = probes = 0
        for container in sorted(containers, key=lambda c: c["name"]):
            name = container["name"]
            startup, liveness, readiness = (_probe(container.get(kind)) for kind in ("startup", "liveness", "readiness"))
            probes += sum(p is not None for p in (startup, liveness, readiness))
            for kind, probe in (("startup", startup), ("liveness", liveness)):
                if probe is not None and probe["success_threshold"] != 1:
                    raise _Invalid("containers", f"a container's {kind} success_threshold must be 1")
            if readiness is None:
                missing_readiness += 1
                if serves:
                    findings.append(_finding("readiness-missing", "major", name,
                                             "no readiness probe: the pod receives traffic as soon as the container starts"))
            if liveness is None:
                findings.append(_finding("liveness-missing", "info", name, "no liveness probe: a hung process is never restarted"))
            else:
                window = liveness["failure_threshold"] * liveness["period_seconds"]
                if window < LIVENESS_MIN_WINDOW_S:
                    findings.append(_finding("liveness-too-aggressive", "major", name,
                                             f"liveness restarts after {window}s of failures (failureThreshold "
                                             f"{liveness['failure_threshold']} x period {liveness['period_seconds']}s < {LIVENESS_MIN_WINDOW_S}s)"))
            if (liveness is not None and readiness is not None and liveness["endpoint"]
                    and (liveness["kind"], liveness["endpoint"]) == (readiness["kind"], readiness["endpoint"])):
                findings.append(_finding("liveness-equals-readiness", "minor", name,
                                         f"liveness and readiness use the same {liveness['kind']} probe target: a failing dependency restarts "
                                         "the pod instead of only removing it from the Service"))
            guard = startup if startup is not None else liveness
            if expected is not None and guard is not None:
                budget = guard["initial_delay_seconds"] + guard["failure_threshold"] * guard["period_seconds"]
                kind = "startup" if startup is not None else "liveness"
                if budget < _dec(expected):
                    rule = "startup-budget-too-small" if startup is not None else "liveness-kills-slow-start"
                    findings.append(_finding(rule, "blocker", name,
                                             f"{kind} probe allows {budget}s but the container needs {_fmt(expected)}s to start: it is "
                                             "restarted before it is ready"))
                elif budget < _dec(expected) * STARTUP_HEADROOM:
                    findings.append(_finding("thin-startup-budget", "minor", name,
                                             f"{kind} probe allows {budget}s for a {_fmt(expected)}s start (under 1.25x headroom)"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "containers_checked": len(containers), "missing_readiness": missing_readiness,
                "probes_defined": probes, "status": _status(findings), "findings": [{**f, "subject": f"{workload}/{f['subject']}"} for f in findings]}

    return _reply(payload, _guard(work))


# --- 4. backup coverage ------------------------------------------------------------------------------------------


def backup_coverage(payload: dict[str, Any]) -> Report:
    """Backup jobs versus the assets (volumes, etcd, databases, object stores) they must protect.

    Several jobs of one asset merge: shortest interval, longest retention, latest success, any off-site copy, latest
    restore test (so an hourly job with short retention plus a daily job with long retention count as one hourly,
    long-retention backup: pass per-job evidence to the owner when that matters). Rules per asset: backup-missing,
    backup-never-succeeded (blocker for critical assets, else major), schedule-exceeds-rpo (major, interval > RPO; an
    interval equal to the RPO passes, ignoring the backup's own duration), backup-stale (major when the last success is
    older than the RPO, blocker when older than twice the RPO: house policy), retention-too-short (major),
    no-offsite-copy (major critical, else minor), restore-untested (major critical, else minor), restore-test-stale
    (minor, older than restore_test_max_age_days).
    """

    def work() -> Report:
        as_of = _ts(payload["as_of"], "as_of")
        assets = payload["assets"]
        _unique([a["name"] for a in assets], "assets", "asset")
        known = {a["name"] for a in assets}
        merged: dict[str, dict[str, Any]] = {}
        for job in payload["jobs"]:
            if job["asset"] not in known:
                raise _Invalid("jobs", "a job references an asset that is not in assets")
            last = _ts(job["last_success"], "jobs") if "last_success" in job else None
            tested = _ts(job["last_restore_test"], "jobs") if "last_restore_test" in job else None
            for stamp in (last, tested):
                if stamp is not None and stamp > as_of:
                    raise _Invalid("jobs", "a job has a timestamp after as_of")
            combined = merged.setdefault(job["asset"], {"interval": job["interval_hours"], "retention": job["retention_days"],
                                                         "last": last, "offsite": job["offsite"], "tested": tested})
            combined["interval"] = min(combined["interval"], job["interval_hours"])
            combined["retention"] = max(combined["retention"], job["retention_days"])
            combined["offsite"] = combined["offsite"] or job["offsite"]
            for key, stamp in (("last", last), ("tested", tested)):
                if stamp is not None and (combined[key] is None or stamp > combined[key]):
                    combined[key] = stamp
        findings: list[Finding] = []
        covered = protected = 0
        for asset in sorted(assets, key=lambda a: a["name"]):
            name, critical = asset["name"], asset["criticality"] == "critical"
            rpo = _dec(asset["rpo_hours"])
            entry = merged.get(name)
            before = len(findings)
            if entry is None:
                findings.append(_finding("backup-missing", "blocker" if critical else "major", name,
                                         f"{asset['kind']} {asset['criticality']} asset has no backup job"))
            else:
                covered += 1
                if _dec(entry["interval"]) > rpo:
                    findings.append(_finding("schedule-exceeds-rpo", "major", name,
                                             f"backup every {_fmt(entry['interval'])}h cannot meet an RPO of {_fmt(rpo)}h"))
                if entry["last"] is None:
                    findings.append(_finding("backup-never-succeeded", "blocker" if critical else "major", name,
                                             "backup job has no successful run"))
                else:
                    age = _seconds(as_of - entry["last"])
                    if age > 2 * rpo * 3600:
                        findings.append(_finding("backup-stale", "blocker", name,
                                                 f"last success {_fmt(age / 3600)}h ago, more than twice the RPO of {_fmt(rpo)}h"))
                    elif age > rpo * 3600:
                        findings.append(_finding("backup-stale", "major", name,
                                                 f"last success {_fmt(age / 3600)}h ago exceeds the RPO of {_fmt(rpo)}h"))
                if entry["retention"] < payload["min_retention_days"]:
                    findings.append(_finding("retention-too-short", "major", name,
                                             f"retention {entry['retention']}d is below the required {payload['min_retention_days']}d"))
                if not entry["offsite"]:
                    findings.append(_finding("no-offsite-copy", "major" if critical else "minor", name,
                                             "no copy outside the failure domain of the source"))
                if entry["tested"] is None:
                    findings.append(_finding("restore-untested", "major" if critical else "minor", name,
                                             "no restore test recorded: the backup is unproven"))
                elif _seconds(as_of - entry["tested"]) > payload["restore_test_max_age_days"] * DAY_SECONDS:
                    findings.append(_finding("restore-test-stale", "minor", name,
                                             f"last restore test is older than {payload['restore_test_max_age_days']}d"))
            if not any(f["severity"] in ("blocker", "major") for f in findings[before:]):
                protected += 1
        findings = _ordered(findings)
        return {"state": "SUCCESS", "assets_total": len(assets), "assets_covered": covered, "assets_protected": protected,
                "coverage_percent": _round(Decimal(covered) * 100 / Decimal(len(assets)), 2), "status": _status(findings),
                "findings": findings}

    return _reply(payload, _guard(work))


# --- 5. restore RTO estimate -------------------------------------------------------------------------------------


def restore_rto(payload: dict[str, Any]) -> Report:
    """Restore time = fixed overhead + data / effective throughput + log replay, scaled by a safety margin.

    Effective throughput = per-stream throughput x (1 + (streams - 1) x efficiency), capped by network_limit_mib_s.
    GiB are 1024 MiB. The estimate meets the target when estimated x margin <= target (equal passes).
    Rules: rto-exceeds-target (blocker, estimate above target), rto-margin-eroded (major, estimate within target but
    estimate x margin above it), thin-rto-headroom (minor, under 10% of the target left), network-bound (info).
    max_data_gib_for_target is the largest data size that still meets the target with the same rates and margin.
    """

    def work() -> Report:
        size, throughput = _dec(payload["data_size_gib"]), _dec(payload["restore_throughput_mib_s"])
        streams, efficiency = payload["parallel_streams"], _dec(payload["parallel_efficiency"])
        replay_gib = _dec(payload["replay_size_gib"])
        if replay_gib > 0 and "replay_rate_mib_s" not in payload:
            raise _Invalid("replay_rate_mib_s", "replay_rate_mib_s is required when replay_size_gib is above 0")
        overhead, margin, target = _dec(payload["fixed_overhead_minutes"]), _dec(payload["safety_margin"]), _dec(payload["target_rto_minutes"])
        raw_rate = throughput * (1 + (streams - 1) * efficiency)
        limit = payload.get("network_limit_mib_s")
        network_bound = limit is not None and raw_rate > _dec(limit)
        rate = _dec(limit) if network_bound else raw_rate
        transfer_min = size * 1024 / rate / 60
        replay_min = replay_gib * 1024 / _dec(payload["replay_rate_mib_s"]) / 60 if replay_gib > 0 else Decimal(0)
        estimated = overhead + transfer_min + replay_min
        with_margin = estimated * margin
        available = target / margin - overhead - replay_min
        max_data = available * 60 * rate / 1024 if available > 0 else Decimal(0)
        meets = with_margin <= target
        findings: list[Finding] = []
        if estimated > target:
            findings.append(_finding("rto-exceeds-target", "blocker", "restore",
                                     f"estimated {_fmt(estimated)} min exceeds the {_fmt(target)} min RTO target"))
        elif not meets:
            findings.append(_finding("rto-margin-eroded", "major", "restore",
                                     f"estimate {_fmt(estimated)} min fits the target but x{_fmt(margin)} margin gives {_fmt(with_margin)} min "
                                     f"> {_fmt(target)} min"))
        elif target - with_margin < target / 10:
            findings.append(_finding("thin-rto-headroom", "minor", "restore",
                                     f"only {_fmt(target - with_margin)} min of the {_fmt(target)} min target remain after the margin"))
        if network_bound and limit is not None:
            findings.append(_finding("network-bound", "info", "restore",
                                     f"{_fmt(raw_rate)} MiB/s of parallel restore is capped by the {_fmt(limit)} MiB/s network limit"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "effective_throughput_mib_s": _round(rate, 2), "transfer_minutes": _round(transfer_min, 2),
                "replay_minutes": _round(replay_min, 2), "estimated_minutes": _round(estimated, 2),
                "estimated_with_margin_minutes": _round(with_margin, 2), "headroom_minutes": _round(target - with_margin, 2),
                "max_data_gib_for_target": _round(max_data, 2), "meets_target": meets, "status": _status(findings),
                "findings": findings}

    return _reply(payload, _guard(work))


# --- 6. chaos experiment spec check ------------------------------------------------------------------------------


def chaos_spec_check(payload: dict[str, Any]) -> Report:
    """Is a chaos experiment safe to run? Steady state, blast radius, abort conditions, window, approvals.

    Required autonomy follows the A25 ceiling: staging experiments L2, production experiments L3. The experiment
    window is [start, start + duration); a freeze window [start, end) conflicts when the two intervals overlap
    (touching is not overlapping). Blast radius = affected / total instances x 100; limits come from the caller.
    Rules: no-steady-state-hypothesis, no-abort-conditions (blocker); blast-radius-exceeds-limit (blocker in
    production, major in staging); duration-exceeds-limit (major); freeze-window (blocker in production, major in
    staging); no-rollback-plan (major); production only: manual-abort-only, approval-missing,
    error-budget-gate-not-passed (blocker); abort-not-tied-to-steady-state (minor); customer-impact (info).
    """

    def work() -> Report:
        production = payload["environment"] == "production"
        start = _ts(payload["start"], "start")
        end = _after(start, timedelta(minutes=payload["duration_minutes"]), "start")
        affected, total = payload["affected_instances"], payload["total_instances"]
        if affected > total:
            raise _Invalid("affected_instances", "affected_instances cannot exceed total_instances")
        blast = Decimal(affected) * 100 / Decimal(total)
        name = payload["name"]
        findings: list[Finding] = []
        if not payload["steady_state"]:
            findings.append(_finding("no-steady-state-hypothesis", "blocker", name,
                                     "no steady-state hypothesis: nothing defines what 'healthy' means during the experiment"))
        if not payload["abort_conditions"]:
            findings.append(_finding("no-abort-conditions", "blocker", name, "no abort conditions: nothing stops the experiment when it hurts"))
        elif payload["steady_state"]:
            known = {s["metric"] for s in payload["steady_state"]}
            if not any(a["metric"] in known for a in payload["abort_conditions"]):
                findings.append(_finding("abort-not-tied-to-steady-state", "minor", name,
                                         "no abort condition watches a steady-state metric"))
        if blast > _dec(payload["max_blast_percent"]):
            findings.append(_finding("blast-radius-exceeds-limit", "blocker" if production else "major", name,
                                     f"blast radius {_fmt(round(blast, 4))}% exceeds the {_fmt(payload['max_blast_percent'])}% limit"))
        if payload["duration_minutes"] > payload["max_duration_minutes"]:
            findings.append(_finding("duration-exceeds-limit", "major", name,
                                     f"{payload['duration_minutes']} min exceeds the {payload['max_duration_minutes']} min limit"))
        for number, window in enumerate(payload["freeze_windows"], 1):
            w_start, w_end = _ts(window["start"], "freeze_windows"), _ts(window["end"], "freeze_windows")
            if w_end <= w_start:
                raise _Invalid("freeze_windows", f"freeze window #{number} ends before it starts")
            if start < w_end and end > w_start:
                # the free-text reason is not echoed: operators paste tickets, links and tokens into it
                findings.append(_finding("freeze-window", "blocker" if production else "major", name,
                                         f"overlaps freeze window #{number} ({window['start']} to {window['end']})"))
        if not payload["rollback_documented"]:
            findings.append(_finding("no-rollback-plan", "major", name, "no documented rollback or recovery step"))
        if production:
            if not payload["automatic_abort"]:
                findings.append(_finding("manual-abort-only", "blocker", name, "production experiments need an automatic abort, not a human watching"))
            if not payload["approval_recorded"]:
                findings.append(_finding("approval-missing", "blocker", name, "no recorded approval for a production experiment (L3)"))
            if not payload["error_budget_gate_passed"]:
                findings.append(_finding("error-budget-gate-not-passed", "blocker", name,
                                         "the error-budget gate (resil_chaos_budget_gate) has not returned go"))
            if payload["customer_facing"]:
                findings.append(_finding("customer-impact", "info", name, "customer-facing traffic is in scope: announce the window"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "window_end": end.isoformat(), "blast_radius_percent": _round(blast, 4),
                "required_autonomy": "L3" if production else "L2",
                "blockers": sum(f["severity"] == "blocker" for f in findings), "status": _status(findings), "findings": findings}

    return _reply(payload, _guard(work))


# --- 7. FMEA risk priority numbers -------------------------------------------------------------------------------


def fmea_rpn(payload: dict[str, Any]) -> Report:
    """FMEA: RPN = severity x occurrence x detection (each 1-10, detection 10 = hardest to detect).

    A mode needs action when RPN >= rpn_threshold or severity >= 9 (house policy modelled on AIAG 4th-edition practice
    of addressing severity 9-10 whatever the RPN; under AIAG-VDA 2019 Action Priority this is not unconditional).
    Findings: blocker at RPN >= 2 x threshold, major when action is needed, minor at
    RPN >= threshold / 2. pareto_count is the fewest top modes whose RPNs sum to at least 80% of the total.
    Ranking: RPN descending, then severity descending, then id. RPN is the classic AIAG FMEA (4th ed.) ranking;
    the AIAG-VDA 2019 handbook replaces it with Action Priority tables, so treat the threshold as a team policy.
    """

    def work() -> Report:
        modes = payload["modes"]
        threshold = payload["rpn_threshold"]
        _unique([m["id"] for m in modes], "modes", "failure mode")
        ranked = sorted(({"id": m["id"], "rpn": m["severity"] * m["occurrence"] * m["detection"], "severity": m["severity"],
                          "occurrence": m["occurrence"], "detection": m["detection"]} for m in modes),
                        key=lambda m: (-m["rpn"], -m["severity"], m["id"]))
        findings: list[Finding] = []
        total = sum(m["rpn"] for m in ranked)
        cumulative = pareto = 0
        for mode in ranked:
            mode["action_required"] = mode["rpn"] >= threshold or mode["severity"] >= FMEA_ALWAYS_ACT_SEVERITY
            if mode["rpn"] >= 2 * threshold:
                severity = "blocker"
            elif mode["action_required"]:
                severity = "major"
            elif 2 * mode["rpn"] >= threshold:
                severity = "minor"
            else:
                severity = None
            if severity:
                rule = ("rpn-over-threshold" if mode["rpn"] >= threshold
                        else "critical-severity" if mode["severity"] >= FMEA_ALWAYS_ACT_SEVERITY else "rpn-approaching")
                findings.append(_finding(rule, severity, mode["id"],
                                         f"RPN {mode['rpn']} (S{mode['severity']} x O{mode['occurrence']} x D{mode['detection']}) "
                                         f"against threshold {threshold}"))
            if Decimal(cumulative) < PARETO_SHARE * total:
                pareto += 1
                cumulative += mode["rpn"]
        findings = _ordered(findings)
        return {"state": "SUCCESS", "modes_total": len(modes), "requiring_action": sum(m["action_required"] for m in ranked),
                "max_rpn": ranked[0]["rpn"], "total_rpn": total, "pareto_count": pareto, "ranked": ranked,
                "status": _status(findings), "findings": findings}

    return _reply(payload, _guard(work))


# --- 8. error-budget gate for chaos experiments ------------------------------------------------------------------


def chaos_budget_gate(payload: dict[str, Any]) -> Report:
    """Go / no-go of a chaos experiment against the error budget (feed budget_remaining_fraction from A13's obs_budget_remaining).

    Worst-case cost as a fraction of the whole window budget = expected_error_ratio x duration /
    (window minutes x (1 - slo_target)), assuming uniform traffic over the window. Production no-go reasons
    (all blockers): open-incident, budget-exhausted (remaining <= 0), burn-rate-too-high (burn rate >= 1, house
    policy: at 1 the budget is spent exactly by the end of the window per the SRE Workbook, so no spare pace remains),
    experiment-too-expensive (cost > max_cost_fraction),
    reserve-breached (remaining - cost < reserve_fraction). Staging experiments do not spend production budget and
    are always go. max_duration_minutes is the longest run the budget arithmetic alone allows (0 when none).
    """

    def work() -> Report:
        slo, window_days = _dec(payload["slo_target"]), payload["window_days"]
        window_minutes = Decimal(window_days * 1440)
        duration, ratio = _dec(payload["experiment_duration_minutes"]), _dec(payload["expected_error_ratio"])
        remaining, burn = _dec(payload["budget_remaining_fraction"]), _dec(payload["current_burn_rate"])
        reserve, max_cost = _dec(payload["reserve_fraction"]), _dec(payload["max_cost_fraction"])
        budget_minutes = window_minutes * (1 - slo)  # minutes of full outage the window budget allows
        cost = ratio * duration / budget_minutes
        after = remaining - cost
        spendable = min(max_cost, remaining - reserve)
        max_duration = spendable * budget_minutes / ratio if spendable > 0 else Decimal(0)
        findings: list[Finding] = []
        if payload["environment"] == "staging":
            findings.append(_finding("staging-not-gated", "info", "staging", "staging experiments do not spend the production error budget"))
        else:
            if payload["open_incident"]:
                findings.append(_finding("open-incident", "blocker", "production", "an incident is open: no experiments until it is resolved"))
            if remaining <= 0:
                findings.append(_finding("budget-exhausted", "blocker", "production", "no error budget remains in the window"))
            if burn >= 1:
                findings.append(_finding("burn-rate-too-high", "blocker", "production",
                                         f"burn rate {_fmt(burn)} is at or above 1: the budget is already spent at or beyond the sustainable pace"))
            if cost > max_cost:
                findings.append(_finding("experiment-too-expensive", "blocker", "production",
                                         f"worst case spends {_fmt(round(cost, 6))} of the window budget, above the {_fmt(max_cost)} cap"))
            if after < reserve:
                findings.append(_finding("reserve-breached", "blocker", "production",
                                         f"{_fmt(round(after, 6))} of the budget would remain, below the {_fmt(reserve)} reserve"))
        findings = _ordered(findings)
        go = not any(f["severity"] == "blocker" for f in findings)
        return {"state": "SUCCESS", "decision": "go" if go else "no-go", "cost_fraction": _round(cost, 6),
                "remaining_after_fraction": _round(after, 6), "max_duration_minutes": _round(max_duration, 2),
                "status": "pass" if go else "fail", "findings": findings}

    return _reply(payload, _guard(work))


# --- 9. graceful shutdown ----------------------------------------------------------------------------------------


def shutdown_check(payload: dict[str, Any]) -> Report:
    """terminationGracePeriodSeconds versus the time shutdown needs: preStop hook, then SIGTERM drain, sequentially.

    required = prestop_seconds + app_drain_seconds. When the pod serves traffic and stops accepting it on SIGTERM,
    endpoint removal races the signal, so preStop must cover endpoint_propagation_seconds (a sleep is the usual
    form); recommended_grace_seconds adds max(5 s, 10%) of margin to that effective need (house policy). When the kubelet
    finds the preStop hook still running at the grace deadline it grants one 2 s extension; that is not counted.
    Rules: grace-zero (blocker, 0 = kill immediately), sigkill-before-drain (blocker, required > grace),
    thin-margin (minor, margin < 5 s), traffic-after-sigterm (major), sigterm-ignored (major with in-flight work to
    drain, else minor), grace-exceeds-drain-timeout (major, grace > node drain timeout).
    """

    def work() -> Report:
        grace, prestop, drain = payload["termination_grace_period_seconds"], payload["prestop_seconds"], payload["app_drain_seconds"]
        propagation = payload["endpoint_propagation_seconds"]
        serves, keeps = payload["serves_traffic"], payload["keeps_serving_after_sigterm"]
        workload = payload["workload"]
        required = prestop + drain
        margin = grace - required
        effective_prestop = max(prestop, propagation) if serves and not keeps else prestop
        base = effective_prestop + drain
        recommended = base + max(SHUTDOWN_MIN_MARGIN_S, _ceil(Decimal(base) * SHUTDOWN_MARGIN_FRACTION))
        findings: list[Finding] = []
        if grace == 0:
            findings.append(_finding("grace-zero", "blocker", workload, "terminationGracePeriodSeconds is 0: the container is killed immediately"))
        elif required > grace:
            findings.append(_finding("sigkill-before-drain", "blocker", workload,
                                     f"shutdown needs {required}s (preStop {prestop}s + drain {drain}s) but the grace period is {grace}s: "
                                     "SIGKILL drops in-flight work"))
        elif margin < SHUTDOWN_MIN_MARGIN_S:
            findings.append(_finding("thin-margin", "minor", workload, f"only {margin}s of grace remain after preStop and drain"))
        if serves and not keeps and prestop < propagation:
            findings.append(_finding("traffic-after-sigterm", "major", workload,
                                     f"preStop {prestop}s is shorter than endpoint propagation {propagation}s: requests still arrive after SIGTERM"))
        if not payload["handles_sigterm"]:
            findings.append(_finding("sigterm-ignored", "major" if drain > 0 else "minor", workload,
                                     f"the process ignores SIGTERM, so every termination waits the full {grace}s and ends in SIGKILL"))
        node_timeout = payload.get("node_drain_timeout_seconds")
        if node_timeout is not None and grace > node_timeout:
            findings.append(_finding("grace-exceeds-drain-timeout", "major", workload,
                                     f"grace period {grace}s is longer than the {node_timeout}s node drain timeout: the drain gives up first"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "required_seconds": required, "margin_seconds": margin, "recommended_grace_seconds": recommended,
                "status": _status(findings), "findings": findings}

    return _reply(payload, _guard(work))


# --- 10. DR drill cadence ----------------------------------------------------------------------------------------


def drill_cadence(payload: dict[str, Any]) -> Report:
    """Are DR drills run often enough, with evidence, and do they meet RTO and RPO?

    A drill counts for cadence only when its outcome is 'passed' and, if the requirement demands evidence, an
    evidence_ref is present. due = last counted drill + max_interval_days; overdue when elapsed > interval (equal is
    on time). Rules per scenario: never-drilled and no-passing-drill (blocker), drill-overdue (major, blocker when
    elapsed > 2x the interval), drill-without-evidence (major), latest-drill-failed (major, a failed or partial drill
    after the last counted pass), rto-target-missed and rpo-target-missed (major), rto-not-measured and
    rpo-not-measured (minor), drill-without-requirement (info, catches scenario typos).
    """

    def work() -> Report:
        as_of = _ts(payload["as_of"], "as_of")
        requirements = payload["requirements"]
        _unique([r["scenario"] for r in requirements], "requirements", "scenario")
        by_scenario: dict[str, list[tuple[datetime, Report]]] = {}
        first_drill: dict[str, int] = {}
        for index, drill in enumerate(payload["drills"]):
            when = _ts(drill["performed_at"], "drills")
            if when > as_of:
                raise _Invalid("drills", "a drill is dated after as_of")
            by_scenario.setdefault(drill["scenario"], []).append((when, drill))
            first_drill.setdefault(drill["scenario"], index)
        findings: list[Finding] = []
        schedule: list[Report] = []
        compliant = overdue = never = 0
        for req in sorted(requirements, key=lambda r: r["scenario"]):
            name, interval = req["scenario"], req["max_interval_days"]
            drills = sorted(by_scenario.get(name, []), key=lambda d: d[0])
            counted = [d for d in drills if d[1]["outcome"] == "passed" and (not req["require_evidence"] or d[1]["evidence_ref"])]
            before = len(findings)
            if not drills:
                never += 1
                findings.append(_finding("never-drilled", "blocker", name, "no drill recorded for this scenario"))
                schedule.append({"scenario": name, "state": "never-drilled", "last_passed_at": "", "due_at": "", "days_until_due": 0})
            elif not counted:
                never += 1
                findings.append(_finding("no-passing-drill", "blocker", name,
                                         "no passed drill" + (" with evidence" if req["require_evidence"] else "") + " on record"))
                schedule.append({"scenario": name, "state": "never-drilled", "last_passed_at": "", "due_at": "", "days_until_due": 0})
            else:
                last_when, last = counted[-1]
                due = _after(last_when, timedelta(days=interval), "drills")
                until = _seconds(due - as_of) / DAY_SECONDS
                elapsed = _seconds(as_of - last_when) / DAY_SECONDS
                late = elapsed > interval
                if late:
                    overdue += 1
                    findings.append(_finding("drill-overdue", "blocker" if elapsed > 2 * interval else "major", name,
                                             f"last counted drill {_fmt(round(elapsed, 2))} days ago, interval {interval} days"))
                schedule.append({"scenario": name, "state": "overdue" if late else "compliant", "last_passed_at": last_when.isoformat(),
                                 "due_at": due.isoformat(), "days_until_due": _round(until, 2)})
                if drills[-1][0] > last_when and drills[-1][1]["outcome"] != "passed":
                    findings.append(_finding("latest-drill-failed", "major", name,
                                             f"latest drill ({drills[-1][1]['outcome']}) is newer than the last counted pass"))
                for kind, measured_key, target_key in (("rto", "measured_rto_minutes", "target_rto_minutes"),
                                                       ("rpo", "measured_rpo_minutes", "target_rpo_minutes")):
                    target = req.get(target_key)
                    if target is None:
                        continue
                    if measured_key not in last:
                        findings.append(_finding(f"{kind}-not-measured", "minor", name, f"the last counted drill did not measure {kind.upper()}"))
                    elif _dec(last[measured_key]) > _dec(target):
                        findings.append(_finding(f"{kind}-target-missed", "major", name,
                                                 f"measured {kind.upper()} {_fmt(last[measured_key])} min exceeds the {_fmt(target)} min target"))
            if drills and req["require_evidence"] and drills[-1][1]["outcome"] == "passed" and not drills[-1][1]["evidence_ref"]:
                findings.append(_finding("drill-without-evidence", "major", name,
                                         f"the latest passed drill ({drills[-1][0].isoformat()}) has no evidence_ref and does not count"))
            if not any(f["severity"] in ("blocker", "major") for f in findings[before:]):
                compliant += 1
        required = {r["scenario"] for r in requirements}
        for scenario in sorted(set(by_scenario) - required):
            findings.append(_finding("drill-without-requirement", "info", f"drills[{first_drill[scenario]}]",
                                     "drills exist for a scenario that has no cadence requirement"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "scenarios_total": len(requirements), "compliant": compliant, "overdue": overdue,
                "never_drilled": never, "schedule": schedule, "status": _status(findings), "findings": findings}

    return _reply(payload, _guard(work))


# --- 11. load-shedding policy ------------------------------------------------------------------------------------


def load_shed_check(payload: dict[str, Any]) -> Report:
    """Load-shedding policy of one service: demand by Little's law, priority-ordered admission, queue bounds.

    needed concurrency = ceil(expected_rps x p99_service_ms / 1000): Little's law L = lambda x W, with the p99 service
    time standing in for the mean W, so it overstates demand (the safe side). Admission walks classes by priority
    (1 = most important, ties by name): first each class's reservation, then the rest up to min(needed,
    max_concurrency) while capacity remains; the shortfall is shed. Queue wait at a full queue = queue_depth x
    p99_service_ms / max_concurrency; the request that joins a full queue then needs one more p99 of service.
    Rules: reservations-exceed-capacity, unbounded-queue, non-sheddable-class-starved (blocker);
    queue-wait-exceeds-timeout (major, wait + p99 service > client_timeout_ms: the work finishes after the caller left);
    shed-at-nominal-load (minor); no-priority-differentiation (minor, fewer than two priorities);
    critical-class-unreserved (minor, a top-priority class reserves less than it needs).
    It is a steady-state model: bursts, retries and warm-up are out of scope.
    """

    def work() -> Report:
        capacity, timeout = payload["capacity_concurrency"], _dec(payload["client_timeout_ms"])
        classes = payload["classes"]
        _unique([c["name"] for c in classes], "classes", "class")
        rows = []
        for c in classes:
            if c["reserved_concurrency"] > c["max_concurrency"]:
                raise _Invalid("classes", "a class's reserved_concurrency exceeds its max_concurrency")
            needed = _ceil(_dec(c["expected_rps"]) * _dec(c["p99_service_ms"]) / 1000)
            rows.append({"c": c, "needed": needed, "cap": min(needed, c["max_concurrency"]), "admitted": 0})
        rows.sort(key=lambda r: (r["c"]["priority"], r["c"]["name"]))
        left = capacity
        for row in rows:
            take = min(row["cap"], row["c"]["reserved_concurrency"], left)
            row["admitted"] = take
            left -= take
        for row in rows:
            take = min(row["cap"] - row["admitted"], left)
            row["admitted"] += take
            left -= take
        findings: list[Finding] = []
        reserved_total = sum(c["reserved_concurrency"] for c in classes)
        if reserved_total > capacity:
            findings.append(_finding("reservations-exceed-capacity", "blocker", "capacity",
                                     f"reservations total {reserved_total} but capacity is {capacity}"))
        top = min(c["priority"] for c in classes)
        if len({c["priority"] for c in classes}) < 2:
            findings.append(_finding("no-priority-differentiation", "minor", "classes",
                                     "all traffic shares one priority: overload sheds at random, not by importance"))
        plan = []
        for row in rows:
            c, needed = row["c"], row["needed"]
            shed = Decimal(needed - row["admitted"]) * 100 / needed if needed else Decimal(0)
            plan.append({"class": c["name"], "needed_concurrency": needed, "admitted_concurrency": row["admitted"],
                         "shed_percent": _round(shed, 2)})
            if shed > 0 and not c["sheddable"]:
                findings.append(_finding("non-sheddable-class-starved", "blocker", c["name"],
                                         f"needs {needed} concurrent slots, admitted {row['admitted']} ({_fmt(round(shed, 2))}% shed) but is not sheddable"))
            elif shed > 0:
                findings.append(_finding("shed-at-nominal-load", "minor", c["name"],
                                         f"needs {needed} concurrent slots, admitted {row['admitted']}: {_fmt(round(shed, 2))}% shed at expected load"))
            if not c["queue_bounded"]:
                findings.append(_finding("unbounded-queue", "blocker", c["name"], "unbounded queue: overload grows latency without limit"))
            elif c["queue_depth"]:
                wait = Decimal(c["queue_depth"]) * _dec(c["p99_service_ms"]) / c["max_concurrency"]
                total = wait + _dec(c["p99_service_ms"])
                if total > timeout:
                    findings.append(_finding("queue-wait-exceeds-timeout", "major", c["name"],
                                             f"a full queue waits {_fmt(round(wait, 2))} ms and then needs {_fmt(_dec(c['p99_service_ms']))} ms of "
                                             f"service: {_fmt(round(total, 2))} ms, longer than the {_fmt(timeout)} ms client timeout"))
            if c["priority"] == top and c["reserved_concurrency"] < row["cap"]:
                findings.append(_finding("critical-class-unreserved", "minor", c["name"],
                                         f"top-priority class reserves {c['reserved_concurrency']} of the {row['cap']} slots it needs"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "total_needed_concurrency": sum(r["needed"] for r in rows), "capacity_concurrency": capacity,
                "overloaded": any(r["admitted"] < r["needed"] for r in rows), "shed_plan": plan, "status": _status(findings),
                "findings": findings}

    return _reply(payload, _guard(work))


# --- 12. StatefulSet quorum safety -------------------------------------------------------------------------------


def quorum_safety(payload: dict[str, Any]) -> Report:
    """Quorum survival of a replicated StatefulSet (etcd, ZooKeeper, Consul, Raft or Kafka-style) across zones and drains.

    quorum = floor(n / 2) + 1 unless quorum_size overrides it; fault tolerance = n - quorum. A zone may hold at most
    fault-tolerance members to survive its loss, so min_zones_required = ceil(n / tolerance) (-1 when tolerance is 0).
    pdb_disruptions_allowed is the budget with all members ready (-1 without a PodDisruptionBudget).
    Rules: quorum-lost (blocker, unavailable members > tolerance), quorum-below-majority (blocker, a quorum_size under the
    majority when consensus is true, the default: a consensus store can split brain; consensus false is a non-consensus
    replica set such as Kafka min.insync.replicas, where a quorum below majority is allowed and reports no finding),
    no-failure-headroom (major, unavailable == tolerance > 0), no-fault-tolerance (major, tolerance 0),
    even-member-count (minor, default quorum and even n), zone-loss-breaks-quorum (major), pdb-missing (major,
    n >= 2), pdb-unsatisfiable (blocker, desired healthy above n: the rule of resil_pdb_check), pdb-allows-quorum-loss
    (blocker, the budget permits more simultaneous evictions than the tolerance), pdb-blocks-eviction (major, 0 allowed with
    tolerance >= 1: the rule of resil_pdb_check),
    zone-loss-while-degraded (major, some but fewer than tolerance members down, and losing the fullest zone on top
    would drop below quorum; down members assumed outside that zone; at tolerance no-failure-headroom already applies),
    survives_zone_loss describes the healthy cluster. Only majority arithmetic: learners,
    witnesses, rack awareness and leader placement are out of scope.
    """

    def work() -> Report:
        workload, n = payload["workload"], payload["replicas"]
        zones = payload["members_per_zone"]
        if sum(zones) != n:
            raise _Invalid("members_per_zone", f"members_per_zone sums to {sum(zones)} but replicas is {n}")
        custom = payload.get("quorum_size")
        consensus = payload.get("consensus", True)
        if not isinstance(consensus, bool):
            raise _Invalid("consensus", "consensus must be a boolean: true for a consensus store, false for a replica set")
        if custom is not None and custom > n:
            raise _Invalid("quorum_size", "quorum_size cannot exceed replicas")
        unavailable = payload["unavailable_members"]
        if unavailable > n:
            raise _Invalid("unavailable_members", "unavailable_members cannot exceed replicas")
        quorum = custom if custom is not None else n // 2 + 1
        tolerance = n - quorum
        desired = _desired_healthy(n, payload.get("min_available"), payload.get("max_unavailable"), f"workload {workload}",
                                   ("min_available", "max_unavailable"))
        worst = max(zones)
        survives = n - worst >= quorum
        findings: list[Finding] = []
        majority = n // 2 + 1
        if custom is not None and custom < majority and consensus:
            findings.append(_finding("quorum-below-majority", "blocker", workload,
                                     f"quorum {custom} is below the majority {majority} of {n} members: two partitions can each reach "
                                     "quorum and diverge; set consensus false only for a non-consensus replica set such as Kafka"))
        if unavailable > tolerance:
            findings.append(_finding("quorum-lost", "blocker", workload,
                                     f"{_count(unavailable, 'member')} down, more than the tolerance of {tolerance}: no quorum"))
        elif unavailable == tolerance and unavailable > 0:
            findings.append(_finding("no-failure-headroom", "major", workload,
                                     f"{_count(unavailable, 'member')} down: one more loss breaks quorum"))
        if tolerance == 0:
            verb = "tolerates" if n == 1 else "tolerate"
            findings.append(_finding("no-fault-tolerance", "major", workload, f"{_count(n, 'member')} with quorum {quorum} {verb} no failure"))
        if custom is None and n % 2 == 0:
            findings.append(_finding("even-member-count", "minor", workload,
                                     f"{n} members tolerate {tolerance} failures, the same as {n - 1}: the extra member adds cost, not safety"))
        if not survives:
            findings.append(_finding("zone-loss-breaks-quorum", "major", workload,
                                     f"the fullest zone holds {worst} of {_count(n, 'member')}; losing it leaves {n - worst} < quorum {quorum}"))
        elif 0 < unavailable < tolerance and n - worst - unavailable < quorum:
            # survives_zone_loss describes a healthy cluster; members already down (assumed outside the lost zone) shrink it
            findings.append(_finding("zone-loss-while-degraded", "major", workload,
                                     f"{_count(unavailable, 'member')} already down: losing the fullest zone ({worst} members) as well can leave "
                                     f"{n - worst - unavailable} < quorum {quorum}"))
        allowed = -1
        if desired is None:
            if n >= 2:
                findings.append(_finding("pdb-missing", "major", workload, "no PodDisruptionBudget: parallel node drains can evict beyond the quorum tolerance"))
        else:
            allowed = max(0, n - desired)
            if desired > n:
                findings.append(_finding("pdb-unsatisfiable", "blocker", workload,
                                         f"budget demands {desired} healthy members of {n}: every eviction is refused"))
            elif allowed > tolerance:
                findings.append(_finding("pdb-allows-quorum-loss", "blocker", workload,
                                         f"budget allows {allowed} simultaneous evictions but quorum tolerates {tolerance}"))
            elif allowed == 0 and tolerance >= 1:
                findings.append(_finding("pdb-blocks-eviction", "major", workload, "budget allows 0 evictions: node drains and upgrades stall"))
        findings = _ordered(findings)
        return {"state": "SUCCESS", "quorum": quorum, "fault_tolerance": tolerance, "zones_occupied": sum(count > 0 for count in zones),
                "worst_zone_members": worst, "survives_zone_loss": survives, "max_members_per_zone": tolerance,
                "min_zones_required": -(-n // tolerance) if tolerance > 0 else -1, "pdb_disruptions_allowed": allowed,
                "status": _status(findings), "findings": findings}

    return _reply(payload, _guard(work))
