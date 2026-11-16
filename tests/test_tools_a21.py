"""Behaviour tests for the A21 Kubernetes Cluster Operator tools: k8s_version_skew, k8s_deprecated_api_scan, k8s_nodepool_capacity,
k8s_upgrade_plan, k8s_drain_feasibility, k8s_etcd_health, k8s_autoscaler_sanity, k8s_cert_expiry, k8s_quota_fit,
k8s_addon_compatibility, k8s_fleet_drift and k8s_maintenance_window.

Every assertion is on output values for specific inputs, boundaries, INVALID_INPUT fields and callers. The registry
document is the live registry plus this lane's intake fragment while it is unmerged, so the file passes before and
after `scripts/_registry_intake.py --apply` merges and deletes the fragment.

tests/conftest.py evicts swarm* modules between tests: modules are resolved through importlib at call time.
"""
from __future__ import annotations

import copy
import functools
import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT, SLUG, PREFIX = "A21", "a21-cluster-ops", "k8s_"
INTAKE = ROOT / "registry" / "intake"
TOOL_NAMES = [
    "k8s_version_skew", "k8s_deprecated_api_scan", "k8s_nodepool_capacity", "k8s_upgrade_plan", "k8s_drain_feasibility",
    "k8s_etcd_health", "k8s_autoscaler_sanity", "k8s_cert_expiry", "k8s_quota_fit", "k8s_addon_compatibility",
    "k8s_fleet_drift", "k8s_maintenance_window",
]


def _registry():
    return importlib.import_module("swarm.tool_registry")


def _fragment_entries() -> list[dict]:
    """Entries still waiting in the lane's intake fragment(s): <slug>.tools.json and <slug>.<shard>.tools.json."""
    entries: list[dict] = []
    for path in sorted(INTAKE.glob(f"{SLUG}.*tools.json")):
        entries.extend(json.loads(path.read_text(encoding="utf-8")))
    return entries


@functools.cache
def lane_document() -> dict:
    """The registry plus this lane's fragment entries that are not merged yet."""
    document = copy.deepcopy(_registry().load_document())
    agent = next(a for a in document["agents"] if a["id"] == AGENT)
    present = {t["name"] for t in agent["registered_tools"]}
    agent["registered_tools"].extend(copy.deepcopy(e) for e in _fragment_entries() if e["name"] not in present)
    return document


def lane_tools() -> list[dict]:
    return next(a for a in lane_document()["agents"] if a["id"] == AGENT)["registered_tools"]


def call(name: str, payload, caller: str = AGENT) -> dict:
    return _registry().call_tool(name, caller, payload, data=lane_document())


def ok(name: str, payload: dict) -> dict:
    result = call(name, payload)
    assert result["state"] == "SUCCESS", result
    return result


def invalid(name: str, payload, field: str) -> dict:
    result = call(name, payload)
    assert result["state"] == "INVALID_INPUT" and result["field"] == field, result
    return result


# --- registration ---------------------------------------------------------------------------------------------


def test_lane_registers_exactly_its_tools_as_read_only_owner_calls():
    tools = lane_tools()
    assert [t["name"] for t in tools] == TOOL_NAMES
    assert 10 <= len(tools) <= 15
    for tool in tools:
        suffix = tool["name"][len(PREFIX):]
        assert tool["name"].startswith(PREFIX) and tool["owner"] == AGENT and tool["permitted_callers"] == [AGENT]
        assert tool["side_effect_tier"] == "READ" and tool["idempotency"] == "idempotent"
        assert tool["entrypoint"] == f"swarm/tools/{AGENT.lower()}_tools.py"
        assert tool["handler"] == f"swarm.tools.{AGENT.lower()}_tools:{suffix}"
        assert "probe" in tool["input_schema"]["properties"] and "|" not in tool["purpose"]
        assert tool["output_schema"]["properties"]["state"] == {"type": "string", "const": "SUCCESS"}


def test_lane_entries_validate_in_a_copy_of_the_registry(tmp_path):
    copy_path = tmp_path / "agents.json"
    copy_path.write_text(json.dumps(lane_document()), encoding="utf-8")
    errors = _registry().validate_registry(copy_path)
    mine = [e for e in errors if AGENT in e or any(name in e for name in TOOL_NAMES)]
    assert mine == []


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_refuses_other_callers_and_malformed_payloads(name):
    sample = next(t for t in lane_tools() if t["name"] == name)["examples"]["success"]
    assert call(name, sample, caller="A01")["state"] == "UNAUTHORIZED"
    assert call(name, {})["state"] == "INVALID_INPUT"
    assert call(name, [sample])["field"] == "payload"
    assert call(name, {**sample, "probe": "partial"}) == {"state": "PARTIAL_SUCCESS", "completed_fraction": 0.5}
    assert call(name, {**sample, "probe": "dependency"})["state"] == "DEPENDENCY_UNAVAILABLE"


def finds(result: dict) -> list[tuple[str, str]]:
    return [(f["id"], f["severity"]) for f in result["findings"]]


def sev(result: dict, fid: str) -> str:
    return dict(finds(result))[fid]


# --- k8s_version_skew -----------------------------------------------------------------------------------------


def skew(api, *components, **extra):
    return call("k8s_version_skew", {"apiserver_versions": api, "components": list(components), **extra})


def comp(kind, version, name="c1", node=None):
    out = {"name": name, "kind": kind, "version": version}
    if node:
        out["node"] = node
    return out


def test_skew_compliant_fleet_reports_lag_and_upgrade_safety():
    result = skew(["v1.30.2-gke.1000"], comp("kubelet", "1.29.4", "a"), comp("kubelet", "1.28.0", "b"), comp("kubectl", "1.31.0", "k"))
    assert result == {
        "state": "SUCCESS", "apiserver_oldest": "1.30", "apiserver_newest": "1.30", "components_checked": 3, "kubelet_count": 2,
        "max_kubelet_lag": 2, "control_plane_upgrade_safe": True, "verdict": "compliant", "findings": [],
    }


def test_skew_kubelet_three_behind_is_at_the_limit_and_four_is_a_violation():
    at_limit = ok("k8s_version_skew", {"apiserver_versions": ["1.30.0"], "components": [comp("kubelet", "1.27.1", "n1")]})
    assert finds(at_limit) == [("kubelet/n1/at-skew-limit", "minor")]
    assert at_limit["verdict"] == "compliant" and at_limit["control_plane_upgrade_safe"] is False and at_limit["max_kubelet_lag"] == 3
    too_old = ok("k8s_version_skew", {"apiserver_versions": ["1.30.0"], "components": [comp("kubelet", "1.26.9", "n1")]})
    assert finds(too_old) == [("kubelet/n1/too-old", "major")] and too_old["verdict"] == "violations"


def test_skew_limit_below_1_25_is_two_minors_and_1_25_itself_gets_three():
    assert finds(ok("k8s_version_skew", {"apiserver_versions": ["1.26.0"], "components": [comp("kubelet", "1.24.0", "n")]})) == [("kubelet/n/at-skew-limit", "minor")]
    assert finds(ok("k8s_version_skew", {"apiserver_versions": ["1.26.0"], "components": [comp("kubelet", "1.23.0", "n")]})) == [("kubelet/n/too-old", "major")]
    assert finds(ok("k8s_version_skew", {"apiserver_versions": ["1.28.0"], "components": [comp("kubelet", "1.25.0", "n")]})) == [("kubelet/n/at-skew-limit", "minor")]
    assert finds(ok("k8s_version_skew", {"apiserver_versions": ["1.27.0"], "components": [comp("kube-proxy", "1.24.0", "p")]})) == [("kube-proxy/p/too-old", "major")]


def test_skew_kubelet_newer_than_any_apiserver_is_a_blocker():
    result = ok("k8s_version_skew", {"apiserver_versions": ["1.30.1", "1.29.9"], "components": [comp("kubelet", "1.30.0", "n1")]})
    assert finds(result) == [("kubelet/n1/newer-than-apiserver", "blocker")]
    assert result["apiserver_oldest"] == "1.29" and result["apiserver_newest"] == "1.30"


def test_skew_ha_apiserver_spread_and_lag_measured_from_the_newest():
    assert finds(ok("k8s_version_skew", {"apiserver_versions": ["1.31.0", "1.29.0"], "components": [comp("kubectl", "1.30.0")]})) == [("apiserver/spread", "major"), ("kubectl/c1/upgrade-breach", "minor")]
    result = ok("k8s_version_skew", {"apiserver_versions": ["1.30.0", "1.29.0"], "components": [comp("kubelet", "1.27.0", "n")]})
    assert finds(result) == [("kubelet/n/at-skew-limit", "minor")]


def test_skew_controller_manager_scheduler_and_ccm_may_be_one_minor_behind():
    base = {"apiserver_versions": ["1.30.0"]}
    assert ok("k8s_version_skew", {**base, "components": [comp("kube-controller-manager", "1.30.1", "m")]})["findings"] == []
    assert finds(ok("k8s_version_skew", {**base, "components": [comp("kube-scheduler", "1.29.1", "s")]})) == [("kube-scheduler/s/upgrade-breach", "minor"), ("kube-scheduler/s/one-behind", "info")]
    assert finds(ok("k8s_version_skew", {**base, "components": [comp("cloud-controller-manager", "1.28.1", "c")]})) == [("cloud-controller-manager/c/too-old", "major"), ("cloud-controller-manager/c/upgrade-breach", "minor")]
    assert finds(ok("k8s_version_skew", {**base, "components": [comp("kube-scheduler", "1.31.0", "s")]})) == [("kube-scheduler/s/newer-than-apiserver", "blocker")]


def test_skew_kubectl_is_allowed_one_minor_either_way():
    for version, expected in (("1.29.0", [("kubectl/k/upgrade-breach", "minor")]), ("1.31.0", []), ("1.32.0", [("kubectl/k/skew", "minor")]), ("1.28.0", [("kubectl/k/skew", "minor"), ("kubectl/k/upgrade-breach", "minor")])):
        assert finds(ok("k8s_version_skew", {"apiserver_versions": ["1.30.0"], "components": [comp("kubectl", version, "k")]})) == expected


def test_skew_upgrade_safety_checks_control_plane_kinds_after_the_hop():
    # F-01: the hop moves each kube-apiserver one minor up; controller-manager/scheduler/CCM, kubectl and the apiserver spread must stay within policy.
    result = skew(["1.30"], comp("kube-controller-manager", "1.29", "cm"), comp("kubelet", "1.30", "k1", "n1"))
    assert result["control_plane_upgrade_safe"] is False and result["verdict"] == "compliant"
    assert finds(result) == [("kube-controller-manager/cm/upgrade-breach", "minor"), ("kube-controller-manager/cm/one-behind", "info")]
    assert result["findings"][0]["summary"].startswith("kube-controller-manager cm is 1.29; after the upgrade")
    safe = skew(["1.30.0"], comp("kube-controller-manager", "1.30.1", "m"), comp("kube-scheduler", "1.30.0", "s"), comp("cloud-controller-manager", "1.30.0", "c"), comp("kubectl", "1.31.0", "k"))
    assert safe["control_plane_upgrade_safe"] is True and safe["findings"] == []
    spread = skew(["1.31.0", "1.29.0"], comp("kubelet", "1.29.0", "n"))
    assert spread["control_plane_upgrade_safe"] is False and finds(spread) == [("apiserver/spread", "major")]
    ha = skew(["1.30.0", "1.31.0"], comp("kube-scheduler", "1.30.0", "s"))
    assert ha["control_plane_upgrade_safe"] is False and finds(ha) == [("kube-scheduler/s/upgrade-breach", "minor"), ("kube-scheduler/s/one-behind", "info")]
    far = skew(["1.31.0", "1.32.0"], comp("kubectl", "1.27.0", "c0"))
    assert far["control_plane_upgrade_safe"] is False and finds(far) == [("kubectl/c0/skew", "minor"), ("kubectl/c0/upgrade-breach", "minor")]


def test_skew_upgrade_safety_rejects_kubelet_newer_than_the_post_hop_oldest_apiserver():
    # R-01: after the hop the oldest kube-apiserver is 1.31, so a 1.32 kubelet or kube-proxy breaches the upgrade.
    newer = skew(["1.30", "1.31"], comp("kubelet", "1.32", "k", "n"))
    assert newer["control_plane_upgrade_safe"] is False and newer["verdict"] == "violations"
    assert finds(newer) == [("kubelet/k/newer-than-apiserver", "blocker")]
    assert skew(["1.30", "1.31"], comp("kube-proxy", "1.32", "p", "n"))["control_plane_upgrade_safe"] is False
    assert skew(["1.30", "1.31"], comp("kubelet", "1.31", "k", "n"))["control_plane_upgrade_safe"] is True


def test_skew_kube_proxy_is_paired_with_the_kubelet_on_its_node():
    result = ok("k8s_version_skew", {"apiserver_versions": ["1.30.0"], "components": [
        comp("kubelet", "1.30.0", "kl", "n1"), comp("kube-proxy", "1.26.0", "kp", "n1")]})
    assert ("kube-proxy/kp/kubelet-skew", "major") in finds(result) and ("kube-proxy/kp/too-old", "major") in finds(result)
    paired_ok = ok("k8s_version_skew", {"apiserver_versions": ["1.30.0"], "components": [
        comp("kubelet", "1.30.0", "kl", "n1"), comp("kube-proxy", "1.28.0", "kp", "n1")]})
    assert paired_ok["findings"] == []
    legacy = ok("k8s_version_skew", {"apiserver_versions": ["1.24.0"], "components": [
        comp("kubelet", "1.24.0", "kl", "n1"), comp("kube-proxy", "1.21.0", "kp", "n1")]})
    assert ("kube-proxy/kp/kubelet-skew", "major") in finds(legacy)


def test_skew_rejects_unparseable_versions_and_duplicate_kubelets_naming_the_field():
    invalid("k8s_version_skew", {"apiserver_versions": ["latest"], "components": [comp("kubelet", "1.30.0")]}, "apiserver_versions")
    invalid("k8s_version_skew", {"apiserver_versions": ["2.0.0"], "components": [comp("kubelet", "1.30.0")]}, "apiserver_versions")
    invalid("k8s_version_skew", {"apiserver_versions": ["1.30.0"], "components": [comp("kubelet", "v1")]}, "components")
    invalid("k8s_version_skew", {"apiserver_versions": ["1.30.0"], "components": [comp("kubelet", "1.30.0", "a", "n1"), comp("kubelet", "1.30.0", "b", "n1")]}, "components")


# --- k8s_deprecated_api_scan ----------------------------------------------------------------------------------

# every embedded removal row: (apiVersion, kind used in the manifest, minor that stopped serving it, replacement)
REMOVALS = [
    ("extensions/v1beta1", "DaemonSet", 16, "apps/v1"),
    ("extensions/v1beta1", "Deployment", 16, "apps/v1"),
    ("extensions/v1beta1", "ReplicaSet", 16, "apps/v1"),
    ("extensions/v1beta1", "NetworkPolicy", 16, "networking.k8s.io/v1"),
    ("extensions/v1beta1", "PodSecurityPolicy", 16, "policy/v1beta1"),
    ("apps/v1beta1", "StatefulSet", 16, "apps/v1"),
    ("apps/v1beta2", "DaemonSet", 16, "apps/v1"),
    ("extensions/v1beta1", "Ingress", 22, "networking.k8s.io/v1"),
    ("networking.k8s.io/v1beta1", "Ingress", 22, "networking.k8s.io/v1"),
    ("networking.k8s.io/v1beta1", "IngressClass", 22, "networking.k8s.io/v1"),
    ("admissionregistration.k8s.io/v1beta1", "MutatingWebhookConfiguration", 22, "admissionregistration.k8s.io/v1"),
    ("admissionregistration.k8s.io/v1beta1", "ValidatingWebhookConfiguration", 22, "admissionregistration.k8s.io/v1"),
    ("apiextensions.k8s.io/v1beta1", "CustomResourceDefinition", 22, "apiextensions.k8s.io/v1"),
    ("apiregistration.k8s.io/v1beta1", "APIService", 22, "apiregistration.k8s.io/v1"),
    ("authentication.k8s.io/v1beta1", "TokenReview", 22, "authentication.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "LocalSubjectAccessReview", 22, "authorization.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "SelfSubjectAccessReview", 22, "authorization.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "SelfSubjectRulesReview", 22, "authorization.k8s.io/v1"),
    ("authorization.k8s.io/v1beta1", "SubjectAccessReview", 22, "authorization.k8s.io/v1"),
    ("certificates.k8s.io/v1beta1", "CertificateSigningRequest", 22, "certificates.k8s.io/v1"),
    ("coordination.k8s.io/v1beta1", "Lease", 22, "coordination.k8s.io/v1"),
    ("rbac.authorization.k8s.io/v1beta1", "ClusterRole", 22, "rbac.authorization.k8s.io/v1"),
    ("rbac.authorization.k8s.io/v1alpha1", "RoleBinding", 23, "rbac.authorization.k8s.io/v1"),  # CHANGELOG-1.23 (#104248)
    ("scheduling.k8s.io/v1alpha1", "PriorityClass", 23, "scheduling.k8s.io/v1"),  # CHANGELOG-1.23 (#104248)
    ("scheduling.k8s.io/v1beta1", "PriorityClass", 22, "scheduling.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "CSIDriver", 22, "storage.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "CSINode", 22, "storage.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "StorageClass", 22, "storage.k8s.io/v1"),
    ("storage.k8s.io/v1beta1", "VolumeAttachment", 22, "storage.k8s.io/v1"),
    ("batch/v1beta1", "CronJob", 25, "batch/v1"),
    ("discovery.k8s.io/v1beta1", "EndpointSlice", 25, "discovery.k8s.io/v1"),
    ("events.k8s.io/v1beta1", "Event", 25, "events.k8s.io/v1"),
    ("autoscaling/v2beta1", "HorizontalPodAutoscaler", 25, "autoscaling/v2"),
    ("policy/v1beta1", "PodDisruptionBudget", 25, "policy/v1"),
    ("policy/v1beta1", "PodSecurityPolicy", 25, "Pod Security Admission"),
    ("node.k8s.io/v1beta1", "RuntimeClass", 25, "node.k8s.io/v1"),
    ("autoscaling/v2beta2", "HorizontalPodAutoscaler", 26, "autoscaling/v2"),
    ("flowcontrol.apiserver.k8s.io/v1beta1", "FlowSchema", 26, "flowcontrol.apiserver.k8s.io/v1 (served from 1.29"),
    ("flowcontrol.apiserver.k8s.io/v1beta1", "PriorityLevelConfiguration", 26, "flowcontrol.apiserver.k8s.io/v1 (served from 1.29"),
    ("storage.k8s.io/v1beta1", "CSIStorageCapacity", 27, "storage.k8s.io/v1"),
    ("flowcontrol.apiserver.k8s.io/v1beta2", "FlowSchema", 29, "flowcontrol.apiserver.k8s.io/v1"),
    ("flowcontrol.apiserver.k8s.io/v1beta3", "PriorityLevelConfiguration", 32, "flowcontrol.apiserver.k8s.io/v1"),
]


def scan(target, *resources, **extra):
    return call("k8s_deprecated_api_scan", {"target_version": target, "resources": [
        {"api_version": a, "kind": k, "name": n} for a, k, n in resources], **extra})


@pytest.mark.parametrize(("api_version", "kind", "removed_in", "replacement"), REMOVALS)
def test_removal_table_row_blocks_from_its_minor_and_warns_one_minor_before(api_version, kind, removed_in, replacement):
    at = ok("k8s_deprecated_api_scan", {"target_version": f"1.{removed_in}", "resources": [{"api_version": api_version, "kind": kind, "name": "x"}]})
    assert at["verdict"] == "blocked" and (at["removed_count"], at["upcoming_count"]) == (1, 0)
    assert finds(at) == [(f"api/{api_version}/{kind}/x", "blocker")]
    assert f"removed in 1.{removed_in}" in at["findings"][0]["summary"] and replacement in at["findings"][0]["summary"]
    before = ok("k8s_deprecated_api_scan", {"target_version": f"1.{removed_in - 1}", "resources": [{"api_version": api_version, "kind": kind, "name": "x"}]})
    assert before["verdict"] == "attention" and (before["removed_count"], before["upcoming_count"]) == (0, 1)
    assert finds(before) == [(f"api/{api_version}/{kind}/x", "minor")]
    far = ok("k8s_deprecated_api_scan", {"target_version": f"1.{removed_in - 2}", "resources": [{"api_version": api_version, "kind": kind, "name": "x"}]})
    assert far["verdict"] == "clear" and far["findings"] == []


def test_scan_horizon_widens_or_disables_the_early_warning():
    resource = ("batch/v1beta1", "CronJob", "nightly")
    assert scan("1.24", resource, horizon_minors=0)["verdict"] == "clear"
    assert scan("1.23", resource)["verdict"] == "clear"
    assert scan("1.23", resource, horizon_minors=2)["verdict"] == "attention"


def test_scan_ignores_served_apis_and_unlisted_kinds_of_a_listed_group_version():
    result = ok("k8s_deprecated_api_scan", {"target_version": "1.32", "resources": [
        {"api_version": "apps/v1", "kind": "Deployment", "name": "web"},
        {"api_version": "batch/v1", "kind": "CronJob", "name": "ok"},
        {"api_version": "storage.k8s.io/v1beta1", "kind": "Unlisted", "name": "u"},
        {"api_version": "policy/v1beta1", "kind": "PodDisruptionBudget", "name": "pdb"}]})
    assert result["resources_scanned"] == 4 and result["removed_count"] == 1
    assert finds(result) == [("api/policy/v1beta1/PodDisruptionBudget/pdb", "blocker")]


def test_scan_orders_blockers_before_minor_findings_and_normalizes_the_target():
    result = scan("v1.25.3", ("autoscaling/v2beta2", "HorizontalPodAutoscaler", "z"), ("batch/v1beta1", "CronJob", "a"), ("policy/v1beta1", "PodDisruptionBudget", "b"))
    assert result["target_version"] == "1.25"
    assert finds(result) == [("api/batch/v1beta1/CronJob/a", "blocker"), ("api/policy/v1beta1/PodDisruptionBudget/b", "blocker"),
                             ("api/autoscaling/v2beta2/HorizontalPodAutoscaler/z", "minor")]
    assert (result["removed_count"], result["upcoming_count"], result["verdict"]) == (2, 1, "blocked")


def test_scan_rejects_bad_target_versions_naming_the_field():
    invalid("k8s_deprecated_api_scan", {"target_version": "next", "resources": [{"api_version": "v1", "kind": "Pod", "name": "p"}]}, "target_version")
    invalid("k8s_deprecated_api_scan", {"target_version": "2.1", "resources": [{"api_version": "v1", "kind": "Pod", "name": "p"}]}, "target_version")


# --- k8s_nodepool_capacity ------------------------------------------------------------------------------------


def pool(**over):
    return {"name": "general", "nodes": 4, "allocatable_cpu_m": 4000, "allocatable_memory_mib": 16000,
            "daemonset_cpu_m": 500, "daemonset_memory_mib": 1000, **over}


def wl(replicas, cpu, mem=1000, name="api", pool_name="general"):
    return {"name": name, "pool": pool_name, "replicas": replicas, "cpu_request_m": cpu, "memory_request_mib": mem}


def capacity(pools, workloads, **extra):
    return call("k8s_nodepool_capacity", {"pools": pools, "workloads": workloads, **extra})


def test_capacity_ok_pool_reports_utilization_and_recommended_nodes():
    result = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(6, 1000, 2000)]})
    assert result["pools"] == [{
        "name": "general", "nodes": 4, "requested_cpu_m": 6000, "requested_memory_mib": 12000, "cpu_utilization_pct": 42.86,
        "memory_utilization_pct": 20.0, "utilization_after_failure_pct": 57.14, "status": "ok", "recommended_nodes": 4}]
    assert (result["pools_at_risk"], result["verdict"], result["findings"]) == (0, "ok", [])


def test_capacity_tight_when_the_loss_pushes_utilization_above_the_target():
    result = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(9, 1000)]})
    assert result["pools"][0]["status"] == "tight" and result["pools"][0]["utilization_after_failure_pct"] == 85.71
    assert result["pools"][0]["recommended_nodes"] == 5
    assert finds(result) == [("pool/general/tight", "minor")] and result["verdict"] == "tight"


def test_capacity_target_utilization_boundary_is_inclusive():
    exact = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(3, 2975)]})  # 8925 mCPU = 85% of 3 x 3500
    assert exact["pools"][0]["status"] == "ok" and exact["pools"][0]["utilization_after_failure_pct"] == 85.0
    over = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(2, 2975), wl(1, 2976, name="b")]})
    assert over["pools"][0]["status"] == "tight"
    custom = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(3, 2975)], "max_utilization_pct": 84})
    assert custom["pools"][0]["status"] == "tight"


def test_capacity_at_risk_when_requests_do_not_fit_after_the_failure():
    full = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(3, 3500)]})  # 10500 = exactly 3 nodes
    assert full["pools"][0]["status"] == "tight" and full["pools"][0]["utilization_after_failure_pct"] == 100.0
    result = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(11, 1000)]})
    assert result["pools"][0]["status"] == "at_risk" and result["pools"][0]["utilization_after_failure_pct"] == 104.76
    assert finds(result) == [("pool/general/n-plus-1", "major")] and (result["pools_at_risk"], result["verdict"]) == (1, "at_risk")


def test_capacity_tolerated_failures_and_edge_pools():
    assert ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(13, 1000)], "tolerated_node_failures": 0})["pools"][0]["status"] == "tight"
    assert ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(13, 1000)]})["pools"][0]["status"] == "at_risk"
    single = ok("k8s_nodepool_capacity", {"pools": [pool(nodes=1)], "workloads": [wl(1, 100)]})
    assert single["pools"][0]["status"] == "at_risk" and single["pools"][0]["utilization_after_failure_pct"] == -1.0
    empty = ok("k8s_nodepool_capacity", {"pools": [pool(nodes=0)], "workloads": [wl(1, 100)]})
    assert empty["pools"][0]["cpu_utilization_pct"] == -1.0 and empty["pools"][0]["memory_utilization_pct"] == -1.0
    idle = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(0, 500)]})
    assert idle["pools"][0] == {"name": "general", "nodes": 4, "requested_cpu_m": 0, "requested_memory_mib": 0, "cpu_utilization_pct": 0.0,
                                "memory_utilization_pct": 0.0, "utilization_after_failure_pct": 0.0, "status": "ok", "recommended_nodes": 0}


def test_capacity_flags_pods_larger_than_a_node_and_autoscaler_ceilings():
    result = ok("k8s_nodepool_capacity", {"pools": [pool(max_nodes=4)], "workloads": [wl(9, 1000), wl(1, 4000, name="big")]})
    assert ("workload/big/exceeds-node", "blocker") in finds(result)
    assert ("pool/general/max-nodes", "major") in finds(result)
    assert result["verdict"] == "at_risk"


def test_capacity_memory_can_be_the_binding_resource():
    result = ok("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(7, 100, 6000)]})  # 42000 MiB of 3 x 15000
    assert result["pools"][0]["utilization_after_failure_pct"] == 93.33 and result["pools"][0]["status"] == "tight"
    assert result["pools"][0]["recommended_nodes"] == 5  # ceil(4200000 / (85 * 15000)) = 4, plus one tolerated failure


def test_capacity_rejects_inconsistent_pools_and_workloads():
    invalid("k8s_nodepool_capacity", {"pools": [pool()], "workloads": [wl(1, 100, pool_name="ghost")]}, "workloads")
    invalid("k8s_nodepool_capacity", {"pools": [pool(), pool()], "workloads": [wl(1, 100)]}, "pools")
    invalid("k8s_nodepool_capacity", {"pools": [pool(daemonset_cpu_m=4000)], "workloads": [wl(1, 100)]}, "pools")
    invalid("k8s_nodepool_capacity", {"pools": [pool(nodes=5, max_nodes=4)], "workloads": [wl(1, 100)]}, "pools")


# --- k8s_upgrade_plan -----------------------------------------------------------------------------------------


def upool(name="workers", version="1.29.3", nodes=6, surge=1, unavailable=0, **extra):
    return {"name": name, "version": version, "nodes": nodes, "max_surge": surge, "max_unavailable": unavailable, **extra}


def phases(result):
    return [(s["phase"], s["target_version"], s["batches"]) for s in result["steps"]]


def test_plan_single_minor_upgrade_orders_preflight_control_plane_addons_pools_verify():
    result = ok("k8s_upgrade_plan", {"current_version": "1.29.3", "target_version": "1.30.1", "node_pools": [upool()]})
    assert phases(result) == [("preflight", "1.30.1", 0), ("control_plane", "1.30.1", 0), ("verify", "1.30.1", 0),
                              ("addons", "1.30.1", 0), ("node_pool", "1.30.1", 6), ("verify", "1.30.1", 0)]
    assert [s["order"] for s in result["steps"]] == [1, 2, 3, 4, 5, 6]
    assert (result["step_count"], result["control_plane_hops"], result["node_pool_batches"], result["verdict"], result["findings"]) == (6, 1, 6, "ready", [])


def test_plan_multi_minor_upgrade_has_one_control_plane_hop_per_minor():
    result = ok("k8s_upgrade_plan", {"current_version": "1.27.5", "target_version": "1.30", "node_pools": [upool(version="1.27.5")]})
    cp = [s["target_version"] for s in result["steps"] if s["phase"] == "control_plane"]
    assert cp == ["1.28", "1.29", "1.30"] and result["control_plane_hops"] == 3
    assert [s["phase"] for s in result["steps"]].count("node_pool") == 1  # skew 3 at the last hop is still allowed
    assert finds(result) == [("plan/multi-hop", "info")] and result["verdict"] == "ready"


def test_plan_upgrades_a_pool_first_when_the_next_hop_would_break_kubelet_skew():
    result = ok("k8s_upgrade_plan", {"current_version": "1.26.1", "target_version": "1.30.0", "node_pools": [upool(version="1.26.1", nodes=4, surge=2)]})
    assert phases(result) == [
        ("preflight", "1.30.0", 0), ("control_plane", "1.27", 0), ("verify", "1.27", 0), ("control_plane", "1.28", 0), ("verify", "1.28", 0),
        ("control_plane", "1.29", 0), ("verify", "1.29", 0), ("node_pool", "1.29", 2), ("control_plane", "1.30.0", 0), ("verify", "1.30.0", 0),
        ("addons", "1.30.0", 0), ("node_pool", "1.30.0", 2), ("verify", "1.30.0", 0)]
    assert "before the control plane moves to 1.30" in result["steps"][7]["action"] and result["node_pool_batches"] == 4


def test_plan_skew_limit_below_1_25_is_two_minors():
    result = ok("k8s_upgrade_plan", {"current_version": "1.24.0", "target_version": "1.27.0", "node_pools": [upool(version="1.24.0")]})
    pre = [s for s in result["steps"] if s["phase"] == "node_pool"]
    assert [s["target_version"] for s in pre] == ["1.26", "1.27.0"] and result["steps"][5]["phase"] == "node_pool"


def test_plan_upgrades_smallest_pools_first_and_skips_pools_already_at_target():
    result = ok("k8s_upgrade_plan", {"current_version": "1.29.0", "target_version": "1.30.1", "node_pools": [
        upool("big", nodes=10), upool("b-small", nodes=2), upool("a-small", nodes=2), upool("done", version="1.30.2", nodes=3)]})
    names = [s["action"].split()[2] for s in result["steps"] if s["phase"] == "node_pool"]
    assert names == ["a-small", "b-small", "big"]


def test_plan_batches_are_nodes_over_surge_plus_unavailable_rounded_up():
    result = ok("k8s_upgrade_plan", {"current_version": "1.29.0", "target_version": "1.30.0", "node_pools": [upool(nodes=7, surge=2, unavailable=1)]})
    assert result["node_pool_batches"] == 3


def test_plan_patch_only_and_same_version_plans():
    patch = ok("k8s_upgrade_plan", {"current_version": "1.30.1", "target_version": "1.30.4", "node_pools": [upool(version="1.30.1")]})
    assert patch["control_plane_hops"] == 1 and [s["target_version"] for s in patch["steps"] if s["phase"] == "control_plane"] == ["1.30.4"]
    same = ok("k8s_upgrade_plan", {"current_version": "1.30.1", "target_version": "1.30", "node_pools": [upool(version="1.30.1")]})
    assert (same["control_plane_hops"], same["node_pool_batches"]) == (0, 0)
    assert [s["phase"] for s in same["steps"]] == ["preflight", "addons", "verify"]
    assert ok("k8s_upgrade_plan", {"current_version": "1.29.0", "target_version": "1.30.0", "node_pools": []})["node_pool_batches"] == 0


def test_plan_findings_block_on_pdbs_whole_pool_outage_and_skewed_pools():
    pdb = ok("k8s_upgrade_plan", {"current_version": "1.29.0", "target_version": "1.30.0", "node_pools": [upool(pdb_min_disruptions_allowed=0)]})
    assert finds(pdb) == [("pool/workers/pdb-blocks-drain", "major")] and pdb["verdict"] == "blocked"
    assert ok("k8s_upgrade_plan", {"current_version": "1.29.0", "target_version": "1.30.0", "node_pools": [upool(pdb_min_disruptions_allowed=1)]})["verdict"] == "ready"
    whole = ok("k8s_upgrade_plan", {"current_version": "1.29.0", "target_version": "1.30.0", "node_pools": [upool(nodes=2, surge=0, unavailable=2)]})
    assert finds(whole) == [("pool/workers/whole-pool-unavailable", "major")]
    ahead = ok("k8s_upgrade_plan", {"current_version": "1.29.0", "target_version": "1.30.0", "node_pools": [upool(version="1.30.0")]})
    assert finds(ahead) == [("pool/workers/ahead", "blocker")] and ahead["verdict"] == "blocked"
    skewed = ok("k8s_upgrade_plan", {"current_version": "1.30.0", "target_version": "1.31.0", "node_pools": [upool(version="1.26.0")]})
    assert finds(skewed) == [("pool/workers/skew", "blocker")]


def test_plan_rejects_downgrades_long_paths_and_bad_pools_naming_the_field():
    base = {"current_version": "1.29.0", "target_version": "1.30.0", "node_pools": [upool()]}
    invalid("k8s_upgrade_plan", {**base, "target_version": "1.28.9"}, "target_version")
    invalid("k8s_upgrade_plan", {**base, "current_version": "1.30.2", "target_version": "1.30.1"}, "target_version")
    invalid("k8s_upgrade_plan", {**base, "current_version": "1.20.0", "target_version": "1.27.0"}, "target_version")
    assert ok("k8s_upgrade_plan", {**base, "current_version": "1.24.0", "target_version": "1.30.0", "node_pools": []})["control_plane_hops"] == 6
    invalid("k8s_upgrade_plan", {**base, "current_version": "later"}, "current_version")
    invalid("k8s_upgrade_plan", {**base, "node_pools": [upool(surge=0, unavailable=0)]}, "node_pools")
    invalid("k8s_upgrade_plan", {**base, "node_pools": [upool(), upool()]}, "node_pools")
    invalid("k8s_upgrade_plan", {**base, "node_pools": [upool(version="x")]}, "node_pools")


# --- k8s_drain_feasibility ------------------------------------------------------------------------------------


def pdb(name="web", ns="shop", expected=5, healthy=5, pods=1, **budget):
    budget = budget or {"min_available": 4}
    return {"name": name, "namespace": ns, "expected_pods": expected, "current_healthy": healthy, "pods_on_drained_nodes": pods, **budget}


def drain(*pdbs):
    return call("k8s_drain_feasibility", {"pdbs": list(pdbs)})


def test_drain_free_when_every_pod_fits_the_disruption_budget():
    result = ok("k8s_drain_feasibility", {"pdbs": [pdb(pods=1)]})
    assert result["results"] == [{"name": "web", "namespace": "shop", "desired_healthy": 4, "disruptions_allowed": 1,
                                  "pods_on_drained_nodes": 1, "status": "free", "waves": 1}]
    assert (result["verdict"], result["max_waves"], result["blocked_count"], result["throttled_count"], result["affected_count"], result["findings"]) == ("feasible", 1, 0, 0, 1, [])


def test_drain_throttled_when_pods_exceed_the_allowed_disruptions():
    result = ok("k8s_drain_feasibility", {"pdbs": [pdb(pods=2)]})
    assert result["results"][0]["status"] == "throttled" and result["results"][0]["waves"] == 2
    assert finds(result) == [("pdb/shop/web", "minor")] and (result["verdict"], result["max_waves"]) == ("throttled", 2)
    assert ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=10, healthy=10, pods=5, min_available=7)]})["results"][0]["waves"] == 2  # 3 allowed, ceil(5/3)


def test_drain_blocked_when_no_disruption_is_allowed():
    result = ok("k8s_drain_feasibility", {"pdbs": [pdb(min_available=5)]})
    assert result["results"][0]["status"] == "blocked" and result["results"][0]["disruptions_allowed"] == 0
    assert finds(result) == [("pdb/shop/web", "blocker")] and result["verdict"] == "blocked" and result["blocked_count"] == 1
    degraded = ok("k8s_drain_feasibility", {"pdbs": [pdb(healthy=4, min_available=4)]})
    assert "already below the expected pod count" in degraded["findings"][0]["summary"]


def test_drain_single_replica_with_full_availability_is_blocked_unless_no_pod_is_on_the_node():
    assert ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=1, healthy=1, min_available=1)]})["verdict"] == "blocked"
    idle = ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=1, healthy=1, pods=0, min_available=1)]})
    assert idle["results"][0]["status"] == "unaffected" and (idle["verdict"], idle["max_waves"], idle["affected_count"]) == ("feasible", 0, 0)


def test_drain_disruption_boundary_pods_equal_to_allowed_is_free_one_more_is_throttled():
    assert ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=6, healthy=6, pods=2, min_available=4)]})["results"][0]["status"] == "free"
    assert ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=6, healthy=6, pods=3, min_available=4)]})["results"][0]["status"] == "throttled"


@pytest.mark.parametrize(("budget", "expected", "healthy", "desired", "allowed"), [
    ({"min_available_percent": 50}, 5, 5, 3, 2),      # 2.5 rounds up
    ({"min_available_percent": 100}, 4, 4, 4, 0),
    ({"max_unavailable_percent": 25}, 10, 10, 7, 3),  # 2.5 unavailable rounds up to 3
    ({"max_unavailable_percent": 10}, 5, 5, 4, 1),    # 0.5 rounds up to 1
    ({"max_unavailable": 1}, 3, 3, 2, 1),
    ({"max_unavailable": 5}, 3, 3, 0, 3),
    ({"max_unavailable": 1}, 3, 2, 2, 0),             # an already unhealthy pod uses the budget
    ({"min_available": 2}, 3, 1, 2, 0),               # healthy below desired never goes negative
])
def test_drain_budget_semantics(budget, expected, healthy, desired, allowed):
    result = ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=expected, healthy=healthy, pods=1, **budget)]})
    assert (result["results"][0]["desired_healthy"], result["results"][0]["disruptions_allowed"]) == (desired, allowed)


def test_drain_unsatisfiable_min_available_is_flagged_and_results_are_sorted_with_blocked_first_in_verdict():
    result = ok("k8s_drain_feasibility", {"pdbs": [
        pdb(name="b", ns="z", pods=2), pdb(name="a", ns="z", min_available=9, pods=0), pdb(name="c", ns="a", min_available=5)]})
    assert [(r["namespace"], r["name"]) for r in result["results"]] == [("a", "c"), ("z", "a"), ("z", "b")]
    assert ("pdb/z/a/unsatisfiable", "major") in finds(result)
    assert result["verdict"] == "blocked" and (result["blocked_count"], result["throttled_count"], result["pdb_count"]) == (1, 1, 3)


def test_drain_rejects_malformed_budgets_naming_the_field():
    invalid("k8s_drain_feasibility", {"pdbs": [{"name": "w", "namespace": "n", "expected_pods": 3, "current_healthy": 3, "pods_on_drained_nodes": 1}]}, "pdbs")
    invalid("k8s_drain_feasibility", {"pdbs": [pdb(min_available=1, max_unavailable=1)]}, "pdbs")
    invalid("k8s_drain_feasibility", {"pdbs": [pdb(healthy=6)]}, "pdbs")
    invalid("k8s_drain_feasibility", {"pdbs": [pdb(pods=6)]}, "pdbs")
    invalid("k8s_drain_feasibility", {"pdbs": [pdb(), pdb()]}, "pdbs")


# --- k8s_etcd_health ------------------------------------------------------------------------------------------

MIB = 1024**2
GIB = 1024**3


def members(*states, learners=()):
    return [{"name": f"etcd-{i}", "healthy": state, **({"learner": True} if i in learners else {})} for i, state in enumerate(states)]


def etcd(**over):
    payload = {"db_size_bytes": 512 * MIB, "leader_changes_15m": 0, "wal_fsync_p99_ms": 6.5, "members": members(True, True, True)}
    payload.update(over)
    return call("k8s_etcd_health", payload)


def test_etcd_healthy_baseline_values():
    assert etcd() == {"state": "SUCCESS", "verdict": "healthy", "quota_used_pct": 25.0, "fragmentation_pct": -1.0, "voters": 3,
                      "healthy_voters": 3, "quorum": 2, "tolerable_failures": 1, "findings": []}


def test_etcd_quota_major_is_inclusive_at_80_and_blocker_is_strict_above_95_percent():
    assert etcd(quota_bytes=1000, db_size_bytes=799)["findings"] == []
    high = etcd(quota_bytes=1000, db_size_bytes=800)
    assert finds(high) == [("etcd/quota-high", "major")] and high["verdict"] == "degraded" and high["quota_used_pct"] == 80.0
    assert finds(etcd(quota_bytes=1000, db_size_bytes=949)) == [("etcd/quota-high", "major")]
    full = etcd(quota_bytes=1000, db_size_bytes=950)  # exactly 95%: the blocker is strictly above 95
    assert finds(full) == [("etcd/quota-high", "major")] and full["verdict"] == "degraded"
    over = etcd(quota_bytes=1000, db_size_bytes=951)  # 95.1%
    assert finds(over) == [("etcd/quota-exhausted", "blocker")] and over["verdict"] == "critical"


def test_etcd_size_and_quota_above_the_suggested_8_gib():
    assert etcd(db_size_bytes=8 * GIB, quota_bytes=16 * GIB)["findings"] == [
        {"id": "etcd/quota-above-suggested-max", "severity": "minor", "summary": "backend quota is above the suggested 8 GiB maximum"}]
    big = etcd(db_size_bytes=8 * GIB + 1, quota_bytes=16 * GIB)
    assert ("etcd/size-above-suggested-max", "major") in finds(big)
    assert etcd(db_size_bytes=GIB, quota_bytes=8 * GIB)["findings"] == []


def test_etcd_fragmentation_reports_the_free_share_and_ignores_empty_files():
    half = etcd(db_size_bytes=400 * MIB, db_size_in_use_bytes=150 * MIB)
    assert finds(half) == [("etcd/defrag-recommended", "minor")] and half["fragmentation_pct"] == 62.5 and half["verdict"] == "healthy"
    assert "(157286400 bytes in use)" in half["findings"][0]["summary"]
    small = etcd(db_size_bytes=99 * MIB, db_size_in_use_bytes=0)
    assert small["findings"] == [] and small["fragmentation_pct"] == 100.0
    assert etcd(db_size_bytes=0, db_size_in_use_bytes=0)["fragmentation_pct"] == -1.0  # an empty file has no meaningful ratio


def test_etcd_disk_latency_boundaries():
    assert etcd(wal_fsync_p99_ms=10)["findings"] == []
    assert finds(etcd(wal_fsync_p99_ms=10.5)) == [("etcd/wal-fsync-above-guidance", "minor")]
    assert finds(etcd(wal_fsync_p99_ms=500)) == [("etcd/wal-fsync-above-guidance", "minor")]
    assert finds(etcd(wal_fsync_p99_ms=500.1)) == [("etcd/wal-fsync-slow", "major")]
    assert etcd(backend_commit_p99_ms=25)["findings"] == []
    assert finds(etcd(backend_commit_p99_ms=25.1)) == [("etcd/backend-commit-above-guidance", "minor")]
    assert finds(etcd(backend_commit_p99_ms=250)) == [("etcd/backend-commit-above-guidance", "minor")]
    assert finds(etcd(backend_commit_p99_ms=250.5)) == [("etcd/backend-commit-slow", "major")]


def test_etcd_member_quorum_rules():
    lost = etcd(members=members(True, False, False))
    assert finds(lost)[0] == ("etcd/no-quorum", "blocker") and lost["verdict"] == "critical" and lost["tolerable_failures"] == 0
    edge = etcd(members=members(True, True, False))
    assert ("etcd/no-failure-tolerance", "major") in finds(edge) and ("etcd/member/etcd-2/unhealthy", "minor") in finds(edge)
    assert edge["verdict"] == "degraded" and (edge["healthy_voters"], edge["quorum"], edge["tolerable_failures"]) == (2, 2, 0)
    five = etcd(members=members(True, True, True, True, False))
    assert finds(five) == [("etcd/member/etcd-4/unhealthy", "minor")] and (five["quorum"], five["tolerable_failures"], five["verdict"]) == (3, 1, "healthy")
    single = etcd(members=members(True))
    assert finds(single) == [("etcd/single-member", "minor")] and single["tolerable_failures"] == 0
    even = etcd(members=members(True, True, True, True))
    assert finds(even) == [("etcd/even-members", "minor")] and (even["quorum"], even["tolerable_failures"]) == (3, 1)
    pair = etcd(members=members(True, True))
    assert ("etcd/no-failure-tolerance", "major") in finds(pair)


def test_etcd_learners_do_not_count_toward_quorum():
    result = etcd(members=members(True, True, True, False, learners=(3,)))
    assert finds(result) == [("etcd/member/etcd-3/learner-unhealthy", "info")] and (result["voters"], result["quorum"]) == (3, 2)


def test_etcd_rejects_inconsistent_inputs_naming_the_field():
    invalid("k8s_etcd_health", {"db_size_bytes": 10, "db_size_in_use_bytes": 11, "leader_changes_15m": 0, "wal_fsync_p99_ms": 1, "members": members(True)}, "db_size_in_use_bytes")
    invalid("k8s_etcd_health", {"db_size_bytes": 10, "leader_changes_15m": 0, "wal_fsync_p99_ms": 1, "members": members(True, learners=(0,))}, "members")
    invalid("k8s_etcd_health", {"db_size_bytes": 10, "leader_changes_15m": 0, "wal_fsync_p99_ms": 1, "members": [{"name": "a", "healthy": True}, {"name": "a", "healthy": True}]}, "members")


# --- k8s_autoscaler_sanity ------------------------------------------------------------------------------------


def hpa(name="web", **over):
    base = {"name": name, "min_replicas": 2, "max_replicas": 10, "current_replicas": 3, "metric": "cpu_utilization", "target_pct": 70,
            "cpu_request_m": 250, "memory_request_mib": 256}
    base.update(over)
    return base


def hpa_check(*hpas, pools=None):
    payload = {"hpas": list(hpas)}
    if pools is not None:
        payload["pools"] = pools
    return call("k8s_autoscaler_sanity", payload)


def apool(name="p", **over):
    base = {"name": name, "min_nodes": 1, "max_nodes": 10, "current_nodes": 3, "allocatable_cpu_m": 1000, "allocatable_memory_mib": 4096,
            "managed_by": "cluster-autoscaler"}
    base.update(over)
    return base


def test_autoscaler_healthy_configuration_is_ok():
    assert ok("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool()]}) == {
        "state": "SUCCESS", "hpa_count": 1, "pool_count": 1, "verdict": "ok", "findings": []}


def test_autoscaler_utilization_metric_needs_a_request():
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa(cpu_request_m=0)]})) == [("hpa/web/no-request", "blocker")]
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa(metric="memory_utilization", memory_request_mib=0)]})) == [("hpa/web/no-request", "blocker")]
    assert ok("k8s_autoscaler_sanity", {"hpas": [hpa(metric="memory_utilization", cpu_request_m=0)]})["findings"] == []
    assert ok("k8s_autoscaler_sanity", {"hpas": [hpa(metric="custom", cpu_request_m=0, memory_request_mib=0)]})["findings"] == []


def test_autoscaler_replica_bounds_and_saturation():
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa(min_replicas=4, max_replicas=4, current_replicas=4)]})) == [("hpa/web/fixed", "minor")]
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa(current_replicas=10)]})) == [("hpa/web/at-max", "major")]
    assert ok("k8s_autoscaler_sanity", {"hpas": [hpa(current_replicas=9)]})["findings"] == []


def test_autoscaler_target_above_the_request_is_minor_100_is_fine():
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa(target_pct=101)]})) == [("hpa/web/target-above-request", "minor")]
    assert ok("k8s_autoscaler_sanity", {"hpas": [hpa(target_pct=100)]})["findings"] == []


@pytest.mark.parametrize(("over", "expected"), [
    ({"vpa_mode": "Auto"}, [("hpa/web/vpa-conflict", "major")]),
    ({"vpa_mode": "Recreate"}, [("hpa/web/vpa-conflict", "major")]),
    ({"vpa_mode": "InPlaceOrRecreate"}, [("hpa/web/vpa-conflict", "major")]),
    ({"vpa_mode": "Initial"}, [("hpa/web/vpa-initial", "minor")]),
    ({"vpa_mode": "Off"}, []),
    ({"vpa_mode": "none"}, []),
    ({"vpa_mode": "Auto", "vpa_resources": ["memory"]}, []),
    ({"vpa_mode": "Auto", "vpa_resources": ["cpu"]}, [("hpa/web/vpa-conflict", "major")]),
    ({"vpa_mode": "Auto", "metric": "custom"}, []),
    ({"vpa_mode": "Auto", "metric": "memory_utilization", "vpa_resources": ["memory"]}, [("hpa/web/vpa-conflict", "major")]),
])
def test_autoscaler_vpa_conflicts_follow_the_scaled_resource(over, expected):
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa(**over)]})) == expected


def test_autoscaler_pool_ceiling_versus_hpa_demand_is_inclusive():
    fits = ok("k8s_autoscaler_sanity", {"hpas": [hpa(max_replicas=20, cpu_request_m=500, pool="p")], "pools": [apool()]})  # 10000 = 10 x 1000
    assert fits["findings"] == []
    over = ok("k8s_autoscaler_sanity", {"hpas": [hpa(max_replicas=20, cpu_request_m=501, pool="p")], "pools": [apool()]})
    assert finds(over) == [("pool/p/hpa-demand-exceeds-ceiling", "major")] and over["verdict"] == "misconfigured"
    memory = ok("k8s_autoscaler_sanity", {"hpas": [hpa(max_replicas=100, cpu_request_m=1, memory_request_mib=410, pool="p")], "pools": [apool()]})
    assert finds(memory) == [("pool/p/hpa-demand-exceeds-ceiling", "major")]
    unmanaged = ok("k8s_autoscaler_sanity", {"hpas": [hpa(max_replicas=10, cpu_request_m=500, pool="p")], "pools": [apool(managed_by="none", current_nodes=3)]})
    assert finds(unmanaged) == [("pool/p/hpa-demand-exceeds-ceiling", "major")]  # 5000 mCPU on a fixed 3-node pool


def test_autoscaler_node_pool_bounds_and_two_autoscalers():
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool(current_nodes=10)]})) == [("pool/p/at-max", "major")]
    assert ok("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool(current_nodes=9)]})["findings"] == []
    assert finds(ok("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool(min_nodes=3, max_nodes=3, current_nodes=2)]})) == [("pool/p/fixed", "minor")]
    assert ok("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool(min_nodes=3, max_nodes=3, managed_by="none")]})["findings"] == []
    both = ok("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool("a"), apool("b", managed_by="karpenter")]})
    assert finds(both) == [("cluster/two-node-autoscalers", "minor")] and both["verdict"] == "attention"


def test_autoscaler_rejects_inconsistent_inputs_naming_the_field():
    invalid("k8s_autoscaler_sanity", {"hpas": [hpa(min_replicas=5, max_replicas=4)]}, "hpas")
    invalid("k8s_autoscaler_sanity", {"hpas": [{k: v for k, v in hpa().items() if k != "target_pct"}]}, "hpas")
    invalid("k8s_autoscaler_sanity", {"hpas": [hpa(pool="ghost")], "pools": [apool()]}, "hpas")
    invalid("k8s_autoscaler_sanity", {"hpas": [hpa(), hpa()]}, "hpas")
    invalid("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool(min_nodes=5, max_nodes=4)]}, "pools")
    invalid("k8s_autoscaler_sanity", {"hpas": [hpa()], "pools": [apool(), apool()]}, "pools")


# --- k8s_cert_expiry ------------------------------------------------------------------------------------------

NOW = "2026-10-20T00:00:00Z"


def certs(*items, now=NOW):
    return call("k8s_cert_expiry", {"now": now, "certs": list(items)})


def cert(name, not_after, **extra):
    return {"name": name, "not_after": not_after, **extra}


@pytest.mark.parametrize(("not_after", "bucket"), [
    ("2026-10-20T00:00:00Z", "critical"),     # RFC 5280: still valid at the not_after instant itself
    ("2026-10-19T23:59:59Z", "expired"),
    ("2026-10-20T00:00:01Z", "critical"),
    ("2026-10-26T23:59:59Z", "critical"),
    ("2026-10-27T00:00:00Z", "warning"),      # exactly 7 days
    ("2026-11-18T23:59:59Z", "warning"),
    ("2026-11-19T00:00:00Z", "notice"),       # exactly 30 days
    ("2027-01-17T23:59:59Z", "notice"),
    ("2027-01-18T00:00:00Z", "ok"),           # exactly 90 days
])
def test_cert_leaf_bucket_boundaries(not_after, bucket):
    assert ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("leaf", not_after)]})["certs"][0]["bucket"] == bucket


@pytest.mark.parametrize(("not_after", "bucket"), [
    ("2026-11-18T23:59:59Z", "critical"),
    ("2026-11-19T00:00:00Z", "warning"),      # exactly 30 days
    ("2027-04-17T23:59:59Z", "warning"),
    ("2027-04-18T00:00:00Z", "notice"),       # exactly 180 days
    ("2027-10-19T23:59:59Z", "notice"),
    ("2027-10-20T00:00:00Z", "ok"),           # exactly 365 days
])
def test_cert_ca_bucket_boundaries(not_after, bucket):
    assert ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("ca", not_after, is_ca=True)]})["certs"][0]["bucket"] == bucket


def test_cert_report_is_sorted_by_expiry_with_counts_and_days():
    result = ok("k8s_cert_expiry", {"now": NOW, "certs": [
        cert("late", "2027-12-01T00:00:00Z"), cert("soon", "2026-10-26T12:00:00Z"), cert("gone", "2026-10-18T00:00:00Z"),
        cert("mid", "2026-11-05T00:00:00Z"), cert("far", "2027-01-01T00:00:00Z")]})
    assert [(c["name"], c["bucket"], c["days_remaining"]) for c in result["certs"]] == [
        ("gone", "expired", -2.0), ("soon", "critical", 6.5), ("mid", "warning", 16.0), ("far", "notice", 73.0), ("late", "ok", 407.0)]
    assert (result["expired"], result["critical"], result["warning"], result["notice"], result["ok"]) == (1, 1, 1, 1, 1)
    assert result["nearest_expiry"] == "gone" and result["verdict"] == "critical"
    assert finds(result) == [("cert/gone/expired", "blocker"), ("cert/soon/critical", "major"), ("cert/mid/warning", "minor"), ("cert/far/notice", "info")]


def test_cert_verdicts_and_ca_flag_in_output():
    assert ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2026-11-10T00:00:00Z")]})["verdict"] == "attention"
    notice_only = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2026-12-20T00:00:00Z")]})
    assert notice_only["verdict"] == "ok" and notice_only["certs"][0]["is_ca"] is False
    assert ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("ca", "2030-01-01T00:00:00Z", is_ca=True)]})["certs"][0]["is_ca"] is True


def test_cert_offsets_are_honoured_and_not_yet_valid_is_flagged():
    result = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2026-10-27T01:00:00+01:00")]})
    assert result["certs"][0]["bucket"] == "warning"
    early = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2027-10-20T00:00:00Z", not_before="2026-10-21T00:00:00Z")]})
    assert finds(early) == [("cert/a/not-yet-valid", "major")]
    assert ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2027-10-20T00:00:00Z", not_before="2026-10-20T00:00:00Z")]})["findings"] == []


def test_cert_rejects_bad_timestamps_and_ranges_naming_the_field():
    invalid("k8s_cert_expiry", {"now": "2026-10-20T00:00:00", "certs": [cert("a", "2027-01-01T00:00:00Z")]}, "now")
    invalid("k8s_cert_expiry", {"now": "yesterday", "certs": [cert("a", "2027-01-01T00:00:00Z")]}, "now")
    invalid("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2027-01-01T00:00:00")]}, "certs")
    invalid("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2027-01-01T00:00:00Z", not_before="2027-01-01T00:00:00Z")]}, "certs")
    invalid("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2027-01-01T00:00:00Z"), cert("a", "2027-02-01T00:00:00Z")]}, "certs")


# --- k8s_quota_fit --------------------------------------------------------------------------------------------


def ctr(name="app", cpu=250, mem=256, cpu_limit=500, mem_limit=512):
    return {"name": name, "cpu_request_m": cpu, "memory_request_mib": mem, "cpu_limit_m": cpu_limit, "memory_limit_mib": mem_limit}


def fit(replicas=3, containers=None, **extra):
    return call("k8s_quota_fit", {"replicas": replicas, "containers": containers or [ctr()], **extra})


def test_quota_fit_reports_effective_values_and_the_replica_capacity():
    result = ok("k8s_quota_fit", {"replicas": 3, "containers": [ctr(), ctr("sidecar", 50, 64, 100, 128)],
                                  "quota_hard": {"requests_cpu_m": 4000, "pods": 10}, "quota_used": {"requests_cpu_m": 1000, "pods": 2}})
    assert result == {"state": "SUCCESS", "verdict": "fits", "effective_requests_cpu_m": 300, "effective_requests_memory_mib": 320,
                      "effective_limits_cpu_m": 600, "effective_limits_memory_mib": 640, "max_replicas_fit": 8, "findings": []}


def test_quota_exact_fill_fits_and_one_more_replica_does_not():
    hard, used = {"requests_cpu_m": 1750}, {"requests_cpu_m": 1000}
    exact = ok("k8s_quota_fit", {"replicas": 3, "containers": [ctr()], "quota_hard": hard, "quota_used": used})
    assert exact["verdict"] == "fits" and exact["max_replicas_fit"] == 3
    over = ok("k8s_quota_fit", {"replicas": 4, "containers": [ctr()], "quota_hard": hard, "quota_used": used})
    assert over["verdict"] == "over_quota" and over["max_replicas_fit"] == 3 and finds(over) == [("quota/requests.cpu/exceeded", "major")]
    assert "need 1000 of requests.cpu but only 750 of the quota's 1750 remain" in over["findings"][0]["summary"]


def test_quota_pods_limits_memory_and_exhausted_quota():
    pods = ok("k8s_quota_fit", {"replicas": 3, "containers": [ctr()], "quota_hard": {"pods": 5}, "quota_used": {"pods": 3}})
    assert pods["verdict"] == "over_quota" and pods["max_replicas_fit"] == 2 and finds(pods) == [("quota/pods/exceeded", "major")]
    memory = ok("k8s_quota_fit", {"replicas": 2, "containers": [ctr()], "quota_hard": {"limits_memory_mib": 1000}})
    assert finds(memory) == [("quota/limits.memory/exceeded", "major")] and memory["max_replicas_fit"] == 1
    spent = ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr()], "quota_hard": {"requests_cpu_m": 500}, "quota_used": {"requests_cpu_m": 800}})
    assert spent["max_replicas_fit"] == 0 and spent["verdict"] == "over_quota" and "but 800 is already used against the quota's 500, over by 300" in spent["findings"][0]["summary"]
    assert ok("k8s_quota_fit", {"replicas": 9, "containers": [ctr()]})["max_replicas_fit"] == -1


def test_quota_requires_requests_and_limits_for_the_resources_it_tracks():
    bare = ctr(cpu=0, mem=0, cpu_limit=0, mem_limit=0)
    cpu = ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "quota_hard": {"requests_cpu_m": 4000}})
    assert finds(cpu) == [("container/app/cpu-request-required", "blocker")] and cpu["verdict"] == "rejected"
    memory = ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(mem_limit=0)], "quota_hard": {"limits_memory_mib": 4096}})
    assert finds(memory) == [("container/app/memory-limit-required", "blocker")] and memory["verdict"] == "rejected"
    assert ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "quota_hard": {"pods": 5}})["verdict"] == "fits"


def test_quota_an_explicit_limit_becomes_the_request():
    result = ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=0, mem=0)], "quota_hard": {"requests_cpu_m": 4000}})
    assert result["verdict"] == "fits" and (result["effective_requests_cpu_m"], result["effective_requests_memory_mib"]) == (500, 512)


def test_quota_limit_range_defaults_fill_missing_requests_and_limits():
    bare = ctr(cpu=0, mem=0, cpu_limit=0, mem_limit=0)
    full = ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "quota_hard": {"requests_cpu_m": 4000, "limits_memory_mib": 4096},
                                "limit_range": {"default_request_cpu_m": 100, "default_request_memory_mib": 128, "default_limit_cpu_m": 500, "default_limit_memory_mib": 512}})
    assert full["verdict"] == "fits" and (full["effective_requests_cpu_m"], full["effective_requests_memory_mib"],
                                          full["effective_limits_cpu_m"], full["effective_limits_memory_mib"]) == (100, 128, 500, 512)
    limits_only = ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "limit_range": {"default_limit_cpu_m": 500, "default_limit_memory_mib": 512}})
    assert (limits_only["effective_requests_cpu_m"], limits_only["effective_requests_memory_mib"]) == (500, 512)  # defaultRequest follows default
    inverted = ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "limit_range": {"default_request_cpu_m": 600, "default_limit_cpu_m": 500}})
    assert ("container/app/cpu-request-above-limit", "blocker") in finds(inverted) and inverted["verdict"] == "rejected"


def test_quota_limit_range_minimum_and_maximum_are_inclusive():
    assert ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=100)], "limit_range": {"min_cpu_m": 100}})["findings"] == []
    low = ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=99)], "limit_range": {"min_cpu_m": 100}})
    assert finds(low) == [("container/app/cpu-below-min", "blocker")] and low["verdict"] == "rejected"
    unset = ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=0, cpu_limit=0)], "limit_range": {"min_cpu_m": 100, "default_request_cpu_m": 0}})
    assert finds(unset) == [("container/app/cpu-below-min", "blocker")] and "no request is specified" in unset["findings"][0]["summary"]
    assert ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu_limit=1000)], "limit_range": {"max_cpu_m": 1000}})["findings"] == []
    assert finds(ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu_limit=1001)], "limit_range": {"max_cpu_m": 1000}})) == [("container/app/cpu-above-max", "blocker")]
    no_limit = ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(mem_limit=0)], "limit_range": {"max_memory_mib": 1024, "default_limit_memory_mib": 0}})
    assert finds(no_limit) == [("container/app/memory-above-max", "blocker")] and "no limit is specified" in no_limit["findings"][0]["summary"]


def test_quota_limit_range_ratio_is_inclusive_and_needs_both_values():
    assert ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=100, cpu_limit=200)], "limit_range": {"max_ratio_cpu": 2}})["findings"] == []
    assert finds(ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=100, cpu_limit=201)], "limit_range": {"max_ratio_cpu": 2}})) == [("container/app/cpu-ratio", "blocker")]
    assert ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=200, cpu_limit=300)], "limit_range": {"max_ratio_cpu": 1.5}})["findings"] == []
    assert finds(ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=200, cpu_limit=301)], "limit_range": {"max_ratio_cpu": 1.5}})) == [("container/app/cpu-ratio", "blocker")]
    assert finds(ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(mem=256, mem_limit=0)], "limit_range": {"max_ratio_memory": 2}})) == [("container/app/memory-ratio", "blocker")]


def test_quota_rejects_a_request_above_its_limit_naming_the_field():
    invalid("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=600, cpu_limit=500)]}, "containers")


# --- k8s_addon_compatibility ----------------------------------------------------------------------------------


def addon(name="calico", kind="cni", lo="1.27", hi="1.32", cand=None):
    out = {"name": name, "kind": kind, "version": "3.27.0", "min_k8s": lo, "max_k8s": hi}
    if cand:
        out.update({"candidate_version": "3.28.1", "candidate_min_k8s": cand[0], "candidate_max_k8s": cand[1]})
    return out


def addons(current, target, *items):
    return call("k8s_addon_compatibility", {"current_version": current, "target_version": target, "addons": list(items)})


def test_addon_covering_the_whole_path_is_compatible():
    result = ok("k8s_addon_compatibility", {"current_version": "1.29.1", "target_version": "v1.31.0", "addons": [addon()]})
    assert result["addons"] == [{"name": "calico", "kind": "cni", "status": "compatible", "upgrade_at": "", "first_incompatible": ""}]
    assert (result["compatible_count"], result["upgrade_required_count"], result["blocked_count"], result["verdict"], result["findings"]) == (1, 0, 0, "ready", [])


def test_addon_range_edges_are_inclusive():
    assert ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.29", hi="1.31")]})["verdict"] == "ready"
    assert ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.30", hi="1.31")]})["verdict"] == "blocked"
    assert ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.29", hi="1.30")]})["verdict"] == "blocked"


def test_addon_upgrade_before_the_control_plane_when_the_candidate_covers_the_path():
    result = ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.27", hi="1.29", cand=("1.28", "1.31"))]})
    assert result["addons"][0] == {"name": "calico", "kind": "cni", "status": "upgrade_required", "upgrade_at": "1.29", "first_incompatible": "1.30"}
    assert finds(result) == [("addon/calico/upgrade", "minor")] and "before the control-plane upgrade starts" in result["findings"][0]["summary"]
    assert (result["upgrade_required_count"], result["verdict"]) == (1, "upgrades_needed")


def test_addon_switch_happens_at_the_earliest_control_plane_minor_that_works():
    result = ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.27", hi="1.30", cand=("1.30", "1.32"))]})
    assert result["addons"][0]["status"] == "upgrade_required" and result["addons"][0]["upgrade_at"] == "1.30" and result["addons"][0]["first_incompatible"] == "1.31"
    assert "while the control plane is at 1.30" in result["findings"][0]["summary"]


def test_addon_blocked_without_a_usable_candidate():
    none = ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.27", hi="1.29")]})
    assert finds(none) == [("addon/calico/blocked", "blocker")] and "no candidate version supplied" in none["findings"][0]["summary"]
    assert (none["blocked_count"], none["verdict"]) == (1, "blocked")
    gap = ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.27", hi="1.29", cand=("1.31", "1.32"))]})
    assert gap["addons"][0]["status"] == "blocked" and "does not cover the whole upgrade path" in gap["findings"][0]["summary"]


def test_addon_already_unsupported_at_the_current_version_is_upgraded_immediately():
    result = ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.30", hi="1.32", cand=("1.28", "1.31"))]})
    assert result["addons"][0]["status"] == "upgrade_required" and result["addons"][0]["upgrade_at"] == "1.29" and result["addons"][0]["first_incompatible"] == "1.29"


def test_addon_results_are_sorted_by_name_and_same_version_target_is_a_single_point_path():
    result = ok("k8s_addon_compatibility", {"current_version": "1.30", "target_version": "1.30", "addons": [
        addon("zeta", "dns", "1.30", "1.30"), addon("alpha", "csi", "1.29", "1.29")]})
    assert [(a["name"], a["kind"], a["status"]) for a in result["addons"]] == [("alpha", "csi", "blocked"), ("zeta", "dns", "compatible")]


def test_addon_rejects_inconsistent_inputs_naming_the_field():
    invalid("k8s_addon_compatibility", {"current_version": "1.30", "target_version": "1.29", "addons": [addon()]}, "target_version")
    invalid("k8s_addon_compatibility", {"current_version": "1.20", "target_version": "1.27", "addons": [addon()]}, "target_version")
    invalid("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.30", "addons": [addon(lo="1.30", hi="1.29")]}, "addons")
    invalid("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.30", "addons": [{**addon(), "candidate_version": "1.0"}]}, "addons")
    invalid("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.30", "addons": [addon(lo="1.27", hi="1.29", cand=("1.32", "1.30"))]}, "addons")
    invalid("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.30", "addons": [addon(), addon()]}, "addons")
    invalid("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.30", "addons": [addon(lo="stable", hi="1.30")]}, "addons")


# --- k8s_fleet_drift ------------------------------------------------------------------------------------------


def cluster(name, environment, version):
    return {"name": name, "environment": environment, "version": version}


def fleet(latest, *clusters, **extra):
    return call("k8s_fleet_drift", {"latest_version": latest, "clusters": list(clusters), **extra})


def test_fleet_within_policy_is_compliant():
    result = ok("k8s_fleet_drift", {"latest_version": "1.31.1", "clusters": [cluster("prod-eu", "prod", "1.30.4"), cluster("dev-eu", "dev", "1.31.1")]})
    assert result["clusters"] == [
        {"name": "dev-eu", "environment": "dev", "minor": "1.31", "minor_lag": 0, "status": "current"},
        {"name": "prod-eu", "environment": "prod", "minor": "1.30", "minor_lag": 1, "status": "supported"}]
    assert (result["cluster_count"], result["compliant_count"], result["out_of_policy_count"], result["max_minor_lag"], result["verdict"], result["findings"]) == (2, 2, 0, 1, "compliant", [])
    assert result["distribution"] == [{"minor": "1.30", "clusters": 1}, {"minor": "1.31", "clusters": 1}]


def test_fleet_lag_boundary_follows_the_policy_and_upstream_support_ends_at_three_minors():
    lag2 = ok("k8s_fleet_drift", {"latest_version": "1.31", "clusters": [cluster("a", "prod", "1.29.0")]})
    assert lag2["clusters"][0]["status"] == "out_of_policy" and finds(lag2) == [("cluster/a/out-of-policy", "major")] and lag2["verdict"] == "noncompliant"
    relaxed = ok("k8s_fleet_drift", {"latest_version": "1.31", "max_minor_lag": 2, "clusters": [cluster("a", "prod", "1.29.0")]})
    assert relaxed["clusters"][0]["status"] == "supported" and relaxed["verdict"] == "compliant" and relaxed["out_of_policy_count"] == 0
    eol = ok("k8s_fleet_drift", {"latest_version": "1.31", "max_minor_lag": 3, "clusters": [cluster("a", "prod", "1.28.0")]})
    assert eol["clusters"][0]["status"] == "out_of_policy" and finds(eol) == [("cluster/a/unsupported-upstream", "blocker")] and eol["verdict"] == "noncompliant"
    both = ok("k8s_fleet_drift", {"latest_version": "1.31", "clusters": [cluster("a", "prod", "1.28.0")]})
    assert finds(both) == [("cluster/a/unsupported-upstream", "blocker"), ("cluster/a/out-of-policy", "major")] and both["max_minor_lag"] == 3


def test_fleet_cluster_newer_than_latest_is_ahead_and_a_drift():
    result = ok("k8s_fleet_drift", {"latest_version": "1.31", "clusters": [cluster("a", "dev", "1.32.0")]})
    assert result["clusters"][0]["status"] == "ahead" and result["clusters"][0]["minor_lag"] == -1 and result["max_minor_lag"] == 0
    assert finds(result) == [("cluster/a/ahead", "minor")] and result["verdict"] == "drift" and result["compliant_count"] == 1


def test_fleet_higher_environments_must_not_lead_lower_ones():
    result = ok("k8s_fleet_drift", {"latest_version": "1.31", "max_minor_lag": 2, "clusters": [
        cluster("prod-a", "prod", "1.31.0"), cluster("stg-a", "staging", "1.30.0"), cluster("dev-a", "dev", "1.29.0")]})
    assert finds(result) == [("cluster/prod-a/ahead-of-lower-env", "minor"), ("cluster/stg-a/ahead-of-lower-env", "minor")]
    assert "dev-a (1.29)" in result["findings"][0]["summary"] and result["verdict"] == "drift"
    same = ok("k8s_fleet_drift", {"latest_version": "1.31", "clusters": [cluster("prod-a", "prod", "1.30.1"), cluster("stg-a", "staging", "1.30.9")]})
    assert same["findings"] == [] and same["verdict"] == "compliant"


def test_fleet_distribution_sorts_minors_numerically_and_rejects_bad_input():
    result = ok("k8s_fleet_drift", {"latest_version": "1.10", "max_minor_lag": 5, "clusters": [cluster("a", "dev", "1.10.0"), cluster("b", "dev", "1.8.2"), cluster("c", "dev", "1.8.0")]})
    assert result["distribution"] == [{"minor": "1.8", "clusters": 2}, {"minor": "1.10", "clusters": 1}]
    invalid("k8s_fleet_drift", {"latest_version": "latest", "clusters": [cluster("a", "dev", "1.30.0")]}, "latest_version")
    invalid("k8s_fleet_drift", {"latest_version": "1.31", "clusters": [cluster("a", "dev", "edge")]}, "clusters")
    invalid("k8s_fleet_drift", {"latest_version": "1.31", "clusters": [cluster("a", "dev", "1.30.0"), cluster("a", "prod", "1.30.0")]}, "clusters")


# --- k8s_maintenance_window -----------------------------------------------------------------------------------

SAT_NIGHT = {"name": "weekend-night", "weekdays": ["sat"], "start": "01:00", "end": "05:00", "utc_offset_minutes": 0}


def window(proposed, minutes, *windows, blackouts=None):
    payload = {"proposed_start": proposed, "duration_minutes": minutes, "windows": list(windows or [SAT_NIGHT])}
    if blackouts is not None:
        payload["blackouts"] = blackouts
    return call("k8s_maintenance_window", payload)


def test_window_change_inside_the_window_is_allowed():
    result = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 120, "windows": [SAT_NIGHT]})
    assert result == {"state": "SUCCESS", "allowed": True, "verdict": "allowed", "window": "weekend-night", "conflicting_blackouts": [],
                      "earliest_compliant_start": "2026-10-24T02:00:00+00:00", "findings": []}


def test_window_boundaries_start_and_end_are_inclusive():
    assert ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T01:00:00Z", "duration_minutes": 240, "windows": [SAT_NIGHT]})["allowed"] is True
    assert ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T03:00:00Z", "duration_minutes": 120, "windows": [SAT_NIGHT]})["allowed"] is True
    late = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T03:00:00Z", "duration_minutes": 121, "windows": [SAT_NIGHT]})
    assert late["allowed"] is False and late["verdict"] == "outside_window" and late["window"] == ""
    assert late["earliest_compliant_start"] == "2026-10-31T01:00:00+00:00" and finds(late) == [("change/outside-window", "major")]
    early = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T00:59:00Z", "duration_minutes": 30, "windows": [SAT_NIGHT]})
    assert early["verdict"] == "outside_window" and early["earliest_compliant_start"] == "2026-10-24T01:00:00+00:00"


def test_window_crossing_midnight_belongs_to_the_day_it_starts_on():
    friday = {"name": "fri-late", "weekdays": ["fri"], "start": "22:00", "end": "02:00", "utc_offset_minutes": 0}
    inside = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T00:30:00Z", "duration_minutes": 60, "windows": [friday]})
    assert inside["allowed"] is True and inside["window"] == "fri-late"
    over = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 30, "windows": [friday]})
    assert over["verdict"] == "outside_window" and over["earliest_compliant_start"] == "2026-10-30T22:00:00+00:00"


def test_window_weekdays_and_clock_are_evaluated_in_the_windows_offset():
    plus = {**SAT_NIGHT, "start": "02:00", "end": "06:00", "utc_offset_minutes": 120}  # Saturday 00:00-04:00 UTC
    assert ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T06:00:00+05:00", "duration_minutes": 60, "windows": [plus]})["allowed"] is True
    minus = {**SAT_NIGHT, "start": "20:00", "end": "23:00", "utc_offset_minutes": -300}  # Saturday evening local is Sunday 01:00-04:00 UTC
    assert ok("k8s_maintenance_window", {"proposed_start": "2026-10-25T02:00:00Z", "duration_minutes": 60, "windows": [minus]})["allowed"] is True
    assert ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 60, "windows": [minus]})["allowed"] is False


def test_window_blackout_blocks_and_the_next_start_skips_past_it():
    blackout = [{"name": "freeze", "start": "2026-10-24T02:30:00Z", "end": "2026-10-24T03:30:00Z"}]
    result = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 120, "windows": [SAT_NIGHT], "blackouts": blackout})
    assert (result["allowed"], result["verdict"], result["window"], result["conflicting_blackouts"]) == (False, "blackout", "weekend-night", ["freeze"])
    assert result["earliest_compliant_start"] == "2026-10-31T01:00:00+00:00" and finds(result) == [("change/blackout", "major")]
    short = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 60, "windows": [SAT_NIGHT], "blackouts": blackout})
    assert short["earliest_compliant_start"] == "2026-10-24T03:30:00+00:00"


def test_window_touching_a_blackout_is_allowed_and_chained_blackouts_are_skipped():
    for start, end in (("2026-10-24T04:00:00Z", "2026-10-24T05:00:00Z"), ("2026-10-24T01:00:00Z", "2026-10-24T02:00:00Z")):
        result = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 120, "windows": [SAT_NIGHT],
                                               "blackouts": [{"name": "edge", "start": start, "end": end}]})
        assert result["allowed"] is True and result["conflicting_blackouts"] == []
    chained = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 60, "windows": [SAT_NIGHT], "blackouts": [
        {"name": "B", "start": "2026-10-24T03:00:00Z", "end": "2026-10-24T03:30:00Z"}, {"name": "A", "start": "2026-10-24T02:30:00Z", "end": "2026-10-24T03:00:00Z"}]})
    assert chained["conflicting_blackouts"] == ["A"] and chained["earliest_compliant_start"] == "2026-10-24T03:30:00+00:00"


def test_window_without_any_compliant_slot_in_14_days():
    short = {**SAT_NIGHT, "end": "02:00"}
    nowhere = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T01:00:00Z", "duration_minutes": 120, "windows": [short]})
    assert nowhere["earliest_compliant_start"] == "" and ("change/no-slot", "major") in finds(nowhere)
    frozen = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 60, "windows": [SAT_NIGHT],
                                           "blackouts": [{"name": "year-end", "start": "2026-10-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}]})
    assert frozen["verdict"] == "blackout" and frozen["earliest_compliant_start"] == "" and ("change/no-slot", "major") in finds(frozen)
    both = ok("k8s_maintenance_window", {"proposed_start": "2026-10-25T02:00:00Z", "duration_minutes": 60, "windows": [SAT_NIGHT],
                                         "blackouts": [{"name": "x", "start": "2026-10-25T01:00:00Z", "end": "2026-10-25T03:00:00Z"}]})
    assert both["verdict"] == "outside_window" and {"change/blackout", "change/outside-window"} <= {i for i, _ in finds(both)}


def test_window_picks_the_alphabetically_first_containing_window_and_earliest_over_all_windows():
    result = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 60,
                                           "windows": [{**SAT_NIGHT, "name": "b-win"}, {**SAT_NIGHT, "name": "a-win"}]})
    assert result["window"] == "a-win"
    sunday = {"name": "sun", "weekdays": ["sun"], "start": "10:00", "end": "12:00", "utc_offset_minutes": 0}
    after = ok("k8s_maintenance_window", {"proposed_start": "2026-10-24T06:00:00Z", "duration_minutes": 60, "windows": [SAT_NIGHT, sunday]})
    assert after["earliest_compliant_start"] == "2026-10-25T10:00:00+00:00"


def test_window_rejects_malformed_inputs_naming_the_field():
    base = {"proposed_start": "2026-10-24T02:00:00Z", "duration_minutes": 60, "windows": [SAT_NIGHT]}
    invalid("k8s_maintenance_window", {**base, "proposed_start": "2026-10-24T02:00:00"}, "proposed_start")
    invalid("k8s_maintenance_window", {**base, "proposed_start": "soon"}, "proposed_start")
    invalid("k8s_maintenance_window", {**base, "windows": [{**SAT_NIGHT, "start": "25:00"}]}, "windows")
    invalid("k8s_maintenance_window", {**base, "windows": [{**SAT_NIGHT, "end": "01:00"}]}, "windows")
    invalid("k8s_maintenance_window", {**base, "windows": [SAT_NIGHT, SAT_NIGHT]}, "windows")
    invalid("k8s_maintenance_window", {**base, "blackouts": [{"name": "b", "start": "2026-10-24T02:00:00Z", "end": "2026-10-24T02:00:00Z"}]}, "blackouts")
    invalid("k8s_maintenance_window", {**base, "blackouts": [{"name": "b", "start": "2026-10-24T02:00:00", "end": "2026-10-24T03:00:00Z"}]}, "blackouts")


# --- independent review regressions (primary sources named per test) --------------------------------------------


def test_review_kubectl_must_be_within_one_minor_of_every_apiserver():
    # https://kubernetes.io/releases/version-skew-policy/ (kubectl): apiservers 1.37 and 1.36 -> kubectl 1.37 and 1.36 only.
    api = ["1.37.0", "1.36.4"]
    for version, flagged in (("1.38.0", True), ("1.37.0", False), ("1.36.9", False), ("1.35.0", True)):
        result = ok("k8s_version_skew", {"apiserver_versions": api, "components": [comp("kubectl", version, "k")]})
        assert (("kubectl/k/skew", "minor") in finds(result)) is flagged, version
    one = ok("k8s_version_skew", {"apiserver_versions": ["1.37.0"], "components": [comp("kubectl", "1.38.0", "k")]})
    assert finds(one) == []


def test_review_leader_changes_alert_at_four_in_fifteen_minutes():
    # etcd-io/etcd contrib/mixin etcdHighNumberOfLeaderChanges: increase(etcd_server_leader_changes_seen_total[15m]) >= 4
    assert etcd(leader_changes_15m=3)["findings"] == []
    four = etcd(leader_changes_15m=4)
    assert finds(four) == [("etcd/leader-churn", "major")] and "4 leader changes" in four["findings"][0]["summary"]
    assert finds(etcd(leader_changes_15m=5)) == [("etcd/leader-churn", "major")]


def test_review_defrag_follows_the_etcd_mixin_fragmentation_alert():
    # etcdDatabaseHighFragmentationRatio: in_use / size < 0.5 and in_use > 104857600 bytes (both strict)
    assert etcd(db_size_bytes=300 * MIB, db_size_in_use_bytes=100 * MIB)["findings"] == []  # in_use is exactly 100 MiB
    assert etcd(db_size_bytes=2 * (100 * MIB + 1), db_size_in_use_bytes=100 * MIB + 1)["findings"] == []  # ratio exactly 0.5
    hit = etcd(db_size_bytes=2 * (100 * MIB + 1) + 1, db_size_in_use_bytes=100 * MIB + 1)
    assert finds(hit) == [("etcd/defrag-recommended", "minor")]
    assert etcd(db_size_bytes=GIB, db_size_in_use_bytes=50 * MIB)["findings"] == []  # large file, small live data: below the 100 MiB floor


def test_review_rbac_v1alpha1_and_scheduling_v1alpha1_were_removed_in_1_23():
    # kubernetes CHANGELOG-1.23 (#104248): "rbac.authorization.k8s.io/v1alpha1 ... removed ... scheduling.k8s.io/v1alpha1 ... removed"
    rbac = ok("k8s_deprecated_api_scan", {"target_version": "1.22", "horizon_minors": 0, "resources": [
        {"api_version": "rbac.authorization.k8s.io/v1alpha1", "kind": "Role", "name": "r"}]})
    assert rbac["verdict"] == "clear"
    for api_version, kind, replacement in (("rbac.authorization.k8s.io/v1alpha1", "ClusterRoleBinding", "rbac.authorization.k8s.io/v1"),
                                           ("scheduling.k8s.io/v1alpha1", "PriorityClass", "scheduling.k8s.io/v1")):
        hit = ok("k8s_deprecated_api_scan", {"target_version": "1.23", "resources": [{"api_version": api_version, "kind": kind, "name": "x"}]})
        assert hit["verdict"] == "blocked" and "removed in 1.23" in hit["findings"][0]["summary"] and replacement in hit["findings"][0]["summary"]
    # 1.35 introduced a Workload kind in scheduling.k8s.io/v1alpha1: only PriorityClass is a removal
    other = ok("k8s_deprecated_api_scan", {"target_version": "1.40", "resources": [{"api_version": "scheduling.k8s.io/v1alpha1", "kind": "Workload", "name": "w"}]})
    assert other["verdict"] == "clear"


def test_review_flowcontrol_replacement_names_the_served_version():
    # deprecation guide: v1beta3 is gone in 1.32 and v1 is served from 1.29, so v1 is the replacement
    hit = ok("k8s_deprecated_api_scan", {"target_version": "1.26", "resources": [{"api_version": "flowcontrol.apiserver.k8s.io/v1beta1", "kind": "FlowSchema", "name": "f"}]})
    assert "flowcontrol.apiserver.k8s.io/v1 (served from 1.29" in hit["findings"][0]["summary"]


def test_review_limit_range_max_defaults_the_limit_and_min_defaults_the_request():
    # kubernetes pkg/apis/core/v1/defaults.go SetDefaults_LimitRangeItem: Default<-Max, DefaultRequest<-Default, DefaultRequest<-Min
    bare = ctr(cpu=0, mem=0, cpu_limit=0, mem_limit=0)
    max_only = ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "limit_range": {"max_cpu_m": 500, "max_memory_mib": 512}})
    assert max_only["findings"] == [] and max_only["verdict"] == "fits"
    assert (max_only["effective_requests_cpu_m"], max_only["effective_requests_memory_mib"],
            max_only["effective_limits_cpu_m"], max_only["effective_limits_memory_mib"]) == (500, 512, 500, 512)
    min_only = ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "limit_range": {"min_cpu_m": 100}})
    assert min_only["findings"] == [] and (min_only["effective_requests_cpu_m"], min_only["effective_limits_cpu_m"]) == (100, 0)
    both = ok("k8s_quota_fit", {"replicas": 1, "containers": [bare], "limit_range": {"min_cpu_m": 100, "max_cpu_m": 800, "default_limit_cpu_m": 400}})
    assert (both["effective_requests_cpu_m"], both["effective_limits_cpu_m"]) == (400, 400)
    request_only = ok("k8s_quota_fit", {"replicas": 1, "containers": [ctr(cpu=100, cpu_limit=0)], "limit_range": {"max_cpu_m": 500}})
    assert (request_only["effective_requests_cpu_m"], request_only["effective_limits_cpu_m"]) == (100, 500) and request_only["findings"] == []
    quota = ok("k8s_quota_fit", {"replicas": 2, "containers": [bare], "quota_hard": {"limits_cpu_m": 800}, "limit_range": {"max_cpu_m": 500}})
    assert quota["verdict"] == "over_quota" and quota["max_replicas_fit"] == 1


def test_review_a_certificate_is_valid_through_its_not_after_instant():
    # RFC 5280 section 4.1.2.5: the validity period runs "from notBefore through notAfter, inclusive"
    at = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("leaf", NOW)]})
    assert at["certs"][0]["bucket"] == "critical" and at["certs"][0]["days_remaining"] == 0.0 and at["expired"] == 0
    gone = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("leaf", "2026-10-19T23:59:59Z")]})
    assert gone["certs"][0]["bucket"] == "expired" and gone["verdict"] == "critical"


def test_review_days_remaining_never_rounds_up_into_the_next_bucket():
    almost = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("leaf", "2026-10-26T23:59:59.999Z")]})
    assert almost["certs"][0]["bucket"] == "critical" and almost["certs"][0]["days_remaining"] == 6.99
    just_expired = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("leaf", "2026-10-19T23:59:59.999Z")]})
    assert just_expired["certs"][0]["bucket"] == "expired" and just_expired["certs"][0]["days_remaining"] == 0.0


def test_review_a_not_yet_valid_certificate_cannot_leave_the_verdict_ok():
    early = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("a", "2028-10-20T00:00:00Z", not_before="2026-10-21T00:00:00Z")]})
    assert finds(early) == [("cert/a/not-yet-valid", "major")] and early["verdict"] == "critical"


def test_review_drain_throttle_message_states_its_assumption():
    result = ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=10, healthy=10, pods=5, min_available=7)]})
    assert "3 disruption(s) for 5 pod(s): 2 eviction waves if the budget stays at 3" in result["findings"][0]["summary"]


CANARIES = ("ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "AKIAIOSFODNN7EXAMPLE", "https://user:s3cr3t@host.example/x",
            "0x" + "ab" * 32, "abandon ability able about above absent absorb abstract absurd abuse access accident")
# input paths whose value is validated by the tool (versions, timestamps, clock times): the invalid value must never be echoed
VALIDATED_FIELDS = {
    "k8s_version_skew": [("apiserver_versions", 0), ("components", 0, "version")],
    "k8s_deprecated_api_scan": [("target_version",)],
    "k8s_upgrade_plan": [("current_version",), ("target_version",), ("node_pools", 0, "version")],
    "k8s_cert_expiry": [("now",), ("certs", 0, "not_after"), ("certs", 0, "not_before")],
    "k8s_addon_compatibility": [("current_version",), ("target_version",), ("addons", 0, "min_k8s"), ("addons", 0, "max_k8s")],
    "k8s_fleet_drift": [("latest_version",), ("clusters", 0, "version")],
    "k8s_maintenance_window": [("proposed_start",), ("windows", 0, "start"), ("windows", 0, "end"), ("blackouts", 0, "start"), ("blackouts", 0, "end")],
}


def _with_leaf(payload, path, value):
    clone = copy.deepcopy(payload)
    node = clone
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return clone


@pytest.mark.parametrize(("name", "path"), [(n, p) for n, paths in VALIDATED_FIELDS.items() for p in paths])
def test_review_invalid_values_of_validated_fields_are_never_echoed(name, path):
    sample = copy.deepcopy(next(t for t in lane_tools() if t["name"] == name)["examples"]["success"])
    if path[0] not in sample:  # an optional block the example does not use: add one to the sample
        sample[path[0]] = [{"name": "b", "start": "2026-10-24T02:00:00Z", "end": "2026-10-24T03:00:00Z"}] if path[0] == "blackouts" else sample.get(path[0])
    if "not_before" in path and "not_before" not in sample["certs"][0]:
        sample["certs"][0]["not_before"] = "2026-01-01T00:00:00Z"
    for canary in CANARIES:
        payload = _with_leaf(sample, path, canary)
        result = call(name, payload)
        assert result["state"] == "INVALID_INPUT", (name, path, result)
        assert canary not in json.dumps(result), (name, path, canary)


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_review_canary_secrets_in_free_text_fields_never_reach_a_finding_or_summary(name):
    # values that are neither identifiers nor validated formats (the example payload with every other leaf untouched)
    sample = next(t for t in lane_tools() if t["name"] == name)["examples"]["success"]
    result = call(name, sample)
    assert result["state"] == "SUCCESS"
    text = json.dumps(result)
    for canary in CANARIES:
        assert canary not in text


# --- independent oracles (seeded, brute force; none of them shares code with the handlers) ---------------------------


def _handlers():
    return importlib.import_module("swarm.tools.a21_tools")


def test_every_removal_table_row_is_pinned_by_an_independent_test_row():
    pinned = {(a, k if a not in {"apps/v1beta1", "apps/v1beta2", "rbac.authorization.k8s.io/v1beta1", "rbac.authorization.k8s.io/v1alpha1",
                                 "flowcontrol.apiserver.k8s.io/v1beta1", "flowcontrol.apiserver.k8s.io/v1beta2", "flowcontrol.apiserver.k8s.io/v1beta3"} else "*"): m
              for a, k, m, _ in REMOVALS}
    table = {key: minor for key, (minor, _) in _handlers().REMOVED_APIS.items()}
    assert table == pinned


def test_oracle_node_pool_capacity_matches_exact_rational_arithmetic():
    from fractions import Fraction
    import random
    rng = random.Random(11)
    executed = 0
    for _ in range(400):
        nodes, cpu, mem = rng.randint(0, 12), rng.choice([500, 1000, 2000, 3999]), rng.choice([1024, 4096, 7000])
        ds_c, ds_m, fail, pct = rng.choice([0, 100, 250]), rng.choice([0, 128, 512]), rng.randint(0, 3), rng.choice([1, 50, 70, 85, 99, 100])
        loads = [{"name": f"w{i}", "pool": "p", "replicas": rng.randint(0, 9), "cpu_request_m": rng.choice([0, 50, 100, 333, 500]),
                  "memory_request_mib": rng.choice([0, 64, 256, 512])} for i in range(rng.randint(1, 3))]
        pool_doc = {"name": "p", "nodes": nodes, "allocatable_cpu_m": cpu, "allocatable_memory_mib": mem, "daemonset_cpu_m": ds_c, "daemonset_memory_mib": ds_m}
        if cpu - ds_c <= 0 or mem - ds_m <= 0:
            continue
        executed += 1
        row = _handlers()._nodepool_capacity({"pools": [pool_doc], "workloads": loads, "tolerated_node_failures": fail, "max_utilization_pct": pct})["pools"][0]
        rc, rm = (sum(w["replicas"] * w[k] for w in loads) for k in ("cpu_request_m", "memory_request_mib"))
        uc, um, survivors = cpu - ds_c, mem - ds_m, max(0, nodes - fail)
        unschedulable = any(w["replicas"] > 0 and (w["cpu_request_m"] > uc or w["memory_request_mib"] > um) for w in loads)
        if rc == rm == 0:
            status = "ok"
        elif unschedulable or survivors == 0 or rc > survivors * uc or rm > survivors * um:
            status = "at_risk"
        elif Fraction(rc, survivors * uc) > Fraction(pct, 100) or Fraction(rm, survivors * um) > Fraction(pct, 100):
            status = "tight"
        else:
            status = "ok"
        needed = 0
        while not (rc <= Fraction(pct, 100) * needed * uc and rm <= Fraction(pct, 100) * needed * um):
            needed += 1
        assert (row["status"], row["recommended_nodes"]) == (status, needed + fail if rc or rm else 0), (pool_doc, loads, fail, pct)
    assert executed >= 100, f"{executed} of 400 capacity cases ran"


def test_oracle_pdb_budget_matches_the_disruption_controller_rounding():
    import math
    import random
    rng = random.Random(3)
    for _ in range(400):
        expected = rng.randint(0, 30)
        healthy, pods = rng.randint(0, expected), rng.randint(0, expected)
        kind = rng.choice(["min_available", "min_available_percent", "max_unavailable", "max_unavailable_percent"])
        value = rng.randint(0, 100) if kind.endswith("percent") else rng.randint(0, 35)
        row = _handlers()._drain_feasibility({"pdbs": [{"name": "a", "namespace": "n", "expected_pods": expected, "current_healthy": healthy,
                                                         "pods_on_drained_nodes": pods, kind: value}]})["results"][0]
        # pkg/controller/disruption/disruption.go: both percentages are scaled with roundUp=true
        desired = {"min_available": value, "min_available_percent": math.ceil(value * expected / 100), "max_unavailable": max(0, expected - value),
                   "max_unavailable_percent": max(0, expected - math.ceil(value * expected / 100))}[kind]
        assert (row["desired_healthy"], row["disruptions_allowed"]) == (desired, max(0, healthy - desired) if expected else 0), (kind, value, expected, healthy)


def test_oracle_version_skew_matches_the_published_policy():
    import random
    rng = random.Random(5)

    def limit(minor):
        return 2 if minor < 25 else 3

    for _ in range(600):
        api = sorted({rng.randint(26, 36) for _ in range(rng.randint(1, 2))})
        if len(api) == 2:
            api[1] = api[0] + 1
        comps = [{"name": f"c{i}", "kind": rng.choice(["kubelet", "kube-proxy", "kube-controller-manager", "kubectl"]), "version": f"1.{rng.randint(20, 40)}.0"}
                 for i in range(rng.randint(1, 4))]
        # Post-hop breaches are covered by the dedicated upgrade test; this oracle models current-state skew only.
        findings = _handlers()._version_skew({"apiserver_versions": [f"1.{m}.0" for m in api], "components": comps})["findings"]
        got = {(f["id"], f["severity"]) for f in findings if not f["id"].endswith("/upgrade-breach")}
        new, old, want = max(api), min(api), set()
        for c in comps:
            minor, kind, ident = int(c["version"].split(".")[1]), c["kind"], f"{c['kind']}/{c['name']}"
            if kind in ("kubelet", "kube-proxy"):
                if minor > old:
                    want.add((f"{ident}/newer-than-apiserver", "blocker"))
                elif new - minor > limit(minor):
                    want.add((f"{ident}/too-old", "major"))
                elif new - minor == limit(minor):
                    want.add((f"{ident}/at-skew-limit", "minor"))
            elif kind == "kubectl":
                if not all(abs(minor - a) <= 1 for a in api):
                    want.add((f"{ident}/skew", "minor"))
            elif minor > old:
                want.add((f"{ident}/newer-than-apiserver", "blocker"))
            elif new - minor > 1:
                want.add((f"{ident}/too-old", "major"))
            elif new - minor == 1:
                want.add((f"{ident}/one-behind", "info"))
        assert got == want, (api, comps)


def test_oracle_upgrade_plan_never_breaks_the_kubelet_skew_between_steps():
    import random
    import re
    rng = random.Random(5)
    executed = 0

    def limit(minor):
        return 2 if minor < 25 else 3

    for _ in range(500):
        cur = rng.randint(22, 34)
        tgt = cur + rng.randint(0, 6)
        pools = [{"name": f"p{i}", "version": f"1.{max(20, cur - rng.randint(0, 3 if cur - 3 >= 25 else 2))}.{rng.randint(0, 5)}", "nodes": rng.randint(1, 20),
                  "max_surge": rng.randint(1, 3), "max_unavailable": rng.randint(0, 3)} for i in range(rng.randint(0, 4))]
        steps = _handlers()._upgrade_plan({"current_version": f"1.{cur}.2", "target_version": f"1.{tgt}", "node_pools": pools})["steps"]
        control, version = cur, {p["name"]: int(p["version"].split(".")[1]) for p in pools}
        if not all(0 <= control - v <= limit(v) for v in version.values()):
            continue
        executed += 1
        for step in steps:
            minor = int(step["target_version"].split(".")[1])
            if step["phase"] == "control_plane":
                assert minor == control + 1
                control = minor
            elif step["phase"] == "node_pool":
                assert minor <= control
                version[re.search(r"upgrade pool (\S+) to", step["action"]).group(1)] = minor
            assert all(0 <= control - v <= limit(v) for v in version.values()), (cur, tgt, pools, step)
        assert control == tgt and set(version.values()) <= {tgt}, (cur, tgt, pools)
    assert executed >= 100, f"{executed} of 500 upgrade-plan cases ran"


def test_oracle_maintenance_window_matches_a_quarter_hour_simulation():
    import random
    from datetime import datetime, timedelta, timezone
    rng = random.Random(7)
    names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

    def hhmm(minutes):
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    def occurrence(moment, win):
        zone = timezone(timedelta(minutes=win["utc_offset_minutes"]))
        local = moment.astimezone(zone)
        begin_min, end_min = (int(win[k][:2]) * 60 + int(win[k][3:]) for k in ("start", "end"))
        length = (end_min - begin_min) % 1440
        for back in (0, 1):
            day = local.date() - timedelta(days=back)
            begin = datetime(day.year, day.month, day.day, tzinfo=zone) + timedelta(minutes=begin_min)
            if begin <= local < begin + timedelta(minutes=length) and names[day.weekday()] in win["weekdays"]:
                return win["name"], day
        return None

    for _ in range(150):
        windows = []
        for i in range(rng.randint(1, 2)):
            begin = rng.randrange(0, 1440, 15)
            end = rng.choice([m for m in range(0, 1440, 15) if m != begin])
            windows.append({"name": f"w{i}", "weekdays": rng.sample(names, rng.randint(1, 3)), "start": hhmm(begin), "end": hhmm(end),
                            "utc_offset_minutes": rng.choice([0, 60, -300, 330, 840, -720, 345])})
        base = datetime(2026, 3, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * rng.randrange(0, 960))
        blackouts = []
        for j in range(rng.randint(0, 2)):
            first = base + timedelta(minutes=15 * rng.randrange(0, 768))
            blackouts.append({"name": f"b{j}", "start": first.isoformat(), "end": (first + timedelta(minutes=15 * rng.randint(1, 60))).isoformat()})
        duration = rng.choice([15, 30, 60, 90, 180, 300])
        payload = {"proposed_start": base.isoformat(), "duration_minutes": duration, "windows": windows, "blackouts": blackouts}

        def compliant(start):
            slots = [start + timedelta(minutes=15 * i) for i in range(duration // 15)]
            if any(datetime.fromisoformat(b["start"]) <= s < datetime.fromisoformat(b["end"]) for b in blackouts for s in slots):
                return False
            return any(len({occurrence(s, w) for s in slots}) == 1 and occurrence(slots[0], w) is not None for w in windows)

        earliest = next((base + timedelta(minutes=15 * k) for k in range(14 * 96 + 1) if compliant(base + timedelta(minutes=15 * k))), None)
        result = _handlers()._maintenance_window(payload)
        assert result["allowed"] is compliant(base), payload
        assert (datetime.fromisoformat(result["earliest_compliant_start"]) if result["earliest_compliant_start"] else None) == earliest, payload


def test_review_out_of_range_dates_are_invalid_input_not_a_handler_failure():
    base = {"duration_minutes": 60, "windows": [SAT_NIGHT]}
    for start in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:00Z", "9999-12-31T23:59:00-12:00"):
        invalid("k8s_maintenance_window", {**base, "proposed_start": start}, "proposed_start")


def test_review_an_empty_certificate_list_is_invalid_input_not_a_false_ok():
    # the input schema has minItems 1: an empty inventory would otherwise report verdict ok, a false green
    result = _handlers().cert_expiry({"now": NOW, "certs": []})
    assert (result["state"], result["field"]) == ("INVALID_INPUT", "certs")


def test_review_addon_versions_without_a_shared_supported_minor_are_blocked():
    # installed 1.28-1.29, candidate 1.30-1.31, control plane 1.29 -> 1.30: no minor supports
    # both versions, so the swap can run neither on 1.29 (candidate unsupported) nor on 1.30
    # (installed unsupported): the path needs a separate migration, not an in-place upgrade.
    result = ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.30", "addons": [addon(lo="1.28", hi="1.29", cand=("1.30", "1.31"))]})
    assert result["addons"][0] == {"name": "calico", "kind": "cni", "status": "blocked", "upgrade_at": "", "first_incompatible": "1.30"}
    assert (result["upgrade_required_count"], result["blocked_count"], result["verdict"]) == (0, 1, "blocked")
    assert finds(result) == [("addon/calico/blocked", "blocker")] and "migrate the add-on separately" in result["findings"][0]["summary"]


def test_review_addon_disjoint_ranges_stay_blocked_over_a_longer_path():
    # Same disjoint 1.28-1.29/1.30-1.31 fixture over 1.28 -> 1.31: the split covers both sides
    # but still shares no supported minor for the swap.
    result = ok("k8s_addon_compatibility", {"current_version": "1.28", "target_version": "1.31", "addons": [addon(lo="1.28", hi="1.29", cand=("1.30", "1.31"))]})
    assert result["addons"][0]["status"] == "blocked" and result["addons"][0]["upgrade_at"] == ""
    assert (result["upgrade_required_count"], result["blocked_count"], result["verdict"]) == (0, 1, "blocked")
    assert "migrate the add-on separately" in result["findings"][0]["summary"]


def test_review_a_non_positive_duration_is_invalid_input_not_an_allowed_change():
    base = {"proposed_start": "2026-10-24T02:00:00Z", "windows": [SAT_NIGHT]}
    for minutes in (-60, 0):
        result = _handlers().maintenance_window({**base, "duration_minutes": minutes})
        assert (result["state"], result["field"]) == ("INVALID_INPUT", "duration_minutes")


def test_review_an_unknown_fleet_environment_is_invalid_input_naming_clusters():
    result = _handlers().fleet_drift({"latest_version": "1.31", "clusters": [cluster("c", "qa", "1.31.0")]})
    assert (result["state"], result["field"]) == ("INVALID_INPUT", "clusters")


def test_review_a_zero_etcd_quota_is_invalid_input_not_a_division_error():
    result = _handlers().etcd_health({"db_size_bytes": 1, "quota_bytes": 0, "leader_changes_15m": 0, "wal_fsync_p99_ms": 1, "members": members(True)})
    assert (result["state"], result["field"]) == ("INVALID_INPUT", "quota_bytes")



def test_review_a_live_addon_failure_with_no_shared_minor_is_blocked_not_upgraded():
    # installed 1.25-1.27 already fails at the running 1.28 and the candidate 1.28-1.30 shares no minor with it: no swap is supported
    result = ok("k8s_addon_compatibility", {"current_version": "1.28", "target_version": "1.29", "addons": [addon(lo="1.25", hi="1.27", cand=("1.28", "1.30"))]})
    assert result["addons"][0] == {"name": "calico", "kind": "cni", "status": "blocked", "upgrade_at": "", "first_incompatible": "1.28"}
    assert finds(result) == [("addon/calico/blocked", "blocker")] and "migrate the add-on separately" in result["findings"][0]["summary"]
    assert (result["upgrade_required_count"], result["verdict"]) == (0, "blocked")


def test_review_a_live_addon_failure_with_a_shared_minor_is_a_major_finding_for_an_immediate_swap():
    # installed 1.30-1.32 already fails at the running 1.29; the candidate 1.28-1.31 supports 1.30 and 1.31 on the path
    result = ok("k8s_addon_compatibility", {"current_version": "1.29", "target_version": "1.31", "addons": [addon(lo="1.30", hi="1.32", cand=("1.28", "1.31"))]})
    assert result["addons"][0]["status"] == "upgrade_required" and result["addons"][0]["upgrade_at"] == "1.29"
    assert finds(result) == [("addon/calico/upgrade", "major")] and "already runs at 1.29" in result["findings"][0]["summary"]


def test_review_an_unsatisfiable_budget_makes_the_drain_verdict_blocked_even_with_no_pods_on_the_node():
    result = ok("k8s_drain_feasibility", {"pdbs": [pdb(expected=2, healthy=2, pods=0, min_available=3)]})
    assert result["results"][0]["status"] == "unaffected"
    assert finds(result) == [("pdb/shop/web/unsatisfiable", "major")] and (result["blocked_count"], result["verdict"]) == (0, "blocked")


def test_review_a_pod_that_fits_no_node_puts_its_pool_at_risk_so_the_row_agrees_with_the_verdict():
    pools = [{"name": "p", "nodes": 2, "allocatable_cpu_m": 4000, "allocatable_memory_mib": 8192, "max_nodes": 2}]
    result = ok("k8s_nodepool_capacity", {"pools": pools, "workloads": [wl(1, 7000, 1024, name="w", pool_name="p")], "tolerated_node_failures": 0})
    assert result["pools"][0]["status"] == "at_risk" and (result["pools_at_risk"], result["verdict"]) == (1, "at_risk")
    assert finds(result) == [("workload/w/exceeds-node", "blocker"), ("pool/p/max-nodes", "major"), ("pool/p/tight", "minor")]


def test_review_a_certificate_that_expired_less_than_a_day_ago_is_not_reported_as_zero_days():
    result = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("leaf", "2026-10-19T23:59:59Z")]})
    assert result["certs"][0]["bucket"] == "expired" and result["findings"][0]["summary"] == "certificate leaf expired less than a day ago"
    whole = ok("k8s_cert_expiry", {"now": NOW, "certs": [cert("leaf", "2026-10-18T00:00:00Z")]})
    assert whole["findings"][0]["summary"] == "certificate leaf expired 2.0 day(s) ago"
