"""A16 Cloud-Native Architect tools: pure analyses of where and how a system runs in the cloud.

Tools: cloud_twelve_factor_audit (twelve-factor conformance per factor), cloud_composite_availability (series/parallel
availability math against a target), cloud_dr_feasibility (DR strategy versus RTO/RPO), cloud_data_residency (data class x
region x residency rule), cloud_landing_zone_lint (account/OU hierarchy, guardrails, audit, break-glass),
cloud_wellarch_scorecard (six-pillar weighted scorecard with ranked remediation), cloud_blast_radius (failure-domain
blast radius and single points of failure), cloud_cell_capacity (cell count with N+1 headroom), cloud_cidr_plan (CIDR
overlap and sizing with the stdlib ipaddress module), cloud_managed_vs_self_hosted (weighted decision matrix),
cloud_replica_placement (quorum safety across availability zones), cloud_latency_budget (per-hop latency composition).

Public sources mirrored by the small embedded tables:
* twelve-factor methodology, https://12factor.net (factor names and numbering).
* AWS Well-Architected Framework, six pillars (operational excellence, security, reliability, performance efficiency,
  cost optimization, sustainability) and its disaster-recovery whitepaper strategies (backup and restore, pilot light,
  warm standby, multi-site active/active).
* AWS Organizations: service control policies do not restrict the management account; AWS and Azure reserve 5 addresses
  per subnet; RFC 1918 / RFC 6598 / RFC 4193 private ranges.
Everything else in the findings is this lane's house policy, not a standard, and the tool contracts say so: the twelve-factor
startup (30 s) and deploy-gap (1 day) limits, the break-glass test age (180 days), the landing-zone guardrail vocabulary and the
guardrails each account role needs, the DR timing model (RTO = detect + cutover + validation + the strategy's own step; active/active
skips validation; RPO = backup interval or replication lag) and the readiness order of the strategies, residency severities by
copy kind, the 10% tail-amplification and half-of-target dominant-stage limits, the criticality-8 single-point-of-failure severity,
the 5-point tie margin, and the scorecard weights (none 0, medium 1/2, high 1). They are named constants below.

Identifiers a caller supplies (ids, names, regions, questions) are echoed in findings. A value that looks like a credential, a
64-hex key, a recovery phrase or a PEM block is replaced by a `[redacted:<kind>:<length>:<digest6>]` label before any logic runs, so
it never reaches an id, a summary or an error message; the label is stable, so references between fields still match.
"""
from __future__ import annotations

import functools
import hashlib
import ipaddress
import math
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, localcontext
from fractions import Fraction
from typing import Any, overload

from swarm.redact import redact_text
from swarm.tools.support import respond

SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
MINUTES_PER_YEAR = 525600  # 365 days, the convention behind "99.9% = 8.76 h/year"

# A registry-validated payload, or one record taken from it. agents.json's input_schema is checked before a handler runs; its nested
# shapes would need a TypedDict per tool to express, so the values read from a payload are Any.
Payload = dict[str, Any]
_Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# twelve-factor: judgement thresholds
STARTUP_MAX_S = Decimal("30")
DEPLOY_GAP_MAX_DAYS = Decimal("1")
# the registered schema bounds: a value outside them is INVALID_INPUT, never a conformant or a failing answer
STARTUP_RANGE_S = (Decimal("0"), Decimal("86400"))
DEPLOY_GAP_RANGE_DAYS = (Decimal("0"), Decimal("3650"))

# disaster recovery: ordered from least to most ready (and least to most costly)
DR_STRATEGIES = ("backup_restore", "pilot_light", "warm_standby", "active_active")

# landing zone
BREAK_GLASS_TEST_MAX_DAYS = 180
LANDING_ROLES = ("management", "security_audit", "log_archive", "network", "shared_services",
                 "workload_prod", "workload_nonprod", "sandbox")
BASELINE_GUARDRAILS = ("deny-leave-organization", "deny-root-user-actions", "protect-audit-logging", "restrict-regions")
ROLE_GUARDRAILS = {
    "workload_prod": ("deny-public-storage", "require-encryption-at-rest"),
    "log_archive": ("deny-log-deletion",),
    "sandbox": ("budget-alarm",),
}

# Well-Architected
PILLARS = ("operational_excellence", "security", "reliability", "performance_efficiency", "cost_optimization", "sustainability")
RISK_FACTOR = {"none": Fraction(0), "medium": Fraction(1, 2), "high": Fraction(1)}

# blast radius
RANKING_LIMIT = 10

# networks
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(text) for text in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",  # RFC 1918
    "100.64.0.0/10",  # RFC 6598 shared address space (commonly used for pod ranges)
    "fc00::/7",  # RFC 4193 unique local addresses
))
DEFAULT_RESERVED_ADDRESSES = 5
FINDINGS_LIMIT = 100
CIDR_FORM = re.compile(r"[0-9A-Fa-f:.]+/[0-9]{1,3}")  # address/prefix-length only: no bare address, netmask, hostmask or scope id
GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")  # what cloud providers allocate for IPv6 VPCs; not an RFC 4193 range
DOCUMENTATION_V6 = ipaddress.ip_network("2001:db8::/32")  # RFC 3849: inside 2000::/3 but never routable

# latency
TAIL_AMPLIFICATION_PCT = Decimal("10")
DOMINANT_STAGE_SHARE = Decimal("0.5")

# decision matrix
DEFAULT_TIE_MARGIN_PCT = Decimal("5")

# availability: enough digits that 1 - 1e-800 (50 replicas of 99.99999999999999) is not rounded to 1
COMPOSITE_PRECISION = 1500


def _invalid(field: str, message: str) -> dict[str, object]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _finding(fid: str, severity: str, summary: str) -> dict[str, str]:
    return {"id": fid, "severity": severity, "summary": summary}


def _ordered(findings: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(findings, key=lambda item: (SEVERITY_RANK[item["severity"]], item["id"]))


def _verdict(findings: Sequence[Mapping[str, str]]) -> str:
    """fail on any major or worse finding, warn on a minor one, else pass."""
    worst = min((SEVERITY_RANK[item["severity"]] for item in findings), default=SEVERITY_RANK["info"])
    return "fail" if worst <= SEVERITY_RANK["major"] else "warn" if worst == SEVERITY_RANK["minor"] else "pass"


def _round(value: Fraction | Decimal | float, places: int = 2) -> float:
    """Round half up on the exact value (a Fraction or Decimal), never on its binary float; clamp to the float range."""
    with localcontext() as ctx:
        ctx.prec = 400
        exact = Decimal(value.numerator) / Decimal(value.denominator) if isinstance(value, Fraction) else Decimal(value)
        rounded = float(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))
    return max(-sys.float_info.max, min(sys.float_info.max, rounded)) + 0.0  # + 0.0 turns -0.0 into 0.0


def _pct(value: Fraction | Decimal | float, places: int = 2) -> float:
    return _round(value, places)


def _respond(payload: Payload, work: Callable[[], dict[str, object]]) -> dict[str, object]:
    """support.respond with its result typed: `respond` is untyped and returns the dict that `work` produces."""
    result: dict[str, object] = respond(payload, work)
    return result


def _nested(inner: _Network, outer: _Network) -> bool:
    """inner.subnet_of(outer); networks of different IP versions are never nested, so the answer is False for them."""
    if isinstance(inner, ipaddress.IPv4Network) and isinstance(outer, ipaddress.IPv4Network):
        return inner.subnet_of(outer)
    if isinstance(inner, ipaddress.IPv6Network) and isinstance(outer, ipaddress.IPv6Network):
        return inner.subnet_of(outer)
    return False


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        (dupes if value in seen else seen).add(value)
    return sorted(dupes)


# --- secret-shaped identifiers ---------------------------------------------------------------------------------

_HEX_KEY = re.compile(r"(?<![0-9A-Fa-f])(?:0x)?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
MNEMONIC_LENGTHS = (12, 15, 18, 21, 24)


def _secret_kind(text: str) -> str:
    """Kind of credential a caller-supplied string looks like, else ''. Best effort: swarm.redact patterns plus bare keys and phrases."""
    if _HEX_KEY.search(text):
        return "hex-key"
    if "-----BEGIN" in text:
        return "pem"
    words = text.split()
    if len(words) in MNEMONIC_LENGTHS and all(w.isascii() and w.isalpha() and w.islower() and 3 <= len(w) <= 8 for w in words):
        return "phrase"
    return "credential" if redact_text(text) != text else ""


def _safe_label(text: str) -> str:
    """The text itself, or a stable `[redacted:<kind>:<length>:<digest6>]` label when it looks like a credential (also used by the scan script)."""
    kind = _secret_kind(text)
    if not kind:
        return text
    return f"[redacted:{kind}:{len(text)}:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:6]}]"


@overload
def _scrub(value: str) -> str: ...
@overload
def _scrub(value: list[object]) -> list[object]: ...
@overload
def _scrub(value: dict[str, object]) -> dict[str, object]: ...
@overload
def _scrub(value: object) -> object: ...
def _scrub(value: object) -> object:
    if isinstance(value, str):
        return _safe_label(value)
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


def _safe(handler: Callable[[Payload], dict[str, object]]) -> Callable[[Payload], dict[str, object]]:
    """Run a handler on a payload whose secret-shaped strings are already labels."""

    @functools.wraps(handler)
    def wrapper(payload: Payload) -> dict[str, object]:
        return handler(_scrub(payload))

    return wrapper


def _unique(finding_id: str, taken: dict[str, int]) -> str:
    """The first use of an id keeps it; a repeat gets #2, #3 so that findings stay addressable by id."""
    taken[finding_id] = taken.get(finding_id, 0) + 1
    return finding_id if taken[finding_id] == 1 else f"{finding_id}#{taken[finding_id]}"


# --- 1. twelve-factor audit ------------------------------------------------------------------------------------

FACTOR_NAMES = {1: "codebase", 2: "dependencies", 3: "config", 4: "backing_services", 5: "build_release_run", 6: "processes",
                7: "port_binding", 8: "concurrency", 9: "disposability", 10: "dev_prod_parity", 11: "logs", 12: "admin_processes"}


def _is_int(value: object, expected: int) -> bool:
    """True when value is the integer `expected`. A boolean is not an integer here, although True == 1."""
    return type(value) is int and value == expected


def _real(value: object) -> bool:
    """A real number that is not a boolean: an int, or a finite float. NaN and infinity are not real."""
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, int)


def _huge(value: object) -> bool:
    """An integer larger than any float (over 1024 bits), so out of range for every bound here. It is judged before any conversion:
    Decimal(repr(value)) raises ValueError past the integer string limit, so an integer such as 10**5000 cannot be converted at all."""
    return isinstance(value, int) and not isinstance(value, bool) and value.bit_length() > 1024


def _number_error(value: object, low: int | Decimal, high: int | Decimal, *, low_open: bool = False, high_open: bool = False,
                  integer: bool = False) -> bool:
    """True when value is not a usable number within the range: a boolean, NaN or infinity, an integer beyond the conversion limit, a
    float where an integer is required (an integer field takes an int only), or a value outside [low, high] (an open end excludes its
    bound). The checks run before any conversion, so a value that cannot be converted is reported as out of range instead of raising."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    if isinstance(value, float) and not math.isfinite(value):
        return True
    if _huge(value) or (integer and not isinstance(value, int)):
        return True
    exact = _dec(value)
    if exact < low or exact > high:
        return True
    return (low_open and exact == low) or (high_open and exact == high)


def _at_most(value: object, limit: Decimal) -> bool:
    """True when value is a real number no greater than limit. Any other type, booleans included, does not pass."""
    return _real(value) and not _huge(value) and _dec(value) <= limit


def _outside_range(value: object, low: Decimal, high: Decimal) -> bool:
    """True when value is a number outside [low, high], including NaN, infinity and an integer too large to convert. Other types are
    not judged here: their own check fails, so a boolean is never reported as out of range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if _huge(value):
        return True
    return not _real(value) or not low <= _dec(value) <= high


# (factor, descriptor key, passes(value), severity when failing, summary). Booleans are listed with the passing value.
TWELVE_FACTOR_CHECKS: tuple[tuple[int, str, Callable[[object], bool], str, str], ...] = (
    (1, "codebase_in_vcs", lambda v: v is True, "blocker", "the app is not tracked in version control"),
    (1, "repos_per_app", lambda v: _is_int(v, 1), "major", "one app must map to exactly one codebase; shared code belongs in libraries"),
    (2, "dependencies_declared", lambda v: v is True, "major", "dependencies are not declared in a manifest"),
    (2, "dependencies_locked", lambda v: v is True, "minor", "dependency versions are not pinned by a lockfile"),
    (2, "assumes_system_tools", lambda v: v is False, "minor", "the app relies on system-wide tools instead of isolated dependencies"),
    (3, "config_from_environment", lambda v: v is True, "major", "config is not read from the environment"),
    (3, "secrets_in_codebase", lambda v: v is False, "blocker", "secrets or credentials are committed to the codebase"),
    (3, "config_grouped_by_named_environment", lambda v: v is False,
     "minor", "config is grouped into named environments (development/test/production) instead of granular per-deploy variables"),
    (4, "backing_services_via_config_urls", lambda v: v is True, "major", "backing services are not attached through config URLs/credentials"),
    (4, "hardcoded_backing_service_hosts", lambda v: _is_int(v, 0), "major", "backing service hosts are hardcoded in the code"),
    (5, "build_release_run_separated", lambda v: v is True, "major", "build, release and run are not separate stages"),
    (5, "immutable_releases", lambda v: v is True, "major", "releases are not immutable and uniquely identified"),
    (5, "code_changed_at_runtime", lambda v: v is False, "major", "code is changed in the running environment"),
    (6, "stateless_processes", lambda v: v is True, "major", "processes are not stateless and share-nothing"),
    (6, "sticky_sessions", lambda v: v is False, "minor", "sticky sessions keep session state in process memory"),
    (6, "local_disk_state_across_requests", lambda v: v is False, "major", "state survives requests only on the local disk"),
    (7, "self_contained_port_binding", lambda v: v is True, "major", "the app is not self-contained and does not export HTTP by binding a port"),
    (7, "port_from_environment", lambda v: v is True, "minor",
     "the bind port is not taken from the environment (house rule from factor III; factor VII itself only requires binding a port)"),
    (8, "scales_out_via_processes", lambda v: v is True, "major", "the app cannot scale out horizontally through the process model"),
    (8, "daemonizes_itself", lambda v: v is False, "minor", "the app daemonizes or writes PID files instead of leaving that to the process manager"),
    (9, "startup_seconds", lambda v: _at_most(v, STARTUP_MAX_S), "minor", f"startup takes longer than {STARTUP_MAX_S} s"),
    (9, "handles_sigterm_gracefully", lambda v: v is True, "major", "SIGTERM does not trigger a graceful shutdown"),
    (9, "jobs_idempotent_or_requeued", lambda v: v is True, "minor", "jobs are neither idempotent nor returned to the queue on a crash"),
    (10, "deploy_gap_days", lambda v: _at_most(v, DEPLOY_GAP_MAX_DAYS), "minor", f"code takes longer than {DEPLOY_GAP_MAX_DAYS} day to reach production"),
    (10, "dev_prod_backing_service_mismatch", lambda v: v is False, "major", "development and production use different backing services"),
    (11, "logs_to_stdout", lambda v: v is True, "major", "logs are not written as an event stream to stdout"),
    (11, "app_manages_log_files", lambda v: v is False, "minor", "the app routes or rotates its own log files"),
    (12, "admin_tasks_run_as_one_off_processes", lambda v: v is True, "minor", "admin tasks do not run as one-off processes"),
    (12, "admin_code_shipped_with_release", lambda v: v is True, "minor", "admin code is not shipped with the release it operates on"),
)


@_safe
def twelve_factor_audit(payload: Payload) -> dict[str, object]:
    """Score a service descriptor against the twelve factors: percent of checks passed per factor and overall.

    A check the descriptor does not declare counts as not passed. Unknown is not conformant, so an undeclared check yields a
    finding at the severity of its own check, capped at major (it is not a demonstrated violation, so never a blocker): a
    descriptor that leaves out a major-class check cannot pass or warn.
    """

    def work() -> dict[str, object]:
        service = payload["service"]
        for key, (low, high) in (("startup_seconds", STARTUP_RANGE_S), ("deploy_gap_days", DEPLOY_GAP_RANGE_DAYS)):
            if key in service and _outside_range(service[key], low, high):
                return _invalid(key, f"{key} must be a number from {low} to {high}")
        passed = dict.fromkeys(FACTOR_NAMES, 0)
        total = dict.fromkeys(FACTOR_NAMES, 0)
        findings: list[dict[str, str]] = []
        for factor, key, passes, severity, summary in TWELVE_FACTOR_CHECKS:
            total[factor] += 1
            if key not in service:  # not demonstrated is not conformant, but it is not a known violation either
                findings.append(_finding(f"factor-{factor:02d}:{key}", "minor" if severity == "minor" else "major",
                                         f"factor {factor} {FACTOR_NAMES[factor]}: {key} is not declared"))
            elif passes(service[key]):
                passed[factor] += 1
            else:
                findings.append(_finding(f"factor-{factor:02d}:{key}", severity, f"factor {factor} {FACTOR_NAMES[factor]}: {summary}"))
        factors = [
            {"factor": factor, "name": FACTOR_NAMES[factor], "passed": passed[factor], "total": total[factor],
             "score_pct": _pct(Decimal(passed[factor] * 100) / Decimal(total[factor]))}
            for factor in sorted(FACTOR_NAMES)
        ]
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "score_pct": _pct(Decimal(sum(passed.values()) * 100) / Decimal(sum(total.values()))),
            "conformant_factors": sum(1 for factor in FACTOR_NAMES if passed[factor] == total[factor]),
            "factors": factors,
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 2. composite availability ---------------------------------------------------------------------------------


def _nines(unavailability: Decimal) -> Decimal:
    """-log10 of the unavailability (3 = 99.9%), from its first 60 significant digits."""
    if unavailability <= 0:
        return Decimal(0)
    with localcontext() as ctx:
        ctx.prec = 60
        return -(+unavailability).log10()


def _k_of_n(availability: Decimal, replicas: int, needed: int) -> Decimal:
    """P(at least `needed` of `replicas` independent replicas are up), each up with probability `availability`."""
    down = Decimal(1) - availability
    return sum((Decimal(math.comb(replicas, up)) * availability**up * down ** (replicas - up) for up in range(needed, replicas + 1)),
               Decimal(0))


@_safe
def composite_availability(payload: Payload) -> dict[str, object]:
    """Series composition of tiers; each tier is k-of-n replicas with independent failures unless they share a failure domain.

    The arithmetic runs at COMPOSITE_PRECISION digits so that the unavailability of a heavily replicated chain (down to 1e-800)
    is not rounded to zero; `nines` is -log10 of that exact unavailability. A tier flagged `shared_failure_domain` is modelled as
    one unit with the per-replica availability (no redundancy credit); replica faults independent of the shared domain are not
    modelled, so a shared tier that needs every replica is an upper bound. Series composition and k-of-n binomial: standard
    reliability arithmetic; the year is 365 days (525600 minutes).
    """

    def work() -> dict[str, object]:
        tiers = payload["tiers"]
        dupes = _duplicates([tier["id"] for tier in tiers])
        if dupes:
            return _invalid("tiers", f"tier ids must be unique: {', '.join(dupes)}")
        for tier in tiers:
            if _number_error(tier["replicas"], 1, 50, integer=True):
                return _invalid("replicas", f"tier {tier['id']}: replicas must be an integer from 1 to 50")
            if _number_error(tier["min_required"], 0, 50, integer=True):
                return _invalid("min_required", f"tier {tier['id']}: min_required must be an integer from 0 to 50")
        for tier in tiers:
            if tier["min_required"] > tier["replicas"]:
                return _invalid("tiers", f"tier {tier['id']}: min_required {tier['min_required']} exceeds replicas {tier['replicas']}")
        for tier in tiers:
            # 100 gives 0**0 in the k-of-n term (no replica down), and so does 0 when no replica is needed (no replica up)
            if _number_error(tier["availability_pct"], 0, 100, high_open=True):
                return _invalid("availability_pct", f"tier {tier['id']}: availability_pct must be at least 0 and below 100")
            if _dec(tier["availability_pct"]) == 0 and tier["min_required"] == 0:
                return _invalid("availability_pct", f"tier {tier['id']}: availability_pct 0 needs min_required of at least 1")
        # the target is an open interval: 0 makes every chain pass and 100 makes every chain fail
        if _number_error(payload["target_pct"], 0, 100, low_open=True, high_open=True):
            return _invalid("target_pct", "target_pct must be above 0 and below 100")
        with localcontext() as ctx:
            ctx.prec = COMPOSITE_PRECISION
            target = _dec(payload["target_pct"]) / 100
            effective: list[tuple[Decimal, str, Payload]] = []
            for tier in tiers:
                single = _dec(tier["availability_pct"]) / 100
                # replicas in one failure domain fail together: no redundancy credit is given
                value = single if tier["shared_failure_domain"] else _k_of_n(single, tier["replicas"], tier["min_required"])
                effective.append((value, tier["id"], tier))
            composite = Decimal(1)
            for value, _, _ in effective:
                composite *= value
            unavailability = Decimal(1) - composite
            nines = _nines(unavailability)
            effective.sort(key=lambda item: (item[0], item[1]))
            meets = composite >= target
            findings: list[dict[str, str]] = []
            for value, tier_id, tier in effective:
                if value < target:
                    findings.append(_finding(f"tier-below-target:{tier_id}", "major",
                                             f"tier {tier_id} offers {_pct(value * 100, 6)}% which caps the chain below the {payload['target_pct']}% target"))
                if tier["shared_failure_domain"] and tier["replicas"] > 1:
                    findings.append(_finding(f"redundancy-not-credited:{tier_id}", "info",
                                             f"tier {tier_id} replicas share a failure domain, so redundancy gives no availability credit"))
            if not meets and not any(item["severity"] == "major" for item in findings):
                findings.append(_finding("chain-below-target", "major",
                                         f"no single tier misses the target but the series product {_pct(composite * 100, 6)}% does"))
            findings = _ordered(findings)
            return {
                "state": "SUCCESS",
                "composite_pct": _pct(composite * 100, 6),
                "nines": _pct(nines, 2),
                "downtime_min_per_year": _pct(unavailability * MINUTES_PER_YEAR, 2),
                "target_downtime_min_per_year": _pct((Decimal(1) - target) * MINUTES_PER_YEAR, 2),
                "meets_target": meets,
                "weakest_tier": effective[0][1],
                "tiers": [{"id": tier_id, "effective_pct": _pct(value * 100, 6)} for value, tier_id, _ in effective],
                "findings": findings,
                "verdict": "pass" if meets else "fail",
            }

    return _respond(payload, work)


# --- 3. DR feasibility -----------------------------------------------------------------------------------------


def _replication_rpo_min(payload: Payload) -> Decimal | None:
    if payload.get("synchronous_replication") is True:
        return Decimal(0)
    if "replication_lag_s" in payload:
        return _dec(payload["replication_lag_s"]) / 60
    return None


def _dr_model(strategy: str, payload: Payload) -> tuple[Decimal, Decimal] | str:
    """(rto_min, rpo_min) of one strategy, or the name of the first input it still needs."""
    base = _dec(payload["detect_decide_min"]) + _dec(payload["traffic_cutover_min"])
    validation = _dec(payload["validation_min"])
    if strategy == "backup_restore":
        for key in ("backup_interval_min", "infra_provision_min", "data_restore_min"):
            if key not in payload:
                return key
        return (base + validation + _dec(payload["infra_provision_min"]) + _dec(payload["data_restore_min"]),
                _dec(payload["backup_interval_min"]))
    lag = _replication_rpo_min(payload)
    if strategy == "active_active":
        return (base, lag) if lag is not None else "replication_lag_s"
    step = "compute_start_min" if strategy == "pilot_light" else "compute_scale_min"
    if step not in payload:
        return step
    return (base + validation + _dec(payload[step]), lag) if lag is not None else "replication_lag_s"


@_safe
def dr_feasibility(payload: Payload) -> dict[str, object]:
    """Whether the declared DR strategy meets RTO and RPO (inclusive), and the least ready strategy that would.

    House model, not a standard: RTO = detect_decide + traffic_cutover + validation + the strategy's step (backup_restore:
    infra_provision + data_restore; pilot_light: compute_start; warm_standby: compute_scale); active_active has no step and no
    validation. RPO = backup_interval (backup_restore) or the replication lag (0 when synchronous). The strategy names and
    their order (least to most ready, the usual order of cost and complexity) follow the AWS disaster-recovery whitepaper; the
    tool ranks by that order and gives no money figure.
    """

    def work() -> dict[str, object]:
        strategy = payload["strategy"]
        declared = _dr_model(strategy, payload)
        if isinstance(declared, str):
            return _invalid(declared, f"{declared} is required to evaluate strategy {strategy}")
        rto, rpo = declared
        rto_target, rpo_target = _dec(payload["rto_target_min"]), _dec(payload["rpo_target_min"])
        meets_rto, meets_rpo = rto <= rto_target, rpo <= rpo_target
        evaluated: list[str] = []
        sufficient = ""
        for name in DR_STRATEGIES:
            model = _dr_model(name, payload)
            if isinstance(model, str):
                continue
            evaluated.append(name)
            if not sufficient and model[0] <= rto_target and model[1] <= rpo_target:
                sufficient = name
        findings: list[dict[str, str]] = []
        if not meets_rto:
            findings.append(_finding("rto-missed", "blocker", f"{strategy} recovers in {_pct(rto, 4)} min, target is {payload['rto_target_min']} min"))
        if not meets_rpo:
            findings.append(_finding("rpo-missed", "blocker", f"{strategy} loses up to {_pct(rpo, 4)} min of data, target is {payload['rpo_target_min']} min"))
        if (not meets_rto or not meets_rpo) and sufficient:
            if DR_STRATEGIES.index(sufficient) > DR_STRATEGIES.index(strategy):
                findings.append(_finding("stronger-strategy-sufficient", "info", f"{sufficient} meets both targets with the supplied figures"))
            else:
                findings.append(_finding("weaker-strategy-sufficient", "info",
                                         f"{sufficient} meets both targets with the supplied figures; {strategy} does not on these figures"))
        if meets_rto and meets_rpo and sufficient and DR_STRATEGIES.index(sufficient) < DR_STRATEGIES.index(strategy):
            findings.append(_finding("cheaper-strategy-sufficient", "info", f"{sufficient} would also meet both targets"))
        if rpo > 0 and payload["rpo_target_min"] == 0:
            findings.append(_finding("zero-rpo-needs-synchronous-replication", "info", "an RPO of 0 requires synchronous replication"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "achievable_rto_min": _pct(rto, 4),
            "achievable_rpo_min": _pct(rpo, 4),
            "rto_margin_min": _pct(rto_target - rto, 4),
            "rpo_margin_min": _pct(rpo_target - rpo, 4),
            "meets_rto": meets_rto,
            "meets_rpo": meets_rpo,
            "evaluated_strategies": evaluated,
            "cheapest_sufficient_strategy": sufficient,
            "findings": findings,
            "verdict": "pass" if meets_rto and meets_rpo else "fail",
        }

    return _respond(payload, work)


# --- 4. data residency -----------------------------------------------------------------------------------------

# kinds whose residency breach is a blocker (persistent copies of the data) versus a major one (transient copies)
PERSISTENT_KINDS = ("primary", "replica", "backup", "analytics")


@_safe
def data_residency(payload: Payload) -> dict[str, object]:
    """Placements of data classes in regions checked against the jurisdictions each class may live in.

    The match is label equality (trimmed, case-folded) between a region's jurisdiction and a class's allowed list; a class
    without a rule fails closed. Severity by copy kind (primary, replica, backup, analytics = blocker; cache, log = major) is
    house policy. Legal questions beyond the label are not modelled.
    """

    def work() -> dict[str, object]:
        regions = payload["regions"]
        dupes = _duplicates([item["region"] for item in regions])
        if dupes:
            return _invalid("regions", f"regions must be unique: {', '.join(dupes)}")
        jurisdiction_of = {item["region"]: item["jurisdiction"].strip().casefold() for item in regions}
        if any(not value for value in jurisdiction_of.values()):
            return _invalid("regions", "jurisdiction must not be blank")
        dupes = _duplicates([item["data_class"] for item in payload["rules"]])
        if dupes:
            return _invalid("rules", f"data classes must have one rule each: {', '.join(dupes)}")
        allowed_of = {item["data_class"]: {j.strip().casefold() for j in item["allowed_jurisdictions"]} for item in payload["rules"]}
        if any("" in allowed for allowed in allowed_of.values()):
            return _invalid("rules", "allowed_jurisdictions must not contain blank entries")
        tally: dict[str, list[int]] = {}
        findings: list[dict[str, str]] = []
        violations = unruled = 0
        taken: dict[str, int] = {}
        for index, place in enumerate(payload["placements"]):
            region = place["region"]
            if region not in jurisdiction_of:
                return _invalid("placements", f"placement {index} ({place['resource']}) uses region {region} that is not in regions")
            data_class = place["data_class"]
            counts = tally.setdefault(data_class, [0, 0])
            counts[0] += 1
            allowed = allowed_of.get(data_class)
            if allowed is None:
                unruled += 1
                findings.append(_finding(_unique(f"no-rule:{data_class}:{place['resource']}", taken), "major",
                                         f"{place['resource']} holds data class {data_class}, which has no residency rule (fail closed)"))
            elif "*" not in allowed and jurisdiction_of[region] not in allowed:
                violations += 1
                counts[1] += 1
                severity = "blocker" if place["kind"] in PERSISTENT_KINDS else "major"
                findings.append(_finding(_unique(f"residency-violation:{place['resource']}:{region}", taken), severity,
                                         f"{place['kind']} of {data_class} in {region} ({jurisdiction_of[region]}) is outside "
                                         f"{', '.join(sorted(allowed))}"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "placements_checked": len(payload["placements"]),
            "violation_count": violations,
            "unruled_count": unruled,
            "by_data_class": [{"data_class": name, "placements": tally[name][0], "violations": tally[name][1]} for name in sorted(tally)],
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 5. landing zone lint --------------------------------------------------------------------------------------


def _ou_paths(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[: i + 1]) for i in range(len(parts))] if path else []


def _required_guardrails(role: str) -> tuple[str, ...]:
    return BASELINE_GUARDRAILS + ROLE_GUARDRAILS.get(role, ())


@_safe
def landing_zone_lint(payload: Payload) -> dict[str, object]:
    """Account/OU hierarchy, guardrail coverage, central audit and log accounts, root MFA and break-glass readiness.

    Guardrail keys and the keys each role needs (BASELINE_GUARDRAILS, ROLE_GUARDRAILS) are this lane's own vocabulary, to be
    mapped to a provider's SCPs, organization policies or Azure Policy by the implementer. The management account is exempt from
    guardrail coverage because AWS service control policies do not restrict it.
    """

    def work() -> dict[str, object]:
        accounts = payload["accounts"]
        dupes = _duplicates([item["id"] for item in accounts])
        if dupes:
            return _invalid("accounts", f"account ids must be unique: {', '.join(dupes)}")
        for item in accounts:
            if any(not part.strip() for part in item["ou"].split("/")) and item["ou"] != "":
                return _invalid("accounts", f"account {item['id']}: ou must be '' (root) or a path of non-empty OU names like Workloads/Prod")
        # canonical OU paths: surrounding whitespace is not part of an OU name, so 'Workloads/Prod ' is the OU 'Workloads/Prod'
        accounts = [{**item, "ou": "/".join(part.strip() for part in item["ou"].split("/"))} for item in accounts]
        findings: list[dict[str, str]] = []
        by_role: dict[str, list[Payload]] = {role: [] for role in LANDING_ROLES}
        for item in accounts:
            by_role[item["role"]].append(item)
        count = len(by_role["management"])
        if count == 0:
            findings.append(_finding("no-management-account", "blocker", "no management account is declared"))
        elif count > 1:
            findings.append(_finding("multiple-management-accounts", "blocker", f"{count} management accounts are declared, expected exactly 1"))
        if not by_role["log_archive"]:
            findings.append(_finding("no-log-archive-account", "blocker", "no dedicated log archive account holds the organization audit logs"))
        if not by_role["security_audit"]:
            findings.append(_finding("no-security-audit-account", "major", "no dedicated security/audit account is declared"))
        ous: dict[str, set[str]] = {}
        ou_set: set[str] = set()
        for item in accounts:
            ou_set.update(_ou_paths(item["ou"]))
            ous.setdefault(item["ou"], set()).add(item["role"])
        for path in sorted(ous):
            if {"workload_prod", "workload_nonprod"} <= ous[path]:
                findings.append(_finding(f"prod-nonprod-shared-ou:{path or '(root)'}", "major",
                                         f"OU {path or '(root)'} mixes prod and non-prod workload accounts"))
        required_total = required_present = 0
        for item in sorted(accounts, key=lambda a: a["id"]):
            if not item["root_mfa"]:
                findings.append(_finding(f"root-mfa-missing:{item['id']}", "blocker",
                                         f"account {item['id']} has no MFA on its root user (declare root_mfa true only when MFA is on, or the root "
                                         "credentials were removed centrally)"))
            if item["iam_users"] > 0:
                findings.append(_finding(f"iam-users-present:{item['id']}", "minor",
                                         f"account {item['id']} has {item['iam_users']} IAM users; use federated access"))
            if item["role"] != "management" and item["ou"] == "":
                findings.append(_finding(f"account-at-root:{item['id']}", "minor", f"account {item['id']} sits directly under the organization root"))
            if item["role"] == "management":
                continue  # service control policies do not restrict the management account
            have = set(item["guardrails"])
            need = _required_guardrails(item["role"])
            missing = [name for name in need if name not in have]
            required_total += len(need)
            required_present += len(need) - len(missing)
            if missing:
                findings.append(_finding(f"guardrail-missing:{item['id']}", "major", f"account {item['id']} ({item['role']}) lacks {', '.join(missing)}"))
        glass = payload["break_glass"]
        if not glass["configured"]:
            findings.append(_finding("break-glass-missing", "blocker", "no break-glass access path is configured"))
        else:
            if not glass["hardware_mfa"]:
                findings.append(_finding("break-glass-no-hardware-mfa", "major", "break-glass credentials are not protected by hardware MFA"))
            if not glass["alert_on_use"]:
                findings.append(_finding("break-glass-no-alert", "major", "using the break-glass path does not raise an alert"))
            if glass["last_tested_days_ago"] > BREAK_GLASS_TEST_MAX_DAYS:
                findings.append(_finding("break-glass-untested", "minor",
                                         f"break-glass was last tested {glass['last_tested_days_ago']} days ago, limit is {BREAK_GLASS_TEST_MAX_DAYS}"))
        if required_total == 0:  # no guardrail check ran: an empty evaluation must neither report full coverage nor pass
            findings.append(_finding("guardrails-not-evaluated", "major",
                                     "no non-management account was evaluated for guardrails, so guardrail coverage is 0.0 and unproven"))
        coverage = Decimal(0) if required_total == 0 else Decimal(required_present * 100) / Decimal(required_total)
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "accounts_checked": len(accounts),
            "ou_count": len(ou_set),
            "guardrail_coverage_pct": _pct(coverage),
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 6. Well-Architected scorecard -----------------------------------------------------------------------------


@_safe
def wellarch_scorecard(payload: Payload) -> dict[str, object]:
    """Weighted per-pillar scores from risk answers (none 0, medium 1/2, high 1 of the weight) and a ranked remediation list.

    The six pillars and the none/medium/high risk levels follow the AWS Well-Architected Framework; the weighting, the
    0-100 scores and the remediation severity (high risk = blocker at weight 4 or more, else major; medium = minor) are house policy.
    """

    def work() -> dict[str, object]:
        answers = payload["answers"]
        keys = [f"{a['pillar']}:{a['question']}" for a in answers]
        dupes = _duplicates(keys)
        if dupes:
            return _invalid("answers", f"each (pillar, question) may be answered once: {', '.join(dupes)}")
        missing = [name for name in PILLARS if not any(a["pillar"] == name for a in answers)]
        if missing:
            return _invalid("answers", f"every pillar needs at least one answer, missing: {', '.join(missing)}")
        pillar_weight = {name: 1 for name in PILLARS}
        pillar_weight.update(payload.get("pillar_weights", {}))
        results: list[Payload] = []
        remediation: list[tuple[Any, int, Any, str, Any]] = []
        for name in PILLARS:
            rows = [a for a in answers if a["pillar"] == name]
            weight_sum = sum(a["weight"] for a in rows)
            risk_sum = sum(a["weight"] * RISK_FACTOR[a["risk"]] for a in rows)
            score = 100 * (1 - risk_sum / weight_sum)
            results.append({"pillar": name, "score": score, "answered": len(rows),
                            "high_risk": sum(1 for a in rows if a["risk"] == "high"),
                            "medium_risk": sum(1 for a in rows if a["risk"] == "medium")})
            for a in rows:
                if a["risk"] != "none":
                    severity = ("blocker" if a["weight"] >= 4 else "major") if a["risk"] == "high" else "minor"
                    remediation.append((-(a["weight"] * RISK_FACTOR[a["risk"]]), PILLARS.index(name), a["question"], severity, a))
        remediation.sort(key=lambda item: item[:3])
        total_weight = sum(pillar_weight.values())
        overall = sum(r["score"] * pillar_weight[r["pillar"]] for r in results) / total_weight
        weakest = min(results, key=lambda r: (r["score"], PILLARS.index(r["pillar"])))
        ranked: list[Payload] = [
            {"rank": rank, "id": f"{a['pillar']}:{a['question']}", "severity": severity, "pillar": a["pillar"],
             "summary": f"{a['risk']} risk on '{a['question']}' (weight {a['weight']})", "impact": float(-neg)}
            for rank, (neg, _, _, severity, a) in enumerate(remediation, start=1)
        ]
        return {
            "state": "SUCCESS",
            "overall_score_pct": _round(overall, 2),
            "pillars": [{"pillar": r["pillar"], "score_pct": _round(r["score"], 2), "answered": r["answered"],
                         "high_risk": r["high_risk"], "medium_risk": r["medium_risk"]} for r in results],
            "weakest_pillar": weakest["pillar"],
            "high_risk_count": sum(r["high_risk"] for r in results),
            "remediation": ranked,
            "verdict": _verdict([{"id": item["id"], "severity": item["severity"]} for item in ranked]),
        }

    return _respond(payload, work)


# --- 7. blast radius -------------------------------------------------------------------------------------------


def _domain_label(key: tuple[str, ...]) -> str:
    """Display name `scope:part/part`. Each part escapes '%' and '/' first, so a '/' inside a region, AZ or cell id cannot read as a separator."""
    return key[0] + ":" + "/".join(part.replace("%", "%25").replace("/", "%2F") for part in key[1:])


@_safe
def blast_radius(payload: Payload) -> dict[str, object]:
    """Loss of each AZ, region and cell: which services go down (directly or through hard dependencies) and the criticality share lost.

    A cell id names one failure domain wherever it appears, so the same id in two regions is one cell. The limit applies to AZ and
    cell losses; region losses are ranked, not gated. A service entirely inside one AZ or cell is a single point of failure
    (major at criticality 8 or more, else minor, house policy).
    """

    def work() -> dict[str, object]:
        services = payload["services"]
        ids = [s["id"] for s in services]
        dupes = _duplicates(ids)
        if dupes:
            return _invalid("services", f"service ids must be unique: {', '.join(dupes)}")
        known = set(ids)
        for s in services:
            unknown = sorted(set(s["depends_on"]) - known)
            if unknown:
                return _invalid("services", f"service {s['id']} depends on unknown services: {', '.join(unknown)}")
        total_weight = sum(s["criticality"] for s in services)
        by_id = {s["id"]: s for s in services}
        # instances of each service per failure domain, keyed by tuples so that no separator inside a name can merge two domains;
        # a service is out when a domain holds all of its instances
        counts: dict[str, dict[tuple[str, ...], int]] = {}
        domains: set[tuple[str, ...]] = set()
        for s in services:
            per: dict[tuple[str, ...], int] = {}
            for inst in s["instances"]:
                keys = [("az", inst["region"], inst["az"]), ("region", inst["region"])]
                if inst.get("cell"):
                    keys.append(("cell", inst["cell"]))
                for key in keys:
                    per[key] = per.get(key, 0) + 1
            counts[s["id"]] = per
            domains.update(per)
        dependents: dict[str, list[str]] = {sid: [] for sid in ids}
        for s in services:
            for dep in s["depends_on"]:
                dependents[dep].append(s["id"])
        evaluated: list[Payload] = []
        spof: set[str] = set()
        for key in sorted(domains):
            scope = key[0]
            direct_down, degraded = set(), set()
            for s in services:
                lost = counts[s["id"]].get(key, 0)
                if lost == len(s["instances"]):
                    direct_down.add(s["id"])
                elif lost:
                    degraded.add(s["id"])
            down = set(direct_down)
            queue = sorted(direct_down)
            while queue:  # hard dependencies propagate; each service is queued once, so dependency cycles terminate
                for dependent in dependents[queue.pop()]:
                    if dependent not in down:
                        down.add(dependent)
                        queue.append(dependent)
            degraded -= down
            if scope in ("az", "cell"):
                spof |= direct_down
            lost_weight = sum(by_id[i]["criticality"] for i in down)
            evaluated.append({"scope": scope, "domain": _domain_label(key), "down": sorted(down), "degraded": len(degraded),
                              "blast": Decimal(lost_weight * 100) / Decimal(total_weight)})
        affecting = [e for e in evaluated if e["down"] or e["degraded"]]
        scope_order = {"cell": 0, "az": 1, "region": 2}
        affecting.sort(key=lambda e: (-e["blast"], -e["degraded"], scope_order[e["scope"]], e["domain"]))
        limit = _dec(payload["max_az_blast_pct"])
        findings: list[dict[str, str]] = []
        for e in affecting:
            if e["scope"] in ("az", "cell") and e["blast"] > limit:
                findings.append(_finding(f"blast-exceeds-limit:{e['domain']}", "major",
                                         f"losing {e['domain']} takes down {_pct(e['blast'])}% of criticality, limit is {payload['max_az_blast_pct']}%"))
        for sid in sorted(spof):
            findings.append(_finding(f"spof:{sid}", "major" if by_id[sid]["criticality"] >= 8 else "minor",
                                     f"service {sid} is entirely inside one AZ or cell and goes down when it fails"))
        findings = _ordered(findings)
        top = affecting[:RANKING_LIMIT]
        return {
            "state": "SUCCESS",
            "domains_evaluated": len(evaluated),
            "worst_domain": top[0]["domain"] if top else "",
            "worst_blast_pct": _pct(top[0]["blast"]) if top else 0.0,
            "ranking": [{"domain": e["domain"], "scope": e["scope"], "blast_pct": _pct(e["blast"]), "down_services": e["down"],
                         "degraded_count": e["degraded"]} for e in top],
            "spof_services": sorted(spof),
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 8. cell capacity ------------------------------------------------------------------------------------------


# cell capacity: the registered input schema (cloud_cell_capacity). Each entry is (low, high, low_open, integer). A value outside it,
# or a boolean, NaN, infinity or an integer beyond the conversion limit, is INVALID_INPUT naming the field, never a computed answer.
CELL_BOUNDS = {
    "load_rps": (0, 1000000000, True, False),
    "cell_max_rps": (1, 1000000000, False, False),
    "target_utilisation_pct": (1, 100, False, False),
    "growth_pct": (0, 1000, False, False),
    "spare_cells": (0, 100, False, True),
    "current_cells": (0, 1000000, False, True),
}


@_safe
def cell_capacity(payload: Payload) -> dict[str, object]:
    """Cells for a projected load at a target utilisation, plus spare cells (N+1 style), the per-cell blast radius and the gap to the deployed count."""

    def work() -> dict[str, object]:
        for field, (low, high, low_open, integer) in CELL_BOUNDS.items():
            if _number_error(payload[field], low, high, low_open=low_open, integer=integer):
                kind = "an integer" if integer else "a number"
                return _invalid(field, f"{field} must be {kind} from {low} to {high}" + (", with 0 excluded" if low_open else ""))
        usable = Fraction(_dec(payload["cell_max_rps"])) * Fraction(_dec(payload["target_utilisation_pct"])) / 100
        projected = Fraction(_dec(payload["load_rps"])) * (100 + Fraction(_dec(payload["growth_pct"]))) / 100
        for_load = max(1, math.ceil(projected / usable))  # exact rational ceil: a load of exactly k cells needs k, not k+1
        required = for_load + payload["spare_cells"]
        provisioned = usable * required
        shortfall = max(0, required - payload["current_cells"])
        findings = []
        if shortfall:
            findings.append(_finding("cells-short", "major", f"{payload['current_cells']} cells deployed, {required} required ({shortfall} short)"))
        if required == 1:
            findings.append(_finding("single-cell", "minor", "one cell serves all traffic: a cell failure is a total outage"))
        if payload["spare_cells"] == 0:
            findings.append(_finding("no-spare-cell", "minor", "no spare cell: losing a cell exceeds the target utilisation"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "usable_rps_per_cell": _round(usable, 4),
            "projected_load_rps": _round(projected, 4),
            "cells_for_load": for_load,
            "cells_required": required,
            "headroom_pct": _round((provisioned - projected) * 100 / projected, 2),
            "blast_radius_pct": _round(Fraction(100, required), 4),
            "shortfall_cells": shortfall,
            "current_cells_sufficient": shortfall == 0,
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 9. CIDR plan ----------------------------------------------------------------------------------------------


def _is_network(text: str) -> bool:
    """True when `text` is a valid network once host bits are ignored (so strict parsing failed only on host bits)."""
    try:
        ipaddress.ip_network(text, strict=False)
    except ValueError:
        return False
    return True


def _relation(a: _Network, b: _Network) -> str:
    if a == b:
        return "equals"
    return "contains" if _nested(b, a) else "is inside"


@_safe
def cidr_plan(payload: Payload) -> dict[str, object]:
    """Overlaps between networks that must coexist (same group, or both routable), private-range use and address sizing.

    Every network is sized like a subnet: `reserved_addresses` (default 5, what AWS and Azure reserve per subnet; Google Cloud
    reserves 4) is subtracted from its address count. IPv4 outside RFC 1918 and RFC 6598 is flagged; IPv6 outside RFC 4193 is
    flagged except global unicast (2000::/3, what providers allocate to IPv6 VPCs) apart from the documentation prefix.
    Inputs are address/prefix-length strings with no host bits; the value is never repeated in a message.
    """

    def work() -> dict[str, object]:
        nets = payload["networks"]
        dupes = _duplicates([n["name"] for n in nets])
        if dupes:
            return _invalid("networks", f"network names must be unique: {', '.join(dupes)}")
        parsed: list[tuple[Payload, _Network]] = []
        for n in nets:
            if not CIDR_FORM.fullmatch(n["cidr"]):
                return _invalid("networks", f"network {n['name']}: not an IPv4 or IPv6 address/prefix-length")
            try:
                parsed.append((n, ipaddress.ip_network(n["cidr"], strict=True)))
            except ValueError:  # the text is address/prefix-shaped: say which rule it broke, never repeat the value
                return _invalid("networks", f"network {n['name']}: host bits are set; give the network address with its prefix length"
                                if _is_network(n["cidr"]) else f"network {n['name']}: not an IPv4 or IPv6 address/prefix-length")
        reserved = payload.get("reserved_addresses", DEFAULT_RESERVED_ADDRESSES)
        headroom = 1 + Fraction(_dec(payload.get("headroom_pct", 0))) / 100
        findings: list[dict[str, str]] = []
        overlaps = undersized = 0
        for i, (a, na) in enumerate(parsed):
            for b, nb in parsed[i + 1:]:
                if na.version != nb.version or not na.overlaps(nb):
                    continue
                both_routable = a["routable"] and b["routable"]
                if not (both_routable or (a.get("group") and a.get("group") == b.get("group"))):
                    continue  # unrouted ranges in different groups may legitimately reuse an address space
                overlaps += 1
                first, second = sorted((a["name"], b["name"]))
                left, right = (na, nb) if a["name"] == first else (nb, na)
                findings.append(_finding(f"overlap:{first}:{second}", "blocker" if both_routable else "major",
                                         f"{first} ({left}) {_relation(left, right)} {second} ({right})"))
        for n, net in parsed:
            if not any(_nested(net, p) for p in PRIVATE_NETWORKS) and not (
                    net.version == 6 and _nested(net, GLOBAL_UNICAST_V6) and not net.overlaps(DOCUMENTATION_V6)):
                findings.append(_finding(f"public-range:{n['name']}", "minor", f"{n['name']} ({net}) is outside RFC 1918, RFC 6598 and RFC 4193 private space"))
            required = n.get("required_addresses")
            if required is not None:
                usable = max(0, net.num_addresses - reserved)
                if usable < Fraction(required) * headroom:
                    undersized += 1
                    findings.append(_finding(f"undersized:{n['name']}", "major",
                                             f"{n['name']} ({net}) has {usable} usable addresses after {reserved} reserved, "
                                             f"{required} required with {payload.get('headroom_pct', 0)}% headroom"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "networks_checked": len(nets),
            "overlap_count": overlaps,
            "undersized_count": undersized,
            "findings": findings[:FINDINGS_LIMIT],
            "findings_truncated": len(findings) > FINDINGS_LIMIT,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 10. managed versus self-hosted ----------------------------------------------------------------------------


@_safe
def managed_vs_self_hosted(payload: Payload) -> dict[str, object]:
    """Weighted 1-5 scores (5 = best for that option) for both options, with knockout on critical criteria scored 1."""

    def work() -> dict[str, object]:
        criteria = payload["criteria"]
        dupes = _duplicates([c["id"] for c in criteria])
        if dupes:
            return _invalid("criteria", f"criterion ids must be unique: {', '.join(dupes)}")
        weight_sum = sum(c["weight"] for c in criteria)
        managed = Decimal(sum(c["weight"] * c["managed_score"] for c in criteria) * 100) / Decimal(5 * weight_sum)
        hosted = Decimal(sum(c["weight"] * c["self_hosted_score"] for c in criteria) * 100) / Decimal(5 * weight_sum)
        margin = abs(managed - hosted)
        out = []
        if any(c["critical"] and c["managed_score"] == 1 for c in criteria):
            out.append("managed")
        if any(c["critical"] and c["self_hosted_score"] == 1 for c in criteria):
            out.append("self_hosted")
        tie_margin = _dec(payload["tie_margin_pct"]) if "tie_margin_pct" in payload else DEFAULT_TIE_MARGIN_PCT
        if len(out) == 2:
            recommendation = "none"
        elif out:
            recommendation = "self_hosted" if out[0] == "managed" else "managed"
        elif margin == 0 or margin < tie_margin:
            recommendation = "tie"
        else:
            recommendation = "managed" if managed > hosted else "self_hosted"
        deltas = sorted(((c["weight"] * abs(c["managed_score"] - c["self_hosted_score"]), c["id"]) for c in criteria if c["managed_score"] != c["self_hosted_score"]),
                        key=lambda item: (-item[0], item[1]))
        findings: list[dict[str, str]] = []
        for option in out:
            findings.append(_finding(f"knocked-out:{option}", "info", f"{option} scores 1 on a critical criterion and is disqualified"))
        if recommendation == "none":
            findings.append(_finding("no-viable-option", "blocker", "both options fail a critical criterion"))
        elif recommendation == "tie":
            findings.append(_finding("tie", "minor", "the scores are identical" if margin == 0 else
                                     f"scores differ by {_pct(margin)} points, below the {tie_margin} point tie margin"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "managed_score_pct": _pct(managed),
            "self_hosted_score_pct": _pct(hosted),
            "margin_pct": _pct(margin),
            "recommendation": recommendation,
            "disqualified": sorted(out),
            "decisive_criteria": [cid for _, cid in deltas[:3]],
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 11. replica placement -------------------------------------------------------------------------------------


def _min_azs(total: int, failures: int, required_live: int) -> int:
    """Smallest AZ count whose balanced spread of `total` replicas keeps `required_live` after losing the `failures` largest AZs; -1 if none."""
    for azs in range(1, total + 1):
        base, extra = divmod(total, azs)
        lost_azs = min(failures, azs)
        lost = min(lost_azs, extra) * (base + 1) + max(0, lost_azs - extra) * base
        if total - lost >= required_live:
            return azs
    return -1


@_safe
def replica_placement(payload: Payload) -> dict[str, object]:
    """Whether a quorum-based replica set keeps its quorum when the largest AZs fail, and the AZ count that would."""

    def work() -> dict[str, object]:
        placements = payload["placements"]
        dupes = _duplicates([p["az"] for p in placements])
        if dupes:
            return _invalid("placements", f"AZs must be listed once: {', '.join(dupes)}")
        total = sum(p["replicas"] for p in placements)
        mode = payload["quorum_mode"]
        intersects = True
        if mode == "majority":
            required_live = total // 2 + 1
        else:
            for key in ("read_quorum", "write_quorum"):
                if key not in payload:
                    return _invalid(key, f"{key} is required when quorum_mode is explicit")
                if payload[key] > total:
                    return _invalid(key, f"{key} {payload[key]} exceeds the {total} replicas")
            read, write = payload["read_quorum"], payload["write_quorum"]
            required_live = max(read, write)
            intersects = read + write > total and 2 * write > total
        failures = payload["tolerate_az_failures"]
        sizes = sorted((p["replicas"] for p in placements), reverse=True)
        remaining = total - sum(sizes[:failures])
        survives = remaining >= required_live
        min_azs = _min_azs(total, failures, required_live)
        findings: list[dict[str, str]] = []
        if not intersects:
            findings.append(_finding("quorums-do-not-intersect", "blocker",
                                     f"read {payload['read_quorum']} + write {payload['write_quorum']} must exceed {total} and 2*write must exceed {total}"))
        if not survives:
            hint = f"; spread across at least {min_azs} AZs" if min_azs > len(placements) else "; no AZ spread of this replica count can tolerate it" if min_azs < 0 else "; rebalance replicas across the existing AZs"
            findings.append(_finding("az-loss-breaks-quorum", "blocker", f"losing the {failures} largest AZ(s) leaves {remaining} replicas, {required_live} needed{hint}"))
        if mode == "majority" and total >= 2 and total % 2 == 0:
            findings.append(_finding("even-replica-count", "minor", f"{total} replicas tolerate {(total - 1) // 2} replica failures, the same as {total - 1}"))
        if sizes[0] - sizes[-1] > 1:
            findings.append(_finding("unbalanced-spread", "info", f"AZ replica counts differ by {sizes[0] - sizes[-1]}"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "total_replicas": total,
            "az_count": len(placements),
            "required_live": required_live,
            "worst_case_remaining": remaining,
            "survives_az_failures": survives,
            "quorums_intersect": intersects,
            "min_azs_for_tolerance": min_azs,
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)


# --- 12. latency budget ----------------------------------------------------------------------------------------


@_safe
def latency_budget(payload: Payload) -> dict[str, object]:
    """Critical-path latency: sequential hops add, hops of one parallel group contribute their maximum; compared with the p99 target.

    Per-hop p99 values summed along the path are a planning figure, not the end-to-end p99 (percentiles do not add), and the
    maximum of a parallel group's p99 values is a lower bound for that group's p99 (the slowest of several calls exceeds any
    one call's p99 more often than 1 in 100). The tail amplification is exact for independent hops: the share of requests that
    meet at least one hop above its own p99 is 1 - 0.99^N for N hop calls.
    """

    def work() -> dict[str, object]:
        hops = payload["hops"]
        dupes = _duplicates([h["name"] for h in hops])
        if dupes:
            return _invalid("hops", f"hop names must be unique: {', '.join(dupes)}")
        for h in hops:
            if h["p50_ms"] > h["p99_ms"]:
                return _invalid("hops", f"hop {h['name']}: p50_ms {h['p50_ms']} exceeds p99_ms {h['p99_ms']}")
        names = {h["name"] for h in hops}
        for h in hops:
            if h.get("group") and f"group:{h['group']}" in names:
                return _invalid("hops", f"hop name group:{h['group']} collides with the stage name of a parallel group")
        stages: dict[str, list[Decimal]] = {}  # stage name -> [p99 contribution, p50 contribution]
        for h in hops:
            group = h.get("group", "")
            name = f"group:{group}" if group else h["name"]
            p99, p50 = _dec(h["p99_ms"]) * h["calls"], _dec(h["p50_ms"]) * h["calls"]
            if name in stages:
                stages[name] = [max(stages[name][0], p99), max(stages[name][1], p50)]
            else:
                stages[name] = [p99, p50]
        target = _dec(payload["p99_target_ms"])
        total99 = sum((v[0] for v in stages.values()), Decimal(0))
        total50 = sum((v[1] for v in stages.values()), Decimal(0))
        calls = sum(h["calls"] for h in hops)
        with localcontext() as ctx:
            ctx.prec = 40
            tail = (1 - Decimal("0.99") ** calls) * 100
        ranked = sorted(stages.items(), key=lambda item: (-item[1][0], item[0]))
        findings: list[dict[str, str]] = []
        if total99 > target:
            findings.append(_finding("over-budget", "major", f"critical path p99 {_pct(total99, 3)} ms exceeds the {payload['p99_target_ms']} ms target"))
        for name, (p99, _) in ranked:
            if p99 > target * DOMINANT_STAGE_SHARE:
                findings.append(_finding(f"dominant-stage:{name}", "minor", f"{name} alone uses {_pct(p99, 3)} ms, over half of the target"))
        if tail >= TAIL_AMPLIFICATION_PCT:
            findings.append(_finding("tail-amplification", "minor", f"{calls} hop calls put {_pct(tail)}% of requests on at least one hop above its p99"))
        findings = _ordered(findings)
        return {
            "state": "SUCCESS",
            "critical_path_p99_ms": _pct(total99, 3),
            "critical_path_p50_ms": _pct(total50, 3),
            "budget_remaining_ms": _pct(target - total99, 3),
            "meets_target": total99 <= target,
            "tail_hit_pct": _pct(tail),
            "stages": [{"name": name, "p99_ms": _pct(p99, 3), "share_pct": _pct(p99 * 100 / total99) if total99 > 0 else 0.0}
                       for name, (p99, _) in ranked[:5]],
            "findings": findings,
            "verdict": _verdict(findings),
        }

    return _respond(payload, work)
