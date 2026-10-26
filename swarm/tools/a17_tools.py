"""A17 Platform Engineer tools: pure analyses of the platform as a product (catalog, golden paths, schemas, guardrails).

Tools: plat_catalog_lint (Backstage-style entity metadata), plat_template_params_check (scaffolder template inputs versus
a golden-path contract), plat_composition_diff (XRD-style schema versions: breaking versus additive),
plat_claim_validate (a claim against a composition schema), plat_maturity_scorecard (service maturity level),
plat_dora_bands (DORA four keys and bands), plat_catalog_graph (dependency cycles, dangling, orphans, blast radius),
plat_ephemeral_env_policy (TTL, idle and cost of ephemeral environments), plat_quota_fit (tenant quota allocation versus
capacity), plat_golden_path_coverage (adoption and version currency), plat_api_deprecation (deprecation windows),
plat_request_guardrails (self-service requests versus policy).

A17 owns the platform as a product. It never applies infrastructure and never edits pipelines (A11 does): every tool here
reads supplied facts and returns a decision-ready verdict with `findings` of {id, severity, summary}. The finding id is
`<rule>@<subject>`; severities are blocker > major > minor > info. Time arrives as ISO-8601 with an explicit offset.

Reference tables and the public source each mirrors (everything else is an explicit, documented house policy):
* Entity grammar, checked against upstream Backstage `catalog-model` validators (KubernetesValidatorFunctions): kind
  `[a-zA-Z][a-z0-9A-Z]*` (1..63), name `([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]` (1..63; separators may repeat), namespace
  a lowercase DNS label `[a-z0-9]+(-+[a-z0-9]+)*` (1..63). References are `[kind:][namespace/]name`; `spec.dependsOn` defaults to
  kind Component and to the namespace of the entity itself, `spec.owner` to kind Group. Core kinds Component/API/Resource/System/
  Domain/Group/User/Location/Template, the spec fields each kind requires (type, lifecycle, owner) and the well-known lifecycle
  values experimental, production, deprecated: "Descriptor Format of Catalog Entities" (backstage.io docs).
  Annotation keys `backstage.io/techdocs-ref` and `backstage.io/source-location` are the Backstage well-known annotations.
* Deprecation windows: beta 9 months (274 days) and alpha 0 follow the Kubernetes Deprecation Policy, rule 4a (beta APIs are
  served 9 months or 3 minor releases after deprecation, whichever is longer; releases are not modelled here). Kubernetes
  states NO time window for GA APIs (they must not be removed within a major version); the 365-day GA window is HOUSE
  POLICY, modelled on Kubernetes rule 5a (CLI elements: GA 12 months or 2 releases) and overridable with `min_window_days`.
* DORA bands: the four keys of the DORA 2023 "Accelerate State of DevOps" report (deployment frequency, change lead time, change
  failure rate, failed deployment recovery time; DORA now counts five metrics, the fifth being rework rate, which is not
  modelled). The report publishes cluster profiles (elite on demand / <1 day / 5 % / <1 hour; high daily-weekly / 1 day-1 week /
  10 % / <1 day; medium weekly-monthly / 1 week-1 month / 15 % / 1 day-1 week), not normative cut-offs, and the 2024 report
  re-clusters differently (20 % / 10 % / 40 % failure rates, medium recovery <1 day). The limits below are this tool's reading
  of the 2023 profiles, named constants (see DORA_*); the overall band is the weakest computed band (house rule).

Secret safety: findings, summaries and error messages never echo a free-form input value (claim values, defaults, enum values,
annotations, links, lifecycle, tier, owner text, timestamps, region/class/size of a request): they are reported by length, count or
not at all. Only identifiers (entity kind/name/namespace, field paths, ids, team and resource names) appear, and only when
they match `[A-Za-z0-9._:/#[]-]` and are short (catalog entities also have to match the Backstage grammar); any other identifier
is replaced by its position (`entry#3`, `requests[2]`) or its length. An identifier that looks like a token but fits that
grammar is still an identifier: do not put credentials in names.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, TypedDict

from swarm.tools.support import respond

Report = dict[str, Any]  # a tool result: JSON-shaped, its values are validated where they are read
Finding = dict[str, str]  # one finding: {id, severity, summary}


class _PathStats(TypedDict):
    id: str
    services: int
    current: int
    outdated: int
    unsupported: int


class _Impact(TypedDict):
    ref: str
    direct_dependents: int
    transitive_dependents: int

SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
MAX_FINDINGS = 300  # the list is cut here; finding_count always carries the true total
MAX_FINDINGS_BYTES = 100_000  # and here, whichever comes first: keeps every result far below the 256 KiB output limit
MAX_SUMMARY = 300
DAY_US = 86_400 * 1_000_000
HOUR_US = 3_600 * 1_000_000

# --- catalog descriptor rules (Backstage descriptor format) ----------------------------------------------------------
CATALOG_KINDS = ("component", "api", "resource", "system", "domain", "group", "user", "location", "template")
LIFECYCLES = ("experimental", "production", "deprecated")
OWNER_REQUIRED_KINDS = ("component", "api", "resource", "system", "domain")
TYPE_REQUIRED_KINDS = ("component", "api", "resource", "group", "template")
LIFECYCLE_REQUIRED_KINDS = ("component", "api")
SYSTEM_EXPECTED_KINDS = ("component", "api", "resource")
DOCS_KINDS = ("component", "api", "system")
DESCRIBED_KINDS = ("component", "api", "resource", "system", "domain")
DOCS_ANNOTATION = "backstage.io/techdocs-ref"
SOURCE_ANNOTATION = "backstage.io/source-location"
RUNBOOK_KEYS = ("runbook", "runbook-url", "runbook_url", "runbook-link")  # last path segment of an annotation key
DEFAULT_TIERS = ("tier-0", "tier-1", "tier-2", "tier-3")  # house convention, overridable per call
CRITICAL_TIERS = ("tier-0", "tier-1")

# --- service maturity (house policy: levels are cumulative) -----------------------------------------------------------
LEVEL_NAMES = ("none", "bronze", "silver", "gold")
DEFAULT_REQUIRED_LEVELS = {"tier-0": 3, "tier-1": 2, "tier-2": 1, "tier-3": 0}
DEFAULT_MIN_COVERAGE_PCT = 70

# --- DORA bands (DORA 2023 clusters; named so a reviewer can retune them) ---------------------------------------------
DORA_FREQUENCY_PER_DAY = (Fraction(1), Fraction(1, 7), Fraction(1, 30))  # elite >= 1/day, high >= 1/week, medium >= 1/month
DORA_LEAD_TIME_HOURS = (Fraction(24), Fraction(168), Fraction(720))  # elite < 1 day, high < 1 week, medium < 30 days
DORA_FAILURE_RATE = (Fraction(5, 100), Fraction(10, 100), Fraction(15, 100))  # elite <= 5%, high <= 10%, medium <= 15%
DORA_RECOVERY_HOURS = (Fraction(1), Fraction(24), Fraction(168))  # elite < 1 hour, high < 1 day, medium < 1 week
BAND_ORDER = ("low", "medium", "high", "elite")

# --- API deprecation windows in days: beta and alpha from the Kubernetes Deprecation Policy, GA is house policy (see the module docstring) ---
DEPRECATION_WINDOW_DAYS = {"ga": 365, "beta": 274, "alpha": 0}

# --- quota thresholds (house policy) --------------------------------------------------------------------------------
QUOTA_HOT = Fraction(9, 10)  # used / quota >= 90% is hot
QUOTA_IDLE = Fraction(1, 5)  # used / quota < 20% is idle

_NUMERIC_TEXT = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?")
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+")  # any digit count: compared exactly with the schema bounds
_ZERO_TEXT = re.compile(r"[+-]?(0+(\.0*)?|\.0+)([eE][+-]?[0-9]+)?")
_SEMVER = re.compile(r"v?([0-9]{1,9})\.([0-9]{1,9})\.([0-9]{1,9})")
_NAME = re.compile(r"(?:[A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9]")  # upstream isValidObjectName
_NAMESPACE = re.compile(r"[a-z0-9]+(?:-+[a-z0-9]+)*")  # upstream isValidNamespace (a lowercase DNS label)
_KIND = re.compile(r"[A-Za-z][A-Za-z0-9]*")  # upstream isValidKind
_SAFE_IDENT = re.compile(r"[A-Za-z0-9._:/#\[\]-]+")


class _Bad(ValueError):
    """A semantic input problem: becomes INVALID_INPUT naming `field`."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _invalid(field: str, message: str) -> Report:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _guarded(work: Callable[[], Report]) -> Callable[[], Report]:
    def run() -> Report:
        try:
            return work()
        except _Bad as exc:
            return _invalid(exc.field, str(exc))

    return run


def _reply(payload: dict[str, Any], work: Callable[[], Report]) -> Report:
    """`support.respond` is annotated to return Any; this keeps every handler's return type explicit."""
    result: Report = respond(payload, work)
    return result


def _is_finite_number(value: object) -> bool:
    """True when the value is a number Decimal reads as finite (NaN, infinity, booleans and text are not)."""
    try:
        return Decimal(repr(value)).is_finite()
    except (InvalidOperation, ValueError):  # ValueError: an integer past Python's digit limit
        return False


def _frac(value: object, field: str) -> Fraction:
    """Exact fraction of the number as written (shortest repr): 0.1 is 1/10, not its binary neighbour. A non-finite or
    non-numeric value is INVALID_INPUT naming `field`, never a value."""
    if not _is_finite_number(value):
        raise _Bad(field, f"{field} is not a finite number")
    return Fraction(Decimal(repr(value)))


def _r(value: Fraction, digits: int = 4) -> float:
    """Round the exact value half away from zero (0.03125 -> 0.0313); float round() would go to even."""
    exact = Fraction(value)
    scale = 10**digits
    rounded = int((abs(exact) * scale * 2 + 1) // 2)
    return float(Fraction(rounded if exact >= 0 else -rounded, scale))


def _ident(value: object, limit: int = 120) -> str:
    """An identifier that is safe to echo: itself when short and made only of [A-Za-z0-9._:/#[]-], else only its length."""
    text = value if isinstance(value, str) else ""
    return text if 0 < len(text) <= limit and _SAFE_IDENT.fullmatch(text) else f"<{len(text)} chars>"


def _label(text: str, fallback: str, limit: int = 120) -> str:
    """The identifier itself when it is echo-safe (see _ident), else the positional `fallback` that points at the input."""
    return text if 0 < len(text) <= limit and _SAFE_IDENT.fullmatch(text) else fallback


def _s(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _ts(value: str, field: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _Bad(field, f"{label}: not an ISO-8601 timestamp ({len(value)} chars)") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _Bad(field, f"{label}: timestamp needs an explicit UTC offset")
    return parsed


def _micro(start: datetime, end: datetime) -> int:
    delta = end - start
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _finding(rule: str, subject: str, severity: str, summary: str) -> Finding:
    """`subject` is echoed through _ident; `summary` is cut at MAX_SUMMARY. Callers never put an input value in a summary."""
    if len(summary) > MAX_SUMMARY:
        summary = summary[: MAX_SUMMARY - 3] + "..."
    return {"id": f"{rule}@{_ident(subject, 240)}" if subject else rule, "severity": severity, "summary": summary}


def _cut(ordered: list[Finding]) -> list[Finding]:
    """The leading findings of an already ordered list that fit MAX_FINDINGS and MAX_FINDINGS_BYTES (always at least one)."""
    kept: list[Finding] = []
    budget = MAX_FINDINGS_BYTES
    for finding in ordered[:MAX_FINDINGS]:
        size = sum(len(v) for v in finding.values() if isinstance(v, str)) + 48
        if kept and size > budget:
            break
        budget -= size
        kept.append(finding)
    return kept


def _finish(found: list[Finding]) -> tuple[list[Finding], int]:
    ordered = sorted(found, key=lambda f: (SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))
    return _cut(ordered), len(found)


def _count(found: list[Finding], *severities: str) -> int:
    return sum(1 for f in found if f["severity"] in severities)


def _status(found: list[Finding]) -> str:
    if _count(found, "blocker", "major"):
        return "fail"
    return "warn" if _count(found, "minor") else "pass"


def _name_ok(text: str) -> bool:
    """Upstream Backstage object name: 1..63 chars, alphanumeric at both ends, [-_.] allowed (and repeatable) inside."""
    return len(text) <= 63 and _NAME.fullmatch(text) is not None


def _ns_ok(text: str) -> bool:
    """Upstream Backstage namespace: a lowercase DNS label, 1..63 chars."""
    return len(text) <= 63 and _NAMESPACE.fullmatch(text) is not None


def _kind_ok(text: str) -> bool:
    return len(text) <= 63 and _KIND.fullmatch(text) is not None


def _parse_ref(text: str, default_kind: str, default_namespace: str = "default") -> tuple[str, str, str] | None:
    """`[kind:][namespace/]name` -> lowercase (kind, namespace, name); None when malformed. The namespace of a reference is read
    case-insensitively (the catalog lowercases it); when it is omitted `default_namespace` applies."""
    kind, namespace, name = default_kind, default_namespace, text
    if ":" in text:
        kind, _, name = text.partition(":")
        if not _kind_ok(kind):
            return None
    if "/" in name:
        namespace, _, name = name.partition("/")
        if not _ns_ok(namespace.lower()):
            return None
    if not _name_ok(name):
        return None
    return kind.lower(), namespace.lower(), name.lower()


def _ref_text(ref: tuple[str, str, str]) -> str:
    return f"{ref[0]}:{ref[1]}/{ref[2]}"


# =============================================================================================================
# 1. catalog entry lint
# =============================================================================================================


def _lint_entries(entries: list[dict[str, Any]], allowed_tiers: tuple[str, ...]) -> list[tuple[int, Finding]]:
    """Every catalog rule over a batch of flattened entities: [(entry index, finding)]. Shared with scripts/plat_catalog_lint.py."""
    found: list[tuple[int, Finding]] = []
    seen: dict[tuple[str, str, str], int] = {}
    for index, entry in enumerate(entries):
        raw_kind, name = entry.get("kind") or "", entry.get("name") or ""  # never stripped: " a" and "a\n" are not valid names
        raw_namespace = entry.get("namespace") or ""
        namespace = raw_namespace or "default"
        kind = raw_kind.lower()
        lowered = namespace.lower()
        subject = f"{kind}:{lowered}/{name.lower()}" if _kind_ok(raw_kind) and _ns_ok(lowered) and _name_ok(name) else f"entry#{index}"
        scope = lowered if _ns_ok(lowered) else "default"  # namespace that relative references resolve in

        def emit(rule: str, severity: str, summary: str, index: int = index, subject: str = subject) -> None:
            found.append((index, _finding(rule, subject, severity, summary)))

        if not _kind_ok(raw_kind):
            emit("kind-invalid", "blocker", "kind must be alphanumeric, start with a letter and be at most 63 chars")
        elif kind not in CATALOG_KINDS:
            emit("kind-unknown", "minor", f"kind is not a core catalog kind ({', '.join(CATALOG_KINDS)})")
        if not _name_ok(name):
            emit("name-invalid", "blocker", "name must be 1-63 chars, start and end alphanumeric, with - _ . allowed inside")
        if raw_namespace and not _ns_ok(raw_namespace):
            emit("namespace-invalid", "blocker", "namespace must be a lowercase DNS label of 1-63 chars (a-z, 0-9, -)")
        if _name_ok(name) and _ns_ok(namespace) and _kind_ok(raw_kind):
            ref = (kind, namespace, name.lower())
            if ref in seen:
                emit("duplicate-entity", "blocker", f"{_ref_text(ref)} is also defined by entry #{seen[ref]}")
            else:
                seen[ref] = index

        lifecycle = _s(entry.get("lifecycle"))
        production = lifecycle.lower() == "production"  # `Production` is flagged as nonstandard but must not escape the production severities
        owner = _s(entry.get("owner"))
        if kind in OWNER_REQUIRED_KINDS:
            if not owner:
                emit("owner-missing", "blocker", "spec.owner is required: every entity needs one accountable Group or User")
            else:
                parsed = _parse_ref(owner, "group", scope)
                if parsed is None:
                    emit("owner-invalid", "major", "owner is not a valid entity reference")
                elif parsed[0] not in ("group", "user"):
                    emit("owner-kind", "major", "owner must reference a Group or User")
        if kind in TYPE_REQUIRED_KINDS and not _s(entry.get("type")):
            emit("type-missing", "major", "spec.type is required")
        if kind in LIFECYCLE_REQUIRED_KINDS:
            if not lifecycle:
                emit("lifecycle-missing", "blocker", "spec.lifecycle is required (experimental, production or deprecated)")
            elif lifecycle not in LIFECYCLES:
                emit("lifecycle-nonstandard", "minor", f"lifecycle is not one of {', '.join(LIFECYCLES)}")
        if kind in SYSTEM_EXPECTED_KINDS:
            system = _s(entry.get("system"))
            if not system:
                emit("system-missing", "major" if production else "minor", "spec.system is empty: the entity belongs to no system")
            else:
                parsed = _parse_ref(system, "system", scope)
                if parsed is None or parsed[0] != "system":
                    emit("system-invalid", "major", "system must be a valid System reference")
        tier = _s(entry.get("tier"))
        if kind == "component":
            if not tier:
                emit("tier-missing", "major" if production else "minor", "no tier label: criticality is unknown")
            elif tier not in allowed_tiers:
                emit("tier-unknown", "minor", f"tier is not one of {', '.join(allowed_tiers)}")
        annotations = [a for a in entry.get("annotations", []) if isinstance(a, str)]
        if kind in DOCS_KINDS and DOCS_ANNOTATION not in annotations:
            emit("docs-missing", "major" if production else "minor", f"annotation {DOCS_ANNOTATION} is missing")
        if kind == "component":
            if SOURCE_ANNOTATION not in annotations:
                emit("source-location-missing", "info", f"annotation {SOURCE_ANNOTATION} is missing")
            has_runbook = any(a.rsplit("/", 1)[-1].lower() in RUNBOOK_KEYS for a in annotations) or any(
                "runbook" in link.lower() for link in entry.get("links", []) if isinstance(link, str)
            )
            if production and not has_runbook:
                emit("runbook-missing", "major" if tier.lower() in CRITICAL_TIERS else "minor", "production component has no runbook annotation or link")
        if kind in DESCRIBED_KINDS and not _s(entry.get("description")):
            emit("description-missing", "info", "metadata.description is empty")
        own = (kind, scope, name.lower())
        seen_deps: set[tuple[str, str, str]] = set()
        for position, dep in enumerate(entry.get("depends_on", [])):
            parsed = _parse_ref(_s(dep), "component", scope)  # Backstage: dependsOn defaults to kind Component in the entity's own namespace
            if parsed is None:
                emit("dependson-invalid", "major", f"dependsOn[{position}] is not a valid entity reference")
            elif parsed == own:
                emit("dependson-self", "major", "the entity depends on itself")
            elif parsed in seen_deps:
                emit("dependson-duplicate", "minor", f"dependsOn lists {_ref_text(parsed)} more than once")
            else:
                seen_deps.add(parsed)
    return found


def catalog_lint(payload: dict[str, Any]) -> Report:
    """Lint flattened catalog entities for owner, lifecycle, system, tier, dependsOn, docs and runbook metadata."""

    def work() -> Report:
        tiers = payload.get("allowed_tiers")
        if tiers is not None:
            if any(not _s(t) for t in tiers):
                raise _Bad("allowed_tiers", "allowed_tiers must not contain blank values")
            allowed = tuple(_s(t) for t in tiers)
        else:
            allowed = DEFAULT_TIERS
        found = [f for _, f in _lint_entries(payload["entries"], allowed)]
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "entry_count": len(payload["entries"]),
            "finding_count": total,
            "blocker_count": _count(found, "blocker"),
            "major_count": _count(found, "major"),
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 2. scaffolder template parameters versus golden-path inputs
# =============================================================================================================


def _by_name(items: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        name = _s(item["name"])
        if not name:
            raise _Bad(field, f"{field}: a name is blank")
        if name in out:
            raise _Bad(field, f"{field}: duplicate name {_ident(name)}")
        out[name] = item
    return out


def template_params_check(payload: dict[str, Any]) -> Report:
    """Compare a scaffolder template's parameters with the golden path's required inputs, types and allowed values."""

    def work() -> Report:
        template = _by_name(payload["template_parameters"], "template_parameters")
        golden = _by_name(payload["golden_inputs"], "golden_inputs")
        found: list[Finding] = []
        missing = mismatched = 0
        for name in sorted(golden):
            want = golden[name]
            have = template.get(name)
            if have is None:
                if want["required"]:
                    missing += 1
                    found.append(_finding("input-missing", name, "blocker", "required golden-path input is not a template parameter"))
                else:
                    found.append(_finding("optional-input-absent", name, "info", "optional golden-path input is not exposed by the template"))
                continue
            if have["type"] != want["type"] and not (want["type"] == "number" and have["type"] == "integer"):
                mismatched += 1
                found.append(_finding("type-mismatch", name, "major", f"template type {have['type']} but the golden path expects {want['type']}"))
            if want["required"] and not have["required"] and "default" not in have:
                found.append(_finding("required-not-enforced", name, "major", "golden path requires it but the template makes it optional without a default"))
            allowed = want.get("allowed_values") or []
            if allowed:
                offered = have.get("enum_values") or []
                if not offered:
                    found.append(_finding("unrestricted-values", name, "major", "golden path restricts the values but the template accepts free-form input"))
                else:
                    extra = set(offered) - set(allowed)
                    if extra:
                        found.append(_finding("values-not-allowed", name, "major", f"template offers {len(extra)} value(s) outside the golden-path allowed values"))
                if "default" in have and have["default"] not in allowed:
                    found.append(_finding("default-not-allowed", name, "major", "template default is outside the allowed values"))
            if "default" in want and "default" in have and want["default"] != have["default"]:
                found.append(_finding("default-differs", name, "minor", "template default differs from the golden-path default"))
        extras = sorted(set(template) - set(golden))
        for name in extras:
            found.append(_finding("extra-parameter", name, "info", "template parameter is not part of the golden-path contract"))
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "parameter_count": len(template),
            "golden_input_count": len(golden),
            "missing_required_count": missing,
            "type_mismatch_count": mismatched,
            "extra_parameter_count": len(extras),
            "finding_count": total,
            "verdict": "incompatible" if _count(found, "blocker", "major") else "compatible",
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# shared schema-field helpers (composition diff, claim validation)
# =============================================================================================================


def _schema_fields(items: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        path = item["path"]
        if not path or any((not seg) or seg != seg.strip() for seg in path.split(".")):
            raise _Bad(field, f"{field}: path {_ident(path, 200)} must be dot-separated non-blank segments")
        if path in out:
            raise _Bad(field, f"{field}: duplicate path {_ident(path, 200)}")
        for key in ("minimum", "maximum", "min_length", "max_length"):
            if key in item and not _is_finite_number(item[key]):
                raise _Bad(field, f"{field}: {_ident(path, 200)} {key} is not a finite number")
        if "minimum" in item and "maximum" in item and _frac(item["minimum"], "minimum") > _frac(item["maximum"], "maximum"):
            raise _Bad(field, f"{field}: {_ident(path, 200)} minimum exceeds maximum")
        if "min_length" in item and "max_length" in item and item["min_length"] > item["max_length"]:
            raise _Bad(field, f"{field}: {_ident(path, 200)} min_length exceeds max_length")
        out[path] = item
    return out


# =============================================================================================================
# 3. composition (XRD-style) schema diff
# =============================================================================================================


def _limit_change(found: list[Finding], stem: str, path: str, old: object, new: object, upper: bool) -> None:
    """Constraint drift of one bound: tightening breaks existing claims, loosening is additive."""
    if old is None and new is None:
        return
    if old is None:
        found.append(_finding(f"{stem}-added", path, "major", f"new {stem.replace('-', ' ')} {new} restricts values that were valid"))
    elif new is None:
        found.append(_finding(f"{stem}-removed", path, "info", f"{stem.replace('-', ' ')} {old} was removed"))
    else:
        before, after = _frac(old, stem), _frac(new, stem)
        if before == after:
            return
        tighter = after < before if upper else after > before
        found.append(
            _finding(f"{stem}-{'tightened' if tighter else 'loosened'}", path, "major" if tighter else "info", f"{stem.replace('-', ' ')} {old} -> {new}")
        )


def _optional_new_parent(path: str, old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> str | None:
    """The shallowest ancestor of `path` that existing claims cannot set and that is new and optional, else None.

    A claim sets a path with every ancestor present (claim_validate's presence rule), and `required` binds only when all of them are
    present. An ancestor is settable by existing claims when the old schema lists it or any path under it: `a.b` listed as a string
    makes `a` and `a.b` present. A required child whose ancestors are all settable, or that has no ancestor, is breaking (None). An
    unsettable new optional ancestor means the child never binds for an existing claim.
    """
    parts = path.split(".")
    ancestors = [".".join(parts[:depth]) for depth in range(1, len(parts))]

    def settable(parent: str) -> bool:
        return any(listed == parent or listed.startswith(parent + ".") for listed in old)

    if all(settable(parent) for parent in ancestors):
        return None
    for parent in ancestors:
        if not settable(parent) and parent in new and not new[parent]["required"]:
            return parent
    return None


def composition_diff(payload: dict[str, Any]) -> Report:
    """Diff two versions of a composition schema (flattened XRD fields): breaking versus additive changes."""

    def work() -> Report:
        old = _schema_fields(payload["old_fields"], "old_fields")
        new = _schema_fields(payload["new_fields"], "new_fields")
        same_version = _s(payload["old_version"]) == _s(payload["new_version"])
        found: list[Finding] = []
        for path in sorted(set(old) | set(new)):
            before, after = old.get(path), new.get(path)
            if before is None:
                if after is not None and after["required"]:
                    parent = _optional_new_parent(path, old, new)
                    if parent is not None:
                        found.append(_finding("field-added-under-optional-parent", path, "info", f"new required field binds only when the new optional parent {_ident(parent, 200)} is set: existing claims do not set it"))
                    else:
                        found.append(_finding("field-added-required", path, "major", "new required field: existing claims no longer validate"))
                else:
                    found.append(_finding("field-added", path, "info", "new optional field"))
                continue
            if after is None:
                found.append(_finding("field-removed", path, "major", "field removed: claims that set it lose the value"))
                continue
            if before["type"] != after["type"]:
                if before["type"] == "integer" and after["type"] == "number":
                    found.append(_finding("type-widened", path, "info", "integer widened to number"))
                else:
                    found.append(_finding("type-changed", path, "major", f"type {before['type']} -> {after['type']}"))
                    continue
            if not before["required"] and after["required"]:
                found.append(_finding("required-added", path, "major", "optional field became required"))
            elif before["required"] and not after["required"]:
                found.append(_finding("required-dropped", path, "info", "required field became optional"))
            old_enum, new_enum = before.get("enum_values") or None, after.get("enum_values") or None
            if old_enum is None and new_enum is not None:
                found.append(_finding("enum-added", path, "major", "free-form field is now restricted to an enum"))
            elif old_enum is not None and new_enum is None:
                found.append(_finding("enum-dropped", path, "info", "enum restriction removed"))
            elif old_enum is not None and new_enum is not None:
                removed, added = set(old_enum) - set(new_enum), set(new_enum) - set(old_enum)
                if removed:
                    found.append(_finding("enum-values-removed", path, "major", f"{len(removed)} value(s) no longer accepted"))
                if added:
                    found.append(_finding("enum-values-added", path, "info", f"{len(added)} value(s) now accepted"))
            _limit_change(found, "minimum", path, before.get("minimum"), after.get("minimum"), upper=False)
            _limit_change(found, "maximum", path, before.get("maximum"), after.get("maximum"), upper=True)
            _limit_change(found, "min-length", path, before.get("min_length"), after.get("min_length"), upper=False)
            _limit_change(found, "max-length", path, before.get("max_length"), after.get("max_length"), upper=True)
        breaking = _count(found, "major")
        additive = _count(found, "info")
        if breaking and same_version:
            found.append(_finding("breaking-change-in-same-version", _s(payload["new_version"]), "blocker", "breaking changes need a new schema version, not an edit of the served one"))
        findings, total = _finish(found)
        verdict = "compatible" if not breaking else "breaking-same-version" if same_version else "breaking-new-version"
        return {
            "state": "SUCCESS",
            "breaking_count": breaking,
            "additive_count": additive,
            "change_class": "breaking" if breaking else "additive" if additive else "none",
            "requires_new_version": breaking > 0,
            "finding_count": total,
            "verdict": verdict,
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 4. claim validation against a composition schema
# =============================================================================================================


def _decimal(text: str) -> Decimal | None:
    """The exact Decimal of numeric text. Zero is zero at any exponent ("0E-1000", "0E99999999999999999999"), so it is read from its
    mantissa; a non-zero value whose exponent is outside the Decimal range is None, which the caller reports as out of range."""
    if _ZERO_TEXT.fullmatch(text):
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _claim_number(item: dict[str, Any], index: int) -> Decimal | None:
    """The numeric reading of a claim value, or None for non-numeric types. Out-of-range or malformed text is INVALID_INPUT."""
    text = item["value"]
    kind = item["type"]
    if kind == "integer":
        if not _INTEGER_TEXT.fullmatch(text):
            raise _Bad("claim", f"claim[{index}] {_ident(item['path'], 200)}: value ({len(text)} chars) is not an integer")
        return Decimal(text)  # exact at any digit count; never int(), which has a digit limit
    if kind == "number":
        if not _NUMERIC_TEXT.fullmatch(text):
            raise _Bad("claim", f"claim[{index}] {_ident(item['path'], 200)}: value ({len(text)} chars) is not a number")
        number = _decimal(text)
        if number is None or number.adjusted() > 100 or number.adjusted() < -100:
            raise _Bad("claim", f"claim[{index}] {_ident(item['path'], 200)}: value is outside the supported magnitude")
        return number
    if kind == "boolean" and item["value"] not in ("true", "false"):
        raise _Bad("claim", f"claim[{index}] {_ident(item['path'], 200)}: boolean value must be 'true' or 'false'")
    return None


def _enum_hit(value: str, number: Decimal | None, allowed: list[str]) -> bool:
    if number is None:
        return value in allowed
    for candidate in allowed:
        # a candidate beyond the Decimal range can never equal a supported claim number (those are bounded), so it never matches
        if _NUMERIC_TEXT.fullmatch(candidate) and _decimal(candidate) == number:
            return True
    return False


def claim_validate(payload: dict[str, Any]) -> Report:
    """Validate a claim's fields against a composition schema: required, types, enums, numeric bounds and string lengths."""

    def work() -> Report:
        schema = _schema_fields(payload["schema"], "schema")
        claim: dict[str, dict[str, Any]] = {}
        numbers: dict[str, Decimal | None] = {}
        for index, item in enumerate(payload["claim"]):
            path = item["path"]
            if path in claim:
                raise _Bad("claim", f"claim: duplicate path {_ident(path, 200)}")
            numbers[path] = _claim_number(item, index)
            claim[path] = item
        present: set[str] = set()
        for path in claim:
            parts = path.split(".")
            present.update(".".join(parts[: i + 1]) for i in range(len(parts)))
        found: list[Finding] = []
        for path in sorted(claim):
            item, spec = claim[path], schema.get(path)
            if spec is None:
                found.append(_finding("unknown-field", path, "major", "field is not in the composition schema and would be pruned"))
                continue
            number, value = numbers[path], item["value"]
            if item["type"] != spec["type"]:
                whole = number is not None and number == number.to_integral_value()
                if not (item["type"] == "integer" and spec["type"] == "number") and not (item["type"] == "number" and spec["type"] == "integer" and whole):
                    found.append(_finding("type-mismatch", path, "blocker", f"claim gives {item['type']} but the schema declares {spec['type']}"))
                    continue
            allowed = spec.get("enum_values") or []
            if allowed and not _enum_hit(value, number, allowed):
                found.append(_finding("enum-violation", path, "blocker", f"value is not one of the {len(allowed)} allowed values"))
            if number is not None:
                if "minimum" in spec and number < Decimal(repr(spec["minimum"])):
                    found.append(_finding("below-minimum", path, "blocker", f"value is below the minimum {spec['minimum']}"))
                if "maximum" in spec and number > Decimal(repr(spec["maximum"])):
                    found.append(_finding("above-maximum", path, "blocker", f"value is above the maximum {spec['maximum']}"))
            if spec["type"] == "string":
                if "min_length" in spec and len(value) < spec["min_length"]:
                    found.append(_finding("too-short", path, "blocker", f"length {len(value)} is below min_length {spec['min_length']}"))
                if "max_length" in spec and len(value) > spec["max_length"]:
                    found.append(_finding("too-long", path, "blocker", f"length {len(value)} is above max_length {spec['max_length']}"))
        for path in sorted(schema):
            if not schema[path]["required"] or path in present:
                continue
            parts = path.split(".")
            ancestors = [".".join(parts[:i]) for i in range(1, len(parts))]
            if all(a in present for a in ancestors):  # JSON Schema: `required` binds only when the parent object exists
                found.append(_finding("required-missing", path, "blocker", "required field is absent from the claim"))
        findings, total = _finish(found)
        violations = _count(found, "blocker", "major")
        return {
            "state": "SUCCESS",
            "checked_count": len(claim),
            "violation_count": violations,
            "valid": violations == 0,
            "finding_count": total,
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 5. service maturity scorecard
# =============================================================================================================


def maturity_scorecard(payload: dict[str, Any]) -> Report:
    """Cumulative maturity level (none, bronze, silver, gold) per service from ownership, SLO, runbook, on-call, tests, security."""

    def work() -> Report:
        minimum = _frac(payload.get("min_coverage_pct", DEFAULT_MIN_COVERAGE_PCT), "min_coverage_pct")
        required = dict(DEFAULT_REQUIRED_LEVELS)
        override = payload.get("required_levels")
        if override is not None:
            required = {"tier-0": override["tier_0"], "tier-1": override["tier_1"], "tier-2": override["tier_2"], "tier-3": override["tier_3"]}
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        found: list[Finding] = []
        counts = [0, 0, 0, 0]
        for position, service in enumerate(payload["services"]):
            name = _s(service["name"])
            if not name or name in seen:
                raise _Bad("services", f"services: name {_ident(name)} is blank or duplicated")
            seen.add(name)
            name = _label(name, f"services[{position}]")
            coverage_ok = _frac(service["test_coverage_pct"], "test_coverage_pct") >= minimum
            tiers = (
                (("owner", service["has_owner"]), ("runbook", service["has_runbook"])),
                (("slo", service["has_slo"]), ("on-call", service["has_on_call"]), ("test-coverage", coverage_ok)),
                (("sast", service["sast"]), ("dependency-scan", service["dependency_scan"]), ("secret-scan", service["secret_scan"])),
            )
            level, climbing = 0, True
            gaps: list[str] = []
            passed = 0
            for criteria in tiers:
                met = [ok for _, ok in criteria]
                passed += sum(met)
                gaps.extend(label for label, ok in criteria if not ok)
                if climbing and all(met):
                    level += 1
                else:
                    climbing = False
            counts[level] += 1
            need = required[service["tier"]]
            meets = level >= need
            if not meets:
                severity = "major" if service["tier"] in CRITICAL_TIERS else "minor"
                found.append(_finding("below-required-level", name, severity, f"{service['tier']} requires {LEVEL_NAMES[need]}, service is {LEVEL_NAMES[level]}; gaps: {', '.join(gaps)}"))
            rows.append({"name": name, "level": level, "level_name": LEVEL_NAMES[level], "passed": passed, "total": 8, "gaps": gaps, "meets_required": meets})
        rows.sort(key=lambda r: r["name"])
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "service_count": len(rows),
            "average_level": _r(Fraction(sum(i * n for i, n in enumerate(counts)), len(rows)), 2),
            "level_counts": {"none": counts[0], "bronze": counts[1], "silver": counts[2], "gold": counts[3]},
            "below_required_count": _count(found, "major", "minor"),
            "services": rows,
            "finding_count": total,
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 6. DORA metrics banding
# =============================================================================================================


def _median(values: list[int]) -> Fraction:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return Fraction(ordered[mid]) if len(ordered) % 2 else Fraction(ordered[mid - 1] + ordered[mid], 2)


def _band_ascending(value: Fraction, limits: tuple[Fraction, Fraction, Fraction], inclusive: bool = False) -> str:
    """Smaller is better (lead time, failure rate, recovery). `inclusive` makes the limit itself belong to the better band."""
    for band, limit in zip(("elite", "high", "medium"), limits):
        if value < limit or (inclusive and value == limit):
            return band
    return "low"


def dora_bands(payload: dict[str, Any]) -> Report:
    """DORA four keys from deployment and change-caused incident events in a window, each banded elite..low."""

    def work() -> Report:
        start = _ts(payload["window_start"], "window_start", "window_start")
        end = _ts(payload["window_end"], "window_end", "window_end")
        window_us = _micro(start, end)
        if window_us <= 0:
            raise _Bad("window_end", "window_end must be after window_start")
        window_days = Fraction(window_us, DAY_US)
        deploys: dict[str, tuple[datetime, datetime]] = {}
        for index, item in enumerate(payload["deployments"]):
            did = _s(item["id"])
            if not did or did in deploys:
                raise _Bad("deployments", f"deployments[{index}]: id {_ident(did)} is blank or duplicated")
            committed = _ts(item["committed_at"], "deployments", f"deployments[{index}].committed_at")
            deployed = _ts(item["deployed_at"], "deployments", f"deployments[{index}].deployed_at")
            if committed > deployed:
                raise _Bad("deployments", f"deployments[{index}]: committed_at is after deployed_at")
            deploys[did] = (committed, deployed)
        in_window = {k: v for k, v in deploys.items() if start <= v[1] < end}
        failed: dict[str, list[tuple[datetime, datetime | None]]] = {}
        for index, item in enumerate(payload["incidents"]):
            started = _ts(item["started_at"], "incidents", f"incidents[{index}].started_at")
            resolved = _ts(item["resolved_at"], "incidents", f"incidents[{index}].resolved_at") if _s(item.get("resolved_at")) else None
            if resolved is not None and resolved < started:
                raise _Bad("incidents", f"incidents[{index}]: resolved_at is before started_at")
            dep = _s(item.get("deployment_id"))
            if not dep:
                continue  # not change-caused: outside the change failure rate
            if dep not in deploys:
                raise _Bad("incidents", f"incidents[{index}]: deployment_id {_ident(dep)} is not in deployments")
            if started < deploys[dep][1]:
                raise _Bad("incidents", f"incidents[{index}]: started before its deployment")
            if dep in in_window:
                failed.setdefault(dep, []).append((started, resolved))
        count = len(in_window)
        found: list[Finding] = []
        if count == 0:
            found.append(_finding("no-deployments", "", "major", "no deployment in the window: the four keys cannot be banded"))
            findings, total = _finish(found)
            return {
                "state": "SUCCESS", "deployment_count": 0, "failed_deployment_count": 0, "unresolved_failure_count": 0,
                "deployments_per_day": 0.0, "median_lead_time_hours": -1.0, "change_failure_rate": 0.0, "median_recovery_hours": -1.0,
                "frequency_band": "none", "lead_time_band": "none", "change_failure_band": "none", "recovery_band": "none",
                "overall_band": "none", "finding_count": total, "findings": findings,
            }
        per_day = Fraction(count) / window_days
        frequency = "elite" if per_day >= DORA_FREQUENCY_PER_DAY[0] else "high" if per_day >= DORA_FREQUENCY_PER_DAY[1] else "medium" if per_day >= DORA_FREQUENCY_PER_DAY[2] else "low"
        lead_hours = _median([_micro(c, d) for c, d in in_window.values()]) / HOUR_US
        lead_band = _band_ascending(lead_hours, DORA_LEAD_TIME_HOURS)
        failure_rate = Fraction(len(failed), count)
        failure_band = _band_ascending(failure_rate, DORA_FAILURE_RATE, inclusive=True)
        recoveries: list[int] = []
        unresolved = 0
        for dep, incidents in failed.items():
            if any(done is None for _, done in incidents):
                unresolved += 1
            else:
                resolved_incidents = [(started, done) for started, done in incidents if done is not None]
                _, resolved = max(resolved_incidents, key=lambda pair: pair[1])  # latest resolution wins
                # contract (dora-baseline step 4): recovery runs from deployed_at to the latest resolved_at, not from the incident start
                recoveries.append(_micro(deploys[dep][1], resolved))
        recovery_hours = _median(recoveries) / HOUR_US if recoveries else None
        if not failed:
            recovery_band = "none"
        elif unresolved or recovery_hours is None:
            recovery_band = "low"
        else:
            recovery_band = _band_ascending(recovery_hours, DORA_RECOVERY_HOURS)
        bands = [frequency, lead_band, failure_band] + ([recovery_band] if recovery_band != "none" else [])
        overall = min(bands, key=BAND_ORDER.index)
        for label, band in (("deployment-frequency", frequency), ("lead-time", lead_band), ("change-failure-rate", failure_band), ("recovery-time", recovery_band)):
            if band == "low":
                found.append(_finding(f"{label}-low", "", "major", f"{label} is in the low band"))
            elif band == "medium":
                found.append(_finding(f"{label}-medium", "", "minor", f"{label} is in the medium band"))
        if unresolved:
            found.append(_finding("unresolved-failures", "", "major", f"{unresolved} failed deployment(s) have an unresolved incident"))
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "deployment_count": count,
            "failed_deployment_count": len(failed),
            "unresolved_failure_count": unresolved,
            "deployments_per_day": _r(per_day),
            "median_lead_time_hours": _r(lead_hours),
            "change_failure_rate": _r(failure_rate),
            "median_recovery_hours": _r(recovery_hours) if recovery_hours is not None else -1.0,
            "frequency_band": frequency,
            "lead_time_band": lead_band,
            "change_failure_band": failure_band,
            "recovery_band": recovery_band,
            "overall_band": overall,
            "finding_count": total,
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 7. catalog dependency graph
# =============================================================================================================


def _tarjan(nodes: list[str], adjacency: dict[str, list[str]]) -> list[list[str]]:
    """Strongly connected components, dependencies before dependants (reverse topological order)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for nxt in adjacency[node]:
            if nxt not in index:
                visit(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            members = []
            while True:
                top = stack.pop()
                on_stack.discard(top)
                members.append(top)
                if top == node:
                    break
            components.append(sorted(members))

    for node in nodes:
        if node not in index:
            visit(node)
    return components


def _loop(cycle: list[str], adjacency: dict[str, list[str]]) -> list[str]:
    """The shortest dependency loop through the first member of a strongly connected set: [a, b, ..., a] (deterministic)."""
    start, members = cycle[0], set(cycle)
    parent: dict[str, str | None] = {start: None}
    queue = [start]
    for node in queue:
        for nxt in adjacency[node]:
            if nxt not in members:
                continue
            if nxt == start:
                path = [node]
                while True:
                    prev = parent[path[-1]]
                    if prev is None:
                        break
                    path.append(prev)
                return [*reversed(path), start]
            if nxt not in parent:
                parent[nxt] = node
                queue.append(nxt)
    return [start, start]


def catalog_graph(payload: dict[str, Any]) -> Report:
    """Analyse the dependsOn graph: cycles, dangling targets, orphans, unowned entities, lifecycle conflicts, depth, blast radius."""

    def work() -> Report:
        lifecycle: dict[str, str] = {}
        owner: dict[str, str] = {}
        edges: dict[str, list[str]] = {}
        dangling: list[tuple[str, str]] = []
        for index, entry in enumerate(payload["entries"]):
            parsed = _parse_ref(_s(entry["ref"]), "component")
            if parsed is None:
                raise _Bad("entries", f"entries[{index}]: ref {_ident(entry['ref'])} is not a valid entity reference")
            ref = _ref_text(parsed)
            if ref in edges:
                raise _Bad("entries", f"entries[{index}]: {ref} is listed twice")
            lifecycle[ref], owner[ref] = _s(entry.get("lifecycle")).lower(), _s(entry.get("owner"))
            targets: set[str] = set()
            for position, dep in enumerate(entry.get("depends_on", [])):
                dependency = _parse_ref(_s(dep), "component", parsed[1])  # Backstage: a relative dependsOn resolves in the entity's own namespace
                if dependency is None:
                    raise _Bad("entries", f"entries[{index}]: dependsOn[{position}] is not a valid entity reference")
                targets.add(_ref_text(dependency))
            edges[ref] = sorted(targets)
        nodes = sorted(edges)
        adjacency = {n: [t for t in edges[n] if t in edges] for n in nodes}
        found: list[Finding] = []
        for node in nodes:
            for target in edges[node]:
                if target not in edges:
                    dangling.append((node, target))
                    found.append(_finding("dangling-dependency", node, "minor", f"depends on {target}, which is not in the catalog"))
        incoming = {n: 0 for n in nodes}
        for node in nodes:
            for target in adjacency[node]:
                incoming[target] += 1
        cycles = [c for c in _tarjan(nodes, adjacency) if len(c) > 1 or c[0] in adjacency[c[0]]]
        cycles.sort(key=lambda c: c[0])
        for cycle in cycles:
            if len(cycle) == 1:
                found.append(_finding("self-dependency", cycle[0], "blocker", "entity depends on itself"))
                continue
            loop = _loop(cycle, adjacency)
            shown = " -> ".join(loop[:7]) + (" -> ..." if len(loop) > 7 else "")
            found.append(_finding("dependency-cycle", cycle[0], "blocker", f"{len(cycle)} entities depend on each other in a cycle; shortest loop through the first: {shown} ({len(loop) - 1} hops)"))
        orphans = [n for n in nodes if n.split(":", 1)[0] in ("component", "resource") and not edges[n] and incoming[n] == 0]
        for node in orphans:
            found.append(_finding("orphan", node, "minor", "no dependency edges in either direction"))
        unowned = [n for n in nodes if not owner[n] and n.split(":", 1)[0] in OWNER_REQUIRED_KINDS]
        for node in unowned:
            found.append(_finding("unowned", node, "major", "entity has no owner"))
        for node in nodes:
            for target in adjacency[node]:
                if node == target:
                    continue
                if lifecycle[target] == "deprecated" and lifecycle[node] != "deprecated":
                    found.append(_finding("deprecated-dependency", node, "major" if lifecycle[node] == "production" else "minor", f"depends on deprecated {target}"))
                if lifecycle[node] == "production" and lifecycle[target] == "experimental":
                    found.append(_finding("experimental-dependency", node, "major", f"production entity depends on experimental {target}"))
        components = _tarjan(nodes, adjacency)
        depth_of: dict[str, int] = {}
        max_depth = 0
        for component in components:
            members = set(component)
            below = [depth_of[t] for n in component for t in adjacency[n] if t not in members]
            depth = 1 + max(below) if below else 0
            for n in component:
                depth_of[n] = depth
            max_depth = max(max_depth, depth)
        reverse: dict[str, list[str]] = {n: [] for n in nodes}
        for node in nodes:
            for target in adjacency[node]:
                reverse[target].append(node)
        impact: list[_Impact] = []
        for node in nodes:
            reached: set[str] = set()
            frontier = list(reverse[node])
            while frontier:
                current = frontier.pop()
                if current in reached:
                    continue
                reached.add(current)
                frontier.extend(reverse[current])
            reached.discard(node)
            impact.append({"ref": node, "direct_dependents": len(set(reverse[node]) - {node}), "transitive_dependents": len(reached)})
        impact.sort(key=lambda r: (-r["transitive_dependents"], r["ref"]))
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "entry_count": len(nodes),
            "edge_count": sum(len(v) for v in edges.values()),
            "cycle_count": len(cycles),
            "cycles": cycles,
            "dangling_count": len(dangling),
            "orphan_count": len(orphans),
            "unowned_count": len(unowned),
            "max_depth": max_depth,
            "most_depended_on": [r for r in impact[:5] if r["transitive_dependents"] > 0],
            "finding_count": total,
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 8. ephemeral environment TTL and cost policy
# =============================================================================================================


def ephemeral_env_policy(payload: dict[str, Any]) -> Report:
    """Check ephemeral environments against TTL, idle and cost policy at a given instant; names what to reclaim (never reclaims)."""

    def work() -> Report:
        checked = _ts(payload["checked_at"], "checked_at", "checked_at")
        max_ttl = _frac(payload["max_ttl_hours"], "max_ttl_hours")
        idle_limit = _frac(payload["idle_after_hours"], "idle_after_hours")
        max_cost = _frac(payload["max_cost_usd"], "max_cost_usd")
        found: list[Finding] = []
        reclaim: set[str] = set()
        seen: set[str] = set()
        expired = idle = over = 0
        accrued_total = projected_total = Fraction(0)
        for index, env in enumerate(payload["environments"]):
            name = _s(env["name"])
            if not name or name in seen:
                raise _Bad("environments", f"environments[{index}]: name {_ident(name)} is blank or duplicated")
            seen.add(name)
            name = _label(name, f"environments[{index}]")
            created = _ts(env["created_at"], "environments", f"environments[{index}].created_at")
            age_us = _micro(created, checked)
            if age_us < 0:
                raise _Bad("environments", f"environments[{index}]: created_at is after checked_at")
            last_text = _s(env.get("last_active_at"))
            idle_us = age_us
            if last_text:
                last = _ts(last_text, "environments", f"environments[{index}].last_active_at")
                if last < created or last > checked:
                    raise _Bad("environments", f"environments[{index}]: last_active_at must lie between created_at and checked_at")
                idle_us = _micro(last, checked)
            age_h = Fraction(age_us, HOUR_US)
            ttl = _frac(env["ttl_hours"], "ttl_hours")
            effective = min(ttl, max_ttl) if ttl > 0 else max_ttl
            hourly = _frac(env["hourly_cost_usd"], "hourly_cost_usd")
            accrued, projected = hourly * age_h, hourly * effective
            accrued_total += accrued
            projected_total += projected
            if ttl == 0:
                found.append(_finding("ttl-missing", name, "blocker", f"no TTL: the policy maximum of {payload['max_ttl_hours']} h is applied"))
            elif ttl > max_ttl:
                found.append(_finding("ttl-above-policy", name, "major", f"ttl {env['ttl_hours']} h exceeds the policy maximum {payload['max_ttl_hours']} h; {payload['max_ttl_hours']} h is applied"))
            if age_h >= effective:
                expired += 1
                reclaim.add(name)
                found.append(_finding("expired", name, "major", f"age {_r(age_h, 2)} h has reached the effective TTL {_r(effective, 2)} h"))
            if Fraction(idle_us, HOUR_US) >= idle_limit:
                idle += 1
                reclaim.add(name)
                found.append(_finding("idle", name, "minor", f"idle for {_r(Fraction(idle_us, HOUR_US), 2)} h (limit {payload['idle_after_hours']} h)"))
            if accrued > max_cost:
                over += 1
                found.append(_finding("cost-over-budget", name, "major", f"accrued {_r(accrued, 2)} USD exceeds the per-environment budget {payload['max_cost_usd']} USD"))
            elif projected > max_cost:
                found.append(_finding("projected-cost-over-budget", name, "minor", f"cost over the full TTL ({_r(projected, 2)} USD) exceeds the budget {payload['max_cost_usd']} USD"))
            if not _s(env.get("owner")):
                found.append(_finding("owner-missing", name, "minor", "no owner to notify before reclaim"))
        total_budget = payload.get("total_budget_usd")
        if total_budget is not None and accrued_total > _frac(total_budget, "total_budget_usd"):
            found.append(_finding("total-budget-exceeded", "", "major", f"accrued {_r(accrued_total, 2)} USD exceeds the total budget {total_budget} USD"))
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "environment_count": len(seen),
            "expired_count": expired,
            "idle_count": idle,
            "over_budget_count": over,
            "total_accrued_cost_usd": _r(accrued_total, 2),
            "total_projected_cost_usd": _r(projected_total, 2),
            "reclaim": sorted(reclaim),
            "finding_count": total,
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 9. tenant quota allocation fit
# =============================================================================================================


def quota_fit(payload: dict[str, Any]) -> Report:
    """Do the teams' quotas fit capacity (after platform reserve and overcommit)? Also flags over-quota, hot and idle teams."""

    def work() -> Report:
        reserve = _frac(payload.get("reserve_pct", 0), "reserve_pct")
        overcommit = _frac(payload.get("overcommit_ratio", 1), "overcommit_ratio")
        capacity: dict[str, Fraction] = {}
        labels: dict[str, str] = {}
        for position, item in enumerate(payload["capacity"]):
            resource = _s(item["resource"])
            if not resource or resource in capacity:
                raise _Bad("capacity", f"capacity: resource {_ident(resource)} is blank or duplicated")
            capacity[resource] = _frac(item["total"], "total")
            labels[resource] = _label(resource, f"capacity[{position}]")
        grants: dict[tuple[str, str], tuple[Fraction, Fraction]] = {}
        for position, item in enumerate(payload["allocations"]):
            team, resource = _s(item["team"]), _s(item["resource"])
            if not team or resource not in capacity:
                raise _Bad("allocations", f"allocations[{position}]: team is blank or resource is blank or has no capacity entry")
            if (team, resource) in grants:
                raise _Bad("allocations", f"allocations[{position}]: team and resource are listed twice")
            grants[(team, resource)] = (_frac(item["quota"], "quota"), _frac(item["used"], "used"))
        found: list[Finding] = []
        rows: list[dict[str, Any]] = []
        oversubscribed = over_quota = 0
        for resource in sorted(capacity):
            total = capacity[resource]
            allocatable = total * (1 - reserve / 100) * overcommit
            quotas = sum((q for (_, r), (q, _) in grants.items() if r == resource), Fraction(0))
            used = sum((u for (_, r), (_, u) in grants.items() if r == resource), Fraction(0))
            fits = quotas <= allocatable
            if not fits:
                oversubscribed += 1
                found.append(_finding("oversubscribed", labels[resource], "major", f"quotas {_r(quotas, 2)} exceed allocatable {_r(allocatable, 2)} by {_r(quotas - allocatable, 2)}"))
            rows.append({
                "resource": labels[resource], "capacity": _r(total, 2), "allocatable": _r(allocatable, 2), "allocated": _r(quotas, 2), "used": _r(used, 2),
                "allocation_ratio": _r(quotas / allocatable), "usage_ratio": _r(used / total), "fits": fits,
                "shortfall": _r(max(Fraction(0), quotas - allocatable), 2),
            })
        for (team, resource), (quota, used) in sorted(grants.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            subject = f"{_label(team, 'team')}/{labels[resource]}"
            if used > quota:
                over_quota += 1
                found.append(_finding("over-quota", subject, "major", f"used {_r(used, 2)} exceeds quota {_r(quota, 2)}"))
            elif quota > 0 and used / quota >= QUOTA_HOT:
                found.append(_finding("quota-hot", subject, "minor", f"used {_r(used / quota)} of quota (>= 0.9): raise the quota or reduce the load"))
            elif quota > 0 and used / quota < QUOTA_IDLE:
                found.append(_finding("quota-idle", subject, "info", f"used {_r(used / quota)} of quota (< 0.2): {_r(quota - used, 2)} could be reclaimed"))
        findings, finding_total = _finish(found)
        return {
            "state": "SUCCESS",
            "resources": rows,
            "oversubscribed_count": oversubscribed,
            "over_quota_count": over_quota,
            "finding_count": finding_total,
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 10. golden-path adoption coverage
# =============================================================================================================


def _version(text: str, field: str, label: str) -> tuple[int, int, int]:
    match = _SEMVER.fullmatch(_s(text))
    if match is None:
        raise _Bad(field, f"{label}: version ({len(_s(text))} chars) is not MAJOR.MINOR.PATCH")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def golden_path_coverage(payload: dict[str, Any]) -> Report:
    """Adoption of golden-path templates across services: on-path, current, outdated, unsupported and off-path."""

    def work() -> Report:
        paths: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {}
        labels: dict[str, str] = {}
        for position, item in enumerate(payload["golden_paths"]):
            pid = _s(item["id"])
            if not pid or pid in paths:
                raise _Bad("golden_paths", f"golden_paths[{position}]: id is blank or duplicated")
            labels[pid] = _label(pid, f"golden_paths[{position}]")
            latest = _version(item["latest_version"], "golden_paths", f"{labels[pid]}.latest_version")
            floor = _version(item["min_supported_version"], "golden_paths", f"{labels[pid]}.min_supported_version")
            if floor > latest:
                raise _Bad("golden_paths", f"golden_paths: {labels[pid]} min_supported_version is above latest_version")
            paths[pid] = (latest, floor)
        stats: dict[str, _PathStats] = {pid: _PathStats(id=labels[pid], services=0, current=0, outdated=0, unsupported=0) for pid in sorted(paths)}
        found: list[Finding] = []
        seen: set[str] = set()
        current = outdated = unsupported = off_path = 0
        for position, service in enumerate(payload["services"]):
            name = _s(service["name"])
            if not name or name in seen:
                raise _Bad("services", f"services: name {_ident(name)} is blank or duplicated")
            seen.add(name)
            name = _label(name, f"services[{position}]")
            critical = _s(service.get("tier")).lower() in CRITICAL_TIERS
            template = _s(service.get("template"))
            if template not in paths:
                off_path += 1
                rule = "custom-template" if template else "no-golden-path"
                what = "uses a template that is not a golden path" if template else "is not on any golden path"
                found.append(_finding(rule, name, "major" if critical else "minor", f"service {what}"))
                continue
            latest, floor = paths[template]
            row = stats[template]
            row["services"] += 1
            raw = _s(service.get("template_version"))
            version = _version(raw, "services", f"{name}.template_version") if _SEMVER.fullmatch(raw) else None
            if version is None:
                unsupported += 1
                row["unsupported"] += 1
                found.append(_finding("version-unknown", name, "major", f"template {labels[template]} has no usable MAJOR.MINOR.PATCH version: currency cannot be proven"))
            elif version < floor:
                unsupported += 1
                row["unsupported"] += 1
                found.append(_finding("golden-path-unsupported", name, "major", f"{labels[template]} {raw} is below the minimum supported version"))
            elif version < latest:
                outdated += 1
                row["outdated"] += 1
                found.append(_finding("golden-path-outdated", name, "minor", f"{labels[template]} {raw} is behind the latest version"))
            else:
                current += 1
                row["current"] += 1
        count = len(seen)
        on_path = count - off_path
        coverage = Fraction(on_path, count)
        meets = coverage >= _frac(payload["target_ratio"], "target_ratio")
        if not meets:
            found.append(_finding("coverage-below-target", "", "major", f"coverage {_r(coverage)} is below the target {payload['target_ratio']}"))
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "service_count": count,
            "on_path_count": on_path,
            "current_count": current,
            "outdated_count": outdated,
            "unsupported_count": unsupported,
            "off_path_count": off_path,
            "coverage_ratio": _r(coverage),
            "supported_ratio": _r(Fraction(current + outdated, count)),
            "meets_target": meets,
            "by_path": list(stats.values()),
            "finding_count": total,
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 11. platform API deprecation window
# =============================================================================================================


def api_deprecation(payload: dict[str, Any]) -> Report:
    """Check platform API deprecations: notice window by stability, sunset dates and consumers still on a sunset API."""

    def work() -> Report:
        checked = _ts(payload["checked_at"], "checked_at", "checked_at")
        warn_days = Fraction(payload.get("warn_days", 30))
        override = payload.get("min_window_days")
        found: list[Finding] = []
        at_risk: set[str] = set()
        seen: set[str] = set()
        deprecated = past = removable = 0
        for index, api in sorted(enumerate(payload["apis"]), key=lambda p: (_s(p[1]["name"]), _s(p[1]["version"]))):
            name, version = _s(api["name"]), _s(api["version"])
            key = f"{name}/{version}"
            if not name or not version or key in seen:
                raise _Bad("apis", f"apis[{index}]: {_label(name, 'name')}/{_label(version, 'version')} is blank or duplicated")
            seen.add(key)
            subject = f"{_label(name, f'apis[{index}]')}/{_label(version, 'version')}"
            deprecated_text, sunset_text = _s(api.get("deprecated_at")), _s(api.get("sunset_at"))
            announced = _ts(deprecated_text, "apis", f"apis[{index}].deprecated_at") if deprecated_text else None
            sunset = _ts(sunset_text, "apis", f"apis[{index}].sunset_at") if sunset_text else None
            if announced is not None and announced > checked:
                raise _Bad("apis", f"apis[{index}]: deprecated_at is after checked_at")
            if announced is not None and sunset is not None and sunset <= announced:
                raise _Bad("apis", f"apis[{index}]: sunset_at must be after deprecated_at")

            def emit(rule: str, severity: str, summary: str, subject: str = subject, key: str = key) -> None:
                found.append(_finding(rule, subject, severity, summary))
                if severity in ("blocker", "major"):
                    at_risk.add(key)

            if api["state"] == "active":
                if sunset is not None:
                    emit("sunset-on-active-api", "minor", "a sunset date is set but the state is still active: mark it deprecated")
                continue
            deprecated += 1
            consumers = api["active_consumers"]
            if announced is None:
                emit("deprecation-date-missing", "major", "deprecated without an announcement date: the notice window cannot be proven")
            if sunset is None:
                emit("sunset-missing", "major", "deprecated without a sunset date: consumers cannot plan a migration")
                continue
            if announced is not None:
                window = Fraction(_micro(announced, sunset), DAY_US)
                needed = Fraction(override) if override is not None else Fraction(DEPRECATION_WINDOW_DAYS[api["stability"]])
                if window < needed:
                    if override is not None:
                        floor = f"the min_window_days override of {_r(needed, 2)} days"
                    elif api["stability"] == "beta":
                        floor = f"the 9-month floor of {_r(needed, 2)} days for beta APIs (the 3-release arm of Kubernetes rule 4a is not modelled)"
                    else:
                        floor = f"{_r(needed, 2)} days for {api['stability']} APIs"
                    emit("window-too-short", "major", f"notice window {_r(window, 2)} days is below {floor}")
            if checked >= sunset:
                past += 1
                if consumers > 0:
                    emit("sunset-passed-with-consumers", "blocker", f"sunset passed but {consumers} consumer(s) still call it")
                else:
                    removable += 1
                    emit("removable", "info", "sunset passed and no consumers remain: safe to remove")
            elif consumers > 0:
                days_left = Fraction(_micro(checked, sunset), DAY_US)
                if days_left <= warn_days:
                    emit("sunset-imminent", "major", f"{_r(days_left, 2)} days to sunset with {consumers} consumer(s) still active")
                else:
                    emit("consumers-pending-migration", "minor", f"{consumers} consumer(s) must migrate within {_r(days_left, 2)} days")
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "api_count": len(seen),
            "deprecated_count": deprecated,
            "past_sunset_count": past,
            "removable_count": removable,
            "at_risk_count": len(at_risk),
            "finding_count": total,
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))


# =============================================================================================================
# 12. self-service request guardrails
# =============================================================================================================


def request_guardrails(payload: dict[str, Any]) -> Report:
    """Decide allow / needs_approval / deny for self-service requests against region, class, size, cap and replica policy."""

    def work() -> Report:
        order = [_s(s) for s in payload["size_order"]]
        if any(not s for s in order) or len(set(order)) != len(order):
            raise _Bad("size_order", "size_order values must be non-blank and unique")
        auto, ceiling = _s(payload["auto_approve_size"]), _s(payload["max_size"])
        if auto not in order:
            raise _Bad("auto_approve_size", "auto_approve_size must be listed in size_order")
        if ceiling not in order:
            raise _Bad("max_size", "max_size must be listed in size_order")
        if order.index(auto) > order.index(ceiling):
            raise _Bad("auto_approve_size", "auto_approve_size must not be larger than max_size")
        regions, classes = set(payload["allowed_regions"]), set(payload["allowed_classes"])
        caps = (("cpu_cores", "cpu-above-cap"), ("memory_gib", "memory-above-cap"), ("storage_gib", "storage-above-cap"))
        limits = {"cpu_cores": payload["max_cpu_cores"], "memory_gib": payload["max_memory_gib"], "storage_gib": payload["max_storage_gib"]}
        found: list[Finding] = []
        decisions: list[dict[str, str]] = []
        seen: set[str] = set()
        for position, request in enumerate(payload["requests"]):
            raw_id = _s(request["id"])
            if not raw_id or raw_id in seen:
                raise _Bad("requests", f"requests[{position}]: id is blank or duplicated")
            seen.add(raw_id)
            rid = _label(raw_id, f"requests[{position}]")
            denied: list[Finding] = []
            approval: list[Finding] = []
            if request["region"] not in regions:
                denied.append(_finding("region-not-allowed", rid, "major", "region is not in allowed_regions"))
            if request["service_class"] not in classes:
                denied.append(_finding("class-not-allowed", rid, "major", "service class is not in allowed_classes"))
            size = _s(request["size"])
            if size not in order:
                denied.append(_finding("size-unknown", rid, "major", "size is not in size_order"))
            elif order.index(size) > order.index(ceiling):
                denied.append(_finding("size-above-max", rid, "major", f"size {_ident(size)} is above the maximum {_ident(ceiling)}"))
            elif order.index(size) > order.index(auto):
                approval.append(_finding("size-needs-approval", rid, "minor", f"size {_ident(size)} is above the auto-approved size {_ident(auto)}"))
            for key, rule in caps:
                if _frac(request[key], key) > _frac(limits[key], key):
                    denied.append(_finding(rule, rid, "major", f"{key} {request[key]} is above the cap {limits[key]}"))
            if request["environment"] == "prod" and request["replicas"] < payload["min_prod_replicas"]:
                denied.append(_finding("replicas-below-prod-minimum", rid, "major", f"{request['replicas']} replica(s) in prod, minimum {payload['min_prod_replicas']}"))
            found.extend(denied + approval)
            decisions.append({"id": rid, "decision": "deny" if denied else "needs_approval" if approval else "allow"})
        decisions.sort(key=lambda d: d["id"])
        findings, total = _finish(found)
        return {
            "state": "SUCCESS",
            "request_count": len(decisions),
            "allowed_count": sum(1 for d in decisions if d["decision"] == "allow"),
            "needs_approval_count": sum(1 for d in decisions if d["decision"] == "needs_approval"),
            "denied_count": sum(1 for d in decisions if d["decision"] == "deny"),
            "decisions": decisions,
            "finding_count": total,
            "status": _status(found),
            "findings": findings,
        }

    return _reply(payload, _guarded(work))
