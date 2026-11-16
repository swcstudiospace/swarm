"""A21 Kubernetes Cluster Operator tools: pure analyses of cluster lifecycle and capacity facts supplied by the caller.

Nothing here talks to a cluster: every input is a manifest or status already parsed into plain values.

Tools:
- k8s_version_skew: kube-apiserver / kubelet / kube-proxy / controller-manager / kubectl skew against the upstream policy.
- k8s_deprecated_api_scan: manifests using an API removed (or about to be removed) by a target Kubernetes version.
- k8s_nodepool_capacity: node-pool requests versus allocatable with N+k node-failure headroom.
- k8s_upgrade_plan: ordered upgrade steps (control plane per minor, add-ons, node pools with surge) and blockers.
- k8s_drain_feasibility: whether a node drain can evict pods under their PodDisruptionBudgets.
- k8s_etcd_health: etcd database size, leader churn, disk latency and member quorum assessment.
- k8s_autoscaler_sanity: HPA, VPA, Cluster Autoscaler and Karpenter bounds and conflicts.
- k8s_cert_expiry: control-plane certificate expiry buckets.
- k8s_quota_fit: whether a workload is admitted by a namespace ResourceQuota and LimitRange and how many replicas fit.
- k8s_addon_compatibility: add-on (CNI, CSI, ingress, DNS) supported-version ranges along an upgrade path.
- k8s_fleet_drift: cluster versions versus the latest minor and an N-1 style support policy.
- k8s_maintenance_window: whether a proposed change fits a recurring maintenance window and avoids blackouts.

Public sources for the embedded rules (also named next to each rule):
- Version skew policy: https://kubernetes.io/releases/version-skew-policy/
- API removals: https://kubernetes.io/docs/reference/using-api/deprecation-guide/
- PodDisruptionBudget semantics: https://kubernetes.io/docs/tasks/run-application/configure-pdb/
- etcd limits and metrics: https://etcd.io/docs/v3.5/dev-guide/limit/ , https://etcd.io/docs/v3.5/op-guide/hardware/ ,
  https://etcd.io/docs/v3.5/op-guide/monitoring/ and the kubernetes-mixin etcd alerts.
- ResourceQuota / LimitRange: https://kubernetes.io/docs/concepts/policy/resource-quotas/ and
  https://kubernetes.io/docs/concepts/policy/limit-range/
- Patch release support: https://kubernetes.io/releases/patch-releases/

House policy, NOT upstream standards (the findings say so where it matters): the 80 % etcd quota band, the certificate 7/30/90 d (leaf)
and 30/180/365 d (CA) buckets, the default 85 % node-pool target utilization and N+1 headroom, the N-1 fleet lag default, the
smallest-pool-first upgrade order and the 6-hop and 14-day search bounds. Verified upstream facts: the skew limits, three supported
minors, the removal table (deprecation guide plus CHANGELOG-1.23 for the v1alpha1 rows), PDB rounding, LimitRange API defaulting and
the etcd quota, latency, leader-change and defragmentation alert values. Not modelled: bin-packing fragmentation, init containers and
pod overhead, pod-level resources, LimitRange Pod and PersistentVolumeClaim items.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

from swarm.tools.support import respond

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
HUNDRED = Decimal(100)
JsonObject = dict[str, Any]  # caller-supplied JSON payloads and the result dicts built from them: untyped values
Finding = dict[str, str]

# Version skew policy (https://kubernetes.io/releases/version-skew-policy/): kubelet and kube-proxy may be up to
# three minor versions older than kube-apiserver (two for versions below 1.25).
SKEW_OLDER_LIMIT = 3
SKEW_OLDER_LIMIT_LEGACY = 2
SKEW_LEGACY_BELOW_MINOR = 25
MAX_UPGRADE_HOPS = 6
UPGRADE_PATH_MINORS = 6

# Removed APIs. Public source: https://kubernetes.io/docs/reference/using-api/deprecation-guide/
# (api_version, kind) -> (minor version of Kubernetes 1.x that stopped serving it, replacement). kind "*" covers the
# whole group version.
REMOVED_APIS: dict[tuple[str, str], tuple[int, str]] = {
    ("extensions/v1beta1", "DaemonSet"): (16, "apps/v1"),
    ("extensions/v1beta1", "Deployment"): (16, "apps/v1"),
    ("extensions/v1beta1", "ReplicaSet"): (16, "apps/v1"),
    ("extensions/v1beta1", "NetworkPolicy"): (16, "networking.k8s.io/v1"),
    ("extensions/v1beta1", "PodSecurityPolicy"): (16, "policy/v1beta1"),
    ("apps/v1beta1", "*"): (16, "apps/v1"),
    ("apps/v1beta2", "*"): (16, "apps/v1"),
    ("extensions/v1beta1", "Ingress"): (22, "networking.k8s.io/v1"),
    ("networking.k8s.io/v1beta1", "Ingress"): (22, "networking.k8s.io/v1"),
    ("networking.k8s.io/v1beta1", "IngressClass"): (22, "networking.k8s.io/v1"),
    ("admissionregistration.k8s.io/v1beta1", "MutatingWebhookConfiguration"): (22, "admissionregistration.k8s.io/v1"),
    ("admissionregistration.k8s.io/v1beta1", "ValidatingWebhookConfiguration"): (22, "admissionregistration.k8s.io/v1"),
    ("apiextensions.k8s.io/v1beta1", "CustomResourceDefinition"): (22, "apiextensions.k8s.io/v1"),
    ("apiregistration.k8s.io/v1beta1", "APIService"): (22, "apiregistration.k8s.io/v1"),
    ("authentication.k8s.io/v1beta1", "TokenReview"): (22, "authentication.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "LocalSubjectAccessReview"): (22, "authorization.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "SelfSubjectAccessReview"): (22, "authorization.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "SelfSubjectRulesReview"): (22, "authorization.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "SubjectAccessReview"): (22, "authorization.k8s.io/v1"),
    ("certificates.k8s.io/v1beta1", "CertificateSigningRequest"): (22, "certificates.k8s.io/v1"),
    ("coordination.k8s.io/v1beta1", "Lease"): (22, "coordination.k8s.io/v1"),
    ("rbac.authorization.k8s.io/v1beta1", "*"): (22, "rbac.authorization.k8s.io/v1"),
    ("rbac.authorization.k8s.io/v1alpha1", "*"): (23, "rbac.authorization.k8s.io/v1"),  # CHANGELOG-1.23 (#104248), not in the guide
    ("scheduling.k8s.io/v1alpha1", "PriorityClass"): (23, "scheduling.k8s.io/v1"),  # same entry; v1alpha1 has other kinds since 1.35
    ("scheduling.k8s.io/v1beta1", "PriorityClass"): (22, "scheduling.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "CSIDriver"): (22, "storage.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "CSINode"): (22, "storage.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "StorageClass"): (22, "storage.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "VolumeAttachment"): (22, "storage.k8s.io/v1"),
    ("batch/v1beta1", "CronJob"): (25, "batch/v1"),
    ("discovery.k8s.io/v1beta1", "EndpointSlice"): (25, "discovery.k8s.io/v1"),
    ("events.k8s.io/v1beta1", "Event"): (25, "events.k8s.io/v1"),
    ("autoscaling/v2beta1", "HorizontalPodAutoscaler"): (25, "autoscaling/v2"),
    ("policy/v1beta1", "PodDisruptionBudget"): (25, "policy/v1"),
    ("policy/v1beta1", "PodSecurityPolicy"): (25, "no API replacement: Pod Security Admission or a policy engine"),
    ("node.k8s.io/v1beta1", "RuntimeClass"): (25, "node.k8s.io/v1"),
    ("autoscaling/v2beta2", "HorizontalPodAutoscaler"): (26, "autoscaling/v2"),
    ("flowcontrol.apiserver.k8s.io/v1beta1", "*"): (26, "flowcontrol.apiserver.k8s.io/v1 (served from 1.29; v1beta3 serves 1.26-1.31)"),
    ("storage.k8s.io/v1beta1", "CSIStorageCapacity"): (27, "storage.k8s.io/v1"),
    ("flowcontrol.apiserver.k8s.io/v1beta2", "*"): (29, "flowcontrol.apiserver.k8s.io/v1"),
    ("flowcontrol.apiserver.k8s.io/v1beta3", "*"): (32, "flowcontrol.apiserver.k8s.io/v1"),
}

# etcd: default backend quota 2 GiB, suggested maximum 8 GiB (https://etcd.io/docs/v3.5/dev-guide/limit/).
ETCD_DEFAULT_QUOTA_BYTES = 2 * 1024**3
ETCD_SUGGESTED_MAX_BYTES = 8 * 1024**3
ETCD_QUOTA_BLOCKER_PCT = Decimal(95)
ETCD_QUOTA_MAJOR_PCT = Decimal(80)
ETCD_DEFRAG_MIN_BYTES = 100 * 1024**2  # etcd mixin etcdDatabaseHighFragmentationRatio: in-use bytes above 104857600 ...
# ... and in-use / size below 0.5 (both strict)
# etcd FAQ: wal fsync p99 should be below 10 ms, backend commit p99 below 25 ms; etcd mixin alerts at fsync p99 > 500 ms and
# commit p99 > 250 ms, and etcdHighNumberOfLeaderChanges fires at 4 or more leader changes within 15 minutes.
ETCD_FSYNC_GUIDE_MS = Decimal(10)
ETCD_FSYNC_ALERT_MS = Decimal(500)
ETCD_COMMIT_GUIDE_MS = Decimal(25)
ETCD_COMMIT_ALERT_MS = Decimal(250)
ETCD_LEADER_CHANGES_15M = 4

# Certificate buckets (house policy): leaf certificates (kubeadm issues one-year leaves), CA certificates (ten years).
CERT_LEAF_DAYS = (7, 30, 90)  # critical, warning, notice upper bounds: remaining < N days
CERT_CA_DAYS = (30, 180, 365)

UPSTREAM_SUPPORTED_MINORS = 3  # https://kubernetes.io/releases/patch-releases/: the three most recent minors
SEARCH_DAYS = 14

_VERSION = re.compile(r"v?([0-9]{1,3})\.([0-9]{1,3})(?:\.([0-9]{1,4}))?(?:[-+][0-9A-Za-z.+-]{1,60})?\Z")
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])\Z")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class _Bad(Exception):
    """A semantic input problem the schema cannot express; `field` names the input property."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _respond(payload: JsonObject, work: Callable[[], JsonObject]) -> JsonObject:
    return cast(JsonObject, respond(payload, work))  # support.respond is untyped (Any); work() always returns a JsonObject


def _invalid(field: str, message: str) -> JsonObject:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr), so boundaries are exact."""
    return Decimal(repr(value))


def _finding(fid: str, severity: str, summary: str) -> Finding:
    return {"id": fid, "severity": severity, "summary": summary}


def _ordered(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))


def _worst(findings: list[Finding]) -> int:
    return max((SEVERITY_RANK[f["severity"]] for f in findings), default=-1)


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _days(delta: timedelta) -> float:
    """Days in whole hundredths, truncated toward zero so a value never rounds into the next bucket."""
    micros = delta // timedelta(microseconds=1)
    hundredths = abs(micros) * 100 // (86_400 * 1_000_000)
    return (hundredths if micros >= 0 else -hundredths) / 100


def _pct(numerator: int, denominator: int) -> float:
    return round(float(Decimal(numerator) * HUNDRED / Decimal(denominator)), 2)


def _parse_version(value: str, field: str) -> tuple[int, int, bool]:
    """(minor, patch, has_patch) of a 1.x version: 1.30, v1.30.2, v1.30.2-gke.1000 or 1.30.2+k3s1."""
    match = _VERSION.match(value.strip())
    if not match:
        raise _Bad(field, "not a Kubernetes version such as 1.30 or v1.30.2")
    if int(match.group(1)) != 1:
        raise _Bad(field, "only Kubernetes major version 1 is supported")
    return int(match.group(2)), int(match.group(3) or 0), match.group(3) is not None


def _minor(value: str, field: str) -> int:
    return _parse_version(value, field)[0]


def _label(minor: int) -> str:
    return f"1.{minor}"


def _timestamp(value: str, field: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _Bad(field, "not an ISO-8601 timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _Bad(field, "needs an explicit UTC offset")
    return parsed


def _skew_older_limit(minor: int) -> int:
    return SKEW_OLDER_LIMIT_LEGACY if minor < SKEW_LEGACY_BELOW_MINOR else SKEW_OLDER_LIMIT


def _unique(items: list[JsonObject], key: str, field: str, what: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item[key] in seen:
            raise _Bad(field, f"duplicate {what} {item[key]!r}")
        seen.add(item[key])


# --- k8s_version_skew -----------------------------------------------------------------------------------------------


def _version_skew(payload: JsonObject) -> JsonObject:
    api_minors = [_minor(v, "apiserver_versions") for v in payload["apiserver_versions"]]
    newest, oldest = max(api_minors), min(api_minors)
    findings: list[Finding] = []
    if newest - oldest > 1:
        findings.append(_finding(
            "apiserver/spread", "major",
            f"kube-apiserver instances span {_label(oldest)}..{_label(newest)}; the newest and oldest must be within one minor"))
    kubelet_minor_by_node: dict[str, int] = {}
    kubelets: list[int] = []
    node_bound: list[tuple[str, str, int]] = []
    for comp in payload["components"]:
        minor = _minor(comp["version"], "components")
        kind, name, node = comp["kind"], comp["name"], comp.get("node", "")
        ident = f"{kind}/{name}"
        if kind in ("kubelet", "kube-proxy"):
            if minor > oldest:
                findings.append(_finding(
                    f"{ident}/newer-than-apiserver", "blocker",
                    f"{kind} {name} is {_label(minor)} but a kube-apiserver is {_label(oldest)}; it must not be newer"))
            else:
                lag, limit = newest - minor, _skew_older_limit(minor)
                if lag > limit:
                    findings.append(_finding(
                        f"{ident}/too-old", "major",
                        f"{kind} {name} is {_label(minor)}, {lag} minors behind kube-apiserver {_label(newest)}; at most {limit} are supported"))
                elif lag == limit:
                    findings.append(_finding(
                        f"{ident}/at-skew-limit", "minor",
                        f"{kind} {name} is {_label(minor)}, exactly {limit} minors behind kube-apiserver {_label(newest)}: the next control-plane upgrade breaches the policy"))
            if kind == "kubelet":
                kubelets.append(minor)
                if node:
                    if node in kubelet_minor_by_node:
                        raise _Bad("components", f"more than one kubelet reported for node {node!r}")
                    kubelet_minor_by_node[node] = minor
            elif node:
                node_bound.append((name, node, minor))
        elif kind == "kubectl":
            span = _label(oldest) if oldest == newest else f"{_label(oldest)}..{_label(newest)}"
            if minor > oldest + 1 or minor < newest - 1:
                findings.append(_finding(
                    f"{ident}/skew", "minor",
                    f"kubectl {name} is {_label(minor)}; it must be within one minor of every kube-apiserver ({span})"))
        else:  # kube-controller-manager, kube-scheduler, cloud-controller-manager
            if minor > oldest:
                findings.append(_finding(
                    f"{ident}/newer-than-apiserver", "blocker",
                    f"{kind} {name} is {_label(minor)} but a kube-apiserver is {_label(oldest)}; it must not be newer"))
            elif newest - minor > 1:
                findings.append(_finding(
                    f"{ident}/too-old", "major",
                    f"{kind} {name} is {_label(minor)}; it may be at most one minor older than kube-apiserver {_label(newest)}"))
            elif newest - minor == 1:
                findings.append(_finding(
                    f"{ident}/one-behind", "info",
                    f"{kind} {name} is one minor behind kube-apiserver {_label(newest)}: allowed while a live upgrade completes"))
    for name, node, proxy_minor in node_bound:
        if node in kubelet_minor_by_node:
            gap, limit = abs(proxy_minor - kubelet_minor_by_node[node]), _skew_older_limit(proxy_minor)
            if gap > limit:
                findings.append(_finding(
                    f"kube-proxy/{name}/kubelet-skew", "major",
                    f"kube-proxy {name} on node {node} differs from its kubelet by {gap} minors; at most {limit} are supported"))
    next_newest, next_oldest = newest + 1, oldest + 1
    # The hop moves every kube-apiserver one minor up; each component is checked against the same rule the policy applies at that position.
    # A kubelet/kube-proxy breach always carries a current-state finding that names it: too-old or at-skew-limit for lag, newer-than-apiserver for a minor above the oldest kube-apiserver.
    upgrade_safe = newest - oldest <= 1
    upgrade_findings: list[Finding] = []
    for comp in payload["components"]:
        minor = _minor(comp["version"], "components")
        kind, name = comp["kind"], comp["name"]
        if kind in ("kubelet", "kube-proxy"):
            if minor > next_oldest or next_newest - minor > _skew_older_limit(minor):
                upgrade_safe = False
            continue
        after_span = _label(next_oldest) if next_oldest == next_newest else f"{_label(next_oldest)}..{_label(next_newest)}"
        if kind == "kubectl":
            breached = minor < next_newest - 1 or minor > next_oldest + 1
            message = f"kubectl {name} is {_label(minor)}; after the upgrade it must be within one minor of every kube-apiserver ({after_span})"
        else:  # kube-controller-manager, kube-scheduler, cloud-controller-manager
            breached = minor > next_oldest or next_newest - minor > 1
            message = f"{kind} {name} is {_label(minor)}; after the upgrade the kube-apiserver is {after_span}, so it must be at most one minor older than the newest and not newer than the oldest"
        if breached:
            upgrade_safe = False
            upgrade_findings.append(_finding(f"{kind}/{name}/upgrade-breach", "minor", message))
    findings.extend(upgrade_findings)
    return {
        "state": "SUCCESS",
        "apiserver_oldest": _label(oldest),
        "apiserver_newest": _label(newest),
        "components_checked": len(payload["components"]),
        "kubelet_count": len(kubelets),
        "max_kubelet_lag": max([0] + [newest - m for m in kubelets]),
        "control_plane_upgrade_safe": upgrade_safe,
        "verdict": "violations" if _worst(findings) >= SEVERITY_RANK["major"] else "compliant",
        "findings": _ordered(findings),
    }


def version_skew(payload: JsonObject) -> JsonObject:
    """Version skew policy: kubelet/kube-proxy not newer than the oldest kube-apiserver and at most 3 (2 below 1.25) minors older than the newest; controller-manager/scheduler at most 1 older; kubectl within 1 minor of every apiserver."""

    def work() -> JsonObject:
        try:
            return _version_skew(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_deprecated_api_scan ----------------------------------------------------------------------------------------


def _deprecated_api_scan(payload: JsonObject) -> JsonObject:
    target = _minor(payload["target_version"], "target_version")
    horizon = payload.get("horizon_minors", 1)
    findings: list[Finding] = []
    removed = upcoming = 0
    for resource in payload["resources"]:
        entry = REMOVED_APIS.get((resource["api_version"], resource["kind"])) or REMOVED_APIS.get((resource["api_version"], "*"))
        if entry is None:
            continue
        removed_in, replacement = entry
        ident = f"api/{resource['api_version']}/{resource['kind']}/{resource['name']}"
        subject = f"{resource['kind']} {resource['name']} uses {resource['api_version']}"
        if removed_in <= target:
            removed += 1
            findings.append(_finding(ident, "blocker", f"{subject}, removed in {_label(removed_in)}; migrate to {replacement} before upgrading to {_label(target)}"))
        elif removed_in <= target + horizon:
            upcoming += 1
            findings.append(_finding(ident, "minor", f"{subject}, removed in {_label(removed_in)}; migrate to {replacement} before the upgrade after {_label(target)}"))
    return {
        "state": "SUCCESS",
        "target_version": _label(target),
        "resources_scanned": len(payload["resources"]),
        "removed_count": removed,
        "upcoming_count": upcoming,
        "verdict": "blocked" if removed else "attention" if upcoming else "clear",
        "findings": _ordered(findings),
    }


def deprecated_api_scan(payload: JsonObject) -> JsonObject:
    """APIs removed up to and including the target minor are blockers; APIs removed within horizon_minors (default 1) after it are minor findings."""

    def work() -> JsonObject:
        try:
            return _deprecated_api_scan(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_nodepool_capacity ------------------------------------------------------------------------------------------


def _utilization(requested: int, per_node: int, nodes: int) -> float:
    """Percent of `nodes` worth of capacity requested; -1.0 when something is requested but no node exists."""
    if requested == 0:
        return 0.0
    return -1.0 if nodes == 0 else _pct(requested, nodes * per_node)


def _nodepool_capacity(payload: JsonObject) -> JsonObject:
    failures = payload.get("tolerated_node_failures", 1)
    max_pct = payload.get("max_utilization_pct", 85)
    pools = payload["pools"]
    _unique(pools, "name", "pools", "pool name")
    by_name = {pool["name"]: pool for pool in pools}
    requested = {name: [0, 0] for name in by_name}
    findings: list[Finding] = []
    unschedulable: set[str] = set()
    for workload in payload["workloads"]:
        pool = by_name.get(workload["pool"])
        if pool is None:
            raise _Bad("workloads", f"workload {workload['name']!r} targets unknown pool {workload['pool']!r}")
        requested[pool["name"]][0] += workload["replicas"] * workload["cpu_request_m"]
        requested[pool["name"]][1] += workload["replicas"] * workload["memory_request_mib"]
        usable_cpu = pool["allocatable_cpu_m"] - pool.get("daemonset_cpu_m", 0)
        usable_mem = pool["allocatable_memory_mib"] - pool.get("daemonset_memory_mib", 0)
        if workload["replicas"] > 0 and (workload["cpu_request_m"] > usable_cpu or workload["memory_request_mib"] > usable_mem):
            unschedulable.add(pool["name"])
            findings.append(_finding(
                f"workload/{workload['name']}/exceeds-node", "blocker",
                f"a pod of {workload['name']} requests more than one {pool['name']} node can offer after daemonsets; it can never be scheduled"))
    rows: list[JsonObject] = []
    for pool in sorted(pools, key=lambda p: p["name"]):
        usable_cpu = pool["allocatable_cpu_m"] - pool.get("daemonset_cpu_m", 0)
        usable_mem = pool["allocatable_memory_mib"] - pool.get("daemonset_memory_mib", 0)
        if usable_cpu <= 0 or usable_mem <= 0:
            raise _Bad("pools", f"pool {pool['name']!r}: daemonset requests leave no allocatable cpu or memory per node")
        if "max_nodes" in pool and pool["nodes"] > pool["max_nodes"]:
            raise _Bad("pools", f"pool {pool['name']!r}: nodes exceeds max_nodes")
        req_cpu, req_mem = requested[pool["name"]]
        nodes = pool["nodes"]
        survivors = max(0, nodes - failures)
        if req_cpu == 0 and req_mem == 0:
            capacity = "ok"
        elif survivors == 0 or req_cpu > survivors * usable_cpu or req_mem > survivors * usable_mem:
            capacity = "at_risk"
        elif req_cpu * 100 > max_pct * survivors * usable_cpu or req_mem * 100 > max_pct * survivors * usable_mem:
            capacity = "tight"
        else:
            capacity = "ok"

        no_survivor = survivors == 0 and (req_cpu > 0 or req_mem > 0)
        after = -1.0 if no_survivor else max(_utilization(req_cpu, usable_cpu, survivors), _utilization(req_mem, usable_mem, survivors))
        needed = max(_ceil_div(req_cpu * 100, max_pct * usable_cpu), _ceil_div(req_mem * 100, max_pct * usable_mem))
        recommended = needed + failures if needed > 0 else 0
        over_max = "max_nodes" in pool and recommended > pool["max_nodes"]
        # A pod that can never be scheduled, or an autoscaler ceiling below the recommended size, puts the pool at risk whatever its arithmetic says.
        status = "at_risk" if capacity == "at_risk" or pool["name"] in unschedulable or over_max else capacity
        rows.append({
            "name": pool["name"],
            "nodes": nodes,
            "requested_cpu_m": req_cpu,
            "requested_memory_mib": req_mem,
            "cpu_utilization_pct": _utilization(req_cpu, usable_cpu, nodes),
            "memory_utilization_pct": _utilization(req_mem, usable_mem, nodes),
            "utilization_after_failure_pct": after,
            "status": status,
            "recommended_nodes": recommended,
        })
        if capacity == "at_risk":
            findings.append(_finding(
                f"pool/{pool['name']}/n-plus-{failures}", "major",
                f"pool {pool['name']} cannot hold its requests after losing {failures} node(s); needs {recommended} nodes at {max_pct}% target utilization"))
        elif capacity == "tight":
            findings.append(_finding(
                f"pool/{pool['name']}/tight", "minor",
                f"pool {pool['name']} stays below 100% after losing {failures} node(s) but above the {max_pct}% target; recommended {recommended} nodes"))
        if over_max:
            findings.append(_finding(
                f"pool/{pool['name']}/max-nodes", "major",
                f"pool {pool['name']} max_nodes {pool['max_nodes']} is below the recommended {recommended}"))
    verdict = ("at_risk" if any(r["status"] == "at_risk" for r in rows) or _worst(findings) >= SEVERITY_RANK["major"]
               else "tight" if any(r["status"] == "tight" for r in rows) else "ok")
    return {
        "state": "SUCCESS",
        "pools": rows,
        "pools_at_risk": sum(1 for r in rows if r["status"] == "at_risk"),
        "verdict": verdict,
        "findings": _ordered(findings),
    }


def nodepool_capacity(payload: JsonObject) -> JsonObject:
    """Per node pool: requests must fit on (nodes - tolerated failures) nodes; above max_utilization_pct (house default 85) after the loss the pool is tight, above 100% it is at risk. A pod that fits no node or a recommended size above max_nodes also puts the pool at risk. Aggregate requests only: no bin-packing or fragmentation model."""

    def work() -> JsonObject:
        try:
            return _nodepool_capacity(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_upgrade_plan -----------------------------------------------------------------------------------------------


def _step(steps: list[JsonObject], phase: str, action: str, target: str, batches: int = 0) -> None:
    steps.append({"order": len(steps) + 1, "phase": phase, "action": action, "target_version": target, "batches": batches})


def _upgrade_plan(payload: JsonObject) -> JsonObject:
    cur_minor, cur_patch, _ = _parse_version(payload["current_version"], "current_version")
    tgt_minor, tgt_patch, tgt_has_patch = _parse_version(payload["target_version"], "target_version")
    if tgt_minor == cur_minor and not tgt_has_patch:
        tgt_patch = cur_patch  # "1.30" as a target means any 1.30.x: nothing to do for the control plane
    if (tgt_minor, tgt_patch) < (cur_minor, cur_patch):
        raise _Bad("target_version", "target_version is older than current_version; downgrades are not planned")
    if tgt_minor - cur_minor > MAX_UPGRADE_HOPS:
        raise _Bad("target_version", f"more than {MAX_UPGRADE_HOPS} minor versions ahead of current_version")
    target_label = _label(tgt_minor) + (f".{tgt_patch}" if tgt_has_patch else "")
    pools = payload["node_pools"]
    _unique(pools, "name", "node_pools", "pool name")
    state: dict[str, tuple[int, int]] = {}
    findings: list[Finding] = []
    for pool in pools:
        minor, patch, _ = _parse_version(pool["version"], "node_pools")
        if pool["max_surge"] + pool["max_unavailable"] == 0:
            raise _Bad("node_pools", f"pool {pool['name']!r}: max_surge and max_unavailable cannot both be 0")
        state[pool["name"]] = (minor, patch)
        if minor > cur_minor:
            findings.append(_finding(f"pool/{pool['name']}/ahead", "blocker", f"pool {pool['name']} runs {_label(minor)}, newer than the control plane {_label(cur_minor)}"))
        elif cur_minor - minor > _skew_older_limit(minor):
            findings.append(_finding(f"pool/{pool['name']}/skew", "blocker", f"pool {pool['name']} ({_label(minor)}) is already outside the kubelet skew limit for control plane {_label(cur_minor)}"))
        if "pdb_min_disruptions_allowed" in pool and pool["pdb_min_disruptions_allowed"] == 0:
            findings.append(_finding(f"pool/{pool['name']}/pdb-blocks-drain", "major", f"a PodDisruptionBudget on pool {pool['name']} allows 0 disruptions; node drains will stall (see k8s_drain_feasibility)"))
        if pool["max_unavailable"] >= pool["nodes"]:
            findings.append(_finding(f"pool/{pool['name']}/whole-pool-unavailable", "major", f"pool {pool['name']} may take all {pool['nodes']} node(s) out at once (max_unavailable {pool['max_unavailable']})"))
    by_name = {pool["name"]: pool for pool in pools}
    batches_of = {p["name"]: _ceil_div(p["nodes"], p["max_surge"] + p["max_unavailable"]) for p in pools}
    steps: list[JsonObject] = []
    _step(steps, "preflight", f"snapshot etcd, scan manifests for APIs removed by {target_label}, verify add-on compatibility and PDB budgets, confirm the maintenance window", target_label)
    hops = list(range(cur_minor + 1, tgt_minor + 1))
    if not hops and tgt_patch > cur_patch:
        hops = [cur_minor]  # patch-only upgrade of the control plane
    cp_hops = 0
    for hop in hops:
        hop_label = target_label if hop == tgt_minor else _label(hop)
        for name in sorted(state, key=lambda n: (by_name[n]["nodes"], n)):
            minor = state[name][0]
            if hop > cur_minor and minor < hop - 1 and hop - minor > _skew_older_limit(minor):
                _step(steps, "node_pool", f"upgrade pool {name} to {_label(hop - 1)} before the control plane moves to {_label(hop)} (kubelet skew)", _label(hop - 1), batches_of[name])
                state[name] = (hop - 1, 0)
        _step(steps, "control_plane", f"upgrade the control plane to {hop_label}", hop_label)
        cp_hops += 1
        _step(steps, "verify", f"verify API server, controllers and system pods healthy on {hop_label}", hop_label)
    if cp_hops > 1:
        findings.append(_finding("plan/multi-hop", "info", f"{cp_hops} control-plane hops: the control plane cannot skip minor versions"))
    _step(steps, "addons", f"upgrade CNI, CSI, ingress, DNS and kube-proxy to versions supporting {target_label} (see k8s_addon_compatibility)", target_label)
    for name in sorted(state, key=lambda n: (by_name[n]["nodes"], n)):
        if state[name] < (tgt_minor, tgt_patch):
            pool = by_name[name]
            _step(steps, "node_pool", f"upgrade pool {name} to {target_label} with max_surge {pool['max_surge']} and max_unavailable {pool['max_unavailable']} ({batches_of[name]} batch(es))", target_label, batches_of[name])
    _step(steps, "verify", f"verify every node Ready on {target_label} and run workload smoke checks", target_label)
    worst = _worst(findings)
    return {
        "state": "SUCCESS",
        "steps": steps,
        "step_count": len(steps),
        "control_plane_hops": cp_hops,
        "node_pool_batches": sum(s["batches"] for s in steps),
        "verdict": "blocked" if worst >= SEVERITY_RANK["major"] else "attention" if worst >= SEVERITY_RANK["minor"] else "ready",
        "findings": _ordered(findings),
    }


def upgrade_plan(payload: JsonObject) -> JsonObject:
    """Ordered plan: preflight, one control-plane hop per minor (pools upgraded first when kubelet skew requires it), add-ons, node pools smallest first with surge batches, verify."""

    def work() -> JsonObject:
        try:
            return _upgrade_plan(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_drain_feasibility ------------------------------------------------------------------------------------------


_PDB_SELECTORS = ("min_available", "min_available_percent", "max_unavailable", "max_unavailable_percent")


def _desired_healthy(pdb: JsonObject) -> int:
    """The disruption controller rounds percentages up for minAvailable and maxUnavailable alike."""
    expected: int = pdb["expected_pods"]
    if "min_available" in pdb:
        minimum: int = pdb["min_available"]
        return minimum
    if "min_available_percent" in pdb:
        return _ceil_div(pdb["min_available_percent"] * expected, 100)
    if "max_unavailable" in pdb:
        unavailable: int = pdb["max_unavailable"]
        return max(0, expected - unavailable)
    return max(0, expected - _ceil_div(pdb["max_unavailable_percent"] * expected, 100))


def _drain_feasibility(payload: JsonObject) -> JsonObject:
    pdbs = payload["pdbs"]
    seen: set[tuple[str, str]] = set()
    findings: list[Finding] = []
    rows: list[JsonObject] = []
    for pdb in sorted(pdbs, key=lambda p: (p["namespace"], p["name"])):
        key = (pdb["namespace"], pdb["name"])
        if key in seen:
            raise _Bad("pdbs", f"duplicate PodDisruptionBudget {pdb['namespace']}/{pdb['name']}")
        seen.add(key)
        if sum(1 for selector in _PDB_SELECTORS if selector in pdb) != 1:
            raise _Bad("pdbs", f"{pdb['namespace']}/{pdb['name']}: set exactly one of {', '.join(_PDB_SELECTORS)}")
        if pdb["current_healthy"] > pdb["expected_pods"] or pdb["pods_on_drained_nodes"] > pdb["expected_pods"]:
            raise _Bad("pdbs", f"{pdb['namespace']}/{pdb['name']}: current_healthy and pods_on_drained_nodes cannot exceed expected_pods")
        desired = _desired_healthy(pdb)
        allowed = max(0, pdb["current_healthy"] - desired)
        pods = pdb["pods_on_drained_nodes"]
        ident = f"pdb/{pdb['namespace']}/{pdb['name']}"
        waves = 0
        if pods == 0:
            status = "unaffected"
        elif allowed == 0:
            status = "blocked"
            degraded = " (already below the expected pod count)" if pdb["current_healthy"] < pdb["expected_pods"] else ""
            findings.append(_finding(ident, "blocker", f"{pdb['namespace']}/{pdb['name']} allows 0 disruptions with {pods} pod(s) on the drained nodes{degraded}; evictions return 429 and the drain stalls"))
        elif pods <= allowed:
            status, waves = "free", 1
        else:
            status, waves = "throttled", _ceil_div(pods, allowed)
            findings.append(_finding(ident, "minor", f"{pdb['namespace']}/{pdb['name']} allows {allowed} disruption(s) for {pods} pod(s): {waves} eviction waves if the budget stays at {allowed}, each needing replacements Ready"))
        if "min_available" in pdb and pdb["min_available"] > pdb["expected_pods"]:
            findings.append(_finding(f"{ident}/unsatisfiable", "major", f"{pdb['namespace']}/{pdb['name']} minAvailable {pdb['min_available']} exceeds its {pdb['expected_pods']} expected pods"))
        rows.append({"name": pdb["name"], "namespace": pdb["namespace"], "desired_healthy": desired, "disruptions_allowed": allowed,
                     "pods_on_drained_nodes": pods, "status": status, "waves": waves})
    blocked = sum(1 for r in rows if r["status"] == "blocked")
    throttled = sum(1 for r in rows if r["status"] == "throttled")
    return {
        "state": "SUCCESS",
        "pdb_count": len(rows),
        "affected_count": sum(1 for r in rows if r["status"] != "unaffected"),
        "blocked_count": blocked,
        "throttled_count": throttled,
        "max_waves": max([r["waves"] for r in rows] + [0]),
        "verdict": "blocked" if blocked or _worst(findings) >= SEVERITY_RANK["major"] else "throttled" if throttled else "feasible",
        "results": rows,
        "findings": _ordered(findings),
    }


def drain_feasibility(payload: JsonObject) -> JsonObject:
    """disruptionsAllowed = max(0, currentHealthy - desiredHealthy) with percentages rounded up; 0 allowed with pods on the drained nodes blocks the drain; a minAvailable above the expected pods is a major finding and makes the verdict blocked."""

    def work() -> JsonObject:
        try:
            return _drain_feasibility(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_etcd_health ------------------------------------------------------------------------------------------------


def _etcd_health(payload: JsonObject) -> JsonObject:
    size = payload["db_size_bytes"]
    quota = payload.get("quota_bytes", ETCD_DEFAULT_QUOTA_BYTES)
    in_use = payload.get("db_size_in_use_bytes")
    if in_use is not None and in_use > size:
        raise _Bad("db_size_in_use_bytes", "db_size_in_use_bytes cannot exceed db_size_bytes")
    if quota <= 0:
        raise _Bad("quota_bytes", "quota_bytes must be greater than 0")
    _unique(payload["members"], "name", "members", "member name")
    findings: list[Finding] = []
    used_pct = Decimal(size) * HUNDRED / Decimal(quota)
    if used_pct > ETCD_QUOTA_BLOCKER_PCT:
        findings.append(_finding("etcd/quota-exhausted", "blocker", f"database uses {round(float(used_pct), 2)}% of its {quota}-byte quota; at 100% etcd raises NOSPACE and rejects writes: compact and defragment now"))
    elif used_pct >= ETCD_QUOTA_MAJOR_PCT:
        findings.append(_finding("etcd/quota-high", "major", f"database uses {round(float(used_pct), 2)}% of its quota; schedule compaction and defragmentation"))
    if size > ETCD_SUGGESTED_MAX_BYTES:
        findings.append(_finding("etcd/size-above-suggested-max", "major", "database is larger than the suggested 8 GiB maximum"))
    if quota > ETCD_SUGGESTED_MAX_BYTES:
        findings.append(_finding("etcd/quota-above-suggested-max", "minor", "backend quota is above the suggested 8 GiB maximum"))
    fragmentation = -1.0
    if in_use is not None and size > 0:
        free_pct = Decimal(size - in_use) * HUNDRED / Decimal(size)
        fragmentation = round(float(free_pct), 2)
        if in_use > ETCD_DEFRAG_MIN_BYTES and in_use * 2 < size:
            findings.append(_finding("etcd/defrag-recommended", "minor", f"{fragmentation}% of the {size}-byte database file is free space ({in_use} bytes in use); defragment one member at a time"))
    if payload["leader_changes_15m"] >= ETCD_LEADER_CHANGES_15M:
        findings.append(_finding("etcd/leader-churn", "major", f"{payload['leader_changes_15m']} leader changes in 15 minutes (the etcd mixin alerts at {ETCD_LEADER_CHANGES_15M} or more): check disk latency, network and CPU starvation"))
    fsync = _dec(payload["wal_fsync_p99_ms"])
    if fsync > ETCD_FSYNC_ALERT_MS:
        findings.append(_finding("etcd/wal-fsync-slow", "major", f"WAL fsync p99 {payload['wal_fsync_p99_ms']} ms exceeds the {ETCD_FSYNC_ALERT_MS} ms alert threshold"))
    elif fsync > ETCD_FSYNC_GUIDE_MS:
        findings.append(_finding("etcd/wal-fsync-above-guidance", "minor", f"WAL fsync p99 {payload['wal_fsync_p99_ms']} ms exceeds the {ETCD_FSYNC_GUIDE_MS} ms guidance"))
    if "backend_commit_p99_ms" in payload:
        commit = _dec(payload["backend_commit_p99_ms"])
        if commit > ETCD_COMMIT_ALERT_MS:
            findings.append(_finding("etcd/backend-commit-slow", "major", f"backend commit p99 {payload['backend_commit_p99_ms']} ms exceeds the {ETCD_COMMIT_ALERT_MS} ms alert threshold"))
        elif commit > ETCD_COMMIT_GUIDE_MS:
            findings.append(_finding("etcd/backend-commit-above-guidance", "minor", f"backend commit p99 {payload['backend_commit_p99_ms']} ms exceeds the {ETCD_COMMIT_GUIDE_MS} ms guidance"))
    voters = [m for m in payload["members"] if not m.get("learner", False)]
    healthy = [m for m in voters if m["healthy"]]
    quorum = len(voters) // 2 + 1
    tolerable = max(0, len(healthy) - quorum)
    for member in sorted(payload["members"], key=lambda m: m["name"]):
        if not member["healthy"] and member.get("learner", False):
            findings.append(_finding(f"etcd/member/{member['name']}/learner-unhealthy", "info", f"learner {member['name']} is unhealthy; it does not count toward quorum"))
    if not voters:
        raise _Bad("members", "at least one member must be a voting member (not a learner)")
    if len(healthy) < quorum:
        findings.append(_finding("etcd/no-quorum", "blocker", f"{len(healthy)} of {len(voters)} voting members are healthy; quorum needs {quorum}"))
    elif len(voters) > 1 and tolerable == 0:
        findings.append(_finding("etcd/no-failure-tolerance", "major", f"{len(healthy)} of {len(voters)} voting members healthy (quorum {quorum}) leaves no failure tolerance: one more failure or restart loses quorum"))
    for member in sorted(voters, key=lambda m: m["name"]):
        if not member["healthy"]:
            findings.append(_finding(f"etcd/member/{member['name']}/unhealthy", "minor", f"voting member {member['name']} is unhealthy"))
    if len(voters) == 1:
        findings.append(_finding("etcd/single-member", "minor", "a single voting member has no fault tolerance"))
    elif len(voters) % 2 == 0:
        findings.append(_finding("etcd/even-members", "minor", f"{len(voters)} voting members tolerate {(len(voters) - 1) // 2} failures, the same as {len(voters) - 1}"))
    worst = _worst(findings)
    return {
        "state": "SUCCESS",
        "verdict": "critical" if worst >= SEVERITY_RANK["blocker"] else "degraded" if worst >= SEVERITY_RANK["major"] else "healthy",
        "quota_used_pct": round(float(used_pct), 2),
        "fragmentation_pct": fragmentation,
        "voters": len(voters),
        "healthy_voters": len(healthy),
        "quorum": quorum,
        "tolerable_failures": tolerable,
        "findings": _ordered(findings),
    }


def etcd_health(payload: JsonObject) -> JsonObject:
    """Quota use >= 80% major (house) and > 95% blocker (etcd mixin alerts fire above 95%; exactly 95% is major); leader changes at 4 or more per 15 min, WAL fsync p99 above 500 ms or commit p99 above 250 ms major (etcd mixin); fsync above 10 ms and commit above 25 ms are minor (etcd FAQ guidance); quorum from voting members."""

    def work() -> JsonObject:
        try:
            return _etcd_health(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_autoscaler_sanity ------------------------------------------------------------------------------------------

_VPA_UPDATING = ("Recreate", "Auto", "InPlaceOrRecreate")
_METRIC_RESOURCE = {"cpu_utilization": "cpu", "memory_utilization": "memory"}


def _autoscaler_sanity(payload: JsonObject) -> JsonObject:
    hpas, pools = payload["hpas"], payload.get("pools", [])
    _unique(hpas, "name", "hpas", "HPA name")
    _unique(pools, "name", "pools", "pool name")
    by_pool = {pool["name"]: pool for pool in pools}
    findings: list[Finding] = []
    demand: dict[str, list[int]] = {name: [0, 0] for name in by_pool}
    for hpa in sorted(hpas, key=lambda h: h["name"]):
        name = hpa["name"]
        if hpa["min_replicas"] > hpa["max_replicas"]:
            raise _Bad("hpas", f"HPA {name!r}: min_replicas exceeds max_replicas")
        metric, resource = hpa["metric"], _METRIC_RESOURCE.get(hpa["metric"])
        if resource and "target_pct" not in hpa:
            raise _Bad("hpas", f"HPA {name!r}: target_pct is required for {metric}")
        if "pool" in hpa and hpa["pool"] not in by_pool:
            raise _Bad("hpas", f"HPA {name!r} references unknown pool {hpa['pool']!r}")
        if (resource == "cpu" and hpa["cpu_request_m"] == 0) or (resource == "memory" and hpa["memory_request_mib"] == 0):
            findings.append(_finding(f"hpa/{name}/no-request", "blocker", f"HPA {name} scales on {resource} utilization but its pods have no {resource} request; the metric is unavailable and it never scales"))
        if hpa["min_replicas"] == hpa["max_replicas"]:
            findings.append(_finding(f"hpa/{name}/fixed", "minor", f"HPA {name} has min_replicas == max_replicas ({hpa['max_replicas']}) and cannot scale"))
        elif hpa["current_replicas"] >= hpa["max_replicas"]:
            findings.append(_finding(f"hpa/{name}/at-max", "major", f"HPA {name} is at max_replicas ({hpa['max_replicas']}); it cannot absorb more load"))
        if resource and hpa["target_pct"] > 100:
            findings.append(_finding(f"hpa/{name}/target-above-request", "minor", f"HPA {name} targets {hpa['target_pct']}% of the request: pods are already over-committed before it scales out"))
        mode = hpa.get("vpa_mode", "none")
        controlled = hpa.get("vpa_resources", ["cpu", "memory"])
        if resource and resource in controlled and mode in _VPA_UPDATING:
            findings.append(_finding(f"hpa/{name}/vpa-conflict", "major", f"VPA mode {mode} rewrites {resource} requests while HPA {name} scales on {resource} utilization; the loops fight (use VPA Off or another metric)"))
        elif resource and resource in controlled and mode == "Initial":
            findings.append(_finding(f"hpa/{name}/vpa-initial", "minor", f"VPA Initial changes {resource} requests of new pods, shifting the utilization HPA {name} computes"))
        if "pool" in hpa:
            demand[hpa["pool"]][0] += hpa["max_replicas"] * hpa["cpu_request_m"]
            demand[hpa["pool"]][1] += hpa["max_replicas"] * hpa["memory_request_mib"]
    managers: set[str] = set()
    for pool in sorted(pools, key=lambda p: p["name"]):
        name = pool["name"]
        if pool["min_nodes"] > pool["max_nodes"]:
            raise _Bad("pools", f"pool {name!r}: min_nodes exceeds max_nodes")
        managed = pool["managed_by"] != "none"
        if managed:
            managers.add(pool["managed_by"])
            if pool["min_nodes"] == pool["max_nodes"]:
                findings.append(_finding(f"pool/{name}/fixed", "minor", f"pool {name} is managed by {pool['managed_by']} but min_nodes == max_nodes ({pool['max_nodes']})"))
            if pool["current_nodes"] >= pool["max_nodes"]:
                findings.append(_finding(f"pool/{name}/at-max", "major", f"pool {name} has {pool['current_nodes']} node(s), at max_nodes {pool['max_nodes']}; pending pods cannot trigger a scale-up"))
        ceiling = pool["max_nodes"] if managed else pool["current_nodes"]
        cpu_demand, mem_demand = demand[name]
        if cpu_demand > ceiling * pool["allocatable_cpu_m"] or mem_demand > ceiling * pool["allocatable_memory_mib"]:
            findings.append(_finding(f"pool/{name}/hpa-demand-exceeds-ceiling", "major", f"HPAs on pool {name} can request {cpu_demand} mCPU and {mem_demand} MiB at max replicas, more than its {ceiling}-node ceiling offers before daemonsets"))
    if len(managers) > 1:
        findings.append(_finding("cluster/two-node-autoscalers", "minor", "Cluster Autoscaler and Karpenter both manage pools: make sure they own disjoint node sets"))
    worst = _worst(findings)
    return {
        "state": "SUCCESS",
        "hpa_count": len(hpas),
        "pool_count": len(pools),
        "verdict": "misconfigured" if worst >= SEVERITY_RANK["major"] else "attention" if worst >= SEVERITY_RANK["minor"] else "ok",
        "findings": _ordered(findings),
    }


def autoscaler_sanity(payload: JsonObject) -> JsonObject:
    """HPA bounds and metrics versus requests and VPA, node pool min/max versus current size and the HPA demand ceiling."""

    def work() -> JsonObject:
        try:
            return _autoscaler_sanity(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_cert_expiry ------------------------------------------------------------------------------------------------


def _cert_expiry(payload: JsonObject) -> JsonObject:
    now = _timestamp(payload["now"], "now")
    _unique(payload["certs"], "name", "certs", "certificate name")
    if not payload["certs"]:
        raise _Bad("certs", "at least one certificate is required")
    rows: list[tuple[datetime, JsonObject]] = []
    counts = {"expired": 0, "critical": 0, "warning": 0, "notice": 0, "ok": 0}
    findings: list[Finding] = []
    not_yet_valid = 0
    for cert in payload["certs"]:
        not_after = _timestamp(cert["not_after"], "certs")
        if "not_before" in cert:
            not_before = _timestamp(cert["not_before"], "certs")
            if not_after <= not_before:
                raise _Bad("certs", f"certificate {cert['name']!r}: not_after must be after not_before")
            if not_before > now:
                not_yet_valid += 1
                findings.append(_finding(f"cert/{cert['name']}/not-yet-valid", "major", f"certificate {cert['name']} is not valid before {cert['not_before']}: check clock skew"))
        is_ca = cert.get("is_ca", False)
        critical, warning, notice = (timedelta(days=d) for d in (CERT_CA_DAYS if is_ca else CERT_LEAF_DAYS))
        remaining = not_after - now
        if remaining < timedelta(0):  # RFC 5280 4.1.2.5: valid from notBefore through notAfter, inclusive
            bucket = "expired"
        elif remaining < critical:
            bucket = "critical"
        elif remaining < warning:
            bucket = "warning"
        elif remaining < notice:
            bucket = "notice"
        else:
            bucket = "ok"
        counts[bucket] += 1
        kind = "CA certificate" if is_ca else "certificate"
        days = _days(remaining)
        if bucket == "expired":
            ago = "less than a day ago" if remaining > -timedelta(days=1) else f"{abs(days)} day(s) ago"
            findings.append(_finding(f"cert/{cert['name']}/expired", "blocker", f"{kind} {cert['name']} expired {ago}"))
        elif bucket == "critical":
            findings.append(_finding(f"cert/{cert['name']}/critical", "major", f"{kind} {cert['name']} expires in {days} day(s)"))
        elif bucket == "warning":
            findings.append(_finding(f"cert/{cert['name']}/warning", "minor", f"{kind} {cert['name']} expires in {days} day(s)"))
        elif bucket == "notice":
            findings.append(_finding(f"cert/{cert['name']}/notice", "info", f"{kind} {cert['name']} expires in {days} day(s)"))
        rows.append((not_after, {"name": cert["name"], "bucket": bucket, "days_remaining": days, "is_ca": is_ca}))
    rows.sort(key=lambda item: (item[0], item[1]["name"]))
    return {
        "state": "SUCCESS",
        "certs": [row for _, row in rows],
        "expired": counts["expired"],
        "critical": counts["critical"],
        "warning": counts["warning"],
        "notice": counts["notice"],
        "ok": counts["ok"],
        "nearest_expiry": rows[0][1]["name"],
        "verdict": "critical" if counts["expired"] or counts["critical"] or not_yet_valid else "attention" if counts["warning"] else "ok",
        "findings": _ordered(findings),
    }


def cert_expiry(payload: JsonObject) -> JsonObject:
    """Leaf certs (house buckets): expired after not_after (inclusive), critical < 7 d, warning < 30 d, notice < 90 d; CA certs 30/180/365 d; a not-yet-valid certificate makes the verdict critical."""

    def work() -> JsonObject:
        try:
            return _cert_expiry(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_quota_fit --------------------------------------------------------------------------------------------------

_RESOURCES = (("cpu", "cpu_m", "cpu_request_m", "cpu_limit_m"), ("memory", "memory_mib", "memory_request_mib", "memory_limit_mib"))


def _quota_fit(payload: JsonObject) -> JsonObject:
    replicas = payload["replicas"]
    hard, used, rng = payload.get("quota_hard", {}), payload.get("quota_used", {}), payload.get("limit_range", {})
    findings: list[Finding] = []
    per_replica = {"requests_cpu_m": 0, "requests_memory_mib": 0, "limits_cpu_m": 0, "limits_memory_mib": 0}
    for container in payload["containers"]:
        cname = container["name"]
        for resource, unit, req_key, lim_key in _RESOURCES:
            request, limit = container[req_key], container[lim_key]
            if limit > 0 and request > limit:
                raise _Bad("containers", f"container {cname!r}: {resource} request exceeds its limit")
            if request == 0 and limit > 0:
                request = limit  # API defaulting: an explicit limit becomes the request
            max_limit = rng.get(f"max_{unit}")
            default_limit = rng.get(f"default_limit_{unit}", max_limit)  # LimitRange API defaulting: Default <- Max
            default_request = rng.get(f"default_request_{unit}", default_limit if default_limit is not None else rng.get(f"min_{unit}"))  # DefaultRequest <- Default <- Min
            if limit == 0 and default_limit is not None:
                limit = default_limit
            if request == 0 and default_request is not None:
                request = default_request
            if limit > 0 and request > limit:
                findings.append(_finding(f"container/{cname}/{resource}-request-above-limit", "blocker", f"container {cname}: LimitRange defaults give a {resource} request {request} above its limit {limit}; the pod is rejected"))
            minimum, maximum = rng.get(f"min_{unit}"), rng.get(f"max_{unit}")
            ratio = rng.get(f"max_ratio_{resource}")
            if minimum is not None:
                if request == 0:
                    findings.append(_finding(f"container/{cname}/{resource}-below-min", "blocker", f"container {cname}: LimitRange minimum {minimum} for {resource} but no request is specified"))
                elif request < minimum or 0 < limit < minimum:
                    findings.append(_finding(f"container/{cname}/{resource}-below-min", "blocker", f"container {cname}: {resource} request {request} / limit {limit} is below the LimitRange minimum {minimum}"))
            if maximum is not None:
                if limit == 0:
                    findings.append(_finding(f"container/{cname}/{resource}-above-max", "blocker", f"container {cname}: LimitRange maximum {maximum} for {resource} but no limit is specified"))
                elif limit > maximum or request > maximum:
                    findings.append(_finding(f"container/{cname}/{resource}-above-max", "blocker", f"container {cname}: {resource} request {request} / limit {limit} is above the LimitRange maximum {maximum}"))
            if ratio is not None:
                if request == 0 or limit == 0:
                    findings.append(_finding(f"container/{cname}/{resource}-ratio", "blocker", f"container {cname}: LimitRange max limit/request ratio {ratio} for {resource} needs both a request and a limit"))
                elif _dec(limit) / _dec(request) > _dec(ratio):
                    findings.append(_finding(f"container/{cname}/{resource}-ratio", "blocker", f"container {cname}: {resource} limit/request ratio {round(float(_dec(limit) / _dec(request)), 4)} exceeds the LimitRange maximum {ratio}"))
            if request == 0 and f"requests_{unit}" in hard:
                findings.append(_finding(f"container/{cname}/{resource}-request-required", "blocker", f"container {cname}: the namespace quota tracks requests.{resource}, so every container must specify a request (or inherit one from a LimitRange)"))
            if limit == 0 and f"limits_{unit}" in hard:
                findings.append(_finding(f"container/{cname}/{resource}-limit-required", "blocker", f"container {cname}: the namespace quota tracks limits.{resource}, so every container must specify a limit (or inherit one from a LimitRange)"))
            per_replica[f"requests_{unit}"] += request
            per_replica[f"limits_{unit}"] += limit
    rejected = bool(findings)
    exceeded = False
    max_fit: list[int] = []
    dimensions = (("pods", "pods", 1),) + tuple((f"{kind}.{resource}", f"{kind}_{unit}", per_replica[f"{kind}_{unit}"])
                                                 for resource, unit, _, _ in _RESOURCES for kind in ("requests", "limits"))
    for label, key, each in dimensions:
        if key not in hard:
            continue
        free = hard[key] - used.get(key, 0)
        if each > 0:
            max_fit.append(max(0, free // each))
        if replicas * each > free:
            exceeded = True
            if free >= 0:
                findings.append(_finding(f"quota/{label}/exceeded", "major", f"{replicas} replica(s) need {replicas * each} of {label} but only {free} of the quota's {hard[key]} remain"))
            else:
                findings.append(_finding(f"quota/{label}/exceeded", "major", f"{replicas} replica(s) need {replicas * each} of {label}, but {used.get(key, 0)} is already used against the quota's {hard[key]}, over by {-free}"))
    return {
        "state": "SUCCESS",
        "verdict": "rejected" if rejected else "over_quota" if exceeded else "fits",
        "effective_requests_cpu_m": per_replica["requests_cpu_m"],
        "effective_requests_memory_mib": per_replica["requests_memory_mib"],
        "effective_limits_cpu_m": per_replica["limits_cpu_m"],
        "effective_limits_memory_mib": per_replica["limits_memory_mib"],
        "max_replicas_fit": min(max_fit) if max_fit else -1,
        "findings": _ordered(findings),
    }


def quota_fit(payload: JsonObject) -> JsonObject:
    """Apply API request-from-limit defaulting, LimitRange API defaulting (default limit from max, default request from default limit or min) and Container min/max/ratio, then the namespace ResourceQuota (a replica set that exactly fills the quota fits). Init containers and pod overhead are not modelled."""

    def work() -> JsonObject:
        try:
            return _quota_fit(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_addon_compatibility ----------------------------------------------------------------------------------------


def _addon_compatibility(payload: JsonObject) -> JsonObject:
    cur, tgt = _minor(payload["current_version"], "current_version"), _minor(payload["target_version"], "target_version")
    if tgt < cur:
        raise _Bad("target_version", "target_version is older than current_version")
    if tgt - cur > UPGRADE_PATH_MINORS:
        raise _Bad("target_version", f"more than {UPGRADE_PATH_MINORS} minor versions ahead of current_version")
    _unique(payload["addons"], "name", "addons", "add-on name")
    path = list(range(cur, tgt + 1))
    rows: list[JsonObject] = []
    findings: list[Finding] = []
    for addon in sorted(payload["addons"], key=lambda a: a["name"]):
        name = addon["name"]
        lo, hi = _minor(addon["min_k8s"], "addons"), _minor(addon["max_k8s"], "addons")
        if lo > hi:
            raise _Bad("addons", f"add-on {name!r}: min_k8s is above max_k8s")
        candidate_keys = ("candidate_version", "candidate_min_k8s", "candidate_max_k8s")
        given = [k for k in candidate_keys if k in addon]
        if given and len(given) != 3:
            raise _Bad("addons", f"add-on {name!r}: give candidate_version, candidate_min_k8s and candidate_max_k8s together")
        cand = None
        if given:
            cand = (_minor(addon["candidate_min_k8s"], "addons"), _minor(addon["candidate_max_k8s"], "addons"))
            if cand[0] > cand[1]:
                raise _Bad("addons", f"add-on {name!r}: candidate_min_k8s is above candidate_max_k8s")
        broken = [m for m in path if not lo <= m <= hi]
        live = not lo <= cur <= hi  # the installed add-on already fails at the running control-plane minor
        status, upgrade_at, needs_migration = "compatible", "", False
        if broken:
            status = "blocked"
            if cand is not None:
                shared_on_path = any(lo <= m <= hi and cand[0] <= m <= cand[1] for m in path)
                for switch in path:
                    installed_ok = all(lo <= m <= hi for m in path if m < switch)
                    candidate_ok = all(cand[0] <= m <= cand[1] for m in path if m >= switch)
                    if installed_ok and candidate_ok:
                        # A live incompatibility can only be swapped at the current minor; that is allowed only when some
                        # minor on the path supports both versions. Otherwise the swap needs an out-of-band migration.
                        swap_ok = shared_on_path if live else lo <= switch <= hi and cand[0] <= switch <= cand[1]
                        if not swap_ok:
                            # The split covers both sides but no control-plane minor supports both versions, so the swap
                            # cannot run while either is installed: the add-on needs an out-of-band migration, not an in-place upgrade.
                            needs_migration = True
                            break
                        status, upgrade_at = "upgrade_required", _label(switch)
                        break
        first_break = _label(broken[0]) if broken else ""
        if status == "blocked":
            if cand is None:
                reason = "no candidate version supplied"
            elif needs_migration:
                reason = "the installed and candidate versions share no supported control-plane version for the swap; migrate the add-on separately from the control-plane upgrade"
            else:
                reason = "the candidate does not cover the whole upgrade path"
            findings.append(_finding(f"addon/{name}/blocked", "blocker", f"{addon['kind']} add-on {name} {addon['version']} supports {_label(lo)}..{_label(hi)} and fails at {first_break}; {reason}"))
        elif status == "upgrade_required":
            when = "before the control-plane upgrade starts" if upgrade_at == _label(cur) else f"while the control plane is at {upgrade_at}"
            if live:
                when = f"now, while the control plane already runs at {upgrade_at}"
            findings.append(_finding(f"addon/{name}/upgrade", "major" if live else "minor", f"upgrade {addon['kind']} add-on {name} to {addon['candidate_version']} {when}; {addon['version']} fails at {first_break}"))
        rows.append({"name": name, "kind": addon["kind"], "status": status, "upgrade_at": upgrade_at, "first_incompatible": first_break})
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("compatible", "upgrade_required", "blocked")}
    return {
        "state": "SUCCESS",
        "addons": rows,
        "compatible_count": counts["compatible"],
        "upgrade_required_count": counts["upgrade_required"],
        "blocked_count": counts["blocked"],
        "verdict": "blocked" if counts["blocked"] else "upgrades_needed" if counts["upgrade_required"] else "ready",
        "findings": _ordered(findings),
    }


def addon_compatibility(payload: JsonObject) -> JsonObject:
    """An add-on must support every minor on the upgrade path; otherwise find the earliest control-plane minor supported by both the installed and candidate versions at which it can be swapped for a candidate version covering the rest."""

    def work() -> JsonObject:
        try:
            return _addon_compatibility(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_fleet_drift ------------------------------------------------------------------------------------------------

_ENV_RANK = {"dev": 0, "test": 1, "staging": 2, "prod": 3}


def _fleet_drift(payload: JsonObject) -> JsonObject:
    latest = _minor(payload["latest_version"], "latest_version")
    allowed_lag = payload.get("max_minor_lag", 1)
    clusters = payload["clusters"]
    _unique(clusters, "name", "clusters", "cluster name")
    for cluster in clusters:
        if cluster["environment"] not in _ENV_RANK:
            raise _Bad("clusters", f"cluster {cluster['name']!r}: environment must be one of {', '.join(_ENV_RANK)}")
    parsed = [(c, _minor(c["version"], "clusters")) for c in sorted(clusters, key=lambda c: c["name"])]
    findings: list[Finding] = []
    rows: list[JsonObject] = []
    for cluster, minor in parsed:
        lag = latest - minor
        name = cluster["name"]
        if lag < 0:
            status = "ahead"
            findings.append(_finding(f"cluster/{name}/ahead", "minor", f"cluster {name} runs {_label(minor)}, newer than latest_version {_label(latest)}: refresh latest_version or check the cluster"))
        elif lag == 0:
            status = "current"
        elif lag <= allowed_lag and lag < UPSTREAM_SUPPORTED_MINORS:
            status = "supported"
        else:
            status = "out_of_policy"
        if lag > allowed_lag:
            findings.append(_finding(f"cluster/{name}/out-of-policy", "major", f"cluster {name} ({_label(minor)}) is {lag} minors behind {_label(latest)}; policy allows {allowed_lag}"))
        if lag >= UPSTREAM_SUPPORTED_MINORS:
            findings.append(_finding(f"cluster/{name}/unsupported-upstream", "blocker", f"cluster {name} ({_label(minor)}) is {lag} minors behind {_label(latest)}: upstream patches only the latest {UPSTREAM_SUPPORTED_MINORS} minors"))
        rows.append({"name": name, "environment": cluster["environment"], "minor": _label(minor), "minor_lag": lag, "status": status})
    for cluster, minor in parsed:
        lower = [(m, c["name"]) for c, m in parsed if _ENV_RANK[c["environment"]] < _ENV_RANK[cluster["environment"]] and m < minor]
        if lower:
            behind_minor, behind_name = min(lower)
            findings.append(_finding(f"cluster/{cluster['name']}/ahead-of-lower-env", "minor", f"{cluster['environment']} cluster {cluster['name']} ({_label(minor)}) is newer than lower-environment cluster {behind_name} ({_label(behind_minor)}): the upgrade was not rehearsed first"))
    spread: dict[int, int] = {}
    for _, minor in parsed:
        spread[minor] = spread.get(minor, 0) + 1
    out = sum(1 for r in rows if r["status"] == "out_of_policy")
    return {
        "state": "SUCCESS",
        "cluster_count": len(rows),
        "compliant_count": len(rows) - out,
        "out_of_policy_count": out,
        "max_minor_lag": max([0] + [r["minor_lag"] for r in rows]),
        "distribution": [{"minor": _label(m), "clusters": spread[m]} for m in sorted(spread)],
        "clusters": rows,
        "verdict": "noncompliant" if _worst(findings) >= SEVERITY_RANK["major"] else "drift" if findings else "compliant",
        "findings": _ordered(findings),
    }


def fleet_drift(payload: JsonObject) -> JsonObject:
    """Minor lag behind latest_version: > max_minor_lag (default 1) is out of policy, >= 3 is also outside upstream patch support; higher environments must not lead lower ones."""

    def work() -> JsonObject:
        try:
            return _fleet_drift(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))

    return _respond(payload, work)


# --- k8s_maintenance_window -----------------------------------------------------------------------------------------


def _clock_minutes(value: str, field: str) -> int:
    match = _CLOCK.match(value)
    if not match:
        raise _Bad(field, "not an HH:MM time")
    return int(match.group(1)) * 60 + int(match.group(2))


def _occurrences(window: JsonObject, around: datetime, days: int) -> Iterator[tuple[str, datetime, datetime]]:
    """Window occurrences whose start is within [-1, days] local days of `around`, as (name, start, end)."""
    zone = timezone(timedelta(minutes=window["utc_offset_minutes"]))
    start_min = _clock_minutes(window["start"], "windows")
    length = (_clock_minutes(window["end"], "windows") - start_min) % 1440
    first = around.astimezone(zone).date() - timedelta(days=1)
    for offset in range(days + 3):
        day = first + timedelta(days=offset)
        if _WEEKDAYS[day.weekday()] in window["weekdays"]:
            begin = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=start_min)
            yield window["name"], begin, begin + timedelta(minutes=length)


def _maintenance_window(payload: JsonObject) -> JsonObject:
    start = _timestamp(payload["proposed_start"], "proposed_start")
    if payload["duration_minutes"] < 1:
        raise _Bad("duration_minutes", "duration_minutes must be at least 1")
    duration = timedelta(minutes=payload["duration_minutes"])
    end = start + duration
    windows = payload["windows"]
    _unique(windows, "name", "windows", "window name")
    for window in windows:
        if _clock_minutes(window["start"], "windows") == _clock_minutes(window["end"], "windows"):
            raise _Bad("windows", f"window {window['name']!r}: start and end must differ")
    blackouts = []
    for blackout in payload.get("blackouts", []):
        b_start, b_end = _timestamp(blackout["start"], "blackouts"), _timestamp(blackout["end"], "blackouts")
        if b_end <= b_start:
            raise _Bad("blackouts", f"blackout {blackout['name']!r}: end must be after start")
        blackouts.append((blackout["name"], b_start, b_end))

    def clashes(begin: datetime, finish: datetime) -> list[tuple[str, datetime, datetime]]:
        return [b for b in blackouts if b[1] < finish and begin < b[2]]

    containing = sorted(name for window in windows for name, ws, we in _occurrences(window, start, 1) if ws <= start and end <= we)
    conflicting = sorted(name for name, _, _ in clashes(start, end))
    earliest: datetime | None = None
    horizon = start + timedelta(days=SEARCH_DAYS)
    for window in windows:
        for _, ws, we in _occurrences(window, start, SEARCH_DAYS):
            candidate = max(ws, start)
            while candidate <= horizon and candidate + duration <= we:
                hit = clashes(candidate, candidate + duration)
                if not hit:
                    if earliest is None or candidate < earliest:
                        earliest = candidate
                    break
                candidate = max(b[2] for b in hit)
    findings: list[Finding] = []
    if containing and not conflicting:
        verdict = "allowed"
    elif containing:
        verdict = "blackout"
    else:
        verdict = "outside_window"
    if verdict == "blackout" or (verdict == "outside_window" and conflicting):
        findings.append(_finding("change/blackout", "major", f"the change overlaps blackout(s): {', '.join(conflicting)}"))
    if verdict == "outside_window":
        findings.append(_finding("change/outside-window", "major", "the change does not fit entirely inside one maintenance window occurrence"))
    if earliest is None and verdict != "allowed":
        findings.append(_finding("change/no-slot", "major", f"no compliant start within {SEARCH_DAYS} days; a window may be shorter than the change or fully blacked out"))
    return {
        "state": "SUCCESS",
        "allowed": verdict == "allowed",
        "verdict": verdict,
        "window": containing[0] if containing else "",
        "conflicting_blackouts": conflicting,
        "earliest_compliant_start": earliest.astimezone(timezone.utc).isoformat() if earliest is not None else "",
        "findings": _ordered(findings),
    }


def maintenance_window(payload: JsonObject) -> JsonObject:
    """The whole [start, start + duration) interval must lie inside one occurrence of a weekly window and must not overlap any blackout (touching is allowed); the next compliant start within 14 days is reported."""

    def work() -> JsonObject:
        try:
            return _maintenance_window(payload)
        except _Bad as bad:
            return _invalid(bad.field, str(bad))
        except OverflowError:  # a date near year 1 or 9999 cannot take the duration, the search horizon or a local-date shift
            return _invalid("proposed_start", "too close to the limits of the supported date range")

    return _respond(payload, work)
