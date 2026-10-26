"""A23 Service Mesh & Networking Engineer tools: pure analyses over manifests and specs the caller already parsed.

Nothing here talks to a cluster; every input is a JSON object (a parsed Kubernetes / Istio / Gateway API manifest or a
small fact table) and every result is decision-ready: counts, a `verdict` (pass | warn | fail) and a `findings` list of
{id, severity, summary} with severity info | minor | major | blocker. `fail` means at least one major or blocker finding,
`warn` at least one minor one. Findings are sorted by severity (worst first) then id, and capped at MAX_FINDINGS;
`finding_count` is always the uncapped total. Name lists in results are sorted and capped at MAX_LISTED entries. Resource findings start with `<namespace>/<name>: `.

Tools:
  mesh_authz_lint            Istio AuthorizationPolicy lint: default-deny per namespace, allow-all rules, wildcard principals
  mesh_mtls_coverage         effective PeerAuthentication mode per workload (workload > namespace > mesh > PERMISSIVE)
  mesh_traffic_split         VirtualService route weights, destination subsets, retry and timeout sanity
  mesh_destination_policy    DestinationRule outlier detection, connection pool, TLS mode and subset label checks
  mesh_gateway_api_check     Gateway API Gateway / HTTPRoute attachment and cross-namespace ReferenceGrant validity
  mesh_netpol_coverage       NetworkPolicy default-deny coverage per namespace (policies are additive)
  mesh_netpol_reachability   can pod A reach pod B on a port under the supplied NetworkPolicies
  mesh_retry_amplification   worst-case request multiplication of retries along a call chain
  mesh_timeout_hierarchy     caller timeouts must exceed callee timeouts along a call chain
  mesh_dns_ndots             resolver lookups per name for a pod's ndots / search-domain configuration
  mesh_egress_coverage       observed outbound destinations versus ServiceEntry coverage and outbound policy mode
  mesh_cidr_overlap          pod, service and node CIDR overlaps within and between clusters
  mesh_ingress_tls           ingress TLS hygiene: version floor, ciphers, certificate SANs, HSTS, plaintext listeners

Public references (the small tables below follow them):
  Istio security.istio.io AuthorizationPolicy / PeerAuthentication and networking.istio.io VirtualService / DestinationRule /
  ServiceEntry reference, https://istio.io/latest/docs/reference/config/ ;
  Kubernetes NetworkPolicy, https://kubernetes.io/docs/concepts/services-networking/network-policies/ ;
  Gateway API spec (Gateway, HTTPRoute, ReferenceGrant), https://gateway-api.sigs.k8s.io/reference/spec/ ;
  Kubernetes DNS for Services and Pods, https://kubernetes.io/docs/concepts/services-networking/dns-concepts/ ;
  RFC 8996 (TLS 1.0 and 1.1 deprecated), RFC 7568 (SSLv3 deprecated), RFC 6797 (HSTS), RFC 6125 (certificate names).
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from typing import TypedDict, cast, overload

from swarm.tools.support import respond

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
MAX_FINDINGS = 250
MAX_SUMMARY = 300
MAX_LISTED = 100  # cap on plain name lists in results (counts always cover everything)
MAX_VALUE = 600  # cap on any string in a result (names and hosts come from the caller's manifests and are unbounded)
DEFAULT_NAMESPACE = "default"
DEFAULT_ROOT_NAMESPACE = "istio-system"  # Istio's default rootNamespace: policies there apply mesh-wide
DEFAULT_CLUSTER_DOMAIN = "cluster.local"

# --- Istio AuthorizationPolicy -------------------------------------------------------------------------------------
AUTHZ_ACTIONS = ("ALLOW", "DENY", "AUDIT", "CUSTOM")  # AuthorizationPolicy.spec.action; ALLOW is the default
AUTHZ_NEGATIVE_SOURCE = ("notPrincipals", "notRequestPrincipals", "notNamespaces", "notIpBlocks", "notRemoteIpBlocks")  # source.not*
AUTHZ_NEGATIVE_OPERATION = ("notHosts", "notPorts", "notPaths", "notMethods")  # to.operation.not*
AUTHZ_SOURCE_FIELDS = ("principals", "notPrincipals", "requestPrincipals", "notRequestPrincipals", "namespaces", "notNamespaces",
                       "ipBlocks", "notIpBlocks", "remoteIpBlocks", "notRemoteIpBlocks")  # recognised from.source keys; a value narrows unless universal
AUTHZ_OPERATION_FIELDS = ("hosts", "ports", "methods", "paths")  # positive to.operation keys: "*" matches any value, so no constraint
AUTHZ_CONDITION_FIELDS = ("values", "notValues")  # when[] keys; any value narrows, "*" included; `key` only names the attribute
# --- Istio PeerAuthentication --------------------------------------------------------------------------------------
MTLS_MODES = ("UNSET", "DISABLE", "PERMISSIVE", "STRICT")  # UNSET inherits from the parent; with no parent it is PERMISSIVE
# --- Istio VirtualService / DestinationRule ------------------------------------------------------------------------
ROUTE_WEIGHT_TOTAL = 100  # destination weights of one route are meant to sum to 100
MAX_RETRY_ATTEMPTS = 5  # house sanity ceiling for retries.attempts; Istio itself has no upper bound
TLS_CLIENT_MODES = ("DISABLE", "SIMPLE", "MUTUAL", "ISTIO_MUTUAL")  # ClientTLSSettings.mode
MIN_ISTIO_DURATION_MS = Decimal(1)  # outlier interval / baseEjectionTime / connectTimeout must be >= 1ms
# --- Gateway API ---------------------------------------------------------------------------------------------------
GATEWAY_GROUP = "gateway.networking.k8s.io"
LISTENER_PROTOCOLS = ("HTTP", "HTTPS", "TLS", "TCP", "UDP")  # core protocols; implementation-specific ones contain a "/"
ROUTE_NAMESPACE_SCOPES = ("All", "Selector", "Same", "None")  # allowedRoutes.namespaces.from; "None" lets no route attach
HTTPROUTE_PROTOCOLS = ("HTTP", "HTTPS")  # listener protocols an HTTPRoute attaches to; a TLS listener takes TLSRoute
# --- Kubernetes NetworkPolicy --------------------------------------------------------------------------------------
POLICY_TYPES = ("Ingress", "Egress")
NETWORK_PROTOCOLS = ("TCP", "UDP", "SCTP")
SELECTOR_OPERATORS = ("In", "NotIn", "Exists", "DoesNotExist")
NAMESPACE_NAME_LABEL = "kubernetes.io/metadata.name"  # set by the API server on every namespace since Kubernetes 1.21
MAX_NAMESPACES = 200
# --- Kubernetes DNS ------------------------------------------------------------------------------------------------
MAX_DNS_SEARCH_PATHS = 32  # Pod dnsConfig.searches limit in API validation (MaxDNSSearchPaths, pkg/apis/core/validation)
MAX_DNS_SEARCH_CHARS = 2048  # limit on the space-joined search list (MaxDNSSearchListChars), same validation
MAX_NDOTS = 15  # glibc resolv.conf clamps ndots at 15
# --- TLS -----------------------------------------------------------------------------------------------------------
TLS_VERSIONS = {"SSLV3": (3, 0), "1.0": (3, 1), "1.1": (3, 2), "1.2": (3, 3), "1.3": (3, 4)}  # wire versions
MODERN_TLS = (3, 3)  # TLS 1.2: the floor required by RFC 8996 era guidance
HSTS_RECOMMENDED_MAX_AGE_S = 31_536_000  # one year; also the hstspreload.org minimum
WEAK_CIPHER_TOKENS = ("RC4", "RC2", "DES", "NULL", "EXPORT", "ANON", "ADH", "AECDH", "MD5")  # DES also matches 3DES
KNOWN_CIPHER_TOKENS = ("AES", "CHACHA20", "CAMELLIA", "ARIA", "SEED", "IDEA", "RC4", "DES", "NULL", "RC2")
FORWARD_SECRET_TOKENS = ("ECDHE", "DHE", "EDH")

# Secret safety: results echo names, hosts and values taken from the caller's manifests. Anything shaped like a credential is
# masked on the way out, so a key pasted into a name, host or field never reaches a finding, an id or an error message.
_REDACTED = "[redacted]"
_SECRET_PATTERNS = (
    (re.compile(r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://[^\s/?#]*@"), _REDACTED),  # scheme://user:pass@ (credentials in a URL)
    (re.compile(r"(?<![\w.%+-])[\w.%+-]+:[^\s@/:]+@(?=[\w.-])"), _REDACTED),  # user:pass@host without a scheme
    (re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|APKA)[A-Z0-9]{16}\b"), _REDACTED),  # AWS access key id
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), _REDACTED),  # GitHub tokens
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), _REDACTED),  # Slack tokens
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), _REDACTED),  # API secret keys
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), _REDACTED),  # JWT
    (re.compile(r"(?<![0-9A-Za-z])(?:0x)?[0-9a-fA-F]{40,}(?![0-9A-Za-z])"), _REDACTED),  # 40+ hex digits: private keys, key material
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), _REDACTED),
    (re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), _REDACTED),  # Authorization header values
    (re.compile(r"(?i)(\b(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|auth(?:orization)?)\b\s*=\s*)\S+"), r"\1" + _REDACTED),
    (re.compile(r"(?i)(\b(?:password|passwd|pwd|api[_-]?key|authorization)\b\s*:\s*)\S+"), r"\1" + _REDACTED),
    (re.compile(r"(?<![A-Za-z])(?:[a-z]{3,8} ){11,}[a-z]{3,8}(?![A-Za-z])"), _REDACTED),  # 12+ lowercase words in a row: a seed phrase
)


@overload
def _scrub(value: str) -> str: ...
@overload
def _scrub(value: dict[str, object]) -> dict[str, object]: ...
@overload
def _scrub(value: list[object]) -> list[object]: ...
@overload
def _scrub(value: object) -> object: ...
def _scrub(value: object) -> object:
    """A copy of a result with credential-shaped substrings of every string masked and each string capped at MAX_VALUE
    characters; numbers, booleans and keys stay."""
    if isinstance(value, str):
        for pattern, replacement in _SECRET_PATTERNS:
            value = pattern.sub(replacement, value)
        return value if len(value) <= MAX_VALUE else value[: MAX_VALUE - 3] + "..."
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    return value


class _Invalid(Exception):
    """Semantic input problem; `field` is the top-level input property it concerns."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _invalid(field: str, message: str) -> dict[str, object]:
    """The one INVALID_INPUT shape: {state, field, message}."""
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _guarded(analyse: Callable[[dict[str, object]], dict[str, object]],
             payload: dict[str, object]) -> Callable[[], dict[str, object]]:
    """Wrap an analysis as the `work` callable of `respond`, mapping _Invalid to INVALID_INPUT and masking secrets."""

    def work() -> dict[str, object]:
        try:
            return _scrub(analyse(payload))
        except _Invalid as exc:
            return _scrub(_invalid(exc.field, str(exc)))

    return work


def _respond(payload: dict[str, object], analyse: Callable[[dict[str, object]], dict[str, object]]) -> dict[str, object]:
    """support.respond with its result typed: `respond` is untyped and returns the dict that the guarded analysis produces."""
    result: dict[str, object] = respond(payload, _guarded(analyse, payload))
    return result


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _clip(text: str) -> str:
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 3] + "..."


def _finding(fid: str, severity: str, summary: str) -> dict[str, str]:
    """A raw finding; the summary is masked and clipped once, in `_report`, for the findings that are actually returned."""
    return {"id": fid, "severity": severity, "summary": summary}


def _report(findings: list[dict[str, str]]) -> dict[str, object]:
    """verdict, finding_count and the sorted, capped findings list shared by every tool (summaries masked, then clipped)."""
    ordered = sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))
    top = max((SEVERITY_RANK[f["severity"]] for f in findings), default=0)
    verdict = "fail" if top >= SEVERITY_RANK["major"] else "warn" if top >= SEVERITY_RANK["minor"] else "pass"
    shown = [{**f, "summary": _clip(_scrub(f["summary"]))} for f in ordered[:MAX_FINDINGS]]
    return {"verdict": verdict, "finding_count": len(findings), "findings": shown}


# --- shared parsing helpers ----------------------------------------------------------------------------------------


def _dict(value: object, field: str, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise _Invalid(field, f"{what} must be an object")
    return value


def _list(value: object, field: str, what: str) -> list[object]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _Invalid(field, f"{what} must be a list")
    return value


def _str(value: object, field: str, what: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise _Invalid(field, f"{what} must be a non-empty string")
    if len(value) > 253:  # DNS subdomain length; keeps every name echoed in a result inside its output_schema maxLength
        raise _Invalid(field, f"{what} is longer than 253 characters")
    return value


def _nonneg_int(value: object, field: str, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Invalid(field, f"{what} must be a non-negative integer")
    return value


def _meta(doc: object, field: str, index: int, kind: str) -> tuple[str, str]:
    """(namespace, name) of a manifest; namespace defaults to `default`."""
    manifest = _dict(doc, field, f"{field}[{index}]")
    metadata = _dict(manifest.get("metadata") or {}, field, f"{field}[{index}].metadata")
    name = _str(metadata.get("name"), field, f"{field}[{index}] ({kind}) metadata.name")
    namespace = metadata.get("namespace", DEFAULT_NAMESPACE)
    return _str(namespace, field, f"{field}[{index}] metadata.namespace"), name


def _spec(doc: object, field: str, index: int) -> dict[str, object]:
    return _dict(_dict(doc, field, f"{field}[{index}]").get("spec") or {}, field, f"{field}[{index}].spec")


def _labels(value: object, field: str, what: str) -> dict[str, object]:
    labels = _dict(value if value is not None else {}, field, what)
    for key, val in labels.items():
        if not isinstance(val, str):
            raise _Invalid(field, f"{what} label {key!r} must have a string value")
    return labels


def _selector_matches(selector: object, labels: dict[str, object], field: str) -> bool:
    """Kubernetes LabelSelector semantics: an empty or absent selector matches everything; matchLabels and every
    matchExpressions requirement are ANDed."""
    if selector is None:
        return True
    selector = _dict(selector, field, "selector")
    for key, value in _labels(selector.get("matchLabels"), field, "selector.matchLabels").items():
        if labels.get(key) != value:
            return False
    for expression in _list(selector.get("matchExpressions"), field, "selector.matchExpressions"):
        expression = _dict(expression, field, "selector.matchExpressions item")
        key = _str(expression.get("key"), field, "selector.matchExpressions key")
        operator = expression.get("operator")
        values = _list(expression.get("values"), field, "selector.matchExpressions values")
        if operator not in SELECTOR_OPERATORS:
            raise _Invalid(field, f"selector operator {operator!r} must be one of {', '.join(SELECTOR_OPERATORS)}")
        if operator in ("In", "NotIn") and not values:
            raise _Invalid(field, f"selector operator {operator} on {key!r} needs values")
        if operator in ("Exists", "DoesNotExist") and values:
            raise _Invalid(field, f"selector operator {operator} on {key!r} must not have values")
        present = key in labels
        if operator == "In" and not (present and labels[key] in values):
            return False
        if operator == "NotIn" and present and labels[key] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


def _selector_empty(selector: dict[str, object] | None) -> bool:
    return not selector or (not (selector.get("matchLabels") or {}) and not (selector.get("matchExpressions") or []))


_DURATION_PART = re.compile(r"(\d+\.?\d*|\.\d+)(ns|us|µs|ms|s|m|h)")
_DURATION_MS = {
    "ns": Decimal("0.000001"),
    "us": Decimal("0.001"),
    "µs": Decimal("0.001"),
    "ms": Decimal(1),
    "s": Decimal(1000),
    "m": Decimal(60_000),
    "h": Decimal(3_600_000),
}


def _duration_ms(value: object, field: str, what: str) -> Decimal:
    """Go-style duration ("500ms", "1.5s", "1h30m") in milliseconds; "0" is zero. Negative or malformed -> INVALID_INPUT."""
    if not isinstance(value, str) or not value:
        raise _Invalid(field, f"{what} must be a duration string such as 500ms or 2s")
    if value == "0":
        return Decimal(0)
    position, total = 0, Decimal(0)
    while position < len(value):
        match = _DURATION_PART.match(value, position)
        if match is None:
            raise _Invalid(field, f"{what} {value!r} is not a duration such as 500ms or 2s")
        total += Decimal(match.group(1)) * _DURATION_MS[match.group(2)]
        position = match.end()
    return total


def _fqdn(host: str, namespace: str) -> str:
    """Istio resolves a host without a dot relative to the namespace of the rule; `*`, IP literals and dotted hosts are taken as
    written (pilot/pkg/model ResolveShortnameToFQDN)."""
    host = host.lower()
    if host == "*" or "." in host:
        return host
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return f"{host}.{namespace}.svc.{DEFAULT_CLUSTER_DOMAIN}"


def _istio_duration_problem(milliseconds: Decimal) -> str | None:
    """Istio's agent.ValidateDuration: at least 1ms and a whole number of milliseconds (a field set to 0s is rejected too)."""
    if milliseconds < MIN_ISTIO_DURATION_MS:
        return "below the 1ms minimum"
    if milliseconds % 1:
        return "not a whole number of milliseconds"
    return None


def _ts(value: object, field: str, what: str) -> datetime:
    if not isinstance(value, str):
        raise _Invalid(field, f"{what} must be an ISO-8601 timestamp with an offset")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _Invalid(field, f"{what} {value!r} is not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None:
        raise _Invalid(field, f"{what} {value!r} needs an explicit UTC offset")
    return parsed


# --- mesh_authz_lint -----------------------------------------------------------------------------------------------


def _has_wildcard(values: list[object]) -> tuple[bool, bool]:
    """(exact "*", prefix/suffix pattern) in an Istio string-match list; Istio only supports "*", "prefix*", "*suffix"."""
    exact = any(v == "*" for v in values if isinstance(v, str))
    pattern = any(isinstance(v, str) and v != "*" and (v.startswith("*") or v.endswith("*")) for v in values)
    return exact, pattern


_IPV4_MAPPED_ALL = ipaddress.ip_network("::ffff:0.0.0.0/96")  # every IPv4 address written in IPv6 notation


_Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def _ip_network(value: object, *, strict: bool) -> _Network:
    """ipaddress.ip_network on a parsed JSON value. It takes an int or a str and raises ValueError or TypeError for anything
    else, and every caller catches both."""
    return ipaddress.ip_network(cast(str | int, value), strict=strict)  # the stub takes str or int; other values fail inside ip_network


def _ip_address(value: object) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """ipaddress.ip_address on a parsed JSON value (see _ip_network); callers catch ValueError."""
    return ipaddress.ip_address(cast(str | int, value))  # same cast as _ip_network


def _subnet_of(inner: _Network, outer: _Network) -> bool:
    """inner.subnet_of(outer); networks of different IP versions are never nested, so the answer is False for them."""
    if isinstance(inner, ipaddress.IPv4Network) and isinstance(outer, ipaddress.IPv4Network):
        return inner.subnet_of(outer)
    if isinstance(inner, ipaddress.IPv6Network) and isinstance(outer, ipaddress.IPv6Network):
        return inner.subnet_of(outer)
    return False


def _universal_block(value: object) -> bool:
    """Whether an ipBlock CIDR admits every address: 0.0.0.0/0, ::/0, or an IPv6 network that contains the whole IPv4-mapped range
    ::ffff:0.0.0.0/96 (::/64 and ::/80, and the shorter prefixes down to ::/0; not ::/81 or ::/96). The mapped range counts as universal
    on the inference [INFERENCE] that Istio matches IPv4 peers under that range, so a network containing all of it covers every IPv4
    peer written in IPv6 notation. A narrower mapped range (for example ::ffff:10.0.0.0/104) is not universal."""
    try:
        network = _ip_network(value, strict=False)
    except (TypeError, ValueError):
        return False
    return network.prefixlen == 0 or _subnet_of(_IPV4_MAPPED_ALL, network)


def _no_universal(key: str, values: list[object]) -> bool:
    """No value of this key matches every value of it."""
    return False


def _operation_wildcard(key: str, values: list[object]) -> bool:
    """A to.operation list containing "*" matches every value of its field."""
    return "*" in values


def _source_universal(key: str, values: list[object]) -> bool:
    """An ipBlocks or remoteIpBlocks list containing 0.0.0.0/0 or ::/0 matches every address."""
    return key in ("ipBlocks", "remoteIpBlocks") and any(_universal_block(value) for value in values)


def _narrows(fields: dict[str, object], keys: tuple[str, ...], field: str, where: str, *,
             universal: Callable[[str, list[object]], bool]) -> bool:
    """Whether one group of recognised fields constrains a request: some key holds values that do not all match every value of it.
    `universal(key, values)` says when they do, and such a key does not narrow. Otherwise a value narrows, as Istio reads it: a source
    "*" means any authenticated request, a negated "*" means only requests without that value, and a when "*" means the attribute
    must be present. Unrecognised fields are ignored, as Istio ignores them."""
    for key in keys:
        values = _list(fields.get(key), field, f"{where}.{key}")
        if values and not universal(key, values):
            return True
    return False


def _matches_everything(rule: dict[str, object], field: str, where: str) -> bool:
    """Whether an ALLOW rule matches every request: the literal `{}`, or one whose from, to and when all constrain nothing. Within one
    list the items are ORed, so one unconstrained item frees the list; an absent or empty list constrains nothing; the lists are ANDed,
    so the rule is open only when every one of them is free. A from source is unconstrained when none of its recognised fields narrows
    it: a value narrows unless its key matches every value (a universal ipBlock, 0.0.0.0/0 or ::/0). A to operation is unconstrained
    when its positive fields (hosts, ports, methods, paths) contain "*" or are empty, and its negated fields are empty. A when condition
    is unconstrained when its values and notValues are both empty."""

    def free(key: str, item_free: Callable[[dict[str, object]], bool]) -> bool:
        items = _list(rule.get(key), field, f"{where}.{key}")
        return not items or any(item_free(_dict(item, field, f"{where}.{key} item")) for item in items)

    def source_free(item: dict[str, object]) -> bool:
        source = _dict(item.get("source") or {}, field, f"{where}.from.source")
        return not _narrows(source, AUTHZ_SOURCE_FIELDS, field, f"{where}.from.source", universal=_source_universal)

    def operation_free(item: dict[str, object]) -> bool:
        operation = _dict(item.get("operation") or {}, field, f"{where}.to.operation")
        positive = _narrows(operation, AUTHZ_OPERATION_FIELDS, field, f"{where}.to.operation", universal=_operation_wildcard)
        negated = _narrows(operation, AUTHZ_NEGATIVE_OPERATION, field, f"{where}.to.operation", universal=_no_universal)
        return not (positive or negated)

    def condition_free(item: dict[str, object]) -> bool:
        return not _narrows(item, AUTHZ_CONDITION_FIELDS, field, f"{where}.when", universal=_no_universal)

    return free("from", source_free) and free("to", operation_free) and free("when", condition_free)


def _without_negations(rule: dict[str, object]) -> dict[str, object]:
    """The rule with its negative matches removed (source not*, to.operation not*, when notValues): what its positive matches still
    constrain. Its from, to and when items are validated objects by the time the caller uses it."""
    part = dict(rule)
    from_items = rule.get("from")
    if isinstance(from_items, list):
        part["from"] = [_without_part(item, "source", AUTHZ_NEGATIVE_SOURCE) for item in from_items]
    to_items = rule.get("to")
    if isinstance(to_items, list):
        part["to"] = [_without_part(item, "operation", AUTHZ_NEGATIVE_OPERATION) for item in to_items]
    when_items = rule.get("when")
    if isinstance(when_items, list):
        part["when"] = [{k: v for k, v in _dict(item, "policies", "when item").items() if k != "notValues"} for item in when_items]
    return part


def _without_part(item: object, part_name: str, keys: tuple[str, ...]) -> dict[str, object]:
    """`item` with the listed keys removed from its `part_name` mapping (source or operation)."""
    fields = _dict(item, "policies", "rule item")
    return {**fields, part_name: {k: v for k, v in _dict(fields.get(part_name) or {}, "policies", part_name).items() if k not in keys}}


def _authz_lint(payload: dict[str, object]) -> dict[str, object]:
    root = payload.get("root_namespace", DEFAULT_ROOT_NAMESPACE)
    policies = _list(payload["policies"], "policies", "policies")
    findings: list[dict[str, str]] = []
    deny_all: set[str] = set()  # namespaces where a request needs a matching ALLOW: allow-nothing policy or specific ALLOW rules
    block_all: set[str] = set()  # namespaces with a DENY policy whose empty rule matches every request
    allow_all: set[str] = set()  # namespaces where an ALLOW policy admits every request
    mesh_deny, mesh_block, mesh_allow = False, False, False
    seen: set[str] = set()
    for index, doc in enumerate(policies):
        namespace, name = _meta(doc, "policies", index, "AuthorizationPolicy")
        spec = _spec(doc, "policies", index)
        subject = f"{namespace}/{name}"
        seen.add(namespace)
        selector = _dict(spec.get("selector") or {}, "policies", f"policies[{index}].spec.selector")
        unsupported = sorted(key for key, value in selector.items() if key != "matchLabels" and value)
        if unsupported:
            findings.append(_finding("AUTHZ-SELECTOR-UNSUPPORTED", "minor", f"{subject}: selector sets {', '.join(unsupported)}, which is not part "
                                     "of Istio's WorkloadSelector (matchLabels only); the policy is treated as scoped to workloads"))
        selected = bool(selector.get("matchLabels")) or bool(unsupported)
        target_refs = bool(spec.get("targetRefs") or spec.get("targetRef"))
        if selected and target_refs:
            findings.append(_finding("AUTHZ-SCOPE-CONFLICT", "major", f"{subject}: selector and targetRefs are mutually exclusive"))
        scoped = selected or target_refs
        mesh_wide = namespace == root and not scoped
        action = spec.get("action", "ALLOW")
        if action not in AUTHZ_ACTIONS:
            findings.append(_finding("AUTHZ-ACTION-UNKNOWN", "major", f"{subject}: action {action!r} is not one of {', '.join(AUTHZ_ACTIONS)}"))
            continue
        rules = _list(spec.get("rules"), "policies", f"policies[{index}].spec.rules")
        if action == "CUSTOM":
            provider = spec.get("provider")
            if not (isinstance(provider, dict) and provider.get("name")):
                findings.append(_finding("AUTHZ-CUSTOM-NO-PROVIDER", "major", f"{subject}: CUSTOM action needs provider.name"))
        if action == "DENY" and not rules:
            findings.append(_finding("AUTHZ-DENY-NO-RULES", "minor", f"{subject}: a DENY policy without rules matches nothing"))
        if action == "DENY" and not scoped and any(isinstance(rule, dict) and not rule for rule in rules):
            where = "the mesh" if mesh_wide else "the namespace"  # Istio's documented deny-all: action DENY with rules [{}]
            findings.append(_finding("AUTHZ-DENY-ALL", "info", f"{subject}: a DENY rule {{}} blocks every request to {where}, whatever any ALLOW policy says"))
            if mesh_wide:
                mesh_block = True
            else:
                block_all.add(namespace)
        if action != "ALLOW":
            continue
        if not rules:
            if not scoped:  # an ALLOW policy with no rules allows nothing: the Istio allow-nothing idiom
                if mesh_wide:
                    mesh_deny = True
                else:
                    deny_all.add(namespace)
            continue
        allows_everything = False
        for rule_index, rule in enumerate(rules):
            rule = _dict(rule, "policies", f"policies[{index}].spec.rules[{rule_index}]")
            rule_where = f"policies[{index}].spec.rules[{rule_index}]"
            if _matches_everything(rule, "policies", rule_where):
                allows_everything = True
                where = "the mesh" if mesh_wide else "the namespace" if not scoped else "the selected workloads"
                findings.append(_finding("AUTHZ-ALLOW-ALL", "major" if scoped else "blocker",
                                         f"{subject}: rules[{rule_index}] matches every request (no recognised value in its from, to or when) "
                                         f"and allows every request to {where}"))
                if mesh_wide:
                    mesh_allow = True
                elif not scoped:
                    allow_all.add(namespace)
            negated: list[str] = []  # negative matches in this rule: the rule admits everything except the listed values
            sources = _list(rule.get("from"), "policies", f"{rule_where}.from")
            if rule and not sources and not rule.get("when"):  # the literal {} rule is reported as AUTHZ-ALLOW-ALL alone
                findings.append(_finding("AUTHZ-NO-SOURCE", "minor", f"{subject}: rules[{rule_index}] has no from: any source, including "
                                         "unauthenticated plaintext clients, matches"))
            for source_item in sources:
                source = _dict(_dict(source_item, "policies", "from item").get("source") or {}, "policies", "from.source")
                for key, fid, label in (("principals", "AUTHZ-WILDCARD-PRINCIPAL", "principal"),
                                        ("requestPrincipals", "AUTHZ-WILDCARD-REQUEST-PRINCIPAL", "request principal"),
                                        ("namespaces", "AUTHZ-WILDCARD-NAMESPACE", "namespace")):
                    exact, pattern = _has_wildcard(_list(source.get(key), "policies", f"from.source.{key}"))
                    if exact:
                        findings.append(_finding(fid, "major", f"{subject}: rules[{rule_index}] allows any {label} ({key}: \"*\")"))
                    elif pattern:
                        findings.append(_finding("AUTHZ-BROAD-PATTERN", "minor", f"{subject}: rules[{rule_index}] {key} uses a prefix or "
                                                 "suffix wildcard that admits many identities"))
                for key in ("ipBlocks", "remoteIpBlocks"):
                    if any(_universal_block(v) for v in _list(source.get(key), "policies", f"from.source.{key}")):
                        findings.append(_finding("AUTHZ-OPEN-IPBLOCK", "major", f"{subject}: rules[{rule_index}] {key} admits every address"))
                negated.extend(f"from.source.{key}" for key in AUTHZ_NEGATIVE_SOURCE if _list(source.get(key), "policies", f"from.source.{key}"))
            for op_item in _list(rule.get("to"), "policies", f"policies[{index}].spec.rules[{rule_index}].to"):
                operation = _dict(_dict(op_item, "policies", "to item").get("operation") or {}, "policies", "to.operation")
                negated.extend(f"to.operation.{key}" for key in AUTHZ_NEGATIVE_OPERATION
                               if _list(operation.get(key), "policies", f"to.operation.{key}"))
            for condition in _list(rule.get("when"), "policies", f"policies[{index}].spec.rules[{rule_index}].when"):
                if _list(_dict(condition, "policies", "when item").get("notValues"), "policies", "when.notValues"):
                    negated.append("when.notValues")
            if negated and _matches_everything(_without_negations(rule), "policies", rule_where):
                # only the negative matches constrain this rule, so it admits every value but the listed ones; positive matches (principals,
                # paths, methods, values) that also constrain it leave a narrower rule, which is not an open one
                findings.append(_finding("AUTHZ-NEGATIVE-MATCH", "major", f"{subject}: rules[{rule_index}] has a negative match "
                                         f"({', '.join(sorted(set(negated)))}): it admits every value except the listed ones"))
                allows_everything = True
                if mesh_wide:
                    mesh_allow = True
                elif not scoped:
                    allow_all.add(namespace)
        if not scoped and not allows_everything:  # any ALLOW policy in scope makes a request need a matching rule
            if mesh_wide:
                mesh_deny = True
            else:
                deny_all.add(namespace)
    wanted = payload.get("namespaces")
    if wanted is None:
        targets = sorted(seen)
    else:
        targets = sorted({_str(item, "namespaces", "namespace") for item in _list(wanted, "namespaces", "namespaces")})
    missing = [ns for ns in targets
               if not (mesh_block or ns in block_all or ((mesh_deny or ns in deny_all) and not (mesh_allow or ns in allow_all)))]
    for ns in missing:
        findings.append(_finding("AUTHZ-NO-DEFAULT-DENY", "major",
                                 f"{ns}: no effective default-deny: no namespace-wide ALLOW policy (empty or with specific rules) or DENY rule "
                                 f"{{}} in {ns} or {root}, or an allow-all rule cancels it; workloads no policy selects accept every request"))
    return {
        "state": "SUCCESS",
        "policy_count": len(policies),
        "namespace_count": len(targets),
        "mesh_wide_default_deny": mesh_block or (mesh_deny and not mesh_allow),
        "namespaces_without_default_deny": missing,
        **_report(findings),
    }


def authz_lint(payload: dict[str, object]) -> dict[str, object]:
    """Lint Istio AuthorizationPolicies. Semantics (Istio authorization-policy reference): CUSTOM is evaluated first, then any
    matching DENY denies, then with no ALLOW policy in scope everything is allowed, otherwise a request needs a matching ALLOW.
    A namespace is default-deny when a namespace-wide (or root-namespace) ALLOW policy applies to it (empty `spec`, the
    allow-nothing idiom, or specific rules) or a namespace-wide DENY policy has the rule `{}`; an ALLOW rule `{}` (matches
    everything) cancels the ALLOW-based form, a DENY `{}` cannot be cancelled. An ALLOW rule with a negative match (source
    notPrincipals, notRequestPrincipals, notNamespaces, notIpBlocks, notRemoteIpBlocks; to.operation notHosts, notPorts, notPaths,
    notMethods; when notValues) admits every value but the listed ones, when no positive match narrows the rule further:
    AUTHZ-NEGATIVE-MATCH (major), and then it cancels the ALLOW-based form like `{}`. Wildcards are only a risk in ALLOW rules: principals/requestPrincipals/namespaces "*" (any
    authenticated identity, issuer or namespace), prefix/suffix patterns, 0.0.0.0/0 ip blocks, and rules without `from` (admit
    unauthenticated plaintext clients). A selector key other than matchLabels is AUTHZ-SELECTOR-UNSUPPORTED and scopes the policy
    to workloads. Without `namespaces`, only namespaces that hold a policy are checked: list every namespace to catch one with none."""
    return _respond(payload, _authz_lint)


# --- mesh_mtls_coverage --------------------------------------------------------------------------------------------


class _PeerAuth(TypedDict):
    namespace: str
    name: str
    mode: str
    labels: dict[str, object]
    ports: dict[int, str]
    created: datetime | None


class _Row(TypedDict):
    namespace: str
    workload_count: int
    strict_count: int


def _contenders(candidates: list[_PeerAuth]) -> tuple[_PeerAuth, list[_PeerAuth]]:
    """The oldest same-level candidate (dated ones first, ties by name) and every candidate that could be the effective one: those
    sharing its creationTimestamp, and every undated one, since an undated object cannot be ordered against the others."""
    ordered = sorted(candidates, key=lambda c: (c["created"] is None, c["created"].timestamp() if c["created"] else 0.0, c["name"]))
    top = ordered[0]
    return top, [c for c in ordered if c["created"] is None or c["created"] == top["created"]]


def _conflicting(contenders: list[_PeerAuth], inherited: str | None) -> bool:
    """True when the contenders would give different effective postures (mode and per-port modes), so the outcome depends on which
    one Istio picks. An UNSET mode takes `inherited`, the mode the levels below decide; None (they conflict) leaves it unknown."""
    ports = sorted({port for c in contenders for port in c["ports"]})
    postures: set[tuple[str, tuple[str, ...]]] = set()
    for c in contenders:
        mode = inherited if c["mode"] == "UNSET" else c["mode"]
        if mode is None:
            return True
        per_port = tuple(mode if c["ports"].get(port, "UNSET") == "UNSET" else c["ports"][port] for port in ports)
        postures.add((mode, per_port))
    return len(postures) > 1


def _decided(levels: tuple[tuple[str, list[_PeerAuth]], ...], start: int) -> str | None:
    """The mode the levels from `start` on decide for a workload (PERMISSIVE when none does), or None when a level is in conflict."""
    for index in range(start, len(levels)):
        candidates = levels[index][1]
        if not candidates:
            continue
        pick, contenders = _contenders(candidates)
        below = _decided(levels, index + 1)
        if _conflicting(contenders, below):
            return None
        return pick["mode"] if pick["mode"] != "UNSET" else below
    return "PERMISSIVE"


def _parse_peer_auth(doc: object, index: int) -> _PeerAuth:
    namespace, name = _meta(doc, "peer_authentications", index, "PeerAuthentication")
    spec = _spec(doc, "peer_authentications", index)
    mtls = _dict(spec.get("mtls") or {}, "peer_authentications", "spec.mtls")
    mode = mtls.get("mode", "UNSET")
    if not isinstance(mode, str) or mode not in MTLS_MODES:
        raise _Invalid("peer_authentications", f"{namespace}/{name}: mtls.mode {mode!r} must be one of {', '.join(MTLS_MODES)}")
    selector = _dict(spec.get("selector") or {}, "peer_authentications", "spec.selector")
    if any(value for key, value in selector.items() if key != "matchLabels"):
        raise _Invalid("peer_authentications", f"{namespace}/{name}: spec.selector supports matchLabels only")
    match_labels = _labels(selector.get("matchLabels"), "peer_authentications", "spec.selector.matchLabels")
    ports: dict[int, str] = {}
    for key, value in _dict(spec.get("portLevelMtls") or {}, "peer_authentications", "spec.portLevelMtls").items():
        try:
            port = int(key)
        except (TypeError, ValueError):
            raise _Invalid("peer_authentications", f"{namespace}/{name}: portLevelMtls key {key!r} is not a port number") from None
        if not 1 <= port <= 65535:
            raise _Invalid("peer_authentications", f"{namespace}/{name}: portLevelMtls port {port} is outside 1..65535")
        port_mode = _dict(value, "peer_authentications", "portLevelMtls entry").get("mode", "UNSET")
        if not isinstance(port_mode, str) or port_mode not in MTLS_MODES:
            raise _Invalid("peer_authentications", f"{namespace}/{name}: portLevelMtls {port} mode {port_mode!r} is not a valid mode")
        ports[port] = port_mode
    metadata = _dict(_dict(doc, "peer_authentications", f"peer_authentications[{index}]").get("metadata") or {},
                     "peer_authentications", f"peer_authentications[{index}].metadata")
    stamp = metadata.get("creationTimestamp")
    created = _ts(stamp, "peer_authentications", f"{namespace}/{name} metadata.creationTimestamp") if stamp is not None else None
    return {"namespace": namespace, "name": name, "mode": mode, "labels": match_labels, "ports": ports, "created": created}


def _mtls_coverage(payload: dict[str, object]) -> dict[str, object]:
    root = payload.get("root_namespace", DEFAULT_ROOT_NAMESPACE)
    target = payload.get("min_strict_pct", 100)
    policies = [_parse_peer_auth(doc, i) for i, doc in enumerate(_list(payload["peer_authentications"], "peer_authentications", "list"))]
    findings: list[dict[str, str]] = []
    for pa in policies:
        if pa["ports"] and not pa["labels"]:
            findings.append(_finding("MTLS-PORT-NO-SELECTOR", "major",
                                     f"{pa['namespace']}/{pa['name']}: portLevelMtls requires a workload selector"))
    rows: dict[str, _Row] = {}
    groups: dict[tuple[str, str], list[str]] = {}
    non_strict: list[str] = []
    strict = permissive = disabled = port_exceptions = 0
    workloads = _list(payload["workloads"], "workloads", "workloads")
    for index, workload in enumerate(workloads):
        workload = _dict(workload, "workloads", f"workloads[{index}]")
        namespace = _str(workload.get("namespace"), "workloads", f"workloads[{index}].namespace")
        name = _str(workload.get("name"), "workloads", f"workloads[{index}].name")
        labels = _labels(workload.get("labels"), "workloads", f"workloads[{index}].labels")
        subject = f"{namespace}/{name}"
        in_ns = [p for p in policies if p["namespace"] == namespace]
        levels = (
            ("workload policy", [p for p in in_ns if p["labels"] and all(labels.get(k) == v for k, v in p["labels"].items())]),
            ("namespace policy", [p for p in in_ns if not p["labels"]]),
            ("mesh-wide policy", [p for p in policies if p["namespace"] == root and not p["labels"]]),
        )
        mode, source = "PERMISSIVE", "default (no PeerAuthentication)"
        chosen: _PeerAuth | None = None
        for index, (level, candidates) in enumerate(levels):
            if not candidates:
                continue
            pick, contenders = _contenders(candidates)
            if _conflicting(contenders, _decided(levels, index + 1)):
                names = ", ".join(sorted(f"{c['namespace']}/{c['name']}" for c in contenders)[:5])
                findings.append(_finding("MTLS-CONFLICT", "major", f"{subject}: {len(contenders)} {level}(s) tie or are undated and resolve "
                                         f"to different modes or port rules ({names}); the effective mode is unknown, so the workload is not counted strict"))
                mode = "DISABLE" if any(c["mode"] == "DISABLE" for c in contenders) else "PERMISSIVE"
                source, chosen = f"{level} conflict", None
                break
            if level == "workload policy":
                chosen = pick
            if pick["mode"] != "UNSET":
                mode, source = pick["mode"], f"{level} {pick['namespace']}/{pick['name']}"
                break
        weak_ports: list[tuple[int, str]] = []
        if chosen is not None:
            for port, port_mode in sorted(chosen["ports"].items()):
                effective = mode if port_mode == "UNSET" else port_mode
                if effective != "STRICT":
                    weak_ports.append((port, effective))
        row = rows.setdefault(namespace, {"namespace": namespace, "workload_count": 0, "strict_count": 0})
        row["workload_count"] += 1
        if mode == "STRICT" and not weak_ports:
            strict += 1
            row["strict_count"] += 1
            continue
        if mode == "STRICT":
            port_exceptions += 1
            non_strict.append(f"{subject}:STRICT-with-port-exceptions")
            ports_text = ", ".join(f"{p}={m}" for p, m in weak_ports)
            findings.append(_finding("MTLS-PORT-EXCEPTION", "major" if any(m == "DISABLE" for _, m in weak_ports) else "minor",
                                     f"{subject}: STRICT workload accepts plaintext on port(s) {ports_text}"))
            continue
        non_strict.append(f"{subject}:{mode}")
        if mode == "DISABLE":
            disabled += 1
        else:
            permissive += 1
        groups.setdefault((mode, source), []).append(subject)
    for (mode, source), subjects in sorted(groups.items()):
        shown = ", ".join(sorted(subjects)[:5]) + (" ..." if len(subjects) > 5 else "")
        findings.append(_finding("MTLS-DISABLED" if mode == "DISABLE" else "MTLS-PERMISSIVE", "major" if mode == "DISABLE" else "minor",
                                 f"{len(subjects)} workload(s) accept plaintext ({mode}) via {source}: {shown}"))
    if len(rows) > MAX_NAMESPACES:
        raise _Invalid("workloads", f"at most {MAX_NAMESPACES} namespaces are analysed per call, got {len(rows)}")
    total = len(workloads)
    if total == 0:
        raise _Invalid("workloads", "at least one workload is required")
    share = Decimal(strict) * 100 / Decimal(total)
    pct = round(float(share), 2)
    if _dec(target) > share:
        exact = share.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        findings.append(_finding("MTLS-COVERAGE-BELOW-TARGET", "major",
                                 f"strict mTLS covers {strict} of {total} workloads ({exact}%), target {target}%"))
    table: list[dict[str, object]] = []
    for row in sorted(rows.values(), key=lambda r: r["namespace"]):
        table.append({**row, "strict_pct": round(row["strict_count"] * 100 / row["workload_count"], 2)})
    return {
        "state": "SUCCESS",
        "workload_count": total,
        "strict_count": strict,
        "permissive_count": permissive,
        "disabled_count": disabled,
        "port_exception_count": port_exceptions,
        "strict_pct": pct,
        "namespaces": table,
        "non_strict_workloads": sorted(non_strict)[:MAX_LISTED],
        **_report(findings),
    }


def mtls_coverage(payload: dict[str, object]) -> dict[str, object]:
    """Effective PeerAuthentication mode per workload. Istio precedence: the oldest workload-selector policy that matches
    the pod labels, else the oldest namespace-wide policy, else the mesh-wide policy in the root namespace, else PERMISSIVE;
    mode UNSET at a level defers to the parent. portLevelMtls (selector policies only) can weaken a STRICT workload on single
    ports. A workload counts as strict only when its mode is STRICT and no port is weaker. Same-level policies that tie on
    creationTimestamp (or are undated) whose effective postures differ, an UNSET mode taking the level below, are MTLS-CONFLICT (major) and the workload is not
    counted strict. `non_strict_workloads` lists the first 100 names; at most 200 namespaces are analysed."""
    return _respond(payload, _mtls_coverage)


# --- mesh_traffic_split --------------------------------------------------------------------------------------------


class _Entry(TypedDict):
    host: str
    subset: str | None
    weight: int | None


class _DestRule(TypedDict):
    fqdn: str
    subsets: set[str]


def _destinations(route_list: list[object], field: str, where: str) -> list[_Entry]:
    out: list[_Entry] = []
    for item in route_list:
        item = _dict(item, field, f"{where} route item")
        destination = _dict(item.get("destination") or {}, field, f"{where} destination")
        host = _str(destination.get("host"), field, f"{where} destination.host")
        weight = item.get("weight")
        if weight is not None and (isinstance(weight, bool) or not isinstance(weight, int) or weight < 0):
            raise _Invalid(field, f"{where} weight {weight!r} must be a non-negative integer")
        subset = destination.get("subset")
        if subset is not None and not isinstance(subset, str):
            raise _Invalid(field, f"{where} destination.subset must be a string")
        out.append({"host": host, "subset": subset if isinstance(subset, str) else None,
                    "weight": weight if isinstance(weight, int) else None})
    return out


def _dr_index(destination_rules: list[object], field: str) -> list[_DestRule]:
    index: list[_DestRule] = []
    for i, doc in enumerate(destination_rules):
        namespace, name = _meta(doc, field, i, "DestinationRule")
        spec = _spec(doc, field, i)
        host = _str(spec.get("host"), field, f"{namespace}/{name} spec.host")
        subsets: set[str] = set()
        for subset in _list(spec.get("subsets"), field, f"{namespace}/{name} spec.subsets"):
            subsets.add(_str(_dict(subset, field, "subset").get("name"), field, f"{namespace}/{name} subset name"))
        index.append({"fqdn": _fqdn(host, namespace), "subsets": subsets})
    return index


def _traffic_split(payload: dict[str, object]) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    rules = _dr_index(_list(payload["destination_rules"], "destination_rules", "destination_rules"), "destination_rules")
    services = _list(payload["virtual_services"], "virtual_services", "virtual_services")
    route_count = destination_count = 0
    for index, doc in enumerate(services):
        namespace, name = _meta(doc, "virtual_services", index, "VirtualService")
        spec = _spec(doc, "virtual_services", index)
        subject = f"{namespace}/{name}"
        for protocol in ("http", "tcp", "tls"):
            for r_index, route in enumerate(_list(spec.get(protocol), "virtual_services", f"{subject} spec.{protocol}")):
                route = _dict(route, "virtual_services", f"{subject} {protocol}[{r_index}]")
                where = f"{subject}: {protocol}[{r_index}]" + (f" ({route['name']})" if isinstance(route.get("name"), str) else "")
                route_count += 1
                entries = _destinations(_list(route.get("route"), "virtual_services", f"{where} route"), "virtual_services", where)
                destination_count += len(entries)
                if not entries:
                    if not (route.get("redirect") or route.get("directResponse")):
                        findings.append(_finding("TRAFFIC-ROUTE-EMPTY", "major", f"{where} has no route, redirect or directResponse"))
                elif len(entries) == 1:
                    weight = entries[0]["weight"]
                    if weight not in (None, ROUTE_WEIGHT_TOTAL):
                        findings.append(_finding("TRAFFIC-SINGLE-WEIGHT", "minor", f"{where}: single destination has weight {weight}; "
                                                 "it carries all traffic regardless"))
                else:
                    total = sum(e["weight"] or 0 for e in entries)
                    if total == 0:
                        findings.append(_finding("TRAFFIC-WEIGHT-ZERO", "blocker", f"{where}: destination weights are all 0 or unset, no traffic is routed"))
                    elif total != ROUTE_WEIGHT_TOTAL:
                        shares = " / ".join(f"{e['host']}{'/' + str(e['subset']) if e['subset'] else ''} "
                                            f"{float(round(Decimal(e['weight'] or 0) * 100 / Decimal(total), 2)):g}%" for e in entries)
                        findings.append(_finding("TRAFFIC-WEIGHT-SUM", "minor", f"{where}: weights sum to {total}, not {ROUTE_WEIGHT_TOTAL}; Istio "
                                                 f"treats weights as relative shares, so the split is {shares}"))
                for entry in entries:
                    subset = entry["subset"]
                    if not subset:
                        continue
                    fqdn = _fqdn(entry["host"], namespace)
                    matching = [r for r in rules if r["fqdn"] == fqdn]
                    if not matching:
                        findings.append(_finding("TRAFFIC-SUBSET-NO-DESTINATIONRULE", "major",
                                                 f"{where}: subset {subset!r} of {fqdn} has no DestinationRule defining subsets"))
                    elif not any(subset in r["subsets"] for r in matching):
                        findings.append(_finding("TRAFFIC-SUBSET-MISSING", "major",
                                                 f"{where}: subset {subset!r} is not defined by any DestinationRule for {fqdn} (requests fail 503)"))
                if protocol != "http":
                    continue
                timeout: Decimal | None = None
                if "timeout" in route:
                    timeout = _duration_ms(route["timeout"], "virtual_services", f"{where} timeout")
                    problem = _istio_duration_problem(timeout)
                    if problem:  # Istio's validation rejects it, 0s included; no timeout means the field is absent
                        findings.append(_finding("TRAFFIC-DURATION-INVALID", "major", f"{where}: timeout {route['timeout']} is {problem}; Istio "
                                                 "rejects it (omit the field for no timeout)"))
                        timeout = None
                retries = _dict(route.get("retries") or {}, "virtual_services", f"{where} retries")
                if not retries:
                    continue
                attempts = _nonneg_int(retries.get("attempts", 0), "virtual_services", f"{where} retries.attempts")
                per_try: Decimal | None = None
                if "perTryTimeout" in retries:
                    per_try = _duration_ms(retries["perTryTimeout"], "virtual_services", f"{where} perTryTimeout")
                    problem = _istio_duration_problem(per_try)
                    if problem:
                        findings.append(_finding("TRAFFIC-DURATION-INVALID", "major", f"{where}: perTryTimeout {retries['perTryTimeout']} is {problem}; "
                                                 "Istio rejects it"))
                        per_try = None
                if attempts > MAX_RETRY_ATTEMPTS:
                    findings.append(_finding("TRAFFIC-RETRY-ATTEMPTS-HIGH", "minor", f"{where}: retries.attempts {attempts} exceeds {MAX_RETRY_ATTEMPTS}"))
                if attempts == 0:
                    if "perTryTimeout" in retries or retries.get("retryOn"):
                        findings.append(_finding("TRAFFIC-RETRY-DISABLED-CONFIG", "major", f"{where}: retries.attempts 0 disables retries but "
                                                 "perTryTimeout or retryOn is set; Istio rejects that"))
                    continue
                if timeout is None:
                    findings.append(_finding("TRAFFIC-RETRY-NO-TIMEOUT", "minor", f"{where}: retries without an overall route timeout"))
                elif per_try is not None and per_try > timeout:
                    findings.append(_finding("TRAFFIC-PERTRY-EXCEEDS-TIMEOUT", "major",
                                             f"{where}: perTryTimeout {retries['perTryTimeout']} exceeds route timeout {route['timeout']}"))
                elif per_try is not None and per_try * (attempts + 1) > timeout:
                    findings.append(_finding("TRAFFIC-RETRY-BUDGET", "minor", f"{where}: {attempts + 1} tries of {retries['perTryTimeout']} cannot "
                                             f"complete within the route timeout {route['timeout']}"))
    return {"state": "SUCCESS", "virtual_service_count": len(services), "route_count": route_count,
            "destination_count": destination_count, **_report(findings)}


def traffic_split(payload: dict[str, object]) -> dict[str, object]:
    """Validate Istio VirtualService routing. Weights are relative shares (Istio reference: a destination gets weight / sum of the
    weights): a route with several destinations whose weights are all 0 or unset routes nothing (Istio rejects it: blocker); a
    sum other than 100 is valid and only reported as minor with the effective shares. Every `subset` must be defined by a
    DestinationRule whose host resolves to the same FQDN (a host without a dot is relative to the rule's namespace); an unknown
    subset makes requests fail with 503. Istio's validation rejects a route timeout or perTryTimeout below 1ms or not a whole
    number of milliseconds (0s included: omit the field for no timeout) and perTryTimeout or retryOn next to attempts 0. Retries:
    perTryTimeout above the route timeout is dead, (attempts+1) x perTryTimeout above the timeout can never finish, no timeout
    means unbounded, and attempts above 5 is flagged (a house ceiling, Istio has none)."""
    return _respond(payload, _traffic_split)


# --- mesh_destination_policy ---------------------------------------------------------------------------------------


def _check_traffic_policy(policy: object, label: str, findings: list[dict[str, str]]) -> bool:
    """Check one trafficPolicy / portLevelSettings block; True when it carries outlierDetection."""
    policy = _dict(policy, "destination_rules", f"{label} trafficPolicy")
    outlier = policy.get("outlierDetection")
    if outlier is not None:
        outlier = _dict(outlier, "destination_rules", f"{label} outlierDetection")
        c5 = outlier.get("consecutive5xxErrors")
        cg = outlier.get("consecutiveGatewayErrors", 0)
        if c5 is not None:
            c5 = _nonneg_int(c5, "destination_rules", f"{label} consecutive5xxErrors")
        cg = _nonneg_int(cg, "destination_rules", f"{label} consecutiveGatewayErrors")
        split, local = outlier.get("splitExternalLocalOriginErrors"), outlier.get("consecutiveLocalOriginFailures")
        if split is not None and not isinstance(split, bool):
            raise _Invalid("destination_rules", f"{label}: splitExternalLocalOriginErrors must be a boolean")
        if local is not None:
            local = _nonneg_int(local, "destination_rules", f"{label} consecutiveLocalOriginFailures")
        local_active = split is True and local != 0  # consecutiveLocalOriginFailures defaults to 5 once split is on
        if c5 == 0 and cg == 0 and not local_active:
            findings.append(_finding("DR-OUTLIER-INERT", "major", f"{label}: consecutive5xxErrors 0 and no consecutiveGatewayErrors "
                                     "or local-origin failures disable ejection"))
        elif cg and c5 != 0 and cg >= (5 if c5 is None else c5):
            findings.append(_finding("DR-OUTLIER-GATEWAY-SHADOWED", "info", f"{label}: consecutiveGatewayErrors {cg} is not below "
                                     f"consecutive5xxErrors {5 if c5 is None else c5}; gateway errors count as 5xx, so it has no effect"))
        if local and split is not True:
            findings.append(_finding("DR-OUTLIER-LOCAL-ORIGIN", "major", f"{label}: consecutiveLocalOriginFailures is set but "
                                     "splitExternalLocalOriginErrors is not true; Istio rejects that"))
        for key in ("interval", "baseEjectionTime"):
            if key in outlier:
                problem = _istio_duration_problem(_duration_ms(outlier[key], "destination_rules", f"{label} {key}"))
                if problem:
                    findings.append(_finding("DR-OUTLIER-DURATION", "major", f"{label}: outlierDetection.{key} {outlier[key]} is {problem}"))
        maximum = outlier.get("maxEjectionPercent")
        if maximum is not None:
            maximum = _nonneg_int(maximum, "destination_rules", f"{label} maxEjectionPercent")
            if maximum > 100:
                raise _Invalid("destination_rules", f"{label}: maxEjectionPercent {maximum} is above 100")
            if maximum == 100:
                findings.append(_finding("DR-OUTLIER-EJECT-ALL", "major", f"{label}: maxEjectionPercent 100 lets every endpoint be ejected"))
            elif maximum == 0:
                findings.append(_finding("DR-OUTLIER-EJECT-NONE", "minor", f"{label}: maxEjectionPercent 0 never ejects an endpoint"))
        minimum = outlier.get("minHealthPercent")
        if minimum is not None and _nonneg_int(minimum, "destination_rules", f"{label} minHealthPercent") > 100:
            raise _Invalid("destination_rules", f"{label}: minHealthPercent {minimum} is above 100")
    pool = _dict(policy.get("connectionPool") or {}, "destination_rules", f"{label} connectionPool")
    tcp = _dict(pool.get("tcp") or {}, "destination_rules", f"{label} connectionPool.tcp")
    http = _dict(pool.get("http") or {}, "destination_rules", f"{label} connectionPool.http")
    for section, keys in ((tcp, ("maxConnections",)),
                          (http, ("http1MaxPendingRequests", "http2MaxRequests", "maxRequestsPerConnection", "maxRetries"))):
        for key in keys:
            if key in section:
                value = section[key]
                if isinstance(value, bool) or not isinstance(value, int):
                    raise _Invalid("destination_rules", f"{label}: connectionPool {key} must be an integer")
                if value < 0:
                    findings.append(_finding("DR-POOL-NEGATIVE", "major", f"{label}: connectionPool {key} {value} is negative"))
    if "connectTimeout" in tcp:
        problem = _istio_duration_problem(_duration_ms(tcp["connectTimeout"], "destination_rules", f"{label} connectTimeout"))
        if problem:
            findings.append(_finding("DR-POOL-CONNECT-TIMEOUT", "major", f"{label}: connectTimeout {tcp['connectTimeout']} is {problem}"))
    if http.get("maxRequestsPerConnection") == 1:
        findings.append(_finding("DR-POOL-NO-KEEPALIVE", "minor", f"{label}: maxRequestsPerConnection 1 disables HTTP keep-alive"))
    if http and "maxRetries" not in http:
        findings.append(_finding("DR-POOL-RETRIES-UNBOUNDED", "info", f"{label}: http pool without maxRetries leaves concurrent retries "
                                 "at Istio's 2^32-1 default"))
    tls = policy.get("tls")
    if tls is not None:
        mode = _dict(tls, "destination_rules", f"{label} tls").get("mode")
        if mode is not None and mode not in TLS_CLIENT_MODES:
            raise _Invalid("destination_rules", f"{label}: tls.mode {mode!r} must be one of {', '.join(TLS_CLIENT_MODES)}")
        if mode == "DISABLE":
            findings.append(_finding("DR-TLS-DISABLE", "minor", f"{label}: tls.mode DISABLE sends plaintext and fails against a STRICT "
                                     "PeerAuthentication"))
    return outlier is not None


def _destination_policy(payload: dict[str, object]) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    rules = _list(payload["destination_rules"], "destination_rules", "destination_rules")
    endpoints: dict[str, list[dict[str, object]]] = {}
    for index, item in enumerate(_list(payload["workloads"], "workloads", "workloads")):
        item = _dict(item, "workloads", f"workloads[{index}]")
        namespace = _str(item.get("namespace"), "workloads", f"workloads[{index}].namespace")
        service = _str(item.get("service"), "workloads", f"workloads[{index}].service")
        labels = _labels(item.get("labels"), "workloads", f"workloads[{index}].labels")
        endpoints.setdefault(f"{service.lower()}.{namespace}.svc.{DEFAULT_CLUSTER_DOMAIN}", []).append(labels)
    subset_count = 0
    hosts: dict[tuple[str, str], list[str]] = {}
    for index, doc in enumerate(rules):
        namespace, name = _meta(doc, "destination_rules", index, "DestinationRule")
        spec = _spec(doc, "destination_rules", index)
        subject = f"{namespace}/{name}"
        host = _str(spec.get("host"), "destination_rules", f"{subject} spec.host")
        fqdn = _fqdn(host, namespace)
        hosts.setdefault((namespace, fqdn), []).append(name)
        has_outlier = False
        if spec.get("trafficPolicy") is not None:
            has_outlier |= _check_traffic_policy(spec["trafficPolicy"], f"{subject}: trafficPolicy", findings)
            for p_index, port_setting in enumerate(_list(_dict(spec["trafficPolicy"], "destination_rules", "trafficPolicy")
                                                         .get("portLevelSettings"), "destination_rules", "portLevelSettings")):
                has_outlier |= _check_traffic_policy(port_setting, f"{subject}: portLevelSettings[{p_index}]", findings)
        names: set[str] = set()
        for s_index, subset in enumerate(_list(spec.get("subsets"), "destination_rules", f"{subject} subsets")):
            subset = _dict(subset, "destination_rules", f"{subject} subsets[{s_index}]")
            subset_name = _str(subset.get("name"), "destination_rules", f"{subject} subsets[{s_index}].name")
            subset_count += 1
            if subset_name in names:
                findings.append(_finding("DR-SUBSET-DUPLICATE", "blocker", f"{subject}: subset {subset_name!r} is defined twice"))
            names.add(subset_name)
            labels = _labels(subset.get("labels"), "destination_rules", f"{subject} subset {subset_name} labels")
            if not labels:
                findings.append(_finding("DR-SUBSET-NO-LABELS", "minor", f"{subject}: subset {subset_name!r} has no labels and selects every endpoint"))
            elif fqdn in endpoints and not any(all(e.get(k) == v for k, v in labels.items()) for e in endpoints[fqdn]):
                findings.append(_finding("DR-SUBSET-NO-ENDPOINTS", "major", f"{subject}: no endpoint of {fqdn} carries the labels of subset "
                                         f"{subset_name!r}"))
            if subset.get("trafficPolicy") is not None:
                has_outlier |= _check_traffic_policy(subset["trafficPolicy"], f"{subject}: subset {subset_name}", findings)
        if not has_outlier:
            findings.append(_finding("DR-NO-OUTLIER-DETECTION", "minor", f"{subject}: no outlierDetection, failing endpoints are never ejected"))
    for (namespace, fqdn), owners in sorted(hosts.items()):
        if len(owners) > 1:
            findings.append(_finding("DR-HOST-DUPLICATE", "minor", f"{namespace}: {len(owners)} DestinationRules for {fqdn} "
                                     f"({', '.join(sorted(owners))}); Istio merges them only when subsets do not conflict"))
    return {"state": "SUCCESS", "rule_count": len(rules), "subset_count": subset_count, **_report(findings)}


def destination_policy(payload: dict[str, object]) -> dict[str, object]:
    """Sanity-check Istio DestinationRules. Outlier detection: consecutive5xxErrors defaults to 5 and 0 disables it,
    consecutiveGatewayErrors defaults to 0 (disabled) and has no effect when not below consecutive5xxErrors, local-origin
    failures count only with splitExternalLocalOriginErrors true (default 5), interval and baseEjectionTime need a whole number
    of ms >= 1ms, maxEjectionPercent 100 can empty the pool (house judgement). Connection pool: negative limits, connectTimeout
    below 1ms or sub-ms, maxRequestsPerConnection 1 (no keep-alive), maxRetries left at its 2^32-1 default. Subsets: duplicate
    names, empty labels, labels matching no supplied endpoint of the host (`workloads` are the pods behind each service).
    tls.mode DISABLE conflicts with STRICT PeerAuthentication."""
    return _respond(payload, _destination_policy)


# --- mesh_gateway_api_check ----------------------------------------------------------------------------------------


_HOSTNAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def _valid_hostname(host: str) -> bool:
    """Gateway API Hostname: lowercase RFC 1123 labels, optionally one leading "*." wildcard label."""
    name = host[2:] if host.startswith("*.") else host
    if not name or len(host) > 253:
        return False
    for label in name.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            return False
        if not all(c in _HOSTNAME_CHARS for c in label):
            return False
    return True


def _hosts_overlap(a: str, b: str) -> bool:
    """Whether two Gateway API hostnames can match a common name; `*.example.com` covers any deeper name, not `example.com`."""
    if a == b:
        return True
    if a.startswith("*.") and b.endswith(a[1:]):
        return True
    return b.startswith("*.") and a.endswith(b[1:])


def _api_group(value: object, default: str) -> object:
    return default if value is None else value


def _text(value: object) -> str:
    """A hashable stand-in for a manifest value: strings stay, anything else becomes its repr and so never matches a real name."""
    return value if isinstance(value, str) else repr(value)


_Listener = TypedDict("_Listener", {"name": str, "port": int, "protocol": object, "hostname": str, "from": object,
                                    "selector": object, "kinds": object})
_Grant = TypedDict("_Grant", {"namespace": str, "name": str, "from": list[tuple[object, str, str]],
                              "to": list[tuple[object, str, object]]})


class _Gateway(TypedDict):
    namespace: str
    listeners: list[_Listener]


def _grant_allows(index: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int], used: set[int], *, from_kind: object,
                  from_ns: object, to_group: object, to_kind: object, to_ns: object, to_name: object) -> bool:
    """Whether a ReferenceGrant in `to_ns` allows the sender to reference the target (an empty grant `to.name` allows every
    name). `index` maps (grant namespace, from tuple, to tuple) to the position of the first grant that holds the pair."""
    sender = (GATEWAY_GROUP, _text(from_kind), _text(from_ns))
    positions = [position for name in {_text(to_name), ""}
                 if (position := index.get((_text(to_ns), sender, (_text(to_group), _text(to_kind), name)))) is not None]
    if not positions:
        return False
    used.add(min(positions))
    return True


def _gateway_api_check(payload: dict[str, object]) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    grants: list[_Grant] = []
    for i, doc in enumerate(_list(payload["reference_grants"], "reference_grants", "reference_grants")):
        namespace, name = _meta(doc, "reference_grants", i, "ReferenceGrant")
        spec = _spec(doc, "reference_grants", i)
        senders: list[tuple[object, str, str]] = []
        for f in _list(spec.get("from"), "reference_grants", "from"):
            source_ref = _dict(f, "reference_grants", "from")
            senders.append((_api_group(source_ref.get("group"), ""), _str(source_ref.get("kind"), "reference_grants", "from.kind"),
                            _str(source_ref.get("namespace"), "reference_grants", "from.namespace")))
        receivers: list[tuple[object, str, object]] = []
        for t in _list(spec.get("to"), "reference_grants", "to"):
            target_ref = _dict(t, "reference_grants", "to")
            receivers.append((_api_group(target_ref.get("group"), ""), _str(target_ref.get("kind"), "reference_grants", "to.kind"),
                              target_ref.get("name") or ""))
        if not senders or not receivers:
            findings.append(_finding("GRANT-EMPTY", "major", f"{namespace}/{name}: ReferenceGrant needs at least one from and one to"))
        grants.append({"namespace": namespace, "name": name, "from": senders, "to": receivers})
    grant_index: dict[tuple[str, tuple[str, ...], tuple[str, ...]], int] = {}
    for position, grant in enumerate(grants):
        for sender in grant["from"]:
            for receiver in grant["to"]:
                grant_index.setdefault((_text(grant["namespace"]), tuple(map(_text, sender)), tuple(map(_text, receiver))), position)
    used: set[int] = set()
    namespace_labels: dict[str, dict[str, object]] = {}
    for i, item in enumerate(_list(payload.get("namespaces"), "namespaces", "namespaces")):
        item = _dict(item, "namespaces", f"namespaces[{i}]")
        namespace_labels[_str(item.get("name"), "namespaces", f"namespaces[{i}].name")] = _labels(item.get("labels"), "namespaces", "labels")
    gateways: dict[tuple[str, str], _Gateway] = {}
    gateway_docs = _list(payload["gateways"], "gateways", "gateways")
    for i, doc in enumerate(gateway_docs):
        namespace, name = _meta(doc, "gateways", i, "Gateway")
        spec = _spec(doc, "gateways", i)
        subject = f"{namespace}/{name}"
        if not spec.get("gatewayClassName"):
            findings.append(_finding("GW-NO-CLASS", "major", f"{subject}: spec.gatewayClassName is required"))
        listeners = _list(spec.get("listeners"), "gateways", f"{subject} listeners")
        if not listeners:
            findings.append(_finding("GW-NO-LISTENERS", "major", f"{subject}: a Gateway needs at least one listener"))
        parsed: list[_Listener] = []
        names: set[str] = set()
        for l_index, listener in enumerate(listeners):
            listener = _dict(listener, "gateways", f"{subject} listeners[{l_index}]")
            lname = _str(listener.get("name"), "gateways", f"{subject} listeners[{l_index}].name")
            port, protocol = listener.get("port"), listener.get("protocol")
            where = f"{subject}: listener {lname}"
            if lname in names:
                findings.append(_finding("GW-LISTENER-DUPLICATE", "major", f"{where} name is used twice"))
            names.add(lname)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise _Invalid("gateways", f"{where}: port must be an integer in 1..65535")
            if protocol is not None and not isinstance(protocol, str):
                raise _Invalid("gateways", f"{where}: protocol must be a string")
            if not isinstance(protocol, str) or not (protocol in LISTENER_PROTOCOLS or "/" in protocol):
                findings.append(_finding("GW-LISTENER-PROTOCOL", "major", f"{where}: protocol {protocol!r} is not one of "
                                         f"{', '.join(LISTENER_PROTOCOLS)} or a domain-prefixed custom protocol"))
            hostname_value = listener.get("hostname")
            if hostname_value is not None and not isinstance(hostname_value, str):
                raise _Invalid("gateways", f"{where}: hostname must be a string")
            hostname = hostname_value if isinstance(hostname_value, str) else ""
            if hostname and not _valid_hostname(hostname):
                findings.append(_finding("GW-LISTENER-HOSTNAME", "major", f"{where}: hostname {hostname!r} is not a lowercase RFC 1123 name"))
            if hostname and protocol in ("TCP", "UDP"):
                findings.append(_finding("GW-LISTENER-HOSTNAME", "major", f"{where}: hostname is not valid on a {protocol} listener"))
            tls = _dict(listener.get("tls") or {}, "gateways", f"{where} tls")
            mode = tls.get("mode", "Terminate")
            refs = _list(tls.get("certificateRefs"), "gateways", f"{where} certificateRefs")
            if tls and protocol in ("HTTP", "TCP", "UDP"):
                findings.append(_finding("GW-LISTENER-TLS-UNUSED", "major", f"{where}: tls must not be set on a {protocol} listener; the API rejects it"))
            if protocol == "HTTPS" and (not tls or mode != "Terminate" or not refs):
                findings.append(_finding("GW-LISTENER-TLS-MISSING", "major", f"{where}: an HTTPS listener must terminate TLS with certificateRefs"))
            if protocol == "TLS" and mode == "Terminate" and not refs:
                findings.append(_finding("GW-LISTENER-TLS-MISSING", "major", f"{where}: a TLS listener in Terminate mode needs certificateRefs"))
            for ref in refs:
                ref = _dict(ref, "gateways", f"{where} certificateRef")
                ref_ns = ref.get("namespace") or namespace
                ref_name = _str(ref.get("name"), "gateways", f"{where} certificateRef.name")
                if ref_ns != namespace and not _grant_allows(grant_index, used, from_kind="Gateway", from_ns=namespace, to_group="",
                                                              to_kind=ref.get("kind") or "Secret", to_ns=ref_ns, to_name=ref_name):
                    findings.append(_finding("GW-CERT-NO-GRANT", "major", f"{where}: certificate {ref_ns}/{ref_name} needs a ReferenceGrant "
                                             f"in {ref_ns} allowing Gateways from {namespace}"))
            allowed = _dict(listener.get("allowedRoutes") or {}, "gateways", f"{where} allowedRoutes")
            scope = _dict(allowed.get("namespaces") or {}, "gateways", f"{where} allowedRoutes.namespaces")
            origin = scope.get("from", "Same")
            if origin not in ROUTE_NAMESPACE_SCOPES:
                findings.append(_finding("GW-ROUTES-SCOPE", "major", f"{where}: allowedRoutes.namespaces.from {origin!r} must be one of "
                                         f"{', '.join(ROUTE_NAMESPACE_SCOPES)}"))
            elif origin == "Selector" and not scope.get("selector"):
                findings.append(_finding("GW-ROUTES-SCOPE", "major", f"{where}: from Selector needs a selector"))
            elif origin == "All":
                findings.append(_finding("GW-ROUTES-ALL-NAMESPACES", "info", f"{where}: routes from every namespace may attach"))
            parsed.append({"name": lname, "port": port, "protocol": protocol, "hostname": hostname, "from": origin,
                           "selector": scope.get("selector"), "kinds": allowed.get("kinds")})
        by_port: dict[int, list[_Listener]] = {}
        for entry in parsed:
            by_port.setdefault(entry["port"], []).append(entry)
        for port, group in sorted(by_port.items()):
            if len(group) < 2:
                continue
            protocols = {e["protocol"] for e in group}
            l4 = protocols <= {"TCP", "UDP"}
            if not (protocols <= {"HTTP"} or protocols <= {"HTTPS", "TLS"} or l4):
                findings.append(_finding("GW-LISTENER-PORT-CONFLICT", "major", f"{subject}: listeners {', '.join(sorted(e['name'] for e in group))} "
                                         f"share port {port} with incompatible protocols {', '.join(sorted(map(str, protocols)))}"))
                continue
            seen_keys: dict[tuple[object, str], str] = {}
            for entry in group:
                key = (entry["protocol"] if l4 else None, entry["hostname"])
                if key in seen_keys:
                    findings.append(_finding("GW-LISTENER-PORT-CONFLICT", "major", f"{subject}: listeners {seen_keys[key]} and {entry['name']} "
                                             f"conflict on port {port} (same protocol class and hostname {entry['hostname'] or '<any>'!r})"))
                seen_keys.setdefault(key, entry["name"])
        gateways[(namespace, name)] = {"namespace": namespace, "listeners": parsed}
    unattached: list[str] = []
    routes = _list(payload["routes"], "routes", "routes")
    for i, doc in enumerate(routes):
        namespace, name = _meta(doc, "routes", i, "HTTPRoute")
        spec = _spec(doc, "routes", i)
        subject = f"{namespace}/{name}"
        route_hosts = [_str(h, "routes", f"{subject} hostnames") for h in _list(spec.get("hostnames"), "routes", f"{subject} hostnames")]
        parents = _list(spec.get("parentRefs"), "routes", f"{subject} parentRefs")
        attached = bool(parents)
        if not parents:
            findings.append(_finding("ROUTE-NO-PARENT", "major", f"{subject}: no parentRefs, the route attaches to nothing"))
        for ref in parents:
            ref = _dict(ref, "routes", f"{subject} parentRef")
            if _api_group(ref.get("group"), GATEWAY_GROUP) != GATEWAY_GROUP or (ref.get("kind") or "Gateway") != "Gateway":
                continue  # Service parents (mesh / GAMMA) are outside this check
            parent_ns = _str(ref.get("namespace") or namespace, "routes", f"{subject} parentRef.namespace")
            parent_name = _str(ref.get("name"), "routes", f"{subject} parentRef.name")
            gateway = gateways.get((parent_ns, parent_name))
            if gateway is None:
                findings.append(_finding("ROUTE-PARENT-MISSING", "major", f"{subject}: parent Gateway {parent_ns}/{parent_name} is not in the supplied gateways"))
                attached = False
                continue
            candidates = gateway["listeners"]
            if ref.get("sectionName"):
                candidates = [c for c in candidates if c["name"] == ref["sectionName"]]
                if not candidates:
                    findings.append(_finding("ROUTE-SECTION-MISSING", "major", f"{subject}: Gateway {parent_ns}/{parent_name} has no listener "
                                             f"{ref['sectionName']!r}"))
                    attached = False
                    continue
            if ref.get("port") is not None:
                candidates = [c for c in candidates if c["port"] == ref["port"]]
            reasons: list[str] = []
            accepted = False
            for listener in candidates:
                reason = None
                kinds = listener["kinds"]
                if listener["protocol"] not in HTTPROUTE_PROTOCOLS:
                    reason = f"protocol {listener['protocol']} cannot carry an HTTPRoute"
                elif kinds is not None and not (isinstance(kinds, list) and any(isinstance(k, dict) and k.get("kind") == "HTTPRoute" for k in kinds)):
                    reason = "allowedRoutes.kinds excludes HTTPRoute"
                elif listener["from"] == "None":
                    reason = "allowedRoutes.namespaces.from is None, so no route may attach"
                elif listener["from"] == "Same" and namespace != parent_ns:
                    reason = f"allowedRoutes allows only namespace {parent_ns}"
                elif listener["from"] == "Selector":
                    labels = {NAMESPACE_NAME_LABEL: namespace, **namespace_labels.get(namespace, {})}
                    if not _selector_matches(listener["selector"], labels, "gateways"):
                        reason = f"namespace {namespace} does not match the allowedRoutes selector"
                if reason is None and route_hosts and listener["hostname"] and not any(_hosts_overlap(h, listener["hostname"]) for h in route_hosts):
                    reason = f"hostnames do not intersect {listener['hostname']}"
                if reason is None:
                    accepted = True
                    break
                reasons.append(f"listener {listener['name']}: {reason}")
            if not accepted:
                attached = False
                findings.append(_finding("ROUTE-NOT-ACCEPTED", "major", f"{subject}: Gateway {parent_ns}/{parent_name} accepts the route on no "
                                         f"listener ({'; '.join(reasons) or 'no listener matches parentRef'})"))
        if not attached:
            unattached.append(subject)
        for r_index, rule in enumerate(_list(spec.get("rules"), "routes", f"{subject} rules")):
            rule = _dict(rule, "routes", f"{subject} rules[{r_index}]")
            backends = _list(rule.get("backendRefs"), "routes", f"{subject} rules[{r_index}].backendRefs")
            redirect = any(isinstance(f, dict) and f.get("type") == "RequestRedirect" for f in _list(rule.get("filters"), "routes", "filters"))
            if not backends and not redirect:
                findings.append(_finding("ROUTE-RULE-NO-BACKEND", "minor", f"{subject}: rules[{r_index}] has no backendRefs and no RequestRedirect "
                                         "(requests get a 500)"))
            total_weight = 0
            for ref in backends:
                ref = _dict(ref, "routes", f"{subject} backendRef")
                kind, backend_group = ref.get("kind") or "Service", _api_group(ref.get("group"), "")
                backend_ns, backend_name = ref.get("namespace") or namespace, _str(ref.get("name"), "routes", f"{subject} backendRef.name")
                weight = ref.get("weight", 1)
                if isinstance(weight, bool) or not isinstance(weight, int) or weight < 0:
                    raise _Invalid("routes", f"{subject}: backendRef weight {weight!r} must be a non-negative integer")
                total_weight += weight
                if kind == "Service" and backend_group == "" and ref.get("port") is None:
                    findings.append(_finding("ROUTE-BACKEND-NO-PORT", "major", f"{subject}: Service backendRef {backend_name} needs a port"))
                if backend_ns != namespace and not _grant_allows(grant_index, used, from_kind="HTTPRoute", from_ns=namespace, to_group=backend_group,
                                                                  to_kind=kind, to_ns=backend_ns, to_name=backend_name):
                    findings.append(_finding("ROUTE-BACKEND-NO-GRANT", "major", f"{subject}: backend {backend_ns}/{backend_name} needs a "
                                             f"ReferenceGrant in {backend_ns} allowing HTTPRoutes from {namespace}"))
            if backends and total_weight == 0:
                findings.append(_finding("ROUTE-WEIGHT-ZERO", "major", f"{subject}: rules[{r_index}] backendRef weights are all 0, no traffic is sent"))
    for position, grant in enumerate(grants):
        if position not in used:
            findings.append(_finding("GRANT-UNUSED", "info", f"{grant['namespace']}/{grant['name']}: ReferenceGrant is not needed by any "
                                     "supplied Gateway or HTTPRoute"))
    return {"state": "SUCCESS", "gateway_count": len(gateway_docs), "route_count": len(routes), "grant_count": len(grants),
            "unattached_routes": sorted(unattached)[:MAX_LISTED], **_report(findings)}


def gateway_api_check(payload: dict[str, object]) -> dict[str, object]:
    """Validate Gateway API (gateway.networking.k8s.io) Gateways, HTTPRoutes and ReferenceGrants together. Listeners: unique
    names, valid ports/protocols/hostnames, tls required for HTTPS/TLS and rejected on HTTP/TCP/UDP, HTTPS terminating with
    certificateRefs, allowedRoutes scope (All, Selector, Same, None). Same-port listeners: the spec only requires them to be
    distinct (same protocol: different port or hostname; TCP never shares a port with HTTP/HTTPS/TLS); this tool is stricter and
    also flags HTTP with HTTPS on one port and HTTPS/TLS listeners with the same hostname, the cases implementations report as
    Conflicted. Routes: an HTTPRoute attaches to HTTP and HTTPS listeners only (a TLS listener takes TLSRoute); parentRef
    resolves to a supplied Gateway and sectionName, a listener accepts it (protocol, kinds, namespace scope, hostname
    intersection), Service backendRefs have a port, zero total weight drops traffic. A certificateRef or backendRef in another
    namespace needs a ReferenceGrant in the target namespace naming the sender kind and namespace and the target group/kind
    (and name when given). `unattached_routes` lists at most 100 names."""
    return _respond(payload, _gateway_api_check)


# --- NetworkPolicy shared helpers ----------------------------------------------------------------------------------


class _NetPol(TypedDict):
    namespace: str
    name: str
    selector: dict[str, object]
    types: list[object]
    rules: dict[str, list[dict[str, object]]]


class _Pod(TypedDict):
    namespace: str
    name: str
    labels: dict[str, object]
    ip: object
    ports: list[object]


def _parse_netpols(items: list[object], field: str) -> list[_NetPol]:
    policies: list[_NetPol] = []
    for index, doc in enumerate(items):
        namespace, name = _meta(doc, field, index, "NetworkPolicy")
        spec = _spec(doc, field, index)
        subject = f"{namespace}/{name}"
        if "podSelector" not in spec:
            raise _Invalid(field, f"{subject}: spec.podSelector is required (use {{}} to select every pod)")
        selector = _dict(spec["podSelector"], field, f"{subject} spec.podSelector")
        declared_types = spec.get("policyTypes")
        types: list[object]
        if declared_types is None:  # Kubernetes default (SetDefaults_NetworkPolicy): Ingress always, Egress only when the egress list is non-empty
            types = ["Ingress", *(["Egress"] if spec.get("egress") else [])]
        else:
            types = _list(declared_types, field, f"{subject} policyTypes")
            if not types or any(t not in POLICY_TYPES for t in types):
                raise _Invalid(field, f"{subject}: policyTypes must be a non-empty list of {', '.join(POLICY_TYPES)}")
        rules: dict[str, list[dict[str, object]]] = {}
        for direction, key in (("Ingress", "ingress"), ("Egress", "egress")):
            peers_key = _PEER_KEY[direction]
            rules[direction] = []
            for r in _list(spec.get(key), field, f"{subject} {key}"):
                rule = _dict(r, field, f"{subject} {key} rule")
                for peer in _list(rule.get(peers_key), field, f"{subject} {key} {peers_key}"):
                    _dict(peer, field, f"{subject} {key} {peers_key} peer")
                for entry in _list(rule.get("ports"), field, f"{subject} {key} ports"):
                    _dict(entry, field, f"{subject} {key} port")
                rules[direction].append(rule)
        policies.append({"namespace": namespace, "name": name, "selector": selector, "types": types, "rules": rules})
    return policies


def _parse_pods(items: list[object], field: str) -> list[_Pod]:
    pods: list[_Pod] = []
    for index, item in enumerate(items):
        item = _dict(item, field, f"{field}[{index}]")
        pods.append({
            "namespace": _str(item.get("namespace"), field, f"{field}[{index}].namespace"),
            "name": _str(item.get("name"), field, f"{field}[{index}].name"),
            "labels": _labels(item.get("labels"), field, f"{field}[{index}].labels"),
            "ip": item.get("ip") or "",
            "ports": _list(item.get("ports"), field, f"{field}[{index}].ports"),
        })
    return pods


_PEER_KEY = {"Ingress": "from", "Egress": "to"}


def _all_ports(rule: dict[str, object]) -> bool:
    """Whether a rule's ports admit every port: no ports at all, or an entry without `port` (every port of its protocol)."""
    entries = _list(rule.get("ports"), "network_policies", "ports")
    return not entries or any(_dict(entry, "network_policies", "NetworkPolicy port").get("port") is None for entry in entries)


def _rule_open(rule: dict[str, object], direction: str) -> bool:
    """A rule without peers that admits every port matches all traffic of its direction (empty from/to mean 'any')."""
    return not rule.get(_PEER_KEY[direction]) and _all_ports(rule)


def _rule_world_open(rule: dict[str, object], direction: str) -> bool:
    """A rule that admits every port whose peers include an ipBlock that spans the whole address space (a /0 without `except`)."""
    if not _all_ports(rule):
        return False
    for peer in _list(rule.get(_PEER_KEY[direction]), "network_policies", "peers"):
        block = peer.get("ipBlock") if isinstance(peer, dict) else None
        if not isinstance(block, dict) or block.get("except"):
            continue
        try:
            if _ip_network(block.get("cidr"), strict=False).prefixlen == 0:
                return True
        except (ValueError, TypeError):
            continue
    return False


def _default_deny(policy: _NetPol, direction: str) -> bool:
    return direction in policy["types"] and _selector_empty(policy["selector"]) and not policy["rules"][direction]


# --- mesh_netpol_coverage ------------------------------------------------------------------------------------------


def _netpol_coverage(payload: dict[str, object]) -> dict[str, object]:
    policies = _parse_netpols(_list(payload["network_policies"], "network_policies", "network_policies"), "network_policies")
    pods = _parse_pods(_list(payload["pods"], "pods", "pods"), "pods")
    require_egress = bool(payload.get("require_egress", False))
    namespaces = {p["namespace"] for p in pods} | {p["namespace"] for p in policies}
    namespaces |= {_str(n, "namespaces", "namespace") for n in _list(payload.get("namespaces"), "namespaces", "namespaces")}
    if len(namespaces) > MAX_NAMESPACES:
        raise _Invalid("network_policies", f"at most {MAX_NAMESPACES} namespaces are analysed per call, got {len(namespaces)}")
    findings: list[dict[str, str]] = []
    rows: list[dict[str, object]] = []
    uncovered: list[str] = []
    for namespace in sorted(namespaces):
        local = [p for p in policies if p["namespace"] == namespace]
        members = [p for p in pods if p["namespace"] == namespace]
        row: dict[str, object] = {"namespace": namespace, "pod_count": len(members)}
        for direction in POLICY_TYPES:
            lower = direction.lower()
            open_pods = 0
            for pod in members:
                selecting = [p for p in local if direction in p["types"] and _selector_matches(p["selector"], pod["labels"], "network_policies")]
                if not selecting or any(_rule_open(r, direction) for p in selecting for r in p["rules"][direction]):
                    open_pods += 1
            denied = any(_default_deny(p, direction) for p in local)
            row[f"default_deny_{lower}"] = denied
            row[f"{lower}_open_pods"] = open_pods
            if not denied:
                if direction == "Ingress":
                    uncovered.append(namespace)
                if members and open_pods == 0:
                    findings.append(_finding(f"NETPOL-NO-DEFAULT-DENY-{direction.upper()}", "info",
                                             f"{namespace}: no default-deny {lower} policy, but all {len(members)} pod(s) are isolated by specific "
                                             "policies; new pods start open"))
                else:
                    severity = "major" if direction == "Ingress" or require_egress else "minor"
                    findings.append(_finding(f"NETPOL-NO-DEFAULT-DENY-{direction.upper()}", severity,
                                             f"{namespace}: no default-deny {lower} policy (empty podSelector, {direction} type, no rules); "
                                             f"{open_pods} of {len(members)} known pod(s) are open"))
            elif open_pods:
                findings.append(_finding(f"NETPOL-DEFAULT-DENY-OVERRIDDEN-{direction.upper()}", "major",
                                         f"{namespace}: default-deny {lower} exists but {open_pods} pod(s) remain open through an allow-all rule "
                                         "in another policy (policies are additive)"))
            for policy in local:
                if direction in policy["types"] and any(_rule_open(r, direction) for r in policy["rules"][direction]):
                    findings.append(_finding(f"NETPOL-ALLOW-ALL-{direction.upper()}", "minor",
                                             f"{namespace}/{policy['name']}: a {lower} rule without peers and ports allows all {lower} traffic"))
                if direction in policy["types"] and any(_rule_world_open(r, direction) for r in policy["rules"][direction]):
                    findings.append(_finding(f"NETPOL-ALLOW-ANY-CIDR-{direction.upper()}", "minor",
                                             f"{namespace}/{policy['name']}: a {lower} rule admits every address (ipBlock /0) on all ports; "
                                             "pod IPs are included or not depending on the CNI"))
        rows.append(row)
    return {"state": "SUCCESS", "namespace_count": len(rows), "pod_count": len(pods), "namespaces": rows,
            "uncovered_namespaces": uncovered, **_report(findings)}


def netpol_coverage(payload: dict[str, object]) -> dict[str, object]:
    """NetworkPolicy default-deny coverage per namespace. Kubernetes semantics: a pod is isolated for a direction once any
    policy selects it with that policyType (default policyTypes: Ingress always, Egress only when the egress list is non-empty);
    policies are purely additive allow-lists, so a rule with no peers that admits every port (no ports, or an entry without
    `port`, which is every port of its protocol) re-opens the pod. A default-deny policy has an empty podSelector, lists the
    direction and has no rules for it. A rule admitting every port whose peers include an ipBlock /0 without `except` is
    reported as NETPOL-ALLOW-ANY-CIDR-* (minor; it re-opens the pod to every address, CNI-dependent for pod IPs). Missing
    ingress default-deny is major (minor/info for egress and when every pod is already isolated individually); `require_egress`
    raises egress to major. A port entry without `port` counts as open whatever its protocol, so an allow-all UDP entry is
    treated as open too (conservative: the reachability tool checks one protocol at a time)."""
    return _respond(payload, _netpol_coverage)


# --- mesh_netpol_reachability --------------------------------------------------------------------------------------


def _peer_matches(peer: object, policy_ns: str, other: _Pod, ns_labels: dict[str, dict[str, object]], notes: list[str]) -> bool:
    peer = _dict(peer, "network_policies", "NetworkPolicy peer")
    block, pod_selector, ns_selector = peer.get("ipBlock"), peer.get("podSelector"), peer.get("namespaceSelector")
    if block is not None:
        if pod_selector is not None or ns_selector is not None:
            raise _Invalid("network_policies", "a NetworkPolicy peer cannot combine ipBlock with a selector")
        block = _dict(block, "network_policies", "ipBlock")
        try:
            network = _ip_network(block.get("cidr"), strict=False)
            excluded = [_ip_network(x, strict=False) for x in _list(block.get("except"), "network_policies", "ipBlock.except")]
        except (ValueError, TypeError):
            raise _Invalid("network_policies", f"ipBlock {block!r} has an invalid cidr or except entry") from None
        if not other["ip"]:
            notes.append(f"ipBlock peer not evaluated: pod {other['namespace']}/{other['name']} has no ip")
            return False
        try:
            address = _ip_address(other["ip"])
        except ValueError:
            raise _Invalid("pods", f"pod {other['namespace']}/{other['name']} ip {other['ip']!r} is not an IP address") from None
        notes.append("ipBlock evaluated against a pod ip: Kubernetes documents ipBlock for cluster-external addresses and CNI plugins differ")
        inside = address.version == network.version and address in network
        return inside and not any(address.version == e.version and address in e for e in excluded)
    if pod_selector is None and ns_selector is None:
        raise _Invalid("network_policies", "a NetworkPolicy peer needs podSelector, namespaceSelector or ipBlock")
    if ns_selector is not None:
        if not _selector_matches(ns_selector, ns_labels.get(other["namespace"], {NAMESPACE_NAME_LABEL: other["namespace"]}), "network_policies"):
            return False
    elif other["namespace"] != policy_ns:
        return False  # a bare podSelector selects pods in the policy's own namespace only
    return pod_selector is None or _selector_matches(pod_selector, other["labels"], "network_policies")


def _ports_match(ports: list[object], port: int, protocol: str, destination: _Pod) -> bool:
    if not ports:
        return True
    for entry in ports:
        entry = _dict(entry, "network_policies", "NetworkPolicy port")
        entry_protocol = entry.get("protocol", "TCP")
        if entry_protocol not in NETWORK_PROTOCOLS:
            raise _Invalid("network_policies", f"port protocol {entry_protocol!r} must be one of {', '.join(NETWORK_PROTOCOLS)}")
        if entry_protocol != protocol:
            continue
        wanted = entry.get("port")
        if wanted is None:
            return True
        if isinstance(wanted, str):  # a named port resolves against the destination pod's container ports
            for declared in destination["ports"]:
                declared = _dict(declared, "pods", "pod port")
                if (declared.get("name") == wanted and declared.get("containerPort") == port
                        and declared.get("protocol", "TCP") == protocol):
                    return True
            continue
        if isinstance(wanted, bool) or not isinstance(wanted, int):
            raise _Invalid("network_policies", f"port {wanted!r} must be a number or a port name")
        end = entry.get("endPort")
        if end is None:
            if port == wanted:
                return True
            continue
        if isinstance(end, bool) or not isinstance(end, int) or end < wanted:
            raise _Invalid("network_policies", f"endPort {end!r} must be an integer >= port {wanted}")
        if wanted <= port <= end:
            return True
    return False


def _side(direction: str, policies: list[_NetPol], subject: _Pod, other: _Pod, destination: _Pod,
          ns_labels: dict[str, dict[str, object]], port: int, protocol: str, notes: list[str]) -> tuple[bool, bool, list[str]]:
    """(isolated, allowed, names of allowing policies) for one pod and direction."""
    selecting = [p for p in policies if p["namespace"] == subject["namespace"] and direction in p["types"]
                 and _selector_matches(p["selector"], subject["labels"], "network_policies")]
    if not selecting:
        return False, True, []
    allowing: list[str] = []
    for policy in selecting:
        for rule in policy["rules"][direction]:
            peers = _list(rule.get(_PEER_KEY[direction]), "network_policies", "peers")
            if (not peers or any(_peer_matches(p, policy["namespace"], other, ns_labels, notes) for p in peers)) \
                    and _ports_match(_list(rule.get("ports"), "network_policies", "ports"), port, protocol, destination):
                allowing.append(f"{policy['namespace']}/{policy['name']}")
                break
    return True, bool(allowing), sorted(allowing)


def _netpol_reachability(payload: dict[str, object]) -> dict[str, object]:
    policies = _parse_netpols(_list(payload["network_policies"], "network_policies", "network_policies"), "network_policies")
    pods = _parse_pods(_list(payload["pods"], "pods", "pods"), "pods")
    ns_labels: dict[str, dict[str, object]] = {}
    for index, item in enumerate(_list(payload.get("namespaces"), "namespaces", "namespaces")):
        item = _dict(item, "namespaces", f"namespaces[{index}]")
        name = _str(item.get("name"), "namespaces", f"namespaces[{index}].name")
        ns_labels[name] = {NAMESPACE_NAME_LABEL: name, **_labels(item.get("labels"), "namespaces", "labels")}
    port = cast(int, payload["port"])  # schema: integer 1..65535 (input_schema); _ports_match compares it with pod ports
    protocol = payload.get("protocol", "TCP")
    if not isinstance(protocol, str) or protocol not in NETWORK_PROTOCOLS:
        raise _Invalid("protocol", f"protocol {protocol!r} must be one of {', '.join(NETWORK_PROTOCOLS)}")
    found: dict[str, _Pod] = {}
    for field in ("source", "destination"):
        ref = _dict(payload[field], field, f"{field} pod reference")
        namespace = _str(ref.get("namespace"), field, f"{field}.namespace")
        name = _str(ref.get("name"), field, f"{field}.name")
        match = [p for p in pods if (p["namespace"], p["name"]) == (namespace, name)]
        if not match:
            raise _Invalid(field, f"pod {namespace}/{name} is not in pods")
        found[field] = match[0]
    source, destination = found["source"], found["destination"]
    if source is destination:
        raise _Invalid("destination", "source and destination are the same pod")
    notes: list[str] = []
    e_iso, e_ok, e_by = _side("Egress", policies, source, destination, destination, ns_labels, port, protocol, notes)
    i_iso, i_ok, i_by = _side("Ingress", policies, destination, source, destination, ns_labels, port, protocol, notes)
    reachable = e_ok and i_ok
    if reachable:
        reason = "allowed: " + ("no policy isolates either pod" if not (e_iso or i_iso) else "every isolating policy has a matching rule")
    else:
        parts = []
        if not e_ok:
            parts.append(f"egress of {source['namespace']}/{source['name']} has no rule for {destination['namespace']}/{destination['name']} "
                         f"on {protocol}/{port}")
        if not i_ok:
            parts.append(f"ingress of {destination['namespace']}/{destination['name']} has no rule for {source['namespace']}/{source['name']} "
                         f"on {protocol}/{port}")
        reason = "blocked: " + "; ".join(parts)
    return {
        "state": "SUCCESS",
        "reachable": reachable,
        "verdict": "reachable" if reachable else "blocked",
        "egress_isolated": e_iso,
        "egress_allowed": e_ok,
        "ingress_isolated": i_iso,
        "ingress_allowed": i_ok,
        "egress_policies": e_by[:50],
        "ingress_policies": i_by[:50],
        "reason": _clip(_scrub(reason)),
        "notes": sorted({note if len(note) <= 400 else note[:397] + "..." for note in notes}),
    }


def netpol_reachability(payload: dict[str, object]) -> dict[str, object]:
    """Can `source` reach `destination` on protocol/port. Both sides must allow it: the source's egress policies and the
    destination's ingress policies. A direction without any selecting policy is open; otherwise some rule must name the other
    pod (bare podSelector = policy namespace, namespaceSelector = labels incl. kubernetes.io/metadata.name, both = AND,
    ipBlock matches the pod `ip` minus `except`) and the port (protocol defaults TCP, numeric, named via the destination
    pod's ports, or a port..endPort range). A rule without peers or ports matches everything. At most 50 allowing policy names
    are listed per side."""
    return _respond(payload, _netpol_reachability)


# --- call-chain tools ----------------------------------------------------------------------------------------------


def _chain(calls: list[object], field: str) -> list[dict[str, object]]:
    """Validate an ordered call chain: calls[i+1].caller must be calls[i].callee."""
    out: list[dict[str, object]] = []
    for index, item in enumerate(calls):
        item = _dict(item, field, f"{field}[{index}]")
        caller = _str(item.get("caller"), field, f"{field}[{index}].caller")
        callee = _str(item.get("callee"), field, f"{field}[{index}].callee")
        if out and out[-1]["callee"] != caller:
            raise _Invalid(field, f"{field}[{index}].caller {caller!r} must equal the previous callee {out[-1]['callee']!r}")
        out.append({**item, "caller": caller, "callee": callee})
    return out


def _chain_cycle(chain: list[dict[str, object]]) -> list[str]:
    path = [str(chain[0]["caller"])] + [str(c["callee"]) for c in chain]
    return sorted({s for s in path if path.count(s) > 1})


def _retry_amplification(payload: dict[str, object]) -> dict[str, object]:
    chain = _chain(_list(payload["calls"], "calls", "calls"), "calls")
    limit = _dec(payload["max_multiplier"])
    findings: list[dict[str, str]] = []
    cumulative, unbudgeted = Decimal(1), 0
    hops: list[dict[str, object]] = []
    crossed: tuple[int, dict[str, object]] | None = None
    for index, call in enumerate(chain):
        retries = _nonneg_int(call.get("retries"), "calls", f"calls[{index}].retries")
        budget = call.get("budget_percent")
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 0 <= budget <= 100):
            raise _Invalid("calls", f"calls[{index}].budget_percent must be a number in 0..100")
        extra = Decimal(retries) if budget is None else min(Decimal(retries), _dec(budget) / 100)
        multiplier = 1 + extra
        cumulative *= multiplier
        if retries > 0 and budget is None:
            unbudgeted += 1
        if crossed is None and cumulative > limit:
            crossed = (index, call)
        hops.append({"caller": call["caller"], "callee": call["callee"], "retries": retries,
                     "hop_multiplier": round(float(multiplier), 4), "cumulative_load": round(float(cumulative), 4)})
    for service in _chain_cycle(chain):
        findings.append(_finding("RETRY-CYCLE", "blocker", f"{service} appears twice on the call path; a retry loop multiplies without bound"))
    if crossed is not None:
        findings.append(_finding("RETRY-AMPLIFICATION", "major",
                                 f"load on {crossed[1]['callee']} reaches the {limit}x budget at hop {crossed[0] + 1}; the chain end sees "
                                 f"{round(float(cumulative), 4)}x one client request when every attempt fails"))
    if unbudgeted >= 2:
        findings.append(_finding("RETRY-NESTED", "minor", f"{unbudgeted} hops retry without a retry budget; layered retries multiply"))
    return {"state": "SUCCESS", "hop_count": len(chain), "worst_case_multiplier": round(float(cumulative), 4),
            "over_budget": cumulative > limit, "hops": hops, **_report(findings)}


def retry_amplification(payload: dict[str, object]) -> dict[str, object]:
    """Worst-case load multiplication of retries along caller->callee hops. `retries` are extra attempts after the first
    (Istio retries.attempts, Envoy num_retries), so a hop multiplies load by 1+retries and the load on hop k is the product up
    to k when every attempt fails. A `budget_percent` (Envoy retry budget, an Envoy-level setting that VirtualService does not
    expose: concurrent retries may use at most that share of active requests, default 20) is modelled as capping a hop's extra
    load at min(retries, budget/100); Envoy's min_retry_concurrency (default 3) is ignored, so the figure is an approximation.
    Over budget means strictly above `max_multiplier`."""
    return _respond(payload, _retry_amplification)


def _timeout_hierarchy(payload: dict[str, object]) -> dict[str, object]:
    chain = _chain(_list(payload["calls"], "calls", "calls"), "calls")
    min_gap = cast(int, payload.get("min_gap_ms", 1))  # schema: integer 1..3600000 (input_schema); compared with whole gaps below
    findings: list[dict[str, str]] = []
    totals: list[int] = []
    attempts: list[int] = []
    for index, call in enumerate(chain):
        timeout = call.get("timeout_ms")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            raise _Invalid("calls", f"calls[{index}].timeout_ms must be a positive integer")
        per_try = call.get("per_try_timeout_ms")
        if per_try is not None:
            if isinstance(per_try, bool) or not isinstance(per_try, int) or per_try <= 0:
                raise _Invalid("calls", f"calls[{index}].per_try_timeout_ms must be a positive integer")
        retries = _nonneg_int(call.get("retries", 0), "calls", f"calls[{index}].retries")
        label = f"{call['caller']}->{call['callee']}"
        if per_try is not None and per_try > timeout:
            findings.append(_finding("TIMEOUT-PERTRY-EXCEEDS-TOTAL", "major", f"{label}: per-try timeout {per_try}ms exceeds the total {timeout}ms"))
        elif retries and per_try is None:
            findings.append(_finding("TIMEOUT-RETRIES-NO-PERTRY", "minor", f"{label}: {retries} retries without a per-try timeout; the first "
                                     f"attempt can consume the whole {timeout}ms"))
        elif retries and per_try is not None and per_try * (retries + 1) > timeout:
            findings.append(_finding("TIMEOUT-RETRIES-EXCEED-TOTAL", "minor", f"{label}: {retries + 1} tries of {per_try}ms cannot finish within {timeout}ms"))
        totals.append(timeout)
        attempts.append(min(per_try, timeout) if per_try is not None else timeout)
    gaps = []
    for index in range(len(chain) - 1):
        gap = attempts[index] - totals[index + 1]
        gaps.append(gap)
        label = f"{chain[index]['caller']}->{chain[index]['callee']} ({attempts[index]}ms) over {chain[index + 1]['callee']} ({totals[index + 1]}ms)"
        if gap < 0:
            findings.append(_finding("TIMEOUT-INVERTED", "major", f"{label}: the caller gives up {-gap}ms before the callee's own timeout, "
                                     "so work continues after the caller has failed or retried"))
        elif gap < min_gap:
            findings.append(_finding("TIMEOUT-THIN-MARGIN", "minor", f"{label}: gap {gap}ms is below the required {min_gap}ms"))
    return {"state": "SUCCESS", "hop_count": len(chain), "edge_budget_ms": totals[0], "pair_count": len(gaps),
            "tightest_gap_ms": min(gaps) if gaps else 0, **_report(findings)}


def timeout_hierarchy(payload: dict[str, object]) -> dict[str, object]:
    """Timeouts must shrink toward the leaf. For consecutive calls the caller's attempt budget (per_try_timeout_ms capped at
    timeout_ms, else timeout_ms) must exceed the callee's own total timeout by at least `min_gap_ms`: a negative gap is major
    (the caller gives up first and the callee keeps working), a gap below the minimum is minor, equality with the minimum is
    accepted. Also flags per-try above total, retries without a per-try timeout, and (retries+1) x per-try above the total."""
    return _respond(payload, _timeout_hierarchy)


# --- mesh_dns_ndots ------------------------------------------------------------------------------------------------


def _name_cost(name: str, target: object, ndots: int, search: list[str], expected: list[str]) -> tuple[int, int, str]:
    """(dots, lookups until the answer, how it resolves). A lookup is one candidate name sent to the resolver (A and AAAA
    together). A name with fewer than ndots dots tries the search list first, otherwise it is tried as written first.
    A trailing dot marks an absolute name; IP literals need no lookup. A `cluster` name written as svc, svc.ns or svc.ns.svc
    resolves at search[0..2] when the search list is the Kubernetes default; everything else resolves as written."""
    if name.endswith("."):
        return name.rstrip(".").count("."), 1, "absolute"
    try:
        ipaddress.ip_address(name)
        return 0, 0, "absolute"
    except ValueError:
        pass
    dots = name.count(".")
    short_form = dots < 2 or (dots == 2 and name.endswith(".svc"))  # svc, svc.ns or svc.ns.svc
    if target == "cluster" and short_form and dots < len(search) and search[dots] == expected[dots]:
        return dots, (dots + 1 if dots < ndots else dots + 2), "search"
    return dots, (len(search) + 1 if dots < ndots else 1), "absolute"


def _dns_ndots(payload: dict[str, object]) -> dict[str, object]:
    ndots = cast(int, payload["ndots"])  # schema: integer 0..15 (input_schema); compared with dot counts in _name_cost
    namespace = _str(payload["namespace"], "namespace", "namespace")
    domain = _str(payload.get("cluster_domain", DEFAULT_CLUSTER_DOMAIN), "cluster_domain", "cluster_domain").strip(".")
    search = [_str(s, "search_domains", "search domain") for s in _list(payload["search_domains"], "search_domains", "search_domains")]
    for entry in search:
        if any(c.isspace() for c in entry) or ".." in entry:
            raise _Invalid("search_domains", f"search domain {entry!r} is not a valid DNS suffix")
    expected = [f"{namespace}.svc.{domain}", f"svc.{domain}", domain]
    findings: list[dict[str, str]] = []
    joined = len(" ".join(search))
    if len(search) > MAX_DNS_SEARCH_PATHS or joined > MAX_DNS_SEARCH_CHARS:
        findings.append(_finding("DNS-SEARCH-LIMIT", "major", f"search list has {len(search)} domains / {joined} characters; Kubernetes allows at "
                                 f"most {MAX_DNS_SEARCH_PATHS} / {MAX_DNS_SEARCH_CHARS}"))
    entries: list[tuple[str, object]] = []
    for index, item in enumerate(_list(payload["names"], "names", "names")):
        item = _dict(item, "names", f"names[{index}]")
        name = _str(item.get("name"), "names", f"names[{index}].name")
        if any(c.isspace() for c in name) or name.startswith(".") or ".." in name or name == ".":
            raise _Invalid("names", f"names[{index}].name {name!r} is not a DNS name")
        target = item.get("target")
        if target not in ("cluster", "external"):
            raise _Invalid("names", f"names[{index}].target must be cluster or external")
        entries.append((name, target))
    rows: list[dict[str, object]] = []
    total, wasted = 0, 0
    for name, target in entries:
        dots, lookups, via = _name_cost(name, target, ndots, search, expected)
        rows.append({"name": name, "target": target, "dots": dots, "lookups": lookups, "wasted_lookups": max(lookups - 1, 0)})
        total += lookups
        wasted += max(lookups - 1, 0)
        if lookups > 1 and via == "absolute":
            findings.append(_finding("DNS-NDOTS-WASTE", "minor", f"{name}: {lookups - 1} failed search-domain lookup(s) before it resolves "
                                     f"({dots} dots < ndots {ndots}); write it with a trailing dot or lower ndots"))
        elif lookups > 1 and dots >= ndots:
            findings.append(_finding("DNS-SHORT-NAME-WASTE", "minor", f"{name}: ndots {ndots} makes the resolver try it as written first "
                                     f"({lookups - 1} wasted lookup(s)) before the search list"))
    best, best_total = 1, sum(_name_cost(n, t, 1, search, expected)[1] for n, t in entries)
    for candidate in range(2, MAX_NDOTS + 1):
        candidate_total = sum(_name_cost(n, t, candidate, search, expected)[1] for n, t in entries)
        if candidate_total < best_total:
            best, best_total = candidate, candidate_total
    if best_total < total:
        findings.append(_finding("DNS-NDOTS-TUNE", "info", f"ndots {best} would cut lookups for these names from {total} to {best_total}"))
    return {"state": "SUCCESS", "ndots": ndots, "search_domain_count": len(search), "name_count": len(rows), "total_lookups": total,
            "wasted_lookups": wasted, "best_ndots": best, "best_total_lookups": best_total, "names": rows, **_report(findings)}


def dns_ndots(payload: dict[str, object]) -> dict[str, object]:
    """Resolver cost of a pod's DNS settings (Kubernetes ClusterFirst default: ndots 5 and the search list
    <ns>.svc.<domain>, svc.<domain>, <domain>). A name with fewer dots than ndots goes through the search list before it is
    tried absolute, so an external name like api.example.com costs 4 lookups at ndots 5; a trailing dot avoids it. Each name
    declares whether it is a `cluster` service or `external`. `best_ndots` is the smallest ndots in 1..15 with the fewest total
    lookups. Kubernetes limits the search list to 32 domains and 2048 characters; ndots is clamped to 15 by glibc."""
    return _respond(payload, _dns_ndots)


# --- mesh_egress_coverage ------------------------------------------------------------------------------------------

SE_RESOLUTIONS = ("NONE", "STATIC", "DNS", "DNS_ROUND_ROBIN")
_TLS_CLASS = ("HTTPS", "TLS")
_HTTP_CLASS = ("HTTP", "HTTP2", "GRPC", "GRPC-WEB")


def _host_matches(pattern: str, host: str) -> bool:
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        return host.endswith(pattern[1:])
    return pattern == host


def _protocol_clash(destination: str, declared: str) -> bool:
    return (destination in _TLS_CLASS and declared in _HTTP_CLASS) or (destination in _HTTP_CLASS and declared in _TLS_CLASS)


class _SEntry(TypedDict):
    subject: str
    hosts: list[str]
    ports: list[tuple[int, str]]
    used: bool


def _egress_coverage(payload: dict[str, object]) -> dict[str, object]:
    mode = payload["outbound_policy"]
    findings: list[dict[str, str]] = []
    entries: list[_SEntry] = []
    for index, doc in enumerate(_list(payload["service_entries"], "service_entries", "service_entries")):
        namespace, name = _meta(doc, "service_entries", index, "ServiceEntry")
        spec = _spec(doc, "service_entries", index)
        subject = f"{namespace}/{name}"
        hosts = [_str(h, "service_entries", f"{subject} hosts").lower() for h in _list(spec.get("hosts"), "service_entries", f"{subject} hosts")]
        if not hosts:
            raise _Invalid("service_entries", f"{subject}: spec.hosts must not be empty")
        ports: list[tuple[int, str]] = []
        for item in _list(spec.get("ports"), "service_entries", f"{subject} ports"):
            item = _dict(item, "service_entries", f"{subject} port")
            number = item.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 65535:
                raise _Invalid("service_entries", f"{subject}: port number {number!r} must be in 1..65535")
            ports.append((number, str(item.get("protocol", "TCP")).upper()))
        if not ports:
            findings.append(_finding("SE-NO-PORTS", "minor", f"{subject}: no ports, the entry matches no destination"))
        resolution = spec.get("resolution", "NONE")
        if resolution not in SE_RESOLUTIONS:
            findings.append(_finding("SE-RESOLUTION-INVALID", "major", f"{subject}: resolution {resolution!r} must be one of {', '.join(SE_RESOLUTIONS)}"))
        if resolution == "NONE" and not spec.get("addresses") and any(protocol == "TCP" for _, protocol in ports):
            findings.append(_finding("SE-TCP-NONE-OPEN", "major" if mode == "REGISTRY_ONLY" else "info",
                                     f"{subject}: resolution NONE on a TCP port without addresses allows traffic to any IP on that port"))
        for host in hosts:
            if host == "*":
                findings.append(_finding("EGRESS-WILDCARD-ALL", "major", f"{subject}: host \"*\" allows every external destination"))
            elif host.startswith("*.") and "." not in host[2:]:
                findings.append(_finding("EGRESS-WILDCARD-TLD", "major", f"{subject}: host {host} covers a whole top-level domain"))
            if "*" in host and resolution in ("DNS", "DNS_ROUND_ROBIN") and not spec.get("endpoints"):
                findings.append(_finding("SE-WILDCARD-DNS", "major", f"{subject}: wildcard host {host} cannot be resolved with {resolution} "
                                         "and no endpoints; use resolution NONE"))
        entries.append({"subject": subject, "hosts": hosts, "ports": ports, "used": False})
    uncovered: list[str] = []
    covered, destinations = 0, _list(payload["destinations"], "destinations", "destinations")
    for index, item in enumerate(destinations):
        item = _dict(item, "destinations", f"destinations[{index}]")
        host = _str(item.get("host"), "destinations", f"destinations[{index}].host").lower()
        port, protocol = item.get("port"), str(item.get("protocol", "TCP")).upper()
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise _Invalid("destinations", f"destinations[{index}].port must be in 1..65535")
        matching = [(e, p) for e in entries if any(_host_matches(h, host) for h in e["hosts"]) for p in e["ports"] if p[0] == port]
        if not matching:
            uncovered.append(f"{host}:{port}")
            if mode == "REGISTRY_ONLY":
                findings.append(_finding("EGRESS-BLOCKED", "major", f"{host}:{port} has no ServiceEntry; REGISTRY_ONLY drops the traffic"))
            else:
                findings.append(_finding("EGRESS-UNREGISTERED", "minor", f"{host}:{port} has no ServiceEntry; ALLOW_ANY passes it unmanaged"))
            continue
        covered += 1
        for entry, _ in matching:
            entry["used"] = True
        if all(_protocol_clash(protocol, declared) for _, (_, declared) in matching):
            declared = matching[0][1][1]
            findings.append(_finding("EGRESS-PROTOCOL-MISMATCH", "major", f"{host}:{port} is {protocol} but ServiceEntry {matching[0][0]['subject']} "
                                     f"declares {declared}; Envoy would parse it with the wrong filter chain"))
    for entry in entries:
        if not entry["used"] and entry["ports"]:
            findings.append(_finding("EGRESS-UNUSED-ENTRY", "info", f"{entry['subject']}: no supplied destination uses this ServiceEntry"))
    return {"state": "SUCCESS", "destination_count": len(destinations), "covered_count": covered, "uncovered": sorted(uncovered),
            "unused_entries": sorted(e["subject"] for e in entries if not e["used"] and e["ports"]), **_report(findings)}


def egress_coverage(payload: dict[str, object]) -> dict[str, object]:
    """Match observed outbound destinations (host, port, protocol) to Istio ServiceEntries. A ServiceEntry covers a
    destination when one host pattern ("*", "*.suffix" or exact) matches and the port number is declared. Uncovered traffic is
    blocked under outboundTrafficPolicy REGISTRY_ONLY (major) and passes unmanaged under ALLOW_ANY (minor). Also flags
    over-broad entries ("*" and top-level-domain wildcards), wildcard hosts with DNS resolution and no endpoints, resolution
    NONE on a TCP port without addresses (Istio then allows any IP on that port: major under REGISTRY_ONLY), a TLS-class
    destination on an HTTP-class port declaration (or the reverse), and unused entries."""
    return _respond(payload, _egress_coverage)


# --- mesh_cidr_overlap ---------------------------------------------------------------------------------------------

_CIDR_KINDS = ("pod", "service", "node")
_SAME_CLUSTER: dict[tuple[str, ...], str] = {("pod", "service"): "blocker", ("node", "pod"): "major", ("node", "service"): "major",
                 ("pod", "pod"): "major", ("service", "service"): "major", ("node", "node"): "major"}
_ROUTED: dict[tuple[str, ...], str] = {("pod", "pod"): "blocker", ("node", "node"): "blocker", ("node", "pod"): "blocker", ("pod", "service"): "major",
           ("node", "service"): "major", ("service", "service"): "minor"}


def _cidr_overlap(payload: dict[str, object]) -> dict[str, object]:
    connectivity = payload["connectivity"]
    if connectivity not in ("routed", "gateway"):
        raise _Invalid("connectivity", "connectivity must be routed or gateway")
    entries: list[tuple[str, str, _Network]] = []
    names: set[str] = set()
    for index, cluster in enumerate(_list(payload["clusters"], "clusters", "clusters")):
        cluster = _dict(cluster, "clusters", f"clusters[{index}]")
        name = _str(cluster.get("name"), "clusters", f"clusters[{index}].name")
        if name in names:
            raise _Invalid("clusters", f"cluster name {name!r} is used twice")
        names.add(name)
        for kind in _CIDR_KINDS:
            for text in _list(cluster.get(f"{kind}_cidrs"), "clusters", f"{name} {kind}_cidrs"):
                try:
                    entries.append((name, kind, _ip_network(text, strict=True)))
                except (ValueError, TypeError):
                    raise _Invalid("clusters", f"{name} {kind}_cidrs entry {text!r} is not a CIDR with zeroed host bits") from None
    findings: list[dict[str, str]] = []
    overlaps = 0
    for i, (cluster_a, kind_a, net_a) in enumerate(entries):
        for cluster_b, kind_b, net_b in entries[i + 1:]:
            if net_a.version != net_b.version or not net_a.overlaps(net_b):
                continue
            overlaps += 1
            kinds = tuple(sorted((kind_a, kind_b)))
            same = cluster_a == cluster_b
            severity = _SAME_CLUSTER[kinds] if same else _ROUTED[kinds] if connectivity == "routed" else (
                "major" if kinds == ("node", "node") else "info")
            relation = "is identical to" if net_a == net_b else "contains" if _subnet_of(net_b, net_a) else "is inside"
            scope = "SAME" if same else "CROSS"
            findings.append(_finding(f"CIDR-{scope}-{kinds[0].upper()}-{kinds[1].upper()}", severity,
                                     f"{cluster_a} {kind_a} {net_a} {relation} {cluster_b} {kind_b} {net_b}"))
    return {"state": "SUCCESS", "cluster_count": len(names), "cidr_count": len(entries), "overlap_count": overlaps, **_report(findings)}


def cidr_overlap(payload: dict[str, object]) -> dict[str, object]:
    """Pod, service and node CIDR overlaps within and between clusters (same IP version only; host bits must be zero). The
    severity table is this tool's policy, not a standard. Inside one cluster pod/service overlap is a blocker and every other
    overlap is major. Between clusters with `routed` connectivity (pod IPs routed directly: flat VPC peering, Cilium Cluster Mesh,
    Submariner) overlapping pod or node ranges are blockers, a service range overlapping another cluster's pod or node range is
    major, service/service is minor (assumes ClusterIPs stay cluster-local; global-service setups such as Submariner may need it
    disjoint). With `gateway` connectivity (east-west gateways) only node/node overlap matters (major); the rest is reported as
    info for a later move to routed networking."""
    return _respond(payload, _cidr_overlap)


# --- mesh_ingress_tls ----------------------------------------------------------------------------------------------


def _tls_version(value: object, field: str, what: str) -> tuple[int, int] | None:
    """Wire version tuple for SSLv3 / TLSv1.0..1.3 in nginx, Envoy (TLSv1_2) or Istio (TLSV1_2) spelling; None for TLS_AUTO."""
    text = str(value).strip().upper().replace("_", ".")
    if text == "TLS.AUTO":
        return None
    if text in ("SSLV3", "SSL3", "SSLV3.0"):
        return TLS_VERSIONS["SSLV3"]
    for prefix in ("TLSV", "TLS"):
        if text.startswith(prefix) and text[len(prefix):] in ("1.0", "1.1", "1.2", "1.3"):
            return TLS_VERSIONS[text[len(prefix):]]
    raise _Invalid(field, f"{what} {value!r} is not SSLv3 or TLSv1.0 - TLSv1.3")


def _cipher_flags(name: str) -> set[str]:
    text = name.strip().upper().replace("-", "_")
    tokens = set(text.split("_"))
    if text.startswith(("TLS_AES_", "TLS_CHACHA20_")) and "WITH" not in tokens:
        return {"tls13"}
    if not any(k in text for k in KNOWN_CIPHER_TOKENS):
        return {"unknown"}
    if any(w in text for w in WEAK_CIPHER_TOKENS):
        return {"weak"}
    flags: set[str] = set()
    if not tokens & set(FORWARD_SECRET_TOKENS):
        flags.add("no_pfs")
    if not any(a in text for a in ("GCM", "CCM", "POLY1305")):
        flags.add("cbc")
    return flags


def _san_covers(san: str, host: str) -> bool:
    """RFC 6125 6.4.3: a wildcard SAN stands for exactly one leftmost label and never for the bare domain."""
    if san == host:
        return True
    if san.startswith("*.") and not host.startswith("*."):
        head, _, rest = host.partition(".")
        return bool(head) and rest == san[2:]
    return False


def _ingress_tls(payload: dict[str, object]) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    listeners = _list(payload["listeners"], "listeners", "listeners")
    terminating = 0
    for index, item in enumerate(listeners):
        item = _dict(item, "listeners", f"listeners[{index}]")
        name = _str(item.get("name"), "listeners", f"listeners[{index}].name")
        hostnames = [_str(h, "listeners", f"{name} hostnames").lower() for h in _list(item.get("hostnames"), "listeners", f"{name} hostnames")]
        if not item.get("terminates_tls"):
            if item.get("redirect_http_to_https") is not True:
                findings.append(_finding("TLS-PLAINTEXT-LISTENER", "minor", f"{name}: serves plaintext and does not redirect to HTTPS"))
            continue
        terminating += 1
        low = _tls_version(item["min_tls_version"], "listeners", f"{name} min_tls_version") if "min_tls_version" in item else None
        high = _tls_version(item["max_tls_version"], "listeners", f"{name} max_tls_version") if "max_tls_version" in item else None
        if low is not None and high is not None and high < low:
            raise _Invalid("listeners", f"{name}: max_tls_version is below min_tls_version")
        if low is None:
            findings.append(_finding("TLS-MIN-VERSION-UNSET", "info", f"{name}: no TLS version floor; the implementation default applies"))
        elif low == TLS_VERSIONS["SSLV3"]:
            findings.append(_finding("TLS-VERSION-SSL3", "blocker", f"{name}: SSLv3 is accepted (RFC 7568 prohibits it)"))
        elif low < MODERN_TLS:
            findings.append(_finding("TLS-VERSION-LEGACY", "major", f"{name}: TLS 1.0/1.1 are accepted (deprecated by RFC 8996)"))
        if high is not None and high < MODERN_TLS:
            findings.append(_finding("TLS-MAX-LEGACY", "major", f"{name}: the highest enabled version is below TLS 1.2"))
        ciphers = _list(item.get("cipher_suites"), "listeners", f"{name} cipher_suites")
        if ciphers and low != TLS_VERSIONS["1.3"]:  # a TLS 1.3 floor makes the (configurable) TLS 1.2 list irrelevant
            groups: dict[str, list[str]] = {"weak": [], "no_pfs": [], "cbc": [], "unknown": []}
            for cipher in ciphers:
                cipher_name = _str(cipher, "listeners", f"{name} cipher")
                for flag in _cipher_flags(cipher_name) & set(groups):
                    groups[flag].append(cipher_name)
            for flag, fid, severity, text in (("weak", "TLS-CIPHER-WEAK", "major", "weak (RC4, RC2, DES/3DES, NULL, EXPORT, anonymous or MD5)"),
                                              ("no_pfs", "TLS-CIPHER-NO-PFS", "minor", "without forward secrecy (no ECDHE/DHE)"),
                                              ("cbc", "TLS-CIPHER-CBC", "minor", "non-AEAD (CBC)"),
                                              ("unknown", "TLS-CIPHER-UNKNOWN", "info", "not recognised")):
                if groups[flag]:
                    findings.append(_finding(fid, severity, f"{name}: cipher(s) {text}: {', '.join(sorted(set(groups[flag]))[:5])}"))
        if not str(item.get("certificate_name") or "").strip():
            findings.append(_finding("TLS-NO-CERT", "major", f"{name}: terminates TLS without a certificate reference"))
        sans = [_str(s, "listeners", f"{name} certificate_sans").lower() for s in _list(item.get("certificate_sans"), "listeners", "sans")]
        if sans:
            missing = sorted({h for h in hostnames if not any(_san_covers(s, h) for s in sans)})
            if missing:
                findings.append(_finding("TLS-SAN-MISMATCH", "major", f"{name}: certificate SANs do not cover {', '.join(missing[:5])}"))
        age = item.get("hsts_max_age_s")
        if age is None:
            findings.append(_finding("TLS-HSTS-MISSING", "minor", f"{name}: no Strict-Transport-Security header"))
        else:
            age = _nonneg_int(age, "listeners", f"{name} hsts_max_age_s")
            if age == 0:
                findings.append(_finding("TLS-HSTS-DISABLED", "minor", f"{name}: max-age=0 clears HSTS in browsers"))
            elif age < HSTS_RECOMMENDED_MAX_AGE_S:
                findings.append(_finding("TLS-HSTS-SHORT", "minor", f"{name}: HSTS max-age {age}s is below one year ({HSTS_RECOMMENDED_MAX_AGE_S}s)"))
        if item.get("hsts_preload") and (age is None or age < HSTS_RECOMMENDED_MAX_AGE_S or not item.get("hsts_include_subdomains")):
            findings.append(_finding("TLS-HSTS-PRELOAD-INVALID", "major", f"{name}: preload needs max-age >= {HSTS_RECOMMENDED_MAX_AGE_S}s and "
                                     "includeSubDomains (hstspreload.org)"))
        if item.get("redirect_http_to_https") is False:
            findings.append(_finding("TLS-NO-HTTP-REDIRECT", "minor", f"{name}: plain HTTP is not redirected to HTTPS"))
    return {"state": "SUCCESS", "listener_count": len(listeners), "tls_listener_count": terminating, **_report(findings)}


def ingress_tls(payload: dict[str, object]) -> dict[str, object]:
    """TLS hygiene of ingress listeners. Version floor: SSLv3 blocker (RFC 7568), TLS 1.0/1.1 major (RFC 8996). Ciphers (up to
    TLS 1.2; names in IANA or OpenSSL spelling): RC4, RC2, DES/3DES, NULL, EXPORT, anonymous and MD5 are major, suites without
    ECDHE/DHE and non-AEAD CBC suites minor (house classification). Certificate: a reference is required and, when SANs are
    given, every hostname must be covered (a wildcard SAN covers exactly one label, RFC 6125). HSTS (RFC 6797): missing or
    max-age below one year (the hstspreload.org minimum, used here as the recommendation) is minor, preload needs a one-year
    max-age and includeSubDomains. Plaintext listeners are minor unless they only redirect."""
    return _respond(payload, _ingress_tls)
