"""A24 Cluster Security & Policy Engineer tools: pure analyses of Kubernetes manifests and policies already parsed to JSON.

Nothing here talks to a cluster. Every handler decides one question over supplied data and returns counts, a `verdict`
(`pass` only when no major or blocker finding exists) and `findings` of {id, severity, summary} sorted by severity.
Severities are info|minor|major|blocker. A24 reports to A10 (cluster.security.findings); it never accepts risk, never
waives a finding and never signs a gate, so a verdict is a measurement and not an approval.

Tools: kpol_pss_check (Pod Security Standards baseline/restricted), kpol_rbac_risk_scan (wildcards, escalate/bind/
impersonate, secret read, cluster-admin bindings), kpol_rbac_effective_access (what a subject can do through bindings
and aggregated ClusterRoles), kpol_admission_policy_lint (webhooks, ValidatingAdmissionPolicy, Kyverno, Gatekeeper:
fail-open, exclusions, audit versus enforce), kpol_cis_score (CIS Kubernetes Benchmark result scoring),
kpol_secret_exposure_check (env versus volume secrets, encryption at rest), kpol_sa_token_audit (service-account
token mounts), kpol_image_admission_check (registries, digest pinning, signature evidence), kpol_runtime_rule_lint
(Falco rule lint and noise), kpol_tenant_isolation_check (PSA labels, quota, NetworkPolicy, cross-tenant bindings),
kpol_audit_policy_check (audit.k8s.io/v1 policy completeness), kpol_exception_register_check (expiry and staleness).

Public sources for the embedded reference tables (each is named where it is used):
- Pod Security Standards: https://kubernetes.io/docs/concepts/security/pod-security-standards/
- RBAC authorization (verbs, aggregation, escalate/bind): https://kubernetes.io/docs/reference/access-authn-authz/rbac/
- Auditing: https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/
- Encryption at rest: https://kubernetes.io/docs/tasks/administer-cluster/encrypt-data/
- Removed APIs: https://kubernetes.io/docs/reference/using-api/deprecation-guide/
- Falco rule fields: https://falco.org/docs/concepts/rules/basic-elements/
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Container
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from swarm.redact import REDACTED, redact_spans, redact_text
from swarm.tools.support import respond

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
MAX_FINDINGS = 500  # findings returned; `finding_count` always reports the true total
MAX_NODES = 200_000  # JSON nodes accepted per free-form input
OUTPUT_BUDGET = 100_000  # characters of JSON per capped list; the registry withholds results of 256 KiB or more
MAX_CONTAINERS = 200  # containers accepted per pod spec
MAX_MACRO_EXPANSIONS = 50_000  # macro expansions per rule-lint call (Falco macros): a cyclic or huge macro graph fails as INVALID_INPUT


class _Bad(Exception):
    """Semantic input problem: becomes INVALID_INPUT naming `field`."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field
        self.message = message


def _invalid(field: str, message: str) -> dict[str, Any]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _guarded(work: Callable[[], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    """Wrap `work` so a `_Bad` raised anywhere inside becomes the INVALID_INPUT result."""

    def run() -> dict[str, Any]:
        try:
            return work()
        except _Bad as exc:
            return _invalid(exc.field, exc.message)
        except RecursionError:  # a macro chain nests one call per level; too deep to analyse is an input problem, not a dependency fault
            return _invalid("rules", "macro chain too deep to analyse")

    return run


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _public(text: str) -> str:
    """`text` with credentials replaced by swarm.redact's `[REDACTED]`. A string with no credential comes back unchanged.
    The credential spans are taken from the whole string, so a secret whose key and value are separate words (`password: hunter2`)
    is masked. A false, true or null value is left as written only under a known boolean setting (`automountServiceAccountToken:
    false`); under any other key a boolean-looking value is still a credential (`password=false` is masked)."""
    if redact_text(text) == text:
        return text
    masked_text = []
    last = 0
    for start, end in redact_spans(text):
        if _is_boolean_setting(text, start, end):
            continue
        masked_text.append(text[last:start])
        masked_text.append(REDACTED)
        last = end
    masked_text.append(text[last:])
    return "".join(masked_text)


_BOOLEAN_SETTINGS = frozenset({"automountserviceaccounttoken"})  # keys whose false, true or null value is a setting, not a secret
_NOT_SECRET_VALUES = frozenset({"true", "false", "null", "none", "nil"})
_KEY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
_MAX_KEY = 128  # a longer key is not a known boolean setting


def _key_before_value(text: str, start: int) -> str:
    """The key of the `key: value` or `key=value` pair whose value starts at `start`, lower-cased; "" when there is none or the
    key is longer than _MAX_KEY. One backward pass, so the cost is bounded by the prefix length (no regex rescans it)."""
    i = start
    while i > 0 and text[i - 1] in " \t\"'":
        i -= 1
    if i == 0 or text[i - 1] not in ":=":
        return ""
    i -= 1
    while i > 0 and text[i - 1] in " \t\"'":
        i -= 1
    end = i
    while i > 0 and end - i < _MAX_KEY and text[i - 1] in _KEY_CHARS:
        i -= 1
    if i == end or (end - i == _MAX_KEY and i > 0 and text[i - 1] in _KEY_CHARS):
        return ""
    return text[i:end].lower()


def _is_boolean_setting(text: str, start: int, end: int) -> bool:
    return text[start:end].lower() in _NOT_SECRET_VALUES and _key_before_value(text, start) in _BOOLEAN_SETTINGS


def _finding(rule: str, severity: str, location: str, summary: str) -> dict[str, Any]:
    """A finding. Its location and summary copy names from the manifest, and a name can hold a pasted credential, so both pass
    through `_public`. The rule id is a fixed identifier and is left as written."""
    location, summary = _public(location), _public(summary)
    return {"id": f"{rule}:{location}" if location else rule, "severity": severity, "summary": summary}


def _ordered(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate and sort: worst first, then id, then summary. Deterministic."""
    unique = {(f["id"], f["severity"], f["summary"]): f for f in findings}
    return sorted(unique.values(), key=lambda f: (-SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))


def _verdict(findings: list[dict[str, Any]]) -> str:
    return "fail" if any(SEVERITY_RANK[f["severity"]] >= SEVERITY_RANK["major"] for f in findings) else "pass"


def _within_budget(items: list[dict[str, Any]], limit: int, budget: int = OUTPUT_BUDGET) -> list[dict[str, Any]]:
    """The leading items, at most `limit`, whose JSON stays under `budget` characters (the registry withholds results of 256 KiB)."""
    kept: list[dict[str, Any]] = []
    size = 0
    for item in items[:limit]:
        size += len(json.dumps(item)) + 2  # the item plus its ", " separator
        if size > budget:
            break
        kept.append(item)
    return kept


def _capped_names(names: list[str], budget: int) -> list[str]:
    """Names whose JSON stays under `budget` characters; a list cut short ends with an entry that counts what is not listed
    (these output lists carry no total of their own, and a whole result over 256 KiB would be withheld by the registry)."""
    kept: list[str] = []
    size = 0
    for index, name in enumerate(names):
        size += len(json.dumps(name)) + 2
        if size > budget:
            kept.append(f"... {len(names) - index} more not listed (capped at {budget} characters)")
            break
        kept.append(name)
    return kept


def _report(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """The shared tail of every result: total count, verdict and the ordered list capped by count and by size."""
    ordered = _ordered(findings)
    return {"finding_count": len(ordered), "verdict": _verdict(ordered), "findings": _within_budget(ordered, MAX_FINDINGS)}


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _in(value: object, allowed: Container[str]) -> bool:
    """Membership that tolerates unhashable or non-string junk (a malformed manifest is a violation, not a crash)."""
    return isinstance(value, str) and value in allowed


def _strings(value: object) -> list[str]:
    return [item for item in _list(value) if isinstance(item, str)]


# A field that should hold an enum can hold a pasted secret, so an offending value is echoed only when it is a short plain
# token (at most 16 letters, digits, `.`, `_` or `-`); anything else is reported by length only.
_SHOWN = re.compile(r"[A-Za-z0-9_.-]{0,16}")
_TIMESTAMPISH = re.compile(r"[0-9TtZz:+. -]{1,40}")


def _val(value: object, shown: re.Pattern[str] = _SHOWN) -> str:
    """repr of a short plain value; a long or odd string, a list entry or an object is withheld (length or type only)."""
    if value is None or isinstance(value, (bool, int, float)):
        return repr(value)
    if isinstance(value, str):
        return repr(value) if shown.fullmatch(value) else f"<{len(value)} chars withheld>"
    if isinstance(value, list):
        return "[" + ", ".join(_val(item, shown) for item in value[:8]) + (", ..." if len(value) > 8 else "") + "]"
    return f"<{type(value).__name__} withheld>"



def _check_size(value: object, field: str) -> None:
    """Refuse absurdly large free-form inputs (iterative walk, so depth cannot overflow the stack)."""
    stack, count = [value], 0
    while stack:
        node = stack.pop()
        count += 1
        if count > MAX_NODES:
            raise _Bad(field, f"{field} exceeds {MAX_NODES} JSON nodes")
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _manifests(items: list[Any], field: str, depth: int = 0) -> list[dict[str, Any]]:
    """Validate manifests (objects with a string `kind`) and flatten `kind: List` / `<Kind>List` wrappers.

    Items of a typed list such as PodList carry no `kind` of their own in API responses; they inherit it.
    """
    _check_size(items, field)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or not item["kind"]:
            raise _Bad(field, f"{field}[{index}] must be an object with a non-empty string kind")
        kind = item["kind"]
        if kind.endswith("List") and isinstance(item.get("items"), list):
            if depth >= 2:
                raise _Bad(field, f"{field}[{index}] nests List objects deeper than 3 levels")
            inner = kind[: -len("List")]
            children = [
                {**child, "kind": inner} if inner and isinstance(child, dict) and "kind" not in child else child
                for child in item["items"]
            ]
            out.extend(_manifests(children, field, depth + 1))
        else:
            out.append(item)
    return out


def _meta(manifest: dict[str, Any]) -> dict[str, Any]:
    return _dict(manifest.get("metadata"))


def _loc(manifest: dict[str, Any]) -> str:
    """Stable location: Kind/namespace/name, or Kind/name for cluster-scoped objects."""
    meta = _meta(manifest)
    name = meta.get("name") if isinstance(meta.get("name"), str) and meta.get("name") else "(unnamed)"
    namespace = meta.get("namespace") if isinstance(meta.get("namespace"), str) else ""
    return f"{manifest['kind']}/{namespace}/{name}" if namespace else f"{manifest['kind']}/{name}"


# --- workloads --------------------------------------------------------------------------------------------------

_TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job")


def _workloads(items: list[Any], field: str) -> list[dict[str, Any]]:
    """Pod-bearing manifests as {kind, loc, spec, meta}. Other kinds (Service, ConfigMap, ...) are ignored.

    Pod -> spec; Deployment/StatefulSet/DaemonSet/ReplicaSet/ReplicationController/Job -> spec.template;
    CronJob -> spec.jobTemplate.spec.template. meta is the pod metadata (annotations live there).
    """
    out = []
    for manifest in _manifests(items, field):
        kind = manifest["kind"]
        if kind == "Pod":
            spec, meta = manifest.get("spec"), _meta(manifest)
        elif kind in _TEMPLATE_KINDS:
            template = _dict(_dict(manifest.get("spec")).get("template"))
            spec, meta = template.get("spec"), _dict(template.get("metadata"))
        elif kind == "CronJob":
            template = _dict(_dict(_dict(_dict(manifest.get("spec")).get("jobTemplate")).get("spec")).get("template"))
            spec, meta = template.get("spec"), _dict(template.get("metadata"))
        else:
            continue
        if not isinstance(spec, dict):
            raise _Bad(field, f"{_loc(manifest)} has no pod spec at the path its kind defines")
        out.append({"kind": kind, "loc": _loc(manifest), "spec": spec, "meta": meta, "manifest": manifest})
    return out


def _containers(workload: dict[str, Any], field: str) -> list[tuple[str, str, dict[str, Any]]]:
    """(section, name, container) for containers, initContainers and ephemeralContainers."""
    out = []
    for section in ("containers", "initContainers", "ephemeralContainers"):
        entries = workload["spec"].get(section)
        if entries is None:
            continue
        if not isinstance(entries, list) or any(not isinstance(c, dict) for c in entries):
            raise _Bad(field, f"{workload['loc']} spec.{section} must be a list of objects")
        for container in entries:
            name = container.get("name") if isinstance(container.get("name"), str) and container.get("name") else "(unnamed)"
            out.append((section, name, container))
    if len(out) > MAX_CONTAINERS:
        raise _Bad(field, f"{workload['loc']} defines more than {MAX_CONTAINERS} containers")
    return out


def _csc(container: dict[str, Any]) -> dict[str, Any]:
    return _dict(container.get("securityContext"))


def _cname(section: str, name: str) -> str:
    return f"{section}/{name}"


# --- 1. Pod Security Standards ----------------------------------------------------------------------------------

# Baseline "Capabilities": the only capabilities a container may add (Pod Security Standards, Baseline).
BASELINE_CAPABILITIES = frozenset({
    "AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE",
    "SETFCAP", "SETGID", "SETPCAP", "SETUID", "SYS_CHROOT",
})
# Baseline "SELinux": allowed spec...seLinuxOptions.type values (container_engine_t since Kubernetes 1.31).
SELINUX_TYPES = frozenset({"", "container_t", "container_init_t", "container_kvm_t", "container_engine_t"})
# Baseline "Sysctls": the safe subset (each entry has a "since" version in the Pod Security Standards table).
SAFE_SYSCTLS = frozenset({
    "kernel.shm_rmid_forced", "net.ipv4.ip_local_port_range", "net.ipv4.ip_unprivileged_port_start",
    "net.ipv4.tcp_syncookies", "net.ipv4.ping_group_range", "net.ipv4.ip_local_reserved_ports",
    "net.ipv4.tcp_keepalive_time", "net.ipv4.tcp_fin_timeout", "net.ipv4.tcp_keepalive_intvl",
    "net.ipv4.tcp_keepalive_probes", "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem",
    "net.ipv4.tcp_slow_start_after_idle", "net.ipv4.tcp_notsent_lowat",
})
# Restricted "Volume Types": a volume must set one of these fields.
RESTRICTED_VOLUMES = frozenset({
    "configMap", "csi", "downwardAPI", "emptyDir", "ephemeral", "persistentVolumeClaim", "projected", "secret",
})
SECCOMP_TYPES = ("RuntimeDefault", "Localhost")
APPARMOR_TYPES = ("RuntimeDefault", "Localhost")
_PROBE_NAMES = ("livenessProbe", "readinessProbe", "startupProbe")
_HOOK_NAMES = ("postStart", "preStop")
# Baseline violations that break out of the container boundary are blockers; the other controls are major.
_PSS_BLOCKERS = frozenset({"host-process", "host-namespaces", "privileged", "host-path"})


def _nz(value: object) -> object:
    """A JSON null field is an unset field: it reads as the empty string the PSS tables allow."""
    return "" if value is None else value


def _is_zero(value: object) -> bool:
    """True for a numeric zero; a boolean is not a user id, so `false` is not root."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == 0


def _pss_violations(workload: dict[str, Any], field: str) -> list[tuple[str, str, str]]:
    """(level, control, message) for one pod spec. Level is baseline or restricted."""
    spec, meta = workload["spec"], workload["meta"]
    pod_sc = _dict(spec.get("securityContext"))
    windows = _dict(spec.get("os")).get("name") == "windows"
    containers = _containers(workload, field)
    out: list[tuple[str, str, str]] = []

    def add(level: str, control: str, message: str) -> None:
        out.append((level, control, message))

    # --- baseline
    if _dict(pod_sc.get("windowsOptions")).get("hostProcess") is True:
        add("baseline", "host-process", "spec.securityContext.windowsOptions.hostProcess is true")
    for key in ("hostNetwork", "hostPID", "hostIPC"):
        if spec.get(key) is True:
            add("baseline", "host-namespaces", f"spec.{key} is true")
    for volume in _list(spec.get("volumes")):
        if isinstance(volume, dict) and volume.get("hostPath") is not None:
            add("baseline", "host-path", f"volume {volume.get('name', '(unnamed)')} mounts a hostPath")
    sysctls = _list(pod_sc.get("sysctls"))
    for sysctl in sysctls:
        name = _dict(sysctl).get("name")
        if not _in(name, SAFE_SYSCTLS):
            add("baseline", "sysctls", f"sysctl {_val(name)} is not in the safe set")
    pod_se = _dict(pod_sc.get("seLinuxOptions"))
    pod_aa = _dict(pod_sc.get("appArmorProfile"))
    pod_seccomp = _dict(pod_sc.get("seccompProfile"))
    if not _in(_nz(pod_se.get("type")), SELINUX_TYPES):
        add("baseline", "selinux", f"seLinuxOptions.type {_val(pod_se.get('type'))} is not allowed")
    for key in ("user", "role"):
        if _nz(pod_se.get(key)) != "":
            add("baseline", "selinux", f"seLinuxOptions.{key} must be unset")
    if pod_aa.get("type") is not None and pod_aa.get("type") not in APPARMOR_TYPES:
        add("baseline", "apparmor", f"pod appArmorProfile.type {_val(pod_aa.get('type'))} is not allowed")
    if pod_seccomp.get("type") is not None and pod_seccomp.get("type") not in SECCOMP_TYPES:
        add("baseline", "seccomp", f"pod seccompProfile.type {_val(pod_seccomp.get('type'))} is not allowed")
    annotations = _dict(meta.get("annotations"))
    prefix = "container.apparmor.security.beta.kubernetes.io/"
    for key, value in sorted(annotations.items()):
        if key.startswith(prefix) and not (value == "runtime/default" or (isinstance(value, str) and value.startswith("localhost/"))):
            add("baseline", "apparmor", f"annotation {key} must be runtime/default or localhost/* (value withheld)")

    for section, name, container in containers:
        sc = _csc(container)
        where = _cname(section, name)
        if _dict(sc.get("windowsOptions")).get("hostProcess") is True:
            add("baseline", "host-process", f"{where} windowsOptions.hostProcess is true")
        if sc.get("privileged") is True:
            add("baseline", "privileged", f"{where} runs privileged")
        caps = _dict(sc.get("capabilities"))
        for cap in _list(caps.get("add")):
            if not _in(cap, BASELINE_CAPABILITIES):
                add("baseline", "capabilities-add", f"{where} adds capability {_val(cap)}")
        for port in _list(container.get("ports")):
            host_port = _dict(port).get("hostPort")
            if host_port not in (None, 0):
                add("baseline", "host-port", f"{where} uses hostPort {_val(host_port)}")
        for probe_name in _PROBE_NAMES:
            probe = _dict(container.get(probe_name))
            for handler in ("httpGet", "tcpSocket"):
                if _dict(probe.get(handler)).get("host") not in (None, ""):
                    add("baseline", "host-probe", f"{where} {probe_name}.{handler}.host is set")
        lifecycle = _dict(container.get("lifecycle"))
        for hook_name in _HOOK_NAMES:
            hook = _dict(lifecycle.get(hook_name))
            for handler in ("httpGet", "tcpSocket"):
                if _dict(hook.get(handler)).get("host") not in (None, ""):
                    add("baseline", "host-probe", f"{where} lifecycle.{hook_name}.{handler}.host is set")
        se = _dict(sc.get("seLinuxOptions"))
        if not _in(_nz(se.get("type")), SELINUX_TYPES):
            add("baseline", "selinux", f"{where} seLinuxOptions.type {_val(se.get('type'))} is not allowed")
        for key in ("user", "role"):
            if _nz(se.get(key)) != "":
                add("baseline", "selinux", f"{where} seLinuxOptions.{key} must be unset")
        aa = _dict(sc.get("appArmorProfile"))
        if aa.get("type") is not None and aa.get("type") not in APPARMOR_TYPES:
            add("baseline", "apparmor", f"{where} appArmorProfile.type {_val(aa.get('type'))} is not allowed")
        if sc.get("procMount") not in (None, "Default"):
            add("baseline", "proc-mount", f"{where} procMount {_val(sc.get('procMount'))} must be Default")
        seccomp = _dict(sc.get("seccompProfile"))
        if seccomp.get("type") is not None and seccomp.get("type") not in SECCOMP_TYPES:
            add("baseline", "seccomp", f"{where} seccompProfile.type {_val(seccomp.get('type'))} is not allowed")

    # --- restricted (adds to baseline)
    for volume in _list(spec.get("volumes")):
        if isinstance(volume, dict) and not any(volume.get(kind) is not None for kind in RESTRICTED_VOLUMES):
            kinds = sorted(key for key in volume if key != "name" and volume[key] is not None) or ["none"]
            add("restricted", "volume-types", f"volume {volume.get('name', '(unnamed)')} uses {', '.join(kinds)}")
    for section, name, container in containers:
        sc = _csc(container)
        where = _cname(section, name)
        if not windows and sc.get("allowPrivilegeEscalation") is not False:
            add("restricted", "privilege-escalation", f"{where} must set allowPrivilegeEscalation: false")
        non_root = sc.get("runAsNonRoot") if sc.get("runAsNonRoot") is not None else pod_sc.get("runAsNonRoot")
        if non_root is not True:
            add("restricted", "run-as-non-root", f"{where} does not require runAsNonRoot: true")
        if _is_zero(sc.get("runAsUser")):
            add("restricted", "run-as-user", f"{where} sets runAsUser 0")
        if not windows:
            effective = _dict(sc.get("seccompProfile")).get("type") or pod_seccomp.get("type")
            if effective not in SECCOMP_TYPES:
                add("restricted", "seccomp", f"{where} has no seccompProfile of RuntimeDefault or Localhost")
            caps = _dict(sc.get("capabilities"))
            if "ALL" not in _list(caps.get("drop")):
                add("restricted", "capabilities-drop", f"{where} does not drop ALL capabilities")
            for cap in _list(caps.get("add")):
                if cap != "NET_BIND_SERVICE":
                    add("restricted", "capabilities-add", f"{where} adds {_val(cap)}; only NET_BIND_SERVICE may be added back")
    if _is_zero(pod_sc.get("runAsUser")):
        add("restricted", "run-as-user", "spec.securityContext.runAsUser is 0")
    return out


def pss_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Check pod specs against the Pod Security Standards (baseline, restricted) and say which level they satisfy.

    Controls and allowed values are the tables of the Pod Security Standards page. Restricted includes every baseline
    control. spec.os.name == windows relaxes only the Linux-only restricted controls (privilege escalation, seccomp,
    capabilities), as that page says. Not modelled: the user-namespace (hostUsers: false) relaxations behind a feature
    gate, and per-version pinning of the sysctl list (the full current safe list is accepted). highest_compliant_level is absent when no pod spec was checked.
    """

    def work() -> dict[str, Any]:
        level = payload["level"]
        workloads = _workloads(payload["workloads"], "workloads")
        findings: list[dict[str, Any]] = []
        highest = "restricted"
        for workload in workloads:
            for control_level, control, message in _pss_violations(workload, "workloads"):
                if control_level == "baseline":
                    highest = "privileged"
                elif highest == "restricted":
                    highest = "baseline"
                if control_level == "restricted" and level == "baseline":
                    continue
                severity = "blocker" if control in _PSS_BLOCKERS else "major"
                findings.append(_finding(f"pss.{control}", severity, workload["loc"], f"{message} (violates {control_level})"))
        result = {"state": "SUCCESS", "level": level, "workload_count": len(workloads), **_report(findings)}
        if workloads:  # with no pod spec checked there is no compliance level to claim, so the key is omitted rather than set
            result["highest_compliant_level"] = highest
        return result

    return respond(payload, _guarded(work))


# --- RBAC shared helpers ----------------------------------------------------------------------------------------

_READ_VERBS = frozenset({"get", "list", "watch"})
# rbac.authorization.k8s.io/v1beta1 stopped being served in Kubernetes 1.22 (deprecation guide, v1.22 removals).
_REMOVED_RBAC_API = "rbac.authorization.k8s.io/v1beta1"


def _rules_of(role: dict[str, Any], field: str) -> list[dict[str, Any]]:
    """The PolicyRules of a role, every list normalised to a list of strings. A rule field that is not a list is refused, since a
    bare string such as "*" would otherwise read as no grant."""
    rules = role.get("rules")
    if rules is None:
        return []
    if not isinstance(rules, list) or any(not isinstance(r, dict) for r in rules):
        raise _Bad(field, f"{_loc(role)} rules must be a list of objects")
    out: list[dict[str, Any]] = []
    for rule in rules:
        normalised: dict[str, list[str]] = {}
        for key in ("apiGroups", "resources", "resourceNames", "verbs", "nonResourceURLs"):
            value = rule.get(key)
            if value is not None and not isinstance(value, list):
                raise _Bad(field, f"{_loc(role)} rules {key} must be a list")
            normalised[key] = _strings(value)
        out.append(normalised)
    return out


def _selector_matches(selector: dict[str, Any], labels: dict[str, Any]) -> bool:
    """Kubernetes LabelSelector semantics; an empty selector matches everything."""
    for match_key, value in _dict(selector.get("matchLabels")).items():
        if labels.get(match_key) != value:
            return False
    for expression in _list(selector.get("matchExpressions")):
        expression = _dict(expression)
        key, operator, values = expression.get("key"), expression.get("operator"), _strings(expression.get("values"))
        present = isinstance(key, str) and key in labels
        if operator == "In" and not (present and labels[str(key)] in values):
            return False
        if operator == "NotIn" and present and labels[str(key)] in values:
            return False
        if operator == "Exists" and not present:
            return False
        if operator == "DoesNotExist" and present:
            return False
    return True


AGGREGATION_BUDGET = 1_000_000  # selector matches and walk steps per call; a call that needs more is refused rather than expanded


def _aggregated_children(role: dict[str, Any], cluster_roles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """ClusterRoles whose labels match one of the role's clusterRoleSelectors (an empty selector matches every role), not the role itself."""
    aggregation = role.get("aggregationRule")
    name = _meta(role).get("name")
    selectors = [s for s in _list(_dict(aggregation).get("clusterRoleSelectors")) if isinstance(s, dict)]
    out = []
    for child in cluster_roles:
        if _meta(child).get("name") == name:
            continue
        if any(_selector_matches(selector, _dict(_meta(child).get("labels"))) for selector in selectors):
            out.append(child)
    return out


def _distinct(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rules without repeats, first occurrence kept: aggregated roles that select the same role contribute the same rules."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for rule in rules:
        key = json.dumps(rule, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(rule)
    return out


class _Aggregation:
    """The aggregated ClusterRoles of one call, resolved once. The roles reached from an aggregated role are found by one walk
    that visits each role once, so a shared descendant is not copied once per path: the copies grow exponentially with depth when
    two roles select the same pair. Children and effective rules are memoised per role object, and the rules a walk collects are
    de-duplicated. Selector matches and walk steps share one budget; a call that exceeds it is refused, not partly answered."""

    def __init__(self, cluster_roles: list[dict[str, Any]], field: str):
        self.roles = cluster_roles
        self.field = field
        self.steps = 0
        self._children: dict[int, list[dict[str, Any]]] = {}
        self._rules: dict[int, list[dict[str, Any]]] = {}

    def _spend(self, steps: int) -> None:
        self.steps += steps
        if self.steps > AGGREGATION_BUDGET:
            raise _Bad(self.field, f"the aggregated ClusterRoles need more than {AGGREGATION_BUDGET} steps to resolve: "
                                   "the aggregation graph is too large")

    def children(self, role: dict[str, Any]) -> list[dict[str, Any]]:
        if id(role) not in self._children:
            self._spend(len(self.roles))
            self._children[id(role)] = _aggregated_children(role, self.roles)
        return self._children[id(role)]

    def uses_own_rules(self, role: dict[str, Any]) -> bool:
        """True for a role judged on its own `rules`: not aggregated, or aggregated with selectors that match no supplied role
        (a live dump whose children were not supplied: its rules are then the aggregation result and are never dropped)."""
        aggregation = role.get("aggregationRule")
        if not isinstance(aggregation, dict):
            return True
        has_selectors = any(isinstance(s, dict) for s in _list(aggregation.get("clusterRoleSelectors")))
        return has_selectors and not self.children(role)

    def effective(self, role: dict[str, Any]) -> list[dict[str, Any]]:
        """Rules of a role. An aggregated ClusterRole gets the rules of the roles its selectors reach; the control plane overwrites
        its own `rules` (RBAC docs), so they are ignored, except when no supplied role matches (see `uses_own_rules`)."""
        if id(role) not in self._rules:
            self._rules[id(role)] = _rules_of(role, self.field) if self.uses_own_rules(role) else self._reached(role)
        return self._rules[id(role)]

    def _reached(self, role: dict[str, Any]) -> list[dict[str, Any]]:
        """The own rules of every role reached from `role` through aggregation, each role visited once, de-duplicated."""
        visited = {str(_meta(role).get("name"))}
        stack = list(reversed(self.children(role)))
        found: list[dict[str, Any]] = []
        while stack:
            node = stack.pop()
            name = str(_meta(node).get("name"))
            if name in visited:
                continue
            visited.add(name)
            if self.uses_own_rules(node):
                found.extend(_rules_of(node, self.field))
            else:
                kids = self.children(node)
                self._spend(len(kids))
                stack.extend(reversed(kids))
        return _distinct(found)


def _role_label(kind: str, namespace: str, name: str) -> str:
    return f"{kind}/{namespace}/{name}" if kind == "Role" else f"{kind}/{name}"


def _split_rbac(roles: list[Any], bindings: list[Any], roles_field: str, bindings_field: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    role_list = _manifests(roles, roles_field)
    binding_list = _manifests(bindings, bindings_field)
    for index, role in enumerate(role_list):
        if role["kind"] not in ("Role", "ClusterRole"):
            raise _Bad(roles_field, f"{roles_field}[{index}] kind {role['kind']} is not Role or ClusterRole")
    for index, binding in enumerate(binding_list):
        if binding["kind"] not in ("RoleBinding", "ClusterRoleBinding"):
            raise _Bad(bindings_field, f"{bindings_field}[{index}] kind {binding['kind']} is not RoleBinding or ClusterRoleBinding")
    return role_list, binding_list


def _role_ref(binding: dict[str, Any], field: str) -> tuple[str, str]:
    ref = binding.get("roleRef")
    if not isinstance(ref, dict) or ref.get("kind") not in ("Role", "ClusterRole") or not isinstance(ref.get("name"), str):
        raise _Bad(field, f"{_loc(binding)} roleRef must name a Role or ClusterRole")
    if binding["kind"] == "ClusterRoleBinding" and ref["kind"] != "ClusterRole":
        raise _Bad(field, f"{_loc(binding)} a ClusterRoleBinding can only reference a ClusterRole")
    return ref["kind"], ref["name"]


def _binding_namespace(binding: dict[str, Any], field: str) -> str:
    if binding["kind"] == "ClusterRoleBinding":
        return ""
    namespace = _meta(binding).get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise _Bad(field, f"{_loc(binding)} a RoleBinding needs metadata.namespace")
    return namespace


def _subjects(binding: dict[str, Any], field: str) -> list[dict[str, Any]]:
    subjects = binding.get("subjects")
    if subjects is None:
        return []
    if not isinstance(subjects, list) or any(not isinstance(s, dict) for s in subjects):
        raise _Bad(field, f"{_loc(binding)} subjects must be a list of objects")
    return subjects


# --- 2. RBAC risk scan ------------------------------------------------------------------------------------------

_IMPERSONATE_CORE = frozenset({"users", "groups", "serviceaccounts"})
_RBAC_GROUP, _AUTHN_GROUP = "rbac.authorization.k8s.io", "authentication.k8s.io"


def _covers(resources: set[str], target: str) -> bool:
    """RBAC ResourceMatches without the bare `*`: the exact resource/subresource or `*/<subresource>` (kubernetes rbac helpers)."""
    subresource = target.partition("/")[2]
    return target in resources or (bool(subresource) and f"*/{subresource}" in resources)


def _rule_risks(rule: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(rule id, severity, text) for one normalised PolicyRule."""
    verbs, resources, groups, names = set(rule["verbs"]), set(rule["resources"]), set(rule["apiGroups"]), rule["resourceNames"]
    star_verb, star_resource = "*" in verbs, "*" in resources
    if star_verb and star_resource and "*" in groups and not names:  # resourceNames narrows the grant: not admin-equivalent
        return [("rbac.admin-equivalent", "blocker", "grants every verb on every resource in every API group")]
    out: list[tuple[str, str, str]] = []
    shown = ",".join(sorted(resources)) or "(none)"
    if star_verb:
        out.append(("rbac.wildcard-verb", "major", f"verb * on {shown}"))
    if star_resource and not star_verb:
        severity = "minor" if verbs <= _READ_VERBS else "major"
        out.append(("rbac.wildcard-resource", severity, f"verbs {','.join(sorted(verbs))} on resources * (includes secrets)"))
    if "*" in groups and not star_resource:
        out.append(("rbac.wildcard-apigroup", "minor", f"API group * for resources {shown}"))
    restricted_by_name = bool(names)
    for verb in ("escalate", "bind", "impersonate"):
        if verb not in verbs:
            continue
        if verb == "impersonate":
            core_target = bool(resources & _IMPERSONATE_CORE) and ("" in groups or "*" in groups)
            authn_target = (("uids" in resources) or any(r.startswith("userextras") for r in resources)) and (_AUTHN_GROUP in groups or "*" in groups)
            relevant = star_resource or core_target or authn_target
        else:
            relevant = star_resource or (bool(resources & {"roles", "clusterroles"}) and (_RBAC_GROUP in groups or "*" in groups))
        if relevant:
            out.append((f"rbac.{verb}", "minor" if restricted_by_name else "major",
                        f"verb {verb} on {shown}" + (" limited by resourceNames" if restricted_by_name else "")))
    core = "" in groups or "*" in groups
    if ("secrets" in resources or star_resource) and core and not restricted_by_name:
        # a bare `*` resource includes secrets, so a read-only wildcard is still a secret read
        if star_verb or verbs & {"list", "watch"}:
            out.append(("rbac.secret-read", "major", "list/watch on secrets reads every secret in scope"))
        elif "get" in verbs:
            out.append(("rbac.secret-read", "minor", "get on secrets without resourceNames reads any secret by name"))
    if core and not restricted_by_name:
        if (_covers(resources, "pods/exec") or _covers(resources, "pods/attach")) and (star_verb or verbs & {"create", "get"}):
            out.append(("rbac.pod-exec", "major", "pods/exec or pods/attach runs commands in any pod in scope"))
        if _covers(resources, "nodes/proxy") and (star_verb or verbs & {"get", "create"}):
            out.append(("rbac.nodes-proxy", "major", "nodes/proxy reaches the kubelet API, which can exec into pods on the node"))
        if _covers(resources, "serviceaccounts/token") and (star_verb or "create" in verbs):
            out.append(("rbac.token-request", "major", "create on serviceaccounts/token mints tokens for any service account in scope"))
        if "pods" in resources and (star_verb or "create" in verbs):
            out.append(("rbac.create-pods", "minor", "create on pods lets the subject run any service account's token in scope (CIS 5.1.4)"))
    return out


def _worst(severities: list[str]) -> str:
    return max(severities, key=lambda s: SEVERITY_RANK[s]) if severities else "info"


def rbac_risk_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Scan Roles, ClusterRoles and their bindings for privilege-escalation risk.

    Rules: admin-equivalent (verbs, resources and API groups all *, not narrowed by resourceNames), wildcard verbs/resources/API groups (a read-only `*`
    resource still reads secrets), the escalate, bind and impersonate verbs on the resources and API groups the RBAC docs name,
    secret read without resourceNames, pods/exec, nodes/proxy, serviceaccounts/token (also through `*/exec`-style
    subresource wildcards), pod creation; bindings to cluster-admin, the default admin or edit role cluster-wide,
    system:masters, anonymous or system:authenticated subjects, the default service account, and bindings whose referenced
    role carries a major or blocker rule. No object is exempt for carrying the kubernetes.io/bootstrapping label or a built-in
    name: a manifest can set both, so the default roles (admin, edit, view, cluster-admin) and the cluster-admin and
    system:masters bindings are scanned like any other object, and a real cluster dump shows their findings. Aggregated
    ClusterRoles are judged on the rules they aggregate (their own rules only when no supplied role matches). Severities are
    this tool's house judgement, not a Kubernetes standard.
    rbac.authorization.k8s.io/v1beta1 was removed in Kubernetes 1.22.
    """

    def work() -> dict[str, Any]:
        roles, bindings = _split_rbac(payload["roles"], payload["bindings"], "roles", "bindings")
        if not roles and not bindings:
            raise _Bad("roles", "supply at least one role or binding")
        cluster_roles = [r for r in roles if r["kind"] == "ClusterRole"]
        aggregation = _Aggregation(cluster_roles, "roles")
        findings: list[dict[str, Any]] = []
        risk: dict[str, str] = {}
        for role in roles:
            if role.get("apiVersion") == _REMOVED_RBAC_API:
                findings.append(_finding("rbac.api-removed", "blocker", _loc(role), f"{_REMOVED_RBAC_API} is removed since Kubernetes 1.22; use rbac.authorization.k8s.io/v1"))
            meta = _meta(role)
            label = _role_label(role["kind"], str(meta.get("namespace", "")), str(meta.get("name", "")))
            rules = aggregation.effective(role)
            found = [(index, rule_id, severity, text) for index, rule in enumerate(rules) for rule_id, severity, text in _rule_risks(rule)]
            risk[label] = _worst([severity for _, _, severity, _ in found])
            if not aggregation.uses_own_rules(role):
                continue  # judged through the roles it aggregates; findings are reported on those roles
            for index, rule_id, severity, text in found:
                findings.append(_finding(rule_id, severity, _loc(role), f"rules[{index}]: {text}"))
        for binding in bindings:
            location = _loc(binding)
            if binding.get("apiVersion") == _REMOVED_RBAC_API:
                findings.append(_finding("rbac.api-removed", "blocker", location, f"{_REMOVED_RBAC_API} is removed since Kubernetes 1.22; use rbac.authorization.k8s.io/v1"))
            ref_kind, ref_name = _role_ref(binding, "bindings")
            namespace = _binding_namespace(binding, "bindings")
            subjects = _subjects(binding, "bindings")
            cluster_wide = binding["kind"] == "ClusterRoleBinding"
            if ref_kind == "ClusterRole" and ref_name == "cluster-admin":
                findings.append(_finding("rbac.cluster-admin-binding", "blocker" if cluster_wide else "major", location,
                                         "binds cluster-admin" + ("" if cluster_wide else f" inside namespace {namespace}")))
            if cluster_wide and ref_kind == "ClusterRole" and ref_name in ("admin", "edit"):
                findings.append(_finding("rbac.broad-default-role-binding", "major", location,
                                         f"binds the default {ref_name} role cluster-wide: it can read Secrets and run pods as any service account in every namespace"))
            for subject in subjects:
                kind, name = subject.get("kind"), subject.get("name")
                if kind == "Group" and name == "system:masters":
                    findings.append(_finding("rbac.system-masters", "blocker", location, "binds the system:masters group, whose members get unrestricted access and bypass authorization, so no RBAC rule can narrow it"))
                if (kind == "User" and name == "system:anonymous") or (kind == "Group" and name == "system:unauthenticated"):
                    findings.append(_finding("rbac.anonymous-binding", "blocker", location, f"grants {ref_name} to unauthenticated requests ({kind} {name})"))
                if kind == "Group" and name == "system:authenticated":
                    findings.append(_finding("rbac.authenticated-binding", "major", location, f"grants {ref_name} to every authenticated identity"))
                if kind == "Group" and name == "system:serviceaccounts":
                    findings.append(_finding("rbac.all-serviceaccounts-binding", "major", location, f"grants {ref_name} to every service account in the cluster"))
                if kind == "ServiceAccount" and name == "default":
                    findings.append(_finding("rbac.default-sa-binding", "major" if cluster_wide else "minor", location,
                                             f"grants {ref_name} to the default service account of {subject.get('namespace', '?')}, which every pod without serviceAccountName uses"))
            target = _role_label(ref_kind, namespace, ref_name)
            risky = risk.get(target, "info")
            if SEVERITY_RANK[risky] >= SEVERITY_RANK["major"]:
                severity = risky if cluster_wide else "major"
                who = ", ".join(f"{s.get('kind')}:{s.get('name')}" for s in subjects[:5]) or "(no subjects)"
                findings.append(_finding("rbac.binding-to-risky-role", severity, location, f"{who} gets {target}, which carries {risky} rules"))
        return {"state": "SUCCESS", "role_count": len(roles), "binding_count": len(bindings), **_report(findings)}

    return respond(payload, _guarded(work))


# --- 3. effective access ----------------------------------------------------------------------------------------

MAX_PERMISSIONS = 200


def _identity(subject: dict[str, Any], extra_groups: list[str]) -> dict[str, Any]:
    kind, name, namespace = subject["kind"], subject["name"], subject.get("namespace", "")
    if kind == "ServiceAccount":
        if not namespace:
            raise _Bad("subject", "subject.namespace is required for a ServiceAccount")
        username = f"system:serviceaccount:{namespace}:{name}"
        groups = ["system:serviceaccounts", f"system:serviceaccounts:{namespace}", "system:authenticated"]
    elif kind == "User":
        username = name
        groups = ["system:unauthenticated"] if name == "system:anonymous" else ["system:authenticated"]
    else:
        username, groups = "", [name]
    return {"kind": kind, "name": name, "namespace": namespace, "username": username, "groups": sorted(set(groups) | set(extra_groups))}


def _subject_matches(entry: dict[str, Any], identity: dict[str, Any], binding_namespace: str) -> bool:
    """RBAC appliesToUser: a ServiceAccount subject without namespace means the namespace of its RoleBinding."""
    kind, name = entry.get("kind"), entry.get("name")
    if kind == "User":
        return bool(identity["username"]) and name == identity["username"]
    if kind == "Group":
        return name in identity["groups"]
    if kind == "ServiceAccount":
        namespace = entry.get("namespace") or binding_namespace
        return bool(namespace) and identity["username"] == f"system:serviceaccount:{namespace}:{name}"
    return False


def _resource_matches(rule_resources: list[str], resource: str, subresource: str) -> bool:
    """RBAC ResourceMatches: `*` matches everything including subresources, `*/sub` matches a subresource of any resource."""
    combined = f"{resource}/{subresource}" if subresource else resource
    for entry in rule_resources:
        if entry == "*" or entry == combined:
            return True
        if subresource and entry == f"*/{subresource}":
            return True
    return False


def _rule_allows(rule: dict[str, Any], verb: str, group: str, resource: str, subresource: str, name: str) -> bool:
    if "*" not in rule["verbs"] and verb not in rule["verbs"]:
        return False
    if "*" not in rule["apiGroups"] and group not in rule["apiGroups"]:
        return False
    if not _resource_matches(rule["resources"], resource, subresource):
        return False
    return not rule["resourceNames"] or (name != "" and name in rule["resourceNames"])


def _wildcard_rule() -> dict[str, Any]:
    return {"apiGroups": ["*"], "resources": ["*"], "resourceNames": [], "verbs": ["*"], "nonResourceURLs": ["*"]}


_BOOTSTRAP_CLUSTER_ADMIN = {"kind": "ClusterRole", "metadata": {"name": "cluster-admin"}, "rules": [_wildcard_rule()]}


def _is_admin_rule(rule: dict[str, Any]) -> bool:
    """Every verb on every resource of every API group, not narrowed by resourceNames."""
    return "*" in rule["verbs"] and "*" in rule["resources"] and "*" in rule["apiGroups"] and not rule["resourceNames"]


def rbac_effective_access(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve what one subject can do through RoleBindings, ClusterRoleBindings and aggregated ClusterRoles.

    Identity: a ServiceAccount is the user system:serviceaccount:<ns>:<name> in groups system:serviceaccounts,
    system:serviceaccounts:<ns> and system:authenticated; a User is in system:authenticated (system:anonymous in
    system:unauthenticated); `groups` adds more. A RoleBinding grants inside its own namespace only, even when it
    references a ClusterRole. Rule matching follows the RBAC authorizer: `*` matches any verb, API group or resource
    (subresources included), `*/sub` a subresource of any resource, and a rule with resourceNames only matches a request
    that names an object (so create, list and watch without a name are not granted by it). A ServiceAccount subject without a
    namespace means its RoleBinding's namespace. A binding to the cluster-admin ClusterRole that is not supplied is resolved
    to the API server's own definition (every verb on everything); membership of the system:masters group is
    cluster-admin whatever the bindings say, as that group bypasses authorization. Level `cluster-admin` needs a
    cluster-scoped rule on every verb, resource and API group that resourceNames do not narrow. Non-resource URLs are not modelled.
    """

    def work() -> dict[str, Any]:
        roles, bindings = _split_rbac(payload["roles"], payload["bindings"], "roles", "bindings")
        subject = payload["subject"]
        identity = _identity(subject, _strings(payload.get("groups")))
        cluster_roles = [r for r in roles if r["kind"] == "ClusterRole"]
        aggregation = _Aggregation(cluster_roles, "roles")
        index: dict[str, dict[str, Any]] = {}
        for supplied in roles:
            meta = _meta(supplied)
            index[_role_label(supplied["kind"], str(meta.get("namespace", "")), str(meta.get("name", "")))] = supplied
        grants: list[dict[str, Any]] = []
        roles_used: set[str] = set()
        unresolved: set[str] = set()
        matched = 0
        for binding in bindings:
            if not any(_subject_matches(entry, identity, _meta(binding).get("namespace", "")) for entry in _subjects(binding, "bindings")):
                continue
            matched += 1
            ref_kind, ref_name = _role_ref(binding, "bindings")
            namespace = _binding_namespace(binding, "bindings")
            label = _role_label(ref_kind, namespace, ref_name)
            role = index.get(label)
            if role is None and label == "ClusterRole/cluster-admin":
                role = _BOOTSTRAP_CLUSTER_ADMIN  # the API server's own definition (RBAC docs: super-user access to any action on any resource)
            if role is None:
                unresolved.add(label)
                continue
            roles_used.add(label)
            scope = "cluster" if binding["kind"] == "ClusterRoleBinding" else f"namespace:{namespace}"
            for rule in aggregation.effective(role):
                grants.append({"scope": scope, "role": label, "rule": rule})
        if "system:masters" in identity["groups"]:
            # the API server authorizes this group before RBAC runs (Kubernetes authorization docs: it bypasses authorization)
            grants.append({"scope": "cluster", "role": "Group/system:masters", "rule": _wildcard_rule()})
        cluster_grants = [g for g in grants if g["scope"] == "cluster"]
        if any(_is_admin_rule(g["rule"]) for g in cluster_grants):
            level = "cluster-admin"
        elif cluster_grants:
            level = "cluster-wide"
        elif grants:
            level = "namespaced"
        else:
            level = "none"
        checks = []
        for check in _list(payload.get("checks")):
            verb, group = check["verb"], check.get("api_group", "")
            resource, subresource = check["resource"], check.get("subresource", "")
            namespace, name = check.get("namespace", ""), check.get("resource_name", "")
            granted_by = ""
            for grant in sorted(grants, key=lambda g: (g["scope"], g["role"])):
                in_scope = grant["scope"] == "cluster" or (namespace != "" and grant["scope"] == f"namespace:{namespace}")
                if in_scope and _rule_allows(grant["rule"], verb, group, resource, subresource, name):
                    granted_by = f"{grant['role']} via {grant['scope']}"
                    break
            shown = f"{resource}/{subresource}" if subresource else resource
            checks.append({"verb": verb, "resource": shown, "api_group": group, "namespace": namespace, "resource_name": name,
                           "allowed": granted_by != "", "granted_by": granted_by})
        listed = sorted(
            ({"scope": g["scope"], "role": g["role"], "api_groups": sorted(set(g["rule"]["apiGroups"])),
              "resources": sorted(set(g["rule"]["resources"])), "verbs": sorted(set(g["rule"]["verbs"])),
              "resource_names": sorted(set(g["rule"]["resourceNames"]))} for g in grants),
            key=lambda p: (p["scope"], p["role"], p["resources"], p["verbs"], p["api_groups"], p["resource_names"]),
        )
        return {"state": "SUCCESS", "access_level": level, "binding_count": matched, "permission_count": len(listed),
                "roles": _capped_names(sorted(roles_used), 30_000), "unresolved_roles": _capped_names(sorted(unresolved), 30_000),
                "permissions": _within_budget(listed, MAX_PERMISSIONS, 60_000),
                "checks": checks}

    return respond(payload, _guarded(work))


# --- 4. admission policy lint -----------------------------------------------------------------------------------

_WEBHOOK_KINDS = ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration")
_VAP_ACTIONS = ("Deny", "Warn", "Audit")
# admissionregistration.k8s.io/v1beta1 webhook configurations stopped being served in Kubernetes 1.22 (deprecation guide).
_REMOVED_ADMISSION_API = "admissionregistration.k8s.io/v1beta1"


def _group(manifest: dict[str, Any]) -> str:
    api_version = manifest.get("apiVersion")
    return api_version.split("/")[0] if isinstance(api_version, str) and "/" in api_version else ""


def _exclusions(selector: object) -> list[str]:
    """matchExpressions of a label selector that remove objects from scope (NotIn, DoesNotExist)."""
    out = []
    for expression in _list(_dict(selector).get("matchExpressions")):
        expression = _dict(expression)
        if expression.get("operator") in ("NotIn", "DoesNotExist"):
            values = ",".join(_strings(expression.get("values")))
            out.append(f"{expression.get('key')} {expression.get('operator')}" + (f" [{values}]" if values else ""))
    return sorted(out)


def _lint_webhooks(manifest: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    location, kind = _loc(manifest), manifest["kind"]
    if manifest.get("apiVersion") == _REMOVED_ADMISSION_API:
        findings.append(_finding("admission.api-removed", "blocker", location, f"{_REMOVED_ADMISSION_API} is removed since Kubernetes 1.22; use admissionregistration.k8s.io/v1"))
        return
    for index, webhook in enumerate(_list(manifest.get("webhooks"))):
        webhook = _dict(webhook)
        name = webhook.get("name") if isinstance(webhook.get("name"), str) and webhook.get("name") else f"webhooks[{index}]"
        where = f"{location}#{name}"
        policy = webhook.get("failurePolicy")
        if policy is None:
            policy = "Fail"  # v1 default is Fail; a null field is unset, and the API server defaults it
        if policy == "Ignore":
            severity = "major" if kind == "ValidatingWebhookConfiguration" else "minor"
            findings.append(_finding("admission.fail-open", severity, where, "failurePolicy Ignore admits requests when the webhook is unreachable or times out"))
        elif policy != "Fail":
            findings.append(_finding("admission.invalid-failure-policy", "major", where, f"failurePolicy {_val(policy)} must be Fail or Ignore"))
        timeout = webhook.get("timeoutSeconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 30):
            findings.append(_finding("admission.timeout", "major", where, f"timeoutSeconds {_val(timeout)} must be an integer from 1 to 30 (default 10)"))
        if webhook.get("sideEffects") not in ("None", "NoneOnDryRun"):
            findings.append(_finding("admission.side-effects", "blocker", where, f"sideEffects {_val(webhook.get('sideEffects'))}: v1 only accepts None or NoneOnDryRun"))
        versions = webhook.get("admissionReviewVersions")
        if not isinstance(versions, list) or not versions:
            findings.append(_finding("admission.review-versions", "major", where, "admissionReviewVersions is required in v1 and must not be empty"))
        if webhook.get("matchPolicy") == "Exact":
            findings.append(_finding("admission.match-policy-exact", "minor", where, "matchPolicy Exact skips requests made through an equivalent API group or version; use Equivalent"))
        namespace_exclusions = _exclusions(webhook.get("namespaceSelector"))
        if namespace_exclusions:
            findings.append(_finding("admission.namespace-exclusion", "minor", where, "namespaceSelector excludes: " + "; ".join(namespace_exclusions)))
        object_exclusions = _exclusions(webhook.get("objectSelector"))
        if object_exclusions:
            findings.append(_finding("admission.object-selector-exclusion", "minor", where, "objectSelector lets objects opt out by label: " + "; ".join(object_exclusions)))


def _lint_vap(manifest: dict[str, Any], findings: list[dict[str, Any]]) -> None:
    location, spec = _loc(manifest), _dict(manifest.get("spec"))
    if spec.get("failurePolicy") == "Ignore":
        findings.append(_finding("admission.fail-open", "major", location, "failurePolicy Ignore admits requests when the policy cannot be evaluated"))
    constraints = _dict(spec.get("matchConstraints"))
    if _list(constraints.get("excludeResourceRules")):
        findings.append(_finding("admission.resource-exclusion", "minor", location, "matchConstraints.excludeResourceRules removes resources from scope"))
    exclusions = _exclusions(constraints.get("namespaceSelector"))
    if exclusions:
        findings.append(_finding("admission.namespace-exclusion", "minor", location, "namespaceSelector excludes: " + "; ".join(exclusions)))


def _identity_exclusions(rule: dict[str, Any]) -> tuple[list[str], bool]:
    """(identity-based exclusion kinds, excludes every namespace) over a Kyverno rule's exclude blocks."""
    exclude = _dict(rule.get("exclude"))
    blocks = [exclude] + [_dict(b) for key in ("any", "all") for b in _list(exclude.get(key))]
    kinds: set[str] = set()
    all_namespaces = False
    for block in blocks:
        for key in ("subjects", "roles", "clusterRoles"):
            if block.get(key):
                kinds.add(key)
        if "*" in _strings(_dict(block.get("resources")).get("namespaces")):
            all_namespaces = True
    return sorted(kinds), all_namespaces


def _lint_kyverno(manifest: dict[str, Any], findings: list[dict[str, Any]]) -> tuple[bool, bool]:
    """Lint a Kyverno policy. Returns (has enforce rule, has audit-only validate rule)."""
    location, spec = _loc(manifest), _dict(manifest.get("spec"))
    if spec.get("failurePolicy") == "Ignore":
        findings.append(_finding("admission.fail-open", "major", location, "failurePolicy Ignore admits requests when Kyverno cannot evaluate the policy"))
    default_action = spec.get("validationFailureAction")
    if default_action is None:
        default_action = "Audit"  # Kyverno's default; an explicit null is unset (CRD structural defaulting prunes a null)
    enforcing = audit = False
    for index, rule in enumerate(_list(spec.get("rules"))):
        rule = _dict(rule)
        name = rule.get("name") if isinstance(rule.get("name"), str) else f"rules[{index}]"
        identity, all_namespaces = _identity_exclusions(rule)
        if identity:
            findings.append(_finding("admission.identity-exclusion", "minor", f"{location}#{name}", "exclude lists " + ", ".join(identity) + ": those identities bypass the rule"))
        if all_namespaces:
            findings.append(_finding("admission.namespace-exclusion", "major", f"{location}#{name}", "exclude.resources.namespaces contains *: the rule never applies"))
        validate = rule.get("validate")
        if not isinstance(validate, dict):
            continue
        action = validate.get("failureAction")
        if action is None:
            action = default_action  # rule level overrides the policy default (Kyverno 1.13+); a null rule field is unset
        mode = action.lower() if isinstance(action, str) else ""
        if mode == "enforce":
            enforcing = True
            overrides = _list(spec.get("validationFailureActionOverrides")) + _list(validate.get("failureActionOverrides"))
            for override in overrides:
                if isinstance(override, dict) and str(override.get("action", "")).lower() == "audit":
                    findings.append(_finding("admission.enforce-overridden", "minor", f"{location}#{name}", "failureActionOverrides switches some namespaces to Audit"))
                    break
        elif mode == "audit":
            audit = True
            findings.append(_finding("admission.audit-only", "minor", f"{location}#{name}", "validate rule is in Audit mode: violations are reported, not blocked"))
        else:
            findings.append(_finding("admission.invalid-action", "major", f"{location}#{name}", f"failureAction {_val(action)} must be Audit or Enforce"))
    return enforcing, audit and not enforcing


def _admission_point(entry: dict[str, Any]) -> bool:
    """A scopedEnforcementActions entry acts on admission when it names validation.gatekeeper.sh (the webhook), vap.k8s.io
    (ValidatingAdmissionPolicy) or `*`; audit.gatekeeper.sh and gator.gatekeeper.sh never block a live request, and an entry
    with no enforcementPoints is unrecognised and enforced nowhere (Gatekeeper enforcement-points docs and source)."""
    points = {name for p in _list(entry.get("enforcementPoints")) if isinstance(name := _dict(p).get("name"), str)}
    return bool(points & {"validation.gatekeeper.sh", "vap.k8s.io", "*"})


def _lint_gatekeeper(manifest: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    """Lint a Gatekeeper constraint. Returns enforce | audit | other."""
    location, spec = _loc(manifest), _dict(manifest.get("spec"))
    action = spec.get("enforcementAction")
    if action is None:
        action = "deny"  # Gatekeeper default is deny; a null field is unset
    if action == "deny":
        return "enforce"
    if action in ("dryrun", "warn"):
        findings.append(_finding("admission.audit-only", "minor", location, f"enforcementAction {action} reports violations without denying"))
        return "audit"
    if action == "scoped":
        scoped = [_dict(e) for e in _list(spec.get("scopedEnforcementActions"))]
        if any(e.get("action") == "deny" and _admission_point(e) for e in scoped):
            return "enforce"
        findings.append(_finding("admission.audit-only", "minor", location, "no scopedEnforcementActions entry denies at an admission enforcement point (validation.gatekeeper.sh, vap.k8s.io or *)"))
        return "audit"
    findings.append(_finding("admission.invalid-action", "major", location, f"enforcementAction {_val(action)} must be deny, dryrun, warn or scoped"))
    return "other"


def admission_policy_lint(payload: dict[str, Any]) -> dict[str, Any]:
    """Lint admission control: webhook configurations, ValidatingAdmissionPolicy and bindings, Kyverno, Gatekeeper.

    Flags fail-open (failurePolicy Ignore), invalid v1 webhook fields (timeoutSeconds 1-30, sideEffects None or
    NoneOnDryRun, admissionReviewVersions), matchPolicy Exact, selector exclusions that let objects or namespaces opt out,
    audit-only modes (VAP bindings without Deny, Kyverno Audit, Gatekeeper dryrun or warn, or a `scoped` constraint that
    denies only at audit.gatekeeper.sh or gator.gatekeeper.sh), VAP bindings with both Deny and Warn (rejected by the API
    server), parameterNotFoundAction Allow, identity exclusions, and VAP policies and bindings that do not reference each
    other. Counts `enforcing` and `audit_only` policy units. Severities are this tool's house judgement. admissionregistration.k8s.io/v1beta1
    was removed in Kubernetes 1.22.
    """

    def work() -> dict[str, Any]:
        manifests = _manifests(payload["policies"], "policies")
        findings: list[dict[str, Any]] = []
        counted = enforcing = audit_only = 0
        vap_names: dict[str, str] = {}
        binding_refs: list[tuple[str, str]] = []
        for manifest in manifests:
            kind, group = manifest["kind"], _group(manifest)
            if group == "admissionregistration.k8s.io" and kind in _WEBHOOK_KINDS:
                counted += 1
                _lint_webhooks(manifest, findings)
                if kind == "ValidatingWebhookConfiguration" and _list(manifest.get("webhooks")):
                    enforcing += 1
            elif group == "admissionregistration.k8s.io" and kind == "ValidatingAdmissionPolicy":
                counted += 1
                _lint_vap(manifest, findings)
                vap_names[str(_meta(manifest).get("name", ""))] = _loc(manifest)
            elif group == "admissionregistration.k8s.io" and kind == "ValidatingAdmissionPolicyBinding":
                counted += 1
                spec, location = _dict(manifest.get("spec")), _loc(manifest)
                actions = spec.get("validationActions")
                if not isinstance(actions, list) or not actions or any(a not in _VAP_ACTIONS for a in actions):
                    findings.append(_finding("admission.invalid-action", "major", location, f"validationActions {_val(actions)} must be a non-empty list of Deny, Warn, Audit"))
                elif "Deny" in actions and "Warn" in actions:
                    findings.append(_finding("admission.invalid-action", "major", location, "validationActions must not contain both Deny and Warn: the API server rejects the binding"))
                elif "Deny" in actions:
                    enforcing += 1
                else:
                    audit_only += 1
                    findings.append(_finding("admission.audit-only", "minor", location, f"validationActions {','.join(sorted(actions))} never deny a request"))
                param_ref = _dict(spec.get("paramRef"))
                if param_ref.get("parameterNotFoundAction") == "Allow":
                    findings.append(_finding("admission.param-not-found-allow", "minor", location, "parameterNotFoundAction Allow admits requests when the parameter object is missing"))
                exclusions = _exclusions(_dict(spec.get("matchResources")).get("namespaceSelector"))
                if exclusions:
                    findings.append(_finding("admission.namespace-exclusion", "minor", location, "namespaceSelector excludes: " + "; ".join(exclusions)))
                binding_refs.append((str(spec.get("policyName", "")), location))
            elif group == "kyverno.io" and kind in ("ClusterPolicy", "Policy"):
                counted += 1
                has_enforce, has_audit_only = _lint_kyverno(manifest, findings)
                enforcing += 1 if has_enforce else 0
                audit_only += 1 if has_audit_only else 0
            elif group == "constraints.gatekeeper.sh":
                counted += 1
                mode = _lint_gatekeeper(manifest, findings)
                enforcing += 1 if mode == "enforce" else 0
                audit_only += 1 if mode == "audit" else 0
        referenced = {name for name, _ in binding_refs}
        for name, location in sorted(vap_names.items()):
            if name not in referenced:
                findings.append(_finding("admission.policy-unbound", "minor", location, "no supplied ValidatingAdmissionPolicyBinding references this policy, so it is inert"))
        for name, location in binding_refs:
            if name not in vap_names:
                findings.append(_finding("admission.binding-orphan", "minor", location, f"policyName {name!r} is not among the supplied ValidatingAdmissionPolicy objects"))
        return {"state": "SUCCESS", "policy_count": counted, "enforcing": enforcing, "audit_only": audit_only, **_report(findings)}

    return respond(payload, _guarded(work))


# --- 5. CIS benchmark scoring -----------------------------------------------------------------------------------

_CIS_ID = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){1,5}")


def _pct(value: Decimal) -> float:
    """A percentage rounded half up to two places (not the binary-float half-even of round())."""
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _exact(value: Decimal) -> str:
    """The percentage to six places without trailing zeros (at least one decimal), so a score just under the threshold never prints as the threshold."""
    text = format(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP).normalize(), "f")
    return text if "." in text else f"{text}.0"


def cis_score(payload: dict[str, Any]) -> dict[str, Any]:
    """Score CIS Kubernetes Benchmark results (kube-bench style PASS/FAIL/WARN/INFO per recommendation id).

    Only scored (Automated) recommendations count: score = PASS / (PASS + FAIL + WARN) as a percentage (reported rounded half
    up to two places; the threshold test uses the exact value), so a WARN (could not be determined) is not a pass. Unscored
    (Manual) results are reported but never lower the score. A section is the first two id components (for example 1.2). A
    failed scored Level 1 recommendation is a major finding, a failed Level 2 or an undetermined scored one is minor, and a
    score below `min_score_pct` is major; the verdict is fail on any major finding. Those severities and the Level 1 gate are
    this tool's policy: the CIS benchmark itself only defines the profiles and scored/unscored recommendations. Recommendation
    ids must be unique dotted numbers.
    """

    def work() -> dict[str, Any]:
        results = payload["results"]
        threshold = _dec(payload["min_score_pct"])
        seen: set[str] = set()
        counts = {"PASS": 0, "FAIL": 0, "WARN": 0}
        sections: dict[str, dict[str, int]] = {}
        manual = 0
        findings: list[dict[str, Any]] = []
        for index, item in enumerate(results):
            rec = item["id"]
            if not _CIS_ID.fullmatch(rec):
                raise _Bad("results", f"results[{index}].id {_val(rec)} must be dotted numbers such as 1.2.3")
            if rec in seen:
                raise _Bad("results", f"results[{index}].id {rec} is duplicated")
            seen.add(rec)
            status, scored, level = item["status"], item["scored"], item["level"]
            if not scored:
                manual += 1
                if status == "FAIL":
                    findings.append(_finding("cis.manual-fail", "info", rec, f"unscored (manual) recommendation failed (level {level})"))
                continue
            if status == "INFO":
                continue
            counts[status] += 1
            section = sections.setdefault(".".join(rec.split(".")[:2]), {"PASS": 0, "FAIL": 0, "WARN": 0})
            section[status] += 1
            if status == "FAIL":
                findings.append(_finding("cis.fail", "major" if level == 1 else "minor", rec, f"scored Level {level} recommendation failed"))
            elif status == "WARN":
                findings.append(_finding("cis.warn", "minor", rec, f"scored Level {level} recommendation could not be determined"))
        denominator = sum(counts.values())
        if denominator == 0:
            raise _Bad("results", "results need at least one scored PASS, FAIL or WARN recommendation")
        score = Decimal(counts["PASS"]) * 100 / Decimal(denominator)
        if score < threshold:
            findings.append(_finding("cis.score-below-threshold", "major", "", f"score {_exact(score)}% is below the required {payload['min_score_pct']}%"))
        listed = []
        for name in sorted(sections, key=lambda s: tuple(int(p) for p in s.split("."))):
            c = sections[name]
            listed.append({"section": name, "passed": c["PASS"], "failed": c["FAIL"], "warned": c["WARN"],
                           "score_pct": _pct(Decimal(c["PASS"]) * 100 / Decimal(sum(c.values())))})
        return {"state": "SUCCESS", "total": len(results), "scored_count": denominator, "manual_count": manual,
                "passed": counts["PASS"], "failed": counts["FAIL"], "warned": counts["WARN"],
                "score_pct": _pct(score), "sections": listed, **_report(findings)}

    return respond(payload, _guarded(work))


# --- 6. secret exposure -----------------------------------------------------------------------------------------

ENCRYPTION_PROVIDERS = ("identity", "aescbc", "aesgcm", "secretbox", "kms")
# Provider strength per the "Available providers" table of the encryption-at-rest page: aescbc weak (padding oracle),
# aesgcm must be rotated every 200,000 writes, secretbox strong, kms v1 deprecated since 1.28, kms v2 strongest.


def _secret_volumes(spec: dict[str, Any]) -> dict[str, tuple[str, object, list[Any]]]:
    """volume name -> (kind, defaultMode, per-item modes) for secret and secret-projecting volumes."""
    out: dict[str, tuple[str, object, list[Any]]] = {}
    for volume in _list(spec.get("volumes")):
        volume = _dict(volume)
        name = volume.get("name")
        if not isinstance(name, str):
            continue
        secret = volume.get("secret")
        if isinstance(secret, dict):
            modes = [_dict(i).get("mode") for i in _list(secret.get("items"))]
            out[name] = ("secret", secret.get("defaultMode"), modes)
            continue
        projected = _dict(volume.get("projected"))
        sources = [_dict(s) for s in _list(projected.get("sources"))]
        secret_sources = [_dict(s.get("secret")) for s in sources if isinstance(s.get("secret"), dict)]
        if secret_sources:
            modes = [_dict(i).get("mode") for s in secret_sources for i in _list(s.get("items"))]
            out[name] = ("projected", projected.get("defaultMode"), modes)
    return out


def _encryption_findings(config: dict[str, Any], findings: list[dict[str, Any]]) -> str:
    if config.get("kind") != "EncryptionConfiguration":
        raise _Bad("encryption_config", "encryption_config.kind must be EncryptionConfiguration")
    entries = config.get("resources")
    if not isinstance(entries, list) or not entries or any(not isinstance(e, dict) for e in entries):
        raise _Bad("encryption_config", "encryption_config.resources must be a non-empty list of objects")
    covering = [e for e in entries if {"secrets", "*.", "*.*"} & set(_strings(e.get("resources")))]
    chosen = covering[0] if covering else None
    if chosen is None:
        findings.append(_finding("secret.encryption-missing", "blocker", "", "no EncryptionConfiguration resources entry covers secrets: they are stored in etcd as plaintext"))
        return "unencrypted"
    providers = chosen.get("providers")
    if not isinstance(providers, list) or not providers or any(not isinstance(p, dict) or len(p) != 1 for p in providers):
        raise _Bad("encryption_config", "each resources entry needs providers: a non-empty list of single-key objects")
    names = []
    for provider in providers:
        name = next(iter(provider))
        if name not in ENCRYPTION_PROVIDERS:
            raise _Bad("encryption_config", f"provider {_val(name)} must be one of {', '.join(ENCRYPTION_PROVIDERS)}")
        names.append(name)
    first = names[0]
    status = "strong"
    if first == "identity":
        status = "unencrypted"
        findings.append(_finding("secret.encryption-identity-first", "blocker", "", "identity is the first provider, so new secrets are written in plaintext"))
    elif first == "aescbc":
        status = "weak"
        findings.append(_finding("secret.encryption-weak-provider", "minor", "", "aescbc is not recommended (CBC padding-oracle weakness); prefer kms v2 or secretbox"))
    elif first == "aesgcm":
        status = "conditional"
        findings.append(_finding("secret.encryption-rotation-required", "minor", "", "aesgcm must be rotated every 200,000 writes; use it only with automated key rotation"))
    elif first == "kms":
        kms = _dict(providers[0]["kms"])
        if kms.get("apiVersion") != "v2":
            status = "conditional"
            findings.append(_finding("secret.encryption-kms-v1", "minor", "", "kms v1 is deprecated since Kubernetes 1.28; use kms v2 (stable since 1.29)"))
    if first in ("aescbc", "aesgcm", "secretbox"):
        findings.append(_finding("secret.encryption-local-keys", "info", "", f"{first} keys are stored on the control-plane host; only a kms provider keeps the key-encryption key outside it"))
    if first != "identity" and "identity" in names[1:]:
        findings.append(_finding("secret.encryption-identity-fallback", "info", "", "identity after a real provider still reads plaintext secrets; remove it once all secrets are rewritten"))
    return status


def secret_exposure_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Check how workloads consume Secrets and whether Secrets are encrypted at rest.

    Per workload: secrets injected as environment variables (secretKeyRef, envFrom secretRef) are visible to child
    processes, /proc and crash dumps (minor); a secret volume mode (defaultMode or items[].mode) that grants other users
    read or write is major (secret.world-readable, secret.world-writable), an unset mode (default 0644) is info; a subPath mount of a secret never receives secret updates
    (minor). Optional `encryption_config` is an apiserver EncryptionConfiguration: the first resources entry naming `secrets` or a
    `*.` or `*.*` wildcard (a specific entry must precede a wildcard, as the docs require) is judged by its first provider; identity
    first or no covering entry is a blocker.
    """

    def work() -> dict[str, Any]:
        workloads = _workloads(payload["workloads"], "workloads")
        findings: list[dict[str, Any]] = []
        for workload in workloads:
            volumes = _secret_volumes(workload["spec"])
            for volume_name, (kind, default_mode, item_modes) in sorted(volumes.items()):
                location = f"{workload['loc']}#volume/{volume_name}"
                if default_mode is None:
                    findings.append(_finding("secret.default-mode-unset", "info", location, f"{kind} volume uses the default file mode 0644"))
                for mode in [default_mode, *item_modes]:
                    if isinstance(mode, int) and not isinstance(mode, bool):
                        if mode & 0o004:
                            findings.append(_finding("secret.world-readable", "major", location, f"file mode {mode & 0o777:04o} lets other users read the secret"))
                        if mode & 0o002:
                            findings.append(_finding("secret.world-writable", "major", location, f"file mode {mode & 0o777:04o} lets other users write the secret"))
            for section, name, container in _containers(workload, "workloads"):
                location = f"{workload['loc']}#{_cname(section, name)}"
                env_names, secret_names = [], set()
                for var in _list(container.get("env")):
                    ref = _dict(_dict(_dict(var).get("valueFrom")).get("secretKeyRef"))
                    if ref:
                        env_names.append(str(_dict(var).get("name", "")))
                        secret_names.add(str(ref.get("name", "")))
                if env_names:
                    findings.append(_finding("secret.env-secretkeyref", "minor", location,
                                             f"env {','.join(sorted(env_names))} read from secret {','.join(sorted(secret_names))}; mount a volume instead"))
                from_secrets = sorted({str(_dict(_dict(e).get("secretRef")).get("name", "")) for e in _list(container.get("envFrom")) if _dict(_dict(e).get("secretRef"))})
                if from_secrets:
                    findings.append(_finding("secret.env-from-secret", "minor", location, f"envFrom imports every key of secret {','.join(from_secrets)} as environment variables"))
                for mount in _list(container.get("volumeMounts")):
                    mount = _dict(mount)
                    if isinstance(mount.get("name"), str) and mount["name"] in volumes and (mount.get("subPath") or mount.get("subPathExpr")):
                        findings.append(_finding("secret.subpath-no-rotation", "minor", location, f"subPath mount of secret volume {mount.get('name')} never receives secret updates"))
        status = "not-provided"
        if payload.get("encryption_config") is not None:
            _check_size(payload["encryption_config"], "encryption_config")
            status = _encryption_findings(payload["encryption_config"], findings)
        return {"state": "SUCCESS", "workload_count": len(workloads), "encryption_status": status, **_report(findings)}

    return respond(payload, _guarded(work))


# --- 7. service-account tokens ----------------------------------------------------------------------------------

TOKEN_MIN_SECONDS = 600  # ServiceAccountTokenProjection.expirationSeconds minimum (10 minutes); API maximum is 2^32
TOKEN_MAX_SECONDS = 2**32
TOKEN_LONG_LIVED_SECONDS = 86400


def sa_token_audit(payload: dict[str, Any]) -> dict[str, Any]:
    """Audit service-account token exposure of workloads.

    Effective automount: pod spec.automountServiceAccountToken, else the ServiceAccount's, else true. A workload that
    does not need the API (not listed in `token_required`, by Kind/namespace/name or metadata.name) but still mounts a
    token is minor, major when it uses the shared default service account; one that needs the API on the default
    account is major. Projected serviceAccountToken volumes: expirationSeconds below 600 or above 2^32 is rejected by
    the API (blocker), above 86400 is long-lived (minor). Secrets of type kubernetes.io/service-account-token are
    non-expiring bearer tokens (major). A workload without serviceAccountName uses the namespace's `default` account.
    """

    def work() -> dict[str, Any]:
        workloads = _workloads(payload["workloads"], "workloads")
        accounts = _manifests(payload.get("service_accounts", []), "service_accounts")
        required = set(_strings(payload.get("token_required")))
        findings: list[dict[str, Any]] = []
        known: dict[tuple[str, str], object] = {}
        account_count = 0
        for account in accounts:
            meta = _meta(account)
            if account["kind"] == "ServiceAccount":
                account_count += 1
                known[(str(meta.get("namespace", "default")), str(meta.get("name", "")))] = account.get("automountServiceAccountToken")
            elif account["kind"] == "Secret" and account.get("type") == "kubernetes.io/service-account-token":
                findings.append(_finding("sa.legacy-token-secret", "major", _loc(account), "long-lived service-account token Secret never expires; use TokenRequest or projected tokens"))
        for workload in workloads:
            spec, meta = workload["spec"], _meta(workload["manifest"])
            namespace = str(meta.get("namespace", "default"))
            name = spec.get("serviceAccountName") or spec.get("serviceAccount") or "default"
            default = name == "default"
            declared = spec.get("automountServiceAccountToken")
            if not isinstance(declared, bool):
                declared = known.get((namespace, str(name)))
            automount = declared if isinstance(declared, bool) else True
            needs_api = workload["loc"] in required or (isinstance(meta.get("name"), str) and meta["name"] in required)
            if default:
                findings.append(_finding("sa.default-account", "minor", workload["loc"], "uses the namespace's default service account; create a dedicated one"))
            if needs_api and default:
                findings.append(_finding("sa.api-access-default-account", "major", workload["loc"], "needs API access but shares the default service account; bind roles to a dedicated account"))
            if automount and not needs_api:
                findings.append(_finding("sa.automount-enabled", "major" if default else "minor", workload["loc"],
                                         f"mounts a token for {name} without needing API access; set automountServiceAccountToken: false"))
            for volume in _list(spec.get("volumes")):
                for source in _list(_dict(_dict(volume).get("projected")).get("sources")):
                    token = _dict(_dict(source).get("serviceAccountToken"))
                    seconds = token.get("expirationSeconds")
                    if token and isinstance(seconds, int) and not isinstance(seconds, bool):
                        where = f"{workload['loc']}#volume/{_dict(volume).get('name', '(unnamed)')}"
                        if seconds < TOKEN_MIN_SECONDS or seconds > TOKEN_MAX_SECONDS:
                            findings.append(_finding("sa.token-expiry-invalid", "blocker", where, f"expirationSeconds {seconds} is outside {TOKEN_MIN_SECONDS}..{TOKEN_MAX_SECONDS}"))
                        elif seconds > TOKEN_LONG_LIVED_SECONDS:
                            findings.append(_finding("sa.token-long-lived", "minor", where, f"expirationSeconds {seconds} exceeds {TOKEN_LONG_LIVED_SECONDS} (one day)"))
        return {"state": "SUCCESS", "workload_count": len(workloads), "service_account_count": account_count, **_report(findings)}

    return respond(payload, _guarded(work))


# --- 8. image admission -----------------------------------------------------------------------------------------

# Digest grammar of the OCI image spec: algorithm:encoded. Only registered algorithms with their exact encoded length and
# lowercase hex are accepted (sha256 64, sha384 96, sha512 128 characters): a shorter or longer hex string is not a digest
# a runtime can pull by, so it must not count as pinned or as verified.
_DIGEST_LENGTHS = {"sha256": 64, "sha384": 96, "sha512": 128}
_DIGEST_RE = re.compile(r"(?P<algorithm>[a-z0-9]+(?:[+._-][a-z0-9]+)*):(?P<hex>[0-9a-z]+)")
_DOCKER_ALIASES = ("index.docker.io", "registry-1.docker.io")
# Docker reference grammar (distribution/reference): domain, path components and tag.
_DOMAIN_RE = re.compile(r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*|\[[0-9A-Fa-f:.]+\])(?::[0-9]+)?")
_PATH_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_REPO_RE = re.compile(rf"{_PATH_COMPONENT}(?:/{_PATH_COMPONENT})*")
_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")


def _valid_digest(digest: str) -> bool:
    match = _DIGEST_RE.fullmatch(digest)
    if match is None:
        return False
    length = _DIGEST_LENGTHS.get(match["algorithm"])
    return length is not None and len(match["hex"]) == length and all(ch in "0123456789abcdef" for ch in match["hex"])


def _registry_host(host: str) -> str:
    host = host.lower()
    return "docker.io" if host in _DOCKER_ALIASES else host


def _parse_image(ref: str) -> dict[str, Any]:
    """Split an image reference into registry, repository, tag and digest (Docker reference grammar).

    The first path component is a registry when it contains a dot or a colon, equals localhost or is not all lowercase;
    otherwise the image lives on docker.io, where a single-component name is under library/. A malformed reference raises
    ValueError with a fixed message: the value itself is never put into it, because a pasted reference can carry
    credentials (user:password@host/...).
    """
    if not ref or any(ch.isspace() for ch in ref):
        raise ValueError("empty or contains whitespace (value withheld)")
    name, at, digest = ref.partition("@")
    if at and not _valid_digest(digest):
        raise ValueError("the part after @ is not a sha256, sha384 or sha512 digest of the exact length with lowercase hex (value withheld)")
    last = name.rsplit("/", 1)[-1]
    tag = ""
    if ":" in last:
        tag = last.rsplit(":", 1)[1]
        name = name[: len(name) - len(tag) - 1]
        if not _TAG_RE.fullmatch(tag):
            raise ValueError("the tag is empty or not a valid tag (value withheld)")
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost" or first.lower() != first):
        if not _DOMAIN_RE.fullmatch(first):
            raise ValueError("the registry host is not a valid domain (value withheld)")
        registry, repo = _registry_host(first), rest
    else:
        registry, repo = "docker.io", name
    if not _REPO_RE.fullmatch(repo):
        raise ValueError("the repository path is empty or not a valid lowercase path (value withheld)")
    if registry == "docker.io" and "/" not in repo:
        repo = f"library/{repo}"
    return {"registry": registry, "repo": repo, "tag": tag, "digest": digest}


def image_admission_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Check workload images against an allowed-registry list, digest pinning and signature evidence.

    An allowed entry matches at a path boundary: ghcr.io/acme allows ghcr.io/acme/app but not ghcr.io/acmeevil/app.
    Docker Hub names are normalised (nginx is docker.io/library/nginx; index.docker.io is docker.io). A reference without
    @digest is mutable (a missing tag or latest even more so): minor, major with require_digest. A digest counts only when
    it is a sha256 (64), sha384 (96) or sha512 (128) lowercase hex string; a truncated digest cannot be pulled by and is
    refused. With require_signature an image passes only when its digest is in `verified_digests` (digests the caller
    verified, for example with cosign); a tag-only reference cannot carry signature evidence. Registry mismatch is major.
    A malformed reference is refused without echoing it (it may carry credentials).
    """

    def work() -> dict[str, Any]:
        workloads = _workloads(payload["workloads"], "workloads")
        allowed = []
        for index, entry in enumerate(payload["allowed_registries"]):
            text = entry.strip().rstrip("/")
            if not text or any(ch.isspace() for ch in entry) or "*" in entry or "@" in entry or "://" in entry:
                raise _Bad("allowed_registries", f"allowed_registries[{index}] must be a registry host or host/path prefix without spaces, *, @ or a scheme")
            host, _, path = text.partition("/")
            allowed.append(_registry_host(host) + (f"/{path.lower()}" if path else ""))
        verified = set()
        for index, raw_digest in enumerate(_strings(payload.get("verified_digests"))):
            digest = raw_digest.lower()  # hex is case-insensitive; a runtime only pulls by the lowercase form
            if not _valid_digest(digest):
                raise _Bad("verified_digests", f"verified_digests[{index}] must be sha256:<64 hex> (or sha384/sha512 of the exact length)")
            verified.add(digest)
        require_digest, require_signature = payload["require_digest"], payload["require_signature"]
        findings: list[dict[str, Any]] = []
        distinct: set[str] = set()
        for workload in workloads:
            for section, name, container in _containers(workload, "workloads"):
                ref = container.get("image")
                if not isinstance(ref, str):
                    raise _Bad("workloads", f"{workload['loc']} {_cname(section, name)} has no image string")
                try:
                    image = _parse_image(ref)
                except ValueError as exc:
                    raise _Bad("workloads", f"{workload['loc']} {_cname(section, name)} image is not a valid reference: {exc}") from exc
                distinct.add(ref)
                location = f"{workload['loc']}#{_cname(section, name)}"
                full = f"{image['registry']}/{image['repo']}"
                if not any(full == entry or full.startswith(entry + "/") for entry in allowed):
                    findings.append(_finding("image.registry-not-allowed", "major", location, f"{ref} is hosted at {image['registry']}, outside the allowed registries"))
                if not image["digest"]:
                    kind = "has no tag and no digest" if not image["tag"] else ("uses the mutable tag latest" if image["tag"] == "latest" else f"is pinned by tag {image['tag']} only")
                    findings.append(_finding("image.not-pinned", "major" if require_digest else "minor", location, f"{ref} {kind}; pin it with @sha256:..."))
                if require_signature and image["digest"] not in verified:
                    reason = "a tag-only reference cannot carry signature evidence" if not image["digest"] else "its digest is not in verified_digests"
                    findings.append(_finding("image.unsigned", "major", location, f"{ref} has no verified signature: {reason}"))
        return {"state": "SUCCESS", "workload_count": len(workloads), "image_count": len(distinct), **_report(findings)}

    return respond(payload, _guarded(work))


# --- 9. runtime detection rule lint (Falco) ---------------------------------------------------------------------

# priority values accepted by Falco, case-insensitive: the eight names of the docs plus the legacy alias `info` for
# informational (falco_common.cpp parse_priority). The first eight are what the error text lists.
FALCO_PRIORITIES = ("emergency", "alert", "critical", "error", "warning", "notice", "informational", "debug", "info")
_FALCO_HIGH = ("emergency", "alert", "critical", "error")
_FALCO_WORDS = frozenset({
    "and", "or", "not", "in", "pmatch", "intersects", "contains", "icontains", "bcontains", "startswith", "bstartswith",
    "endswith", "glob", "iglob", "exists", "true", "false",
})
_QUOTED = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'")
_LIST_OPERAND = re.compile(r"\b(?:in|pmatch|intersects)\s*\([^)]*\)")
_VALUE_OPERAND = re.compile(
    r"(?:==|!=|<=|>=|=|<|>|\b(?:contains|icontains|bcontains|startswith|bstartswith|endswith|glob|iglob)\b)\s*[^\s()]+"
)
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.\[\]]*")


def _condition_fields(condition: str, macros: dict[str, str]) -> tuple[set[str], bool]:
    """(filter fields used, True when a bare word could not be resolved to a supplied macro).

    Quoted strings, `in (...)` lists and operator operands are values, not macro references. A word that is not a
    supplied macro may be a macro defined elsewhere, so the caller must not conclude anything about missing fields.
    """
    budget = [MAX_MACRO_EXPANSIONS]  # expansions left for this call: a cyclic macro graph stops here instead of exponential work
    fields, unresolved, _ = _expand_condition(condition, macros, frozenset(), {}, budget)
    return fields, unresolved


def _expand_condition(condition: str, macros: dict[str, str], seen: frozenset[str],
                      done: dict[str, tuple[set[str], bool]], budget: list[int]) -> tuple[set[str], bool, bool]:
    """`_condition_fields` with its expansion state: (fields, unresolved, cut), where cut is True when a macro already on the
    expansion path (`seen`) was named. A macro expanded without a cut has no cycle below it, so its result does not depend on
    the path that reached it and is kept in `done`: a macro that names another macro twice is expanded once, not once per path."""
    text = _VALUE_OPERAND.sub(" ", _LIST_OPERAND.sub(" ", _QUOTED.sub('""', condition)))
    fields: set[str] = set()
    unresolved = cut = False
    for token in _TOKEN.findall(text):
        if "." in token:
            fields.add(token.split("[")[0])
        elif token.lower() in _FALCO_WORDS:
            continue
        elif token in macros:
            if token in seen:
                cut = True
            elif token in done:
                inner, inner_unresolved = done[token]
                fields |= inner
                unresolved = unresolved or inner_unresolved
            else:
                budget[0] -= 1
                if budget[0] < 0:
                    raise _Bad("rules", f"macro expansion exceeds {MAX_MACRO_EXPANSIONS} steps: the macros are cyclic or too many")
                inner, inner_unresolved, inner_cut = _expand_condition(macros[token], macros, seen | {token}, done, budget)
                fields |= inner
                unresolved = unresolved or inner_unresolved
                cut = cut or inner_cut
                if not inner_cut:
                    done[token] = (inner, inner_unresolved)
        else:
            unresolved = True
    return fields, unresolved, cut


def _lint_exceptions(rule: dict[str, Any], name: str, append: bool, findings: list[dict[str, Any]]) -> None:
    exceptions = rule.get("exceptions")
    if exceptions is None:
        return
    if not isinstance(exceptions, list):
        findings.append(_finding("falco.exception-shape", "blocker", name, "exceptions must be a list"))
        return
    names: set[str] = set()
    for index, exception in enumerate(exceptions):
        exception = _dict(exception)
        label = f"exceptions[{index}]"
        ename = exception.get("name")
        if not isinstance(ename, str) or not ename:
            findings.append(_finding("falco.exception-shape", "blocker", name, f"{label} needs a name"))
        elif ename in names:
            findings.append(_finding("falco.exception-shape", "blocker", name, f"{label} repeats the exception name {ename}"))
        else:
            names.add(ename)
        fields, comps, values = exception.get("fields"), exception.get("comps"), exception.get("values")
        if fields is None:
            if not append:
                findings.append(_finding("falco.exception-shape", "blocker", name, f"{label} needs fields"))
            continue
        if isinstance(fields, list) and isinstance(comps, list) and len(fields) != len(comps):
            findings.append(_finding("falco.exception-shape", "blocker", name, f"{label} has {len(fields)} fields but {len(comps)} comps"))
        if isinstance(fields, list) and len(fields) > 1 and isinstance(values, list):
            for value in values:
                if not isinstance(value, list) or len(value) != len(fields):
                    findings.append(_finding("falco.exception-shape", "blocker", name, f"{label} values must be lists of {len(fields)} entries, one per field"))
                    break


def runtime_rule_lint(payload: dict[str, Any]) -> dict[str, Any]:
    """Lint Falco rules (the parsed items of a rules file) for loadability, exceptions, scope and alert noise.

    Items are rules (key `rule`), macros (key `macro`; `append: true` extends an earlier macro) and ignored list or
    required_engine_version items. A rule that is not an append/override fragment needs rule, desc, condition, output and
    priority; priority must be one of emergency, alert, critical, error, warning, notice, informational, debug (any case; `info` is accepted as a legacy alias of informational).
    Exceptions need a unique name, fields, and matching comps and tuple lengths. A syscall rule whose fully macro-resolved
    condition never mentions evt.type is evaluated against every event (minor, skipped when a bare word could be an unseen
    macro or warn_evttypes is false). A disabled rule of priority error or worse is minor. Duplicate rule names are minor.
    With `alert_counts` over `window_hours`, a rate above `max_alerts_per_hour` is noisy: minor, major beyond 10 times it.
    """

    def work() -> dict[str, Any]:
        items = payload["rules"]
        _check_size(items, "rules")
        macros: dict[str, str] = {}
        rules: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise _Bad("rules", f"rules[{index}] must be an object")
            if "rule" in item:
                if not isinstance(item["rule"], str) or not item["rule"]:
                    raise _Bad("rules", f"rules[{index}].rule must be a non-empty string")
                rules.append(item)
            elif "macro" in item:
                if not isinstance(item["macro"], str) or not isinstance(item.get("condition", ""), str):
                    raise _Bad("rules", f"rules[{index}] macro needs a string name and condition")
                joined = f"{macros.get(item['macro'], '')} {item.get('condition', '')}" if item.get("append") is True else item.get("condition", "")
                macros[item["macro"]] = joined
            elif not ({"list", "required_engine_version", "required_plugin_versions"} & set(item)):
                raise _Bad("rules", f"rules[{index}] is neither a rule, a macro, a list nor a version requirement")
        findings: list[dict[str, Any]] = []
        seen: dict[str, int] = {}
        for rule in rules:
            name = rule["rule"]
            append = rule.get("append") is True or isinstance(rule.get("override"), dict)
            if not append:
                seen[name] = seen.get(name, 0) + 1
                for key in ("desc", "condition", "output", "priority"):
                    if not isinstance(rule.get(key), str) or not rule[key].strip():
                        findings.append(_finding("falco.missing-field", "blocker", name, f"{key} is required"))
            priority = rule.get("priority")
            if priority is not None and not (isinstance(priority, str) and priority.lower() in FALCO_PRIORITIES):
                findings.append(_finding("falco.invalid-priority", "blocker", name, f"priority {_val(priority)} must be one of {', '.join(FALCO_PRIORITIES[:8])}"))
            elif rule.get("enabled") is False and isinstance(priority, str) and priority.lower() in _FALCO_HIGH:
                findings.append(_finding("falco.disabled-detection", "minor", name, f"a {priority.lower()} priority rule is disabled"))
            _lint_exceptions(rule, name, append, findings)
            condition = rule.get("condition")
            if isinstance(condition, str) and rule.get("source", "syscall") == "syscall" and rule.get("warn_evttypes") is not False and not append:
                fields, unresolved = _condition_fields(condition, macros)
                if not unresolved and not ({"evt.type", "syscall.type"} & fields):
                    findings.append(_finding("falco.no-evt-type", "minor", name, "condition never mentions evt.type, so every syscall event is evaluated against the rule"))
        for name, count in sorted(seen.items()):
            if count > 1:
                findings.append(_finding("falco.duplicate-rule", "minor", name, f"rule name defined {count} times without append or override"))
        noisy: list[str] = []
        counts = payload.get("alert_counts")
        if counts is not None:
            if payload.get("window_hours") is None:
                raise _Bad("window_hours", "window_hours is required with alert_counts")
            if payload.get("max_alerts_per_hour") is None:
                raise _Bad("max_alerts_per_hour", "max_alerts_per_hour is required with alert_counts")
            window, limit = _dec(payload["window_hours"]), _dec(payload["max_alerts_per_hour"])
            rule_names = {r["rule"] for r in rules}
            counted: set[str] = set()
            for index, entry in enumerate(counts):
                if entry["rule"] not in rule_names:
                    raise _Bad("alert_counts", f"alert_counts[{index}].rule {entry['rule']!r} is not among the rules")
                if entry["rule"] in counted:
                    raise _Bad("alert_counts", f"alert_counts[{index}].rule {entry['rule']!r} is listed twice")
                counted.add(entry["rule"])
                rate = Decimal(entry["count"]) / window
                if rate > limit:
                    noisy.append(entry["rule"])
                    severity = "major" if rate > limit * 10 else "minor"
                    shown = str(_pct(rate))
                    if Decimal(shown) <= limit:  # two places would print a rate that does not look above the limit
                        shown = _exact(rate)
                    findings.append(_finding("falco.noisy", severity, entry["rule"], f"{shown} alerts/hour exceeds {payload['max_alerts_per_hour']}"))
        return {"state": "SUCCESS", "rule_count": len(rules), "macro_count": len(macros), "noisy_rules": _capped_names(sorted(noisy), 60_000), **_report(findings)}

    return respond(payload, _guarded(work))


# --- 10. tenant isolation ---------------------------------------------------------------------------------------

PSA_LABEL = "pod-security.kubernetes.io/"
PSA_LEVELS = {"privileged": 0, "baseline": 1, "restricted": 2}


def _empty_selector(selector: object) -> bool:
    selector = _dict(selector)
    return not selector.get("matchLabels") and not _list(selector.get("matchExpressions"))


def _policy_types(spec: dict[str, Any]) -> set[str]:
    """spec.policyTypes; when omitted Ingress is always set and Egress only if the policy has egress rules."""
    declared = {t for t in _list(spec.get("policyTypes")) if t in ("Ingress", "Egress")}
    if _list(spec.get("policyTypes")):
        return declared
    return {"Ingress", "Egress"} if _list(spec.get("egress")) else {"Ingress"}


def _matches_all_peers(rule: object, side: str) -> bool:
    """A NetworkPolicy rule with no from/to matches every peer; ports, when named, only narrow the ports it admits."""
    return not _list(_dict(rule).get(side))


def tenant_isolation_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Check that tenant namespaces are isolated from each other.

    Per tenant namespace: a Namespace with pod-security.kubernetes.io/enforce at or above `required_psa_level`
    (privileged < baseline < restricted; unpinned enforce-version and missing warn/audit are info); a ResourceQuota
    bounding cpu and memory (requests.*, limits.* or the bare names) and, as minor, pods; a LimitRange (minor); NetworkPolicy
    default-deny for ingress (major) and egress (minor). Policies are additive, so an allow-all rule on an empty
    podSelector cancels a default-deny; an ingress peer whose namespaceSelector is {} admits every namespace (major).
    spec.policyTypes defaults to Ingress, plus Egress only when egress rules exist. RoleBindings that grant to service
    accounts of another tenant namespace, to system:serviceaccounts or to system:serviceaccounts:<other tenant> are major.
    A tenant is `isolated` when it has no major or blocker finding.
    """

    def work() -> dict[str, Any]:
        tenants = sorted(set(payload["tenant_namespaces"]))
        required = payload["required_psa_level"]
        manifests = _manifests(payload["manifests"], "manifests")
        findings: list[dict[str, Any]] = []
        failed: set[str] = set()

        def add(namespace: str, rule: str, severity: str, location: str, summary: str) -> None:
            findings.append(_finding(rule, severity, location, summary))
            if SEVERITY_RANK[severity] >= SEVERITY_RANK["major"]:
                failed.add(namespace)

        def in_namespace(kind: str, namespace: str) -> list[dict[str, Any]]:
            return [m for m in manifests if m["kind"] == kind and str(_meta(m).get("namespace", "default")) == namespace]

        for namespace in tenants:
            home = f"Namespace/{namespace}"
            declared = [m for m in manifests if m["kind"] == "Namespace" and _meta(m).get("name") == namespace]
            if not declared:
                add(namespace, "tenant.namespace-missing", "major", home, "no Namespace manifest supplied, so the PSA labels cannot be verified")
            else:
                labels = _dict(_meta(declared[0]).get("labels"))
                enforce = labels.get(PSA_LABEL + "enforce")
                if enforce is None:
                    add(namespace, "tenant.psa-enforce-missing", "major", home, "no pod-security.kubernetes.io/enforce label: the namespace is unrestricted")
                elif not isinstance(enforce, str) or enforce not in PSA_LEVELS:
                    add(namespace, "tenant.psa-enforce-invalid", "major", home, f"enforce label {_val(enforce)} must be privileged, baseline or restricted")
                else:
                    if PSA_LEVELS[enforce] < PSA_LEVELS[required]:
                        add(namespace, "tenant.psa-enforce-weak", "major", home, f"enforce is {enforce}; {required} is required")
                    if labels.get(PSA_LABEL + "enforce-version") in (None, "latest"):
                        add(namespace, "tenant.psa-version-unpinned", "info", home, "enforce-version is unset or latest: the standard moves with the cluster version")
                if PSA_LABEL + "warn" not in labels or PSA_LABEL + "audit" not in labels:
                    add(namespace, "tenant.psa-warn-audit-missing", "info", home, "warn and audit labels are not both set, so violations of a stricter level go unnoticed")
            hard: set[str] = set()
            quotas = in_namespace("ResourceQuota", namespace)
            for quota in quotas:
                hard |= set(_dict(_dict(quota.get("spec")).get("hard")))
            if not quotas:
                add(namespace, "tenant.quota-missing", "major", home, "no ResourceQuota: one tenant can exhaust node capacity")
            else:
                missing = [label for label, keys in (("cpu", {"cpu", "requests.cpu", "limits.cpu"}), ("memory", {"memory", "requests.memory", "limits.memory"})) if not hard & keys]
                if missing:
                    add(namespace, "tenant.quota-incomplete", "major", home, f"ResourceQuota does not bound {' or '.join(missing)}")
                if "pods" not in hard:
                    add(namespace, "tenant.quota-no-pod-count", "minor", home, "ResourceQuota sets no pods limit")
            if not in_namespace("LimitRange", namespace):
                add(namespace, "tenant.limitrange-missing", "minor", home, "no LimitRange: pods without requests are rejected under a compute quota and nothing sets defaults")
            ingress_deny = egress_deny = allow_all_ingress = allow_all_egress = False
            for policy in in_namespace("NetworkPolicy", namespace):
                spec, location = _dict(policy.get("spec")), _loc(policy)
                types, empty = _policy_types(spec), _empty_selector(spec.get("podSelector"))
                ingress, egress = _list(spec.get("ingress")), _list(spec.get("egress"))
                if empty and "Ingress" in types and not ingress:
                    ingress_deny = True
                if empty and "Egress" in types and not egress:
                    egress_deny = True
                if empty and "Ingress" in types and any(_matches_all_peers(r, "from") for r in ingress):
                    allow_all_ingress = True
                    add(namespace, "tenant.netpol-allow-all-ingress", "major", location, "ingress with no from admits every source (on every port unless ports are named) and cancels any default-deny (policies are additive)")
                if empty and "Egress" in types and any(_matches_all_peers(r, "to") for r in egress):
                    allow_all_egress = True
                if "Ingress" in types:
                    for rule in ingress:
                        for peer in _list(_dict(rule).get("from")):
                            peer = _dict(peer)
                            if "namespaceSelector" in peer and _empty_selector(peer["namespaceSelector"]):
                                add(namespace, "tenant.netpol-all-namespaces", "major", location, "an ingress peer with namespaceSelector {} admits pods from every namespace")
            if not ingress_deny or allow_all_ingress:
                add(namespace, "tenant.ingress-default-deny-missing", "major", home, "no effective default-deny ingress NetworkPolicy (empty podSelector, no ingress rules)")
            if not egress_deny or allow_all_egress:
                add(namespace, "tenant.egress-default-deny-missing", "minor", home, "no effective default-deny egress NetworkPolicy")
            for binding in in_namespace("RoleBinding", namespace):
                for subject in _subjects(binding, "manifests"):
                    kind, name, sub_ns = subject.get("kind"), subject.get("name"), subject.get("namespace")
                    cross = (kind == "ServiceAccount" and sub_ns in tenants and sub_ns != namespace) or \
                            (kind == "Group" and isinstance(name, str) and name.startswith("system:serviceaccounts:") and name.split(":", 2)[2] in tenants and name.split(":", 2)[2] != namespace)
                    if cross or (kind == "Group" and name == "system:serviceaccounts"):
                        add(namespace, "tenant.cross-tenant-binding", "major", _loc(binding), f"grants access to {kind} {name}" + (f" of namespace {sub_ns}" if sub_ns else "") + " outside this tenant")
        return {"state": "SUCCESS", "tenant_count": len(tenants), "isolated_count": len(tenants) - len(failed), **_report(findings)}

    return respond(payload, _guarded(work))


# --- 11. audit policy completeness ------------------------------------------------------------------------------

AUDIT_LEVELS = {"None": 0, "Metadata": 1, "Request": 2, "RequestResponse": 3}
AUDIT_STAGES = ("RequestReceived", "ResponseStarted", "ResponseComplete", "Panic")
PROBE_USER = "kpol-probe-user"
PROBE_GROUPS = ("system:authenticated",)
# (probe id, API group, [(resource, subresource, namespace)], verbs, minimum level, maximum level, under severity, over severity, why)
_WRITE_VERBS = ("create", "update", "patch", "delete")
# per-(probe, verb) maximum that overrides the probe maximum: reads of secrets carry no request body, so Request is safe there
_AUDIT_VERB_MAX = {("secrets", "get"): "Request", ("secrets", "list"): "Request", ("secrets", "watch"): "Request"}
AUDIT_PROBES = (
    ("secrets", "", [("secrets", "", "default")], ("get", "list", "watch", "create", "update", "patch", "delete"), "Metadata", "Metadata", "major", "blocker",
     "secret writes must be audited at Metadata (the request body carries the secret data) and reads at Metadata or Request (only RequestResponse logs the secrets returned)"),
    ("service-account-tokens", "", [("serviceaccounts", "token", "default")], ("create",), "Metadata", "Request", "major", "blocker",
     "the TokenRequest response carries the token, so RequestResponse must not be logged"),
    ("token-reviews", "authentication.k8s.io", [("tokenreviews", "", "")], ("create",), "None", "Metadata", "major", "major",
     "TokenReview request bodies carry bearer tokens"),
    ("rbac-writes", "rbac.authorization.k8s.io",
     [("roles", "", "default"), ("rolebindings", "", "default"), ("clusterroles", "", ""), ("clusterrolebindings", "", "")], _WRITE_VERBS, "Request", "RequestResponse",
     "major", "major", "RBAC changes need the request body to show what was granted"),
    ("pod-exec", "", [("pods", "exec", "default"), ("pods", "attach", "default"), ("pods", "portforward", "default")], ("create", "get"), "Metadata", "RequestResponse",
     "major", "major", "interactive access to pods must leave a trace (the command is in the request URI)"),
    ("pod-changes", "", [("pods", "", "default")], _WRITE_VERBS, "Metadata", "RequestResponse", "minor", "minor", "workload changes should be traceable"),
    ("admission-config", "admissionregistration.k8s.io",
     [("validatingwebhookconfigurations", "", ""), ("mutatingwebhookconfigurations", "", ""), ("validatingadmissionpolicies", "", ""),
      ("validatingadmissionpolicybindings", "", "")], _WRITE_VERBS, "Request", "RequestResponse", "major", "major",
     "changing admission control can disable every other policy"),
    ("namespace-lifecycle", "", [("namespaces", "", "")], ("create", "delete"), "Metadata", "RequestResponse", "minor", "minor", "tenant boundaries are created and removed here"),
)


def _audit_list(rule: dict[str, Any], key: str, index: int) -> list[str]:
    value = rule.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise _Bad("policy", f"policy.rules[{index}].{key} must be a list of strings")
    return value


def _audit_rule(rule: object, index: int) -> dict[str, Any]:
    if not isinstance(rule, dict) or not isinstance(rule.get("level"), str) or rule["level"] not in AUDIT_LEVELS:
        raise _Bad("policy", f"policy.rules[{index}].level must be one of {', '.join(AUDIT_LEVELS)}")
    raw_resources = rule.get("resources")
    if raw_resources is not None and not isinstance(raw_resources, list):
        raise _Bad("policy", f"policy.rules[{index}].resources must be a list")
    resources = []
    for entry in raw_resources or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("group", ""), str):
            raise _Bad("policy", f"policy.rules[{index}].resources entries must be objects with a string group")
        resources.append({"group": entry.get("group", ""), "resources": _audit_list(entry, "resources", index),
                          "names": _audit_list(entry, "resourceNames", index)})
    stages = _audit_list(rule, "omitStages", index)
    if any(s not in AUDIT_STAGES for s in stages):
        raise _Bad("policy", f"policy.rules[{index}].omitStages entries must be one of {', '.join(AUDIT_STAGES)}")
    return {"level": rule["level"], "users": _audit_list(rule, "users", index), "groups": _audit_list(rule, "userGroups", index),
            "verbs": _audit_list(rule, "verbs", index), "namespaces": _audit_list(rule, "namespaces", index),
            "urls": _audit_list(rule, "nonResourceURLs", index), "resources": resources, "stages": stages}


def _audit_matches(rule: dict[str, Any], verb: str, group: str, resource: str, subresource: str, namespace: str) -> bool:
    """Policy rule match for a resource request by the probe user, following the API server's rule checker."""
    if rule["users"] and PROBE_USER not in rule["users"]:
        return False
    if rule["groups"] and not set(PROBE_GROUPS) & set(rule["groups"]):
        return False
    if rule["verbs"] and verb not in rule["verbs"]:
        return False
    if rule["namespaces"] or rule["resources"]:
        if rule["namespaces"] and namespace not in rule["namespaces"]:
            return False
        if not rule["resources"]:
            return True
        combined = f"{resource}/{subresource}" if subresource else resource
        for entry in rule["resources"]:
            if entry["group"] not in (group, "*"):
                continue
            if entry["names"]:
                continue  # the probes name no object, so a resourceNames restriction never matches them
            if not entry["resources"]:
                return True
            for name in entry["resources"]:
                if name in ("*", combined):
                    return True
                if subresource and name == f"*/{subresource}":
                    return True
                if name.endswith("/*") and name[:-2] == resource:
                    return True
        return False
    return not rule["urls"]  # a non-resource rule never matches a resource request


def _audit_level(rules: list[dict[str, Any]], verb: str, group: str, resource: str, subresource: str, namespace: str) -> tuple[str, int]:
    for index, rule in enumerate(rules):
        if _audit_matches(rule, verb, group, resource, subresource, namespace):
            return rule["level"], index
    return "None", -1  # the API server default when no rule matches


def audit_policy_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Check an audit.k8s.io/v1 Policy for completeness by simulating the API server's first-match rule evaluation.

    Eight probe request families are evaluated as the user kpol-probe-user in system:authenticated (rules naming other
    users or groups never shadow them): secrets writes must be exactly Metadata (the body carries the secret) and secrets
    reads at most Request, serviceaccounts/token at most Request (only RequestResponse logs the token), tokenreviews at
    most Metadata (the request body carries the bearer token), RBAC and admission-configuration writes at least Request,
    pod exec/attach/portforward and pod and namespace changes at least Metadata. The minimum and maximum levels are this
    tool's house policy, derived from what each level logs, not a Kubernetes requirement. A request that matches no rule
    is not audited (level None, the API server default). Also: no catch-all rule (minor), rules after a selector-less rule are unreachable (minor), nothing logged at
    all (blocker), RequestReceived stage not omitted and omitManagedFields unset on a Request/RequestResponse policy (info).
    Rule semantics: https://kubernetes.io/docs/tasks/debug/debug-cluster/audit/ (first matching rule wins).
    """

    def work() -> dict[str, Any]:
        policy = payload["policy"]
        _check_size(policy, "policy")
        if policy.get("apiVersion") != "audit.k8s.io/v1" or policy.get("kind") != "Policy":
            raise _Bad("policy", "policy must be an audit.k8s.io/v1 Policy")
        raw = policy.get("rules")
        if not isinstance(raw, list) or not raw:
            raise _Bad("policy", "policy.rules must be a non-empty list")
        rules = [_audit_rule(rule, index) for index, rule in enumerate(raw)]
        policy_stages = policy.get("omitStages")
        if policy_stages is not None and (not isinstance(policy_stages, list) or any(s not in AUDIT_STAGES for s in policy_stages)):
            raise _Bad("policy", f"policy.omitStages entries must be one of {', '.join(AUDIT_STAGES)}")
        findings: list[dict[str, Any]] = []
        failed: set[str] = set()
        for probe, group, targets, verbs, minimum, maximum, under, over, why in AUDIT_PROBES:
            low: list[str] = []
            high: list[str] = []
            for resource, subresource, namespace in targets:
                for verb in verbs:
                    level, index = _audit_level(rules, verb, group, resource, subresource, namespace)
                    shown = f"{verb} {resource}/{subresource}" if subresource else f"{verb} {resource}"
                    via = f"rules[{index}]" if index >= 0 else "no rule"
                    if AUDIT_LEVELS[level] < AUDIT_LEVELS[minimum]:
                        low.append(f"{shown} -> {level} ({via})")
                    elif AUDIT_LEVELS[level] > AUDIT_LEVELS[_AUDIT_VERB_MAX.get((probe, verb), maximum)]:
                        high.append(f"{shown} -> {level} ({via})")
            for group_name, items, severity, bound in (("under-audited", low, under, f"at least {minimum}"), ("over-audited", high, over, f"at most {maximum}")):
                if items:
                    failed.add(probe)
                    extra = f" and {len(items) - 4} more" if len(items) > 4 else ""
                    findings.append(_finding(f"audit.{probe}.{group_name}", severity, "", f"needs {bound}: {'; '.join(items[:4])}{extra}. {why}"))
        selectorless = [i for i, r in enumerate(rules) if not (r["users"] or r["groups"] or r["verbs"] or r["namespaces"] or r["urls"] or r["resources"])]
        if all(r["level"] == "None" for r in rules):
            findings.append(_finding("audit.nothing-logged", "blocker", "", "every rule has level None: no request is audited"))
        if not any(rules[i]["level"] != "None" for i in selectorless):
            findings.append(_finding("audit.no-catch-all", "minor", "", "no selector-less rule above level None: requests matching no rule are not audited"))
        if selectorless and selectorless[0] < len(rules) - 1:
            findings.append(_finding("audit.unreachable-rule", "minor", "", f"{len(rules) - 1 - selectorless[0]} rule(s) after the selector-less rules[{selectorless[0]}] can never match"))
        omitted = "RequestReceived" in (policy_stages or []) or all("RequestReceived" in r["stages"] for r in rules)
        if not omitted:
            findings.append(_finding("audit.request-received-logged", "info", "", "RequestReceived is not omitted: every request is logged twice"))
        if any(r["level"] in ("Request", "RequestResponse") for r in rules) and policy.get("omitManagedFields") is not True:
            findings.append(_finding("audit.managed-fields-logged", "info", "", "omitManagedFields is not true: managedFields bloats Request and RequestResponse events"))
        return {"state": "SUCCESS", "rule_count": len(rules), "failed_probes": len(failed), **_report(findings)}

    return respond(payload, _guarded(work))


# --- 12. policy exception register ------------------------------------------------------------------------------


def _timestamp(value: str, field: str, label: str) -> datetime:
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise _Bad(field, f"{label} {_val(value, _TIMESTAMPISH)} is not an ISO-8601 timestamp") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise _Bad(field, f"{label} {_val(value, _TIMESTAMPISH)} needs an explicit UTC offset")
    try:
        moment.astimezone(timezone.utc)
    except OverflowError as exc:  # year 1 or 9999 pushed outside the representable range by the offset
        raise _Bad(field, f"{label} {_val(value, _TIMESTAMPISH)} is outside the representable range once converted to UTC") from exc
    return moment


def exception_register_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Audit a policy exception register for expiry, staleness and approval hygiene at the instant `now`.

    Per exception (timestamps ISO-8601 with an explicit offset): expired when expires_at <= now (major); expiring soon
    when the remaining time is <= warn_days (minor); no expiry (major); lifetime expires_at - granted_at > max_duration_days
    (major); last review (else grant) older than review_interval_days (minor, strictly greater); no named approver or
    approved by the owner (major); granted in the future (minor). A24 never grants or waives an exception: granting is L4
    and stays with a human, this tool only measures the register. The day limits are the caller's policy and the severities
    this tool's house judgement. Ids must be unique; expires_at must follow granted_at and last_reviewed_at must not
    precede it (either is refused as INVALID_INPUT). `earliest_expiry` is the soonest expiry among exceptions that have not
    expired yet (UTC, Z suffix), empty when none has an end date.
    """

    def work() -> dict[str, Any]:
        now = _timestamp(payload["now"], "now", "now")
        warn, longest, review = (timedelta(days=payload[k]) for k in ("warn_days", "max_duration_days", "review_interval_days"))
        findings: list[dict[str, Any]] = []
        seen: set[str] = set()
        expired = soon = unbounded = 0
        earliest: datetime | None = None
        entries = payload["exceptions"]
        for index, entry in enumerate(entries):
            ident = entry["id"]
            if ident in seen:
                raise _Bad("exceptions", f"exceptions[{index}].id {ident!r} is duplicated")
            seen.add(ident)
            granted = _timestamp(entry["granted_at"], "exceptions", f"exceptions[{index}].granted_at")
            expires = _timestamp(entry["expires_at"], "exceptions", f"exceptions[{index}].expires_at") if "expires_at" in entry else None
            reviewed = _timestamp(entry["last_reviewed_at"], "exceptions", f"exceptions[{index}].last_reviewed_at") if "last_reviewed_at" in entry else None
            if expires is not None and expires <= granted:
                raise _Bad("exceptions", f"exceptions[{index}].expires_at must be after granted_at")
            if reviewed is not None and reviewed < granted:
                raise _Bad("exceptions", f"exceptions[{index}].last_reviewed_at must not precede granted_at")
            where = f"{ident} ({entry['policy']} / {entry['scope']})"
            is_expired = expires is not None and expires <= now
            if expires is not None and expires <= now:
                expired += 1
                findings.append(_finding("exception.expired", "major", where, f"expired at {expires.isoformat()}: remove it or have a human re-approve it"))
            elif expires is None:
                unbounded += 1
                findings.append(_finding("exception.no-expiry", "major", where, "has no expires_at: an exception without an end date is a permanent waiver"))
            else:
                if expires - now <= warn:
                    soon += 1
                    findings.append(_finding("exception.expiring-soon", "minor", where, f"expires {expires.isoformat()}, within {payload['warn_days']} day(s)"))
                if earliest is None or expires < earliest:
                    earliest = expires
            if expires is not None and expires - granted > longest:
                findings.append(_finding("exception.too-long", "major", where, f"lifetime exceeds {payload['max_duration_days']} day(s)"))
            if not is_expired and now - (reviewed or granted) > review:
                findings.append(_finding("exception.stale-review", "minor", where, f"not reviewed for more than {payload['review_interval_days']} day(s)"))
            approver = entry["approved_by"].strip()
            if not approver:
                findings.append(_finding("exception.no-approver", "major", where, "has no named approver"))
            elif approver.lower() == entry["owner"].strip().lower():
                findings.append(_finding("exception.self-approved", "major", where, "approved by its own owner"))
            if granted > now:
                findings.append(_finding("exception.future-grant", "minor", where, f"granted_at {granted.isoformat()} is in the future"))
        stamp = earliest.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if earliest else ""
        return {"state": "SUCCESS", "exception_count": len(entries), "active_count": len(entries) - expired, "expired_count": expired,
                "expiring_soon_count": soon, "no_expiry_count": unbounded, "earliest_expiry": stamp, **_report(findings)}

    return respond(payload, _guarded(work))
