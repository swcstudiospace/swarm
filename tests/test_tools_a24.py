"""Behaviour tests for the A24 Cluster Security & Policy Engineer tools (kpol_*).

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
AGENT, SLUG, PREFIX = "A24", "a24-cluster-security", "kpol_"
INTAKE = ROOT / "registry" / "intake"
TOOL_NAMES = [
    "kpol_pss_check", "kpol_rbac_risk_scan", "kpol_rbac_effective_access", "kpol_admission_policy_lint", "kpol_cis_score",
    "kpol_secret_exposure_check", "kpol_sa_token_audit", "kpol_image_admission_check", "kpol_runtime_rule_lint",
    "kpol_tenant_isolation_check", "kpol_audit_policy_check", "kpol_exception_register_check",
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


def rules_of(result: dict) -> list[tuple[str, str]]:
    """(rule, severity) of every finding, location stripped, in result order."""
    return [(f["id"].split(":")[0], f["severity"]) for f in result["findings"]]


def rule_ids(result: dict) -> set[str]:
    return {f["id"].split(":")[0] for f in result["findings"]}


def severity_of(result: dict, rule: str) -> str:
    return next(f["severity"] for f in result["findings"] if f["id"].split(":")[0] == rule)


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


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_success_example_returns_exactly_the_declared_keys(name):
    tool = next(t for t in lane_tools() if t["name"] == name)
    result = ok(name, tool["examples"]["success"])
    assert set(result) == set(tool["output_schema"]["properties"])
    assert len(json.dumps(result)) < 256 * 1024


# --- builders -------------------------------------------------------------------------------------------------


def pod(name="web", namespace="prod", containers=None, **spec):
    return {"kind": "Pod", "apiVersion": "v1", "metadata": {"name": name, "namespace": namespace},
            "spec": {"containers": containers or [{"name": "app", "image": "x"}], **spec}}


def hardened(**container):
    """A container that satisfies the restricted standard on its own."""
    return {"name": "app", "image": "x", "securityContext": {
        "allowPrivilegeEscalation": False, "runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"},
        "capabilities": {"drop": ["ALL"]}, **container}}


def pss(workloads, level="restricted"):
    return ok("kpol_pss_check", {"workloads": workloads, "level": level})


# --- kpol_pss_check -------------------------------------------------------------------------------------------


def test_pss_baseline_breakouts_are_blockers_and_other_baseline_controls_major():
    web = pod(hostNetwork=True, hostPID=True, hostIPC=True, volumes=[{"name": "h", "hostPath": {"path": "/"}}],
              containers=[{"name": "app", "image": "x", "ports": [{"hostPort": 80}],
                           "securityContext": {"privileged": True, "capabilities": {"add": ["SYS_ADMIN"]}}}])
    result = pss([web], "baseline")
    assert result["highest_compliant_level"] == "privileged" and result["verdict"] == "fail"
    assert sorted(rules_of(result)) == sorted([
        ("pss.host-namespaces", "blocker"), ("pss.host-namespaces", "blocker"), ("pss.host-namespaces", "blocker"),
        ("pss.host-path", "blocker"), ("pss.privileged", "blocker"), ("pss.capabilities-add", "major"), ("pss.host-port", "major")])
    assert result["findings"][0]["id"] == "pss.host-namespaces:Pod/prod/web"
    assert {f["summary"] for f in result["findings"] if f["id"].startswith("pss.host-namespaces")} == {
        "spec.hostNetwork is true (violates baseline)", "spec.hostPID is true (violates baseline)", "spec.hostIPC is true (violates baseline)"}


def test_pss_restricted_pod_passes_and_reports_restricted():
    deployment = {"kind": "Deployment", "metadata": {"name": "ok"}, "spec": {"template": {"spec": {"containers": [hardened()]}}}}
    result = pss([deployment])
    assert (result["finding_count"], result["verdict"], result["highest_compliant_level"], result["workload_count"]) == (0, "pass", "restricted", 1)


def test_pss_bare_pod_is_baseline_compliant_but_not_restricted():
    bare = pod()
    restricted = pss([bare])
    assert sorted(rules_of(restricted)) == [("pss.capabilities-drop", "major"), ("pss.privilege-escalation", "major"),
                                            ("pss.run-as-non-root", "major"), ("pss.seccomp", "major")]
    assert restricted["highest_compliant_level"] == "baseline"
    baseline = pss([bare], "baseline")
    assert (baseline["finding_count"], baseline["verdict"], baseline["highest_compliant_level"]) == (0, "pass", "baseline")


def test_pss_run_as_non_root_container_value_overrides_pod_value():
    pod_true = {"securityContext": {"runAsNonRoot": True}}
    assert "pss.run-as-non-root" not in rule_ids(pss([pod(containers=[hardened(runAsNonRoot=None)], **pod_true)]))
    overridden = pss([pod(containers=[hardened(runAsNonRoot=False)], **pod_true)])
    assert "pss.run-as-non-root" in rule_ids(overridden)
    container_only = pss([pod(containers=[hardened(runAsNonRoot=True)])])
    assert container_only["finding_count"] == 0


@pytest.mark.parametrize("where", ["pod", "container"])
def test_pss_run_as_user_zero_is_refused_at_pod_and_container_level(where):
    workload = pod(containers=[hardened(runAsUser=0 if where == "container" else None)],
                   securityContext={"runAsUser": 0} if where == "pod" else {})
    result = pss([workload])
    assert [f["id"].split(":")[0] for f in result["findings"]] == ["pss.run-as-user"]
    assert pss([pod(containers=[hardened(runAsUser=1000)], securityContext={"runAsUser": 1000})])["finding_count"] == 0


def test_pss_seccomp_pod_level_satisfies_restricted_and_unconfined_breaks_baseline():
    pod_level = pod(securityContext={"seccompProfile": {"type": "Localhost", "localhostProfile": "p.json"}},
                    containers=[hardened(seccompProfile=None)])
    assert pss([pod_level])["finding_count"] == 0
    unconfined = pss([pod(containers=[hardened(seccompProfile={"type": "Unconfined"})])], "baseline")
    assert rules_of(unconfined) == [("pss.seccomp", "major")] and unconfined["highest_compliant_level"] == "privileged"
    pod_unconfined = pss([pod(securityContext={"seccompProfile": {"type": "Unconfined"}})], "baseline")
    assert rules_of(pod_unconfined) == [("pss.seccomp", "major")]


def test_pss_windows_pods_skip_only_the_linux_only_restricted_controls():
    windows = pod(os={"name": "windows"}, containers=[{"name": "app", "image": "x"}])
    assert rules_of(pss([windows])) == [("pss.run-as-non-root", "major")]
    host_process = pod(os={"name": "windows"}, securityContext={"windowsOptions": {"hostProcess": True}, "runAsNonRoot": True})
    assert rules_of(pss([host_process])) == [("pss.host-process", "blocker")]


BASELINE_CAPS = ["AUDIT_WRITE", "CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "MKNOD", "NET_BIND_SERVICE", "SETFCAP", "SETGID",
                 "SETPCAP", "SETUID", "SYS_CHROOT"]


def test_pss_baseline_capability_table_is_pinned():
    module = importlib.import_module("swarm.tools.a24_tools")
    assert module.BASELINE_CAPABILITIES == frozenset(BASELINE_CAPS)


@pytest.mark.parametrize("cap", BASELINE_CAPS)
def test_pss_baseline_allows_each_listed_capability(cap):
    result = pss([pod(containers=[{"name": "app", "image": "x", "securityContext": {"capabilities": {"add": [cap]}}}])], "baseline")
    assert result["finding_count"] == 0


@pytest.mark.parametrize("cap", ["SYS_ADMIN", "NET_RAW", "ALL", "NET_ADMIN"])
def test_pss_baseline_refuses_other_capabilities(cap):
    result = pss([pod(containers=[{"name": "app", "image": "x", "securityContext": {"capabilities": {"add": [cap]}}}])], "baseline")
    assert rules_of(result) == [("pss.capabilities-add", "major")]


def test_pss_restricted_only_allows_net_bind_service_to_be_added_back():
    allowed = hardened(capabilities={"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]})
    assert pss([pod(containers=[allowed])])["finding_count"] == 0
    chown = pss([pod(containers=[hardened(capabilities={"drop": ["ALL"], "add": ["CHOWN"]})])])
    assert rules_of(chown) == [("pss.capabilities-add", "major")]
    assert chown["highest_compliant_level"] == "baseline"
    dropped_some = pss([pod(containers=[hardened(capabilities={"drop": ["NET_RAW"]})])])
    assert rules_of(dropped_some) == [("pss.capabilities-drop", "major")]


SAFE_SYSCTLS = ["kernel.shm_rmid_forced", "net.ipv4.ip_local_port_range", "net.ipv4.ip_unprivileged_port_start", "net.ipv4.tcp_syncookies",
                "net.ipv4.ping_group_range", "net.ipv4.ip_local_reserved_ports", "net.ipv4.tcp_keepalive_time", "net.ipv4.tcp_fin_timeout",
                "net.ipv4.tcp_keepalive_intvl", "net.ipv4.tcp_keepalive_probes", "net.ipv4.tcp_rmem", "net.ipv4.tcp_wmem",
                "net.ipv4.tcp_slow_start_after_idle", "net.ipv4.tcp_notsent_lowat"]


def test_pss_safe_sysctl_table_is_pinned():
    assert importlib.import_module("swarm.tools.a24_tools").SAFE_SYSCTLS == frozenset(SAFE_SYSCTLS)


@pytest.mark.parametrize("name", SAFE_SYSCTLS)
def test_pss_baseline_allows_each_safe_sysctl(name):
    assert pss([pod(securityContext={"sysctls": [{"name": name, "value": "1"}]})], "baseline")["finding_count"] == 0


def test_pss_baseline_refuses_unsafe_sysctls():
    result = pss([pod(securityContext={"sysctls": [{"name": "kernel.msgmax", "value": "1"}]})], "baseline")
    assert [f["summary"] for f in result["findings"]] == ["sysctl 'kernel.msgmax' is not in the safe set (violates baseline)"]


RESTRICTED_VOLUMES = ["configMap", "csi", "downwardAPI", "emptyDir", "ephemeral", "persistentVolumeClaim", "projected", "secret"]


def test_pss_restricted_volume_table_is_pinned():
    assert importlib.import_module("swarm.tools.a24_tools").RESTRICTED_VOLUMES == frozenset(RESTRICTED_VOLUMES)


@pytest.mark.parametrize("kind", RESTRICTED_VOLUMES)
def test_pss_restricted_allows_each_listed_volume_type(kind):
    assert pss([pod(containers=[hardened()], volumes=[{"name": "v", kind: {}}])])["finding_count"] == 0


def test_pss_restricted_refuses_other_volume_types_and_hostpath_breaks_baseline_too():
    nfs = pss([pod(containers=[hardened()], volumes=[{"name": "v", "nfs": {"server": "s", "path": "/"}}])])
    assert rules_of(nfs) == [("pss.volume-types", "major")]
    assert nfs["findings"][0]["summary"] == "volume v uses nfs (violates restricted)"
    host = pss([pod(containers=[hardened()], volumes=[{"name": "v", "hostPath": {"path": "/etc"}}])])
    assert sorted(rules_of(host)) == [("pss.host-path", "blocker"), ("pss.volume-types", "major")]


@pytest.mark.parametrize("selinux_type", ["container_t", "container_init_t", "container_kvm_t", "container_engine_t", ""])
def test_pss_baseline_allows_each_selinux_type(selinux_type):
    assert pss([pod(securityContext={"seLinuxOptions": {"type": selinux_type}})], "baseline")["finding_count"] == 0


def test_pss_baseline_refuses_other_selinux_types_users_and_roles():
    assert rules_of(pss([pod(securityContext={"seLinuxOptions": {"type": "spc_t"}})], "baseline")) == [("pss.selinux", "major")]
    both = pss([pod(containers=[{"name": "app", "image": "x", "securityContext": {"seLinuxOptions": {"user": "u", "role": "r"}}}])], "baseline")
    assert rules_of(both) == [("pss.selinux", "major"), ("pss.selinux", "major")]


def test_pss_baseline_apparmor_profile_and_annotation_rules():
    assert pss([pod(securityContext={"appArmorProfile": {"type": "Localhost", "localhostProfile": "p"}})], "baseline")["finding_count"] == 0
    assert rules_of(pss([pod(securityContext={"appArmorProfile": {"type": "Unconfined"}})], "baseline")) == [("pss.apparmor", "major")]
    container = {"name": "app", "image": "x", "securityContext": {"appArmorProfile": {"type": "Unconfined"}}}
    assert rules_of(pss([pod(containers=[container])], "baseline")) == [("pss.apparmor", "major")]
    key = "container.apparmor.security.beta.kubernetes.io/app"
    for value, bad in (("runtime/default", False), ("localhost/mine", False), ("unconfined", True)):
        workload = pod()
        workload["metadata"]["annotations"] = {key: value}
        assert (pss([workload], "baseline")["finding_count"] == 1) is bad


def test_pss_baseline_proc_mount_host_port_and_host_probes():
    unmasked = {"name": "app", "image": "x", "securityContext": {"procMount": "Unmasked"}}
    assert rules_of(pss([pod(containers=[unmasked])], "baseline")) == [("pss.proc-mount", "major")]
    zero = {"name": "app", "image": "x", "ports": [{"containerPort": 80, "hostPort": 0}]}
    assert pss([pod(containers=[zero])], "baseline")["finding_count"] == 0
    probed = {"name": "app", "image": "x", "livenessProbe": {"httpGet": {"host": "10.0.0.1", "port": 80}},
              "startupProbe": {"tcpSocket": {"host": "x", "port": 80}}, "readinessProbe": {"httpGet": {"host": "", "port": 80}},
              "lifecycle": {"preStop": {"httpGet": {"host": "evil", "port": 80}}, "postStart": {"tcpSocket": {"port": 1}}}}
    summaries = sorted(f["summary"] for f in pss([pod(containers=[probed])], "baseline")["findings"])
    assert summaries == [
        "containers/app lifecycle.preStop.httpGet.host is set (violates baseline)",
        "containers/app livenessProbe.httpGet.host is set (violates baseline)",
        "containers/app startupProbe.tcpSocket.host is set (violates baseline)"]


def test_pss_init_and_ephemeral_containers_are_checked_and_named():
    workload = pod(initContainers=[{"name": "init", "image": "x", "securityContext": {"privileged": True}}],
                   ephemeralContainers=[{"name": "dbg", "image": "x", "securityContext": {"privileged": True}}])
    summaries = {f["summary"] for f in pss([workload], "baseline")["findings"]}
    assert summaries == {"initContainers/init runs privileged (violates baseline)", "ephemeralContainers/dbg runs privileged (violates baseline)"}


def test_pss_finds_pod_specs_in_every_workload_kind_and_lists():
    template = {"template": {"spec": {"containers": [{"name": "app", "image": "x", "securityContext": {"privileged": True}}]}}}
    manifests = [{"kind": kind, "metadata": {"name": kind.lower()}, "spec": template}
                 for kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "ReplicationController", "Job")]
    manifests.append({"kind": "CronJob", "metadata": {"name": "cron"}, "spec": {"jobTemplate": {"spec": template}}})
    manifests.append({"kind": "Service", "metadata": {"name": "ignored"}, "spec": {}})
    result = pss(manifests, "baseline")
    assert result["workload_count"] == 7 and result["finding_count"] == 7
    listed = pss([{"kind": "List", "items": [pod("a"), pod("b")]}, {"kind": "PodList", "items": [{"metadata": {"name": "c"}, "spec": pod()["spec"]}]}])
    assert listed["workload_count"] == 3


def test_pss_rejects_malformed_manifests_naming_workloads():
    invalid("kpol_pss_check", {"workloads": [{"metadata": {}}], "level": "baseline"}, "workloads")
    invalid("kpol_pss_check", {"workloads": ["pod"], "level": "baseline"}, "workloads")
    invalid("kpol_pss_check", {"workloads": [{"kind": "Deployment", "metadata": {"name": "d"}, "spec": {}}], "level": "baseline"}, "workloads")
    invalid("kpol_pss_check", {"workloads": [pod(containers=["nope"])], "level": "baseline"}, "workloads")
    invalid("kpol_pss_check", {"workloads": [{"kind": "List", "items": [{"kind": "List", "items": [{"kind": "List", "items": [
        {"kind": "List", "items": [pod()]}]}]}]}], "level": "baseline"}, "workloads")
    too_many = pod(containers=[{"name": f"c{i}", "image": "x"} for i in range(201)])
    invalid("kpol_pss_check", {"workloads": [too_many], "level": "baseline"}, "workloads")
    huge = pod()
    huge["metadata"]["labels"] = {"x": list(range(200_001))}
    invalid("kpol_pss_check", {"workloads": [huge], "level": "baseline"}, "workloads")


def test_pss_caps_findings_but_reports_the_true_count():
    many = pod(containers=[{"name": f"c{i}", "image": "x"} for i in range(200)])
    result = pss([many])
    assert result["finding_count"] == 800 and len(result["findings"]) == 500


def test_pss_findings_are_ordered_worst_first_then_by_id():
    result = pss([pod("b", hostPID=True), pod("a", hostIPC=True)], "baseline")
    assert [f["id"] for f in result["findings"]] == ["pss.host-namespaces:Pod/prod/a", "pss.host-namespaces:Pod/prod/b"]
    assert pss([pod("b", hostPID=True), pod("a", hostIPC=True)], "baseline") == result


# --- RBAC builders --------------------------------------------------------------------------------------------


def role(name, rules, kind="ClusterRole", namespace=None, **extra):
    meta = {"name": name, **({"namespace": namespace} if namespace else {})}
    return {"kind": kind, "apiVersion": "rbac.authorization.k8s.io/v1", "metadata": meta, "rules": rules, **extra}


def rule(verbs, resources, groups=("",), names=None):
    return {"apiGroups": list(groups), "resources": list(resources), "verbs": list(verbs), **({"resourceNames": names} if names else {})}


def binding(name, ref, subjects, kind="ClusterRoleBinding", namespace=None, ref_kind="ClusterRole", **meta):
    return {"kind": kind, "apiVersion": "rbac.authorization.k8s.io/v1", "metadata": {"name": name, **({"namespace": namespace} if namespace else {}), **meta},
            "roleRef": {"kind": ref_kind, "name": ref, "apiGroup": "rbac.authorization.k8s.io"}, "subjects": subjects}


USER = {"kind": "User", "name": "alice"}


def scan(roles=(), bindings=()):
    return ok("kpol_rbac_risk_scan", {"roles": list(roles), "bindings": list(bindings)})


def role_rules(verbs, resources, groups=("",), names=None):
    return rules_of(scan([role("r", [rule(verbs, resources, groups, names)])]))


# --- kpol_rbac_risk_scan --------------------------------------------------------------------------------------


def test_rbac_scan_admin_equivalent_rule_is_one_blocker():
    result = scan([role("root", [rule(["*"], ["*"], ["*"])])])
    assert rules_of(result) == [("rbac.admin-equivalent", "blocker")]
    assert result["findings"][0]["id"] == "rbac.admin-equivalent:ClusterRole/root"
    assert result["findings"][0]["summary"] == "rules[0]: grants every verb on every resource in every API group"
    assert (result["role_count"], result["binding_count"], result["verdict"], result["finding_count"]) == (1, 0, "fail", 1)


def test_rbac_scan_wildcards_by_kind():
    assert role_rules(["*"], ["configmaps"]) == [("rbac.wildcard-verb", "major")]
    assert role_rules(["*"], ["pods"]) == [("rbac.wildcard-verb", "major"), ("rbac.create-pods", "minor")]
    # a bare `*` resource includes secrets (RBAC ResourceMatches), so a read-only wildcard is also a secret read
    assert role_rules(["get", "list", "watch"], ["*"]) == [("rbac.secret-read", "major"), ("rbac.wildcard-resource", "minor")]
    assert role_rules(["get", "delete"], ["*"]) == [("rbac.wildcard-resource", "major"), ("rbac.secret-read", "minor")]
    assert role_rules(["get", "list"], ["*"], ["apps"]) == [("rbac.wildcard-resource", "minor")]
    assert role_rules(["get"], ["pods"], ["*"]) == [("rbac.wildcard-apigroup", "minor")]
    assert role_rules(["*"], ["*"], ["apps"]) == [("rbac.wildcard-verb", "major")]


RBAC_GROUP, AUTHN_GROUP = "rbac.authorization.k8s.io", "authentication.k8s.io"


@pytest.mark.parametrize("verb, resources, group, relevant", [
    ("escalate", ["clusterroles"], RBAC_GROUP, True), ("escalate", ["roles"], RBAC_GROUP, True), ("bind", ["clusterroles"], RBAC_GROUP, True),
    ("bind", ["roles"], RBAC_GROUP, True), ("impersonate", ["users"], "", True), ("impersonate", ["groups"], "", True),
    ("impersonate", ["serviceaccounts"], "", True), ("impersonate", ["uids"], AUTHN_GROUP, True),
    ("impersonate", ["userextras/scopes"], AUTHN_GROUP, True), ("escalate", ["pods"], "", False), ("bind", ["secrets"], "", False),
    ("impersonate", ["pods"], "", False),
    # RBAC docs: escalate/bind apply to roles and clusterroles of the rbac group, impersonate to users/groups/serviceaccounts of the
    # core group and uids/userextras of authentication.k8s.io; the same resource name in another API group grants nothing of the kind
    ("escalate", ["clusterroles"], "", False), ("bind", ["roles"], "apps", False), ("impersonate", ["users"], "apps", False),
    ("impersonate", ["uids"], "", False)])
def test_rbac_scan_escalation_verbs_only_matter_on_their_resources(verb, resources, group, relevant):
    found = role_rules([verb], resources, [group])
    assert found == ([(f"rbac.{verb}", "major")] if relevant else [])


def test_rbac_scan_escalation_in_wildcard_api_group_is_relevant_and_also_a_wildcard_group():
    assert role_rules(["escalate"], ["roles"], ["*"]) == [("rbac.escalate", "major"), ("rbac.wildcard-apigroup", "minor")]
    assert role_rules(["impersonate"], ["userextras/x"], ["*"]) == [("rbac.impersonate", "major"), ("rbac.wildcard-apigroup", "minor")]


def test_rbac_scan_escalation_limited_by_resource_names_is_minor():
    assert role_rules(["bind"], ["clusterroles"], [RBAC_GROUP], names=["viewer"]) == [("rbac.bind", "minor")]


def test_rbac_scan_secret_read_levels_and_exemptions():
    assert role_rules(["list"], ["secrets"]) == [("rbac.secret-read", "major")]
    assert role_rules(["watch"], ["secrets"]) == [("rbac.secret-read", "major")]
    assert role_rules(["get"], ["secrets"]) == [("rbac.secret-read", "minor")]
    assert role_rules(["get", "list"], ["secrets"], names=["one"]) == []
    assert role_rules(["list"], ["secrets"], groups=["apps"]) == []
    assert role_rules(["create"], ["secrets"]) == []


def test_rbac_scan_other_escalation_primitives():
    assert role_rules(["create"], ["pods/exec"]) == [("rbac.pod-exec", "major")]
    assert role_rules(["get"], ["pods/attach"]) == [("rbac.pod-exec", "major")]
    assert role_rules(["list"], ["pods/exec"]) == []
    assert role_rules(["get"], ["nodes/proxy"]) == [("rbac.nodes-proxy", "major")]
    assert role_rules(["create"], ["serviceaccounts/token"]) == [("rbac.token-request", "major")]
    assert role_rules(["create"], ["pods"]) == [("rbac.create-pods", "minor")]
    assert role_rules(["get", "list"], ["pods"]) == []
    assert role_rules(["create"], ["pods/exec"], names=["x"]) == []


def test_rbac_scan_checks_every_role_whatever_its_name_or_label():
    wildcard = [rule(["*"], ["*"], ["*"])]
    labelled = role("system:thing", wildcard)
    labelled["metadata"]["labels"] = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    assert rules_of(scan([labelled])) == [("rbac.admin-equivalent", "blocker")]  # neither the label nor a system: prefix hides a role
    assert rules_of(scan([role("cluster-admin", wildcard)])) == [("rbac.admin-equivalent", "blocker")]  # checked whatever its name
    assert scan([role("cluster-admin", wildcard, kind="Role", namespace="x")])["finding_count"] == 1
    bootstrap_admin = role("admin", [rule(["get"], ["pods"])])
    bootstrap_admin["metadata"]["labels"] = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    assert scan([bootstrap_admin])["finding_count"] == 0


def test_a_bootstrap_label_never_hides_a_wildcard_role_under_a_built_in_name():
    labels = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    for name in ("admin", "edit", "view"):
        wildcard = role(name, [rule(["*"], ["*"], ["*"])])
        wildcard["metadata"]["labels"] = labels
        assert rules_of(scan([wildcard])) == [("rbac.admin-equivalent", "blocker")], name
        narrow = role(name, [rule(["get"], ["pods"])])
        narrow["metadata"]["labels"] = labels
        assert scan([narrow])["finding_count"] == 0, name


def test_rbac_scan_cluster_admin_bindings():
    crb = scan(bindings=[binding("b", "cluster-admin", [USER])])
    assert rules_of(crb) == [("rbac.cluster-admin-binding", "blocker")]
    rb = scan(bindings=[binding("b", "cluster-admin", [USER], kind="RoleBinding", namespace="dev")])
    assert rules_of(rb) == [("rbac.cluster-admin-binding", "major")]
    assert rb["findings"][0]["summary"] == "binds cluster-admin inside namespace dev"
    labelled = binding("cluster-admin", "cluster-admin", [{"kind": "Group", "name": "system:masters"}], **{"labels": {"kubernetes.io/bootstrapping": "rbac-defaults"}})
    assert rules_of(scan(bindings=[labelled])) == [("rbac.cluster-admin-binding", "blocker"), ("rbac.system-masters", "blocker")]


def test_rbac_scan_dangerous_subjects():
    def only(subject, kind="ClusterRoleBinding", namespace=None):
        return rules_of(scan(bindings=[binding("b", "viewer", [subject], kind=kind, namespace=namespace)]))

    assert only({"kind": "Group", "name": "system:masters"}) == [("rbac.system-masters", "blocker")]
    assert only({"kind": "User", "name": "system:anonymous"}) == [("rbac.anonymous-binding", "blocker")]
    assert only({"kind": "Group", "name": "system:unauthenticated"}) == [("rbac.anonymous-binding", "blocker")]
    assert only({"kind": "Group", "name": "system:authenticated"}) == [("rbac.authenticated-binding", "major")]
    assert only({"kind": "Group", "name": "system:serviceaccounts"}) == [("rbac.all-serviceaccounts-binding", "major")]
    assert only({"kind": "ServiceAccount", "name": "default", "namespace": "a"}) == [("rbac.default-sa-binding", "major")]
    assert only({"kind": "ServiceAccount", "name": "default", "namespace": "a"}, "RoleBinding", "a") == [("rbac.default-sa-binding", "minor")]
    assert only({"kind": "ServiceAccount", "name": "builder", "namespace": "a"}) == []
    assert only({"kind": "Group", "name": "system:serviceaccounts:a"}) == []


def test_rbac_scan_binding_to_risky_role_inherits_severity_but_rolebindings_cap_at_major():
    roles = [role("root", [rule(["*"], ["*"], ["*"])]), role("wide", [rule(["list"], ["secrets"])]), role("ok", [rule(["get"], ["pods"])]),
             role("local", [rule(["*"], ["pods"])], kind="Role", namespace="dev")]
    bindings = [binding("b1", "root", [USER]), binding("b2", "wide", [USER]), binding("b3", "ok", [USER]),
                binding("b4", "root", [USER], kind="RoleBinding", namespace="dev"),
                binding("b5", "local", [USER], kind="RoleBinding", namespace="dev", ref_kind="Role"),
                binding("b6", "local", [USER], kind="RoleBinding", namespace="other", ref_kind="Role")]
    result = scan(roles, bindings)
    risky = {f["id"].split(":")[1]: f["severity"] for f in result["findings"] if f["id"].startswith("rbac.binding-to-risky-role")}
    assert risky == {"ClusterRoleBinding/b1": "blocker", "ClusterRoleBinding/b2": "major", "RoleBinding/dev/b4": "major", "RoleBinding/dev/b5": "major"}
    assert next(f["summary"] for f in result["findings"] if f["id"] == "rbac.binding-to-risky-role:ClusterRoleBinding/b2") == \
        "User:alice gets ClusterRole/wide, which carries major rules"


def test_rbac_scan_aggregated_clusterrole_is_judged_on_aggregated_rules():
    parent = role("agg", [], aggregationRule={"clusterRoleSelectors": [{"matchLabels": {"agg": "yes"}}]})
    child = role("child", [rule(["*"], ["configmaps"])])
    child["metadata"]["labels"] = {"agg": "yes"}
    other = role("unrelated", [rule(["*"], ["services"])])
    result = scan([parent, child, other], [binding("b", "agg", [USER])])
    assert sorted(f["id"] for f in result["findings"]) == [
        "rbac.binding-to-risky-role:ClusterRoleBinding/b", "rbac.wildcard-verb:ClusterRole/child", "rbac.wildcard-verb:ClusterRole/unrelated"]


def test_rbac_scan_removed_beta_api_is_a_blocker():
    old = role("r", [rule(["get"], ["pods"])])
    old["apiVersion"] = "rbac.authorization.k8s.io/v1beta1"
    old_binding = binding("b", "r", [USER])
    old_binding["apiVersion"] = "rbac.authorization.k8s.io/v1beta1"
    result = scan([old], [old_binding])
    assert rules_of(result) == [("rbac.api-removed", "blocker"), ("rbac.api-removed", "blocker")]
    assert "removed since Kubernetes 1.22" in result["findings"][0]["summary"]


def test_rbac_scan_rejects_semantically_bad_input_naming_the_field():
    good_binding = binding("b", "r", [USER])
    invalid("kpol_rbac_risk_scan", {"roles": [], "bindings": []}, "roles")
    invalid("kpol_rbac_risk_scan", {"roles": [pod()], "bindings": []}, "roles")
    invalid("kpol_rbac_risk_scan", {"roles": [], "bindings": [role("r", [])]}, "bindings")
    invalid("kpol_rbac_risk_scan", {"roles": [role("r", "nope")], "bindings": []}, "roles")
    invalid("kpol_rbac_risk_scan", {"roles": [], "bindings": [{**good_binding, "roleRef": {"kind": "Pod", "name": "x"}}]}, "bindings")
    invalid("kpol_rbac_risk_scan", {"roles": [], "bindings": [binding("b", "r", [USER], kind="RoleBinding")]}, "bindings")
    invalid("kpol_rbac_risk_scan", {"roles": [], "bindings": [binding("b", "r", [USER], ref_kind="Role")]}, "bindings")
    invalid("kpol_rbac_risk_scan", {"roles": [], "bindings": [{**good_binding, "subjects": "alice"}]}, "bindings")
    assert scan(bindings=[good_binding])["binding_count"] == 1


# --- kpol_rbac_effective_access -------------------------------------------------------------------------------


def access(roles, bindings, subject, checks=None, groups=None):
    payload = {"roles": roles, "bindings": bindings, "subject": subject}
    if checks is not None:
        payload["checks"] = checks
    if groups is not None:
        payload["groups"] = groups
    return ok("kpol_rbac_effective_access", payload)


def test_effective_access_rolebinding_to_clusterrole_grants_only_inside_its_namespace():
    reader = role("reader", [rule(["get", "list"], ["pods"])])
    rb = binding("rb", "reader", [{"kind": "ServiceAccount", "name": "ci", "namespace": "build"}], kind="RoleBinding", namespace="prod")
    sa = {"kind": "ServiceAccount", "name": "ci", "namespace": "build"}
    checks = [{"verb": "get", "resource": "pods", "namespace": "prod"}, {"verb": "get", "resource": "pods", "namespace": "dev"},
              {"verb": "get", "resource": "pods"}, {"verb": "delete", "resource": "pods", "namespace": "prod"},
              {"verb": "get", "resource": "secrets", "namespace": "prod"}, {"verb": "get", "resource": "pods", "namespace": "prod", "api_group": "apps"}]
    result = access([reader], [rb], sa, checks)
    assert [c["allowed"] for c in result["checks"]] == [True, False, False, False, False, False]
    assert result["checks"][0]["granted_by"] == "ClusterRole/reader via namespace:prod"
    assert (result["access_level"], result["binding_count"], result["roles"], result["permission_count"]) == ("namespaced", 1, ["ClusterRole/reader"], 1)
    assert result["permissions"] == [{"scope": "namespace:prod", "role": "ClusterRole/reader", "api_groups": [""], "resources": ["pods"],
                                      "verbs": ["get", "list"], "resource_names": []}]


def test_effective_access_clusterrolebinding_and_cluster_admin_levels():
    reader = role("reader", [rule(["get"], ["pods"])])
    admin = role("root", [rule(["*"], ["*"], ["*"])])
    crb = binding("b", "reader", [USER])
    wide = access([reader], [crb], USER, [{"verb": "get", "resource": "pods", "namespace": "anywhere"}, {"verb": "get", "resource": "pods"}])
    assert wide["access_level"] == "cluster-wide" and [c["allowed"] for c in wide["checks"]] == [True, True]
    assert wide["checks"][0]["granted_by"] == "ClusterRole/reader via cluster"
    root = access([admin], [binding("b", "root", [USER])], USER, [{"verb": "delete", "resource": "nodes"}])
    assert root["access_level"] == "cluster-admin" and root["checks"][0]["allowed"] is True
    scoped_admin = access([admin], [binding("b", "root", [USER], kind="RoleBinding", namespace="x")], USER)
    assert scoped_admin["access_level"] == "namespaced"
    assert access([], [], USER)["access_level"] == "none"


def test_effective_access_identity_resolution_users_groups_and_service_accounts():
    viewer = role("viewer", [rule(["get"], ["pods"])])
    sa = {"kind": "ServiceAccount", "name": "ci", "namespace": "build"}
    for subject_entry, who in (
        ({"kind": "Group", "name": "system:serviceaccounts"}, sa), ({"kind": "Group", "name": "system:serviceaccounts:build"}, sa),
        ({"kind": "Group", "name": "system:authenticated"}, sa), ({"kind": "Group", "name": "system:authenticated"}, USER),
        ({"kind": "User", "name": "system:serviceaccount:build:ci"}, sa), ({"kind": "ServiceAccount", "name": "ci", "namespace": "build"}, sa),
        ({"kind": "User", "name": "alice"}, USER), ({"kind": "Group", "name": "system:unauthenticated"}, {"kind": "User", "name": "system:anonymous"}),
        ({"kind": "Group", "name": "devs"}, {"kind": "Group", "name": "devs"})):
        assert access([viewer], [binding("b", "viewer", [subject_entry])], who)["binding_count"] == 1, (subject_entry, who)
    for subject_entry, who in (
        ({"kind": "Group", "name": "system:serviceaccounts:other"}, sa), ({"kind": "ServiceAccount", "name": "ci", "namespace": "other"}, sa),
        ({"kind": "Group", "name": "system:authenticated"}, {"kind": "User", "name": "system:anonymous"}),
        ({"kind": "User", "name": "bob"}, USER), ({"kind": "ServiceAccount", "name": "alice", "namespace": "x"}, USER),
        ({"kind": "Group", "name": "devs"}, USER), ({"kind": "ServiceAccount", "name": "ci"}, sa), ({"kind": "Robot", "name": "x"}, USER)):
        assert access([viewer], [binding("b", "viewer", [subject_entry])], who)["binding_count"] == 0, (subject_entry, who)
    assert access([viewer], [binding("b", "viewer", [{"kind": "Group", "name": "devs"}])], USER, groups=["devs"])["binding_count"] == 1
    local_sa = {"kind": "ServiceAccount", "name": "ci"}
    in_build = binding("b", "viewer", [local_sa], kind="RoleBinding", namespace="build")
    assert access([viewer], [in_build], sa)["binding_count"] == 1
    assert access([viewer], [in_build], {"kind": "ServiceAccount", "name": "ci", "namespace": "other"})["binding_count"] == 0
    assert access([viewer], [in_build], {"kind": "User", "name": "system:serviceaccount:build:ci"})["binding_count"] == 1


def test_effective_access_aggregation_selectors_and_nesting():
    parent = role("admin", [rule(["get"], ["nodes"])], aggregationRule={"clusterRoleSelectors": [
        {"matchLabels": {"rbac/aggregate": "true"}}, {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["a", "b"]}]}]})
    by_label = role("by-label", [rule(["get"], ["pods"])])
    by_label["metadata"]["labels"] = {"rbac/aggregate": "true"}
    by_expr = role("by-expr", [rule(["get"], ["services"])])
    by_expr["metadata"]["labels"] = {"tier": "b"}
    nested = role("nested", [rule(["get"], ["ignored"])], aggregationRule={"clusterRoleSelectors": [{"matchLabels": {"leaf": "1"}}]})
    nested["metadata"]["labels"] = {"tier": "a"}
    leaf = role("leaf", [rule(["get"], ["configmaps"])])
    leaf["metadata"]["labels"] = {"leaf": "1"}
    miss = role("miss", [rule(["get"], ["secrets"])])
    miss["metadata"]["labels"] = {"tier": "c"}
    result = access([parent, by_label, by_expr, nested, leaf, miss], [binding("b", "admin", [USER])], USER)
    assert [p["resources"] for p in result["permissions"]] == [["configmaps"], ["pods"], ["services"]]
    assert result["roles"] == ["ClusterRole/admin"] and result["permission_count"] == 3


def test_effective_access_selector_operators():
    parent = role("p", [], aggregationRule={"clusterRoleSelectors": [{"matchExpressions": [
        {"key": "a", "operator": "Exists"}, {"key": "b", "operator": "DoesNotExist"}, {"key": "c", "operator": "NotIn", "values": ["x"]}]}]})
    ok_child = role("ok", [rule(["get"], ["pods"])])
    ok_child["metadata"]["labels"] = {"a": "1", "c": "y"}
    for labels in ({"a": "1", "b": "1"}, {"a": "1", "c": "x"}, {"c": "y"}):
        bad = role("bad", [rule(["get"], ["secrets"])])
        bad["metadata"]["labels"] = labels
        assert [p["resources"] for p in access([parent, ok_child, bad], [binding("b", "p", [USER])], USER)["permissions"]] == [["pods"]], labels
    empty = role("empty-selector", [], aggregationRule={"clusterRoleSelectors": [{}]})
    assert access([empty, ok_child], [binding("b", "empty-selector", [USER])], USER)["permission_count"] == 1
    assert access([role("no-selectors", [rule(["*"], ["*"], ["*"])], aggregationRule={}), ok_child], [binding("b", "no-selectors", [USER])], USER)["permission_count"] == 0


def test_effective_access_resource_names_and_subresources_follow_the_authorizer():
    names = role("names", [rule(["get", "update", "create", "list"], ["configmaps"], names=["leader"])])
    result = access([names], [binding("b", "names", [USER])], USER, [
        {"verb": "get", "resource": "configmaps", "resource_name": "leader"}, {"verb": "get", "resource": "configmaps", "resource_name": "other"},
        {"verb": "get", "resource": "configmaps"}, {"verb": "list", "resource": "configmaps"}, {"verb": "create", "resource": "configmaps"}])
    assert [c["allowed"] for c in result["checks"]] == [True, False, False, False, False]
    sub = role("sub", [rule(["get"], ["pods/log"]), rule(["create"], ["*/scale"], ["apps"]), rule(["get"], ["services"])])
    result = access([sub], [binding("b", "sub", [USER])], USER, [
        {"verb": "get", "resource": "pods", "subresource": "log"}, {"verb": "get", "resource": "pods"},
        {"verb": "create", "resource": "deployments", "subresource": "scale", "api_group": "apps"},
        {"verb": "create", "resource": "deployments", "api_group": "apps"},
        {"verb": "get", "resource": "services", "subresource": "proxy"}])
    assert [c["allowed"] for c in result["checks"]] == [True, False, True, False, False]
    assert [c["resource"] for c in result["checks"]][0] == "pods/log"
    star = role("star", [rule(["*"], ["*"], ["*"])])
    assert access([star], [binding("b", "star", [USER])], USER, [{"verb": "get", "resource": "pods", "subresource": "exec", "api_group": "x"}])["checks"][0]["allowed"]


def test_effective_access_reports_unresolved_roles_and_sorts_permissions():
    r1 = role("b-role", [rule(["get"], ["pods"]), rule(["get"], ["nodes"])])
    r2 = role("a-role", [rule(["list"], ["pods"])])
    result = access([r1, r2], [binding("x", "b-role", [USER]), binding("y", "a-role", [USER]), binding("z", "missing", [USER]),
                               binding("w", "ns-role", [USER], kind="RoleBinding", namespace="n", ref_kind="Role")], USER)
    assert result["unresolved_roles"] == ["ClusterRole/missing", "Role/n/ns-role"]
    assert [(p["role"], p["resources"]) for p in result["permissions"]] == [
        ("ClusterRole/a-role", ["pods"]), ("ClusterRole/b-role", ["nodes"]), ("ClusterRole/b-role", ["pods"])]
    assert result["binding_count"] == 4


def test_effective_access_namespaced_role_reference_resolves_in_the_binding_namespace():
    local = role("local", [rule(["get"], ["pods"])], kind="Role", namespace="n")
    rb = binding("rb", "local", [USER], kind="RoleBinding", namespace="n", ref_kind="Role")
    result = access([local], [rb], USER, [{"verb": "get", "resource": "pods", "namespace": "n"}])
    assert result["checks"][0]["granted_by"] == "Role/n/local via namespace:n"
    elsewhere = binding("rb", "local", [USER], kind="RoleBinding", namespace="m", ref_kind="Role")
    assert access([local], [elsewhere], USER)["unresolved_roles"] == ["Role/m/local"]


def test_effective_access_rejects_bad_subjects_and_manifests():
    invalid("kpol_rbac_effective_access", {"roles": [], "bindings": [], "subject": {"kind": "ServiceAccount", "name": "ci"}}, "subject")
    invalid("kpol_rbac_effective_access", {"roles": [pod()], "bindings": [], "subject": {"kind": "User", "name": "a"}}, "roles")
    invalid("kpol_rbac_effective_access", {"roles": [], "bindings": [binding("b", "r", [USER], kind="RoleBinding")],
                                           "subject": {"kind": "User", "name": "alice"}}, "bindings")
    invalid("kpol_rbac_effective_access", {"roles": [], "bindings": [], "subject": {"kind": "Robot", "name": "a"}}, "subject")


# --- kpol_admission_policy_lint -------------------------------------------------------------------------------


def lint(*policies):
    return ok("kpol_admission_policy_lint", {"policies": list(policies)})


def vwc(*webhooks, kind="ValidatingWebhookConfiguration", api="admissionregistration.k8s.io/v1"):
    return {"apiVersion": api, "kind": kind, "metadata": {"name": "w"}, "webhooks": list(webhooks)}


def hook(**over):
    return {"name": "check.example.com", "sideEffects": "None", "admissionReviewVersions": ["v1"], **over}


def test_admission_clean_validating_webhook_counts_as_enforcing():
    result = lint(vwc(hook()))
    assert (result["policy_count"], result["enforcing"], result["audit_only"], result["finding_count"], result["verdict"]) == (1, 1, 0, 0, "pass")


def test_admission_webhook_fail_open_is_major_for_validating_and_minor_for_mutating():
    assert rules_of(lint(vwc(hook(failurePolicy="Ignore")))) == [("admission.fail-open", "major")]
    mutating = lint(vwc(hook(failurePolicy="Ignore"), kind="MutatingWebhookConfiguration"))
    assert rules_of(mutating) == [("admission.fail-open", "minor")] and mutating["enforcing"] == 0
    assert mutating["findings"][0]["id"] == "admission.fail-open:MutatingWebhookConfiguration/w#check.example.com"
    assert rules_of(lint(vwc(hook(failurePolicy="Fail")))) == []
    assert rules_of(lint(vwc(hook(failurePolicy="Maybe")))) == [("admission.invalid-failure-policy", "major")]


@pytest.mark.parametrize("timeout, bad", [(1, False), (10, False), (30, False), (0, True), (31, True), (-1, True), (5.5, True), ("10", True), (True, True)])
def test_admission_webhook_timeout_must_be_an_integer_from_1_to_30(timeout, bad):
    assert (rules_of(lint(vwc(hook(timeoutSeconds=timeout)))) == [("admission.timeout", "major")]) is bad


def test_admission_webhook_v1_required_fields_and_side_effects():
    assert rules_of(lint(vwc({"name": "x", "admissionReviewVersions": ["v1"]}))) == [("admission.side-effects", "blocker")]
    assert rules_of(lint(vwc(hook(sideEffects="Some")))) == [("admission.side-effects", "blocker")]
    assert rules_of(lint(vwc(hook(sideEffects="NoneOnDryRun")))) == []
    assert rules_of(lint(vwc({"name": "x", "sideEffects": "None"}))) == [("admission.review-versions", "major")]
    assert rules_of(lint(vwc(hook(admissionReviewVersions=[])))) == [("admission.review-versions", "major")]


def test_admission_webhook_scope_escape_hatches():
    assert rules_of(lint(vwc(hook(matchPolicy="Exact")))) == [("admission.match-policy-exact", "minor")]
    assert rules_of(lint(vwc(hook(matchPolicy="Equivalent")))) == []
    ns = hook(namespaceSelector={"matchExpressions": [{"key": "kubernetes.io/metadata.name", "operator": "NotIn", "values": ["kube-system", "tools"]}]})
    result = lint(vwc(ns))
    assert rules_of(result) == [("admission.namespace-exclusion", "minor")]
    assert result["findings"][0]["summary"] == "namespaceSelector excludes: kubernetes.io/metadata.name NotIn [kube-system,tools]"
    obj_sel = hook(objectSelector={"matchExpressions": [{"key": "skip-policy", "operator": "DoesNotExist"}]})
    assert rules_of(lint(vwc(obj_sel))) == [("admission.object-selector-exclusion", "minor")]
    included = hook(namespaceSelector={"matchLabels": {"team": "a"}, "matchExpressions": [{"key": "env", "operator": "In", "values": ["prod"]}]})
    assert rules_of(lint(vwc(included))) == []


def test_admission_removed_webhook_api_is_a_blocker():
    result = lint(vwc(hook(), api="admissionregistration.k8s.io/v1beta1"))
    assert rules_of(result) == [("admission.api-removed", "blocker")] and result["enforcing"] == 1


def vap(name="deny-latest", **spec):
    return {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy", "metadata": {"name": name},
            "spec": {"validations": [{"expression": "true"}], **spec}}


def vap_binding(actions, policy="deny-latest", **spec):
    return {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding", "metadata": {"name": "b"},
            "spec": {"policyName": policy, "validationActions": actions, **spec}}


def test_admission_vap_actions_decide_enforce_versus_audit():
    enforcing = lint(vap(), vap_binding(["Deny"]))
    assert (enforcing["enforcing"], enforcing["audit_only"], enforcing["finding_count"], enforcing["policy_count"]) == (1, 0, 0, 2)
    assert lint(vap(), vap_binding(["Deny", "Audit"]))["enforcing"] == 1
    for actions in (["Warn"], ["Audit"], ["Warn", "Audit"]):
        audit = lint(vap(), vap_binding(actions))
        assert (audit["enforcing"], audit["audit_only"]) == (0, 1)
        assert rules_of(audit) == [("admission.audit-only", "minor")] and audit["verdict"] == "pass"
    for bad in ([], ["Block"], "Deny"):
        assert rules_of(lint(vap(), vap_binding(bad))) == [("admission.invalid-action", "major")]


def test_admission_vap_policy_flags():
    assert rules_of(lint(vap(failurePolicy="Ignore"), vap_binding(["Deny"]))) == [("admission.fail-open", "major")]
    excl = vap(matchConstraints={"excludeResourceRules": [{"resources": ["pods"]}],
                                 "namespaceSelector": {"matchExpressions": [{"key": "env", "operator": "NotIn", "values": ["dev"]}]}})
    assert rules_of(lint(excl, vap_binding(["Deny"]))) == [("admission.namespace-exclusion", "minor"), ("admission.resource-exclusion", "minor")]
    allow = vap_binding(["Deny"], paramRef={"name": "p", "parameterNotFoundAction": "Allow"},
                        matchResources={"namespaceSelector": {"matchExpressions": [{"key": "env", "operator": "DoesNotExist"}]}})
    assert rules_of(lint(vap(), allow)) == [("admission.namespace-exclusion", "minor"), ("admission.param-not-found-allow", "minor")]
    deny = vap_binding(["Deny"], paramRef={"name": "p", "parameterNotFoundAction": "Deny"})
    assert rules_of(lint(vap(), deny)) == []


def test_admission_vap_policies_and_bindings_must_reference_each_other():
    assert rules_of(lint(vap())) == [("admission.policy-unbound", "minor")]
    result = lint(vap_binding(["Deny"], policy="ghost"))
    assert rules_of(result) == [("admission.binding-orphan", "minor")]
    assert result["findings"][0]["summary"] == "policyName 'ghost' is not among the supplied ValidatingAdmissionPolicy objects"


def kyverno(rules, kind="ClusterPolicy", **spec):
    return {"apiVersion": "kyverno.io/v1", "kind": kind, "metadata": {"name": "p"}, "spec": {"rules": rules, **spec}}


def kyverno_rule(name="r", action=None, **over):
    return {"name": name, "validate": {"message": "m", **({"failureAction": action} if action else {})}, **over}


def test_kyverno_modes_and_rule_level_override():
    audit_default = lint(kyverno([kyverno_rule()]))
    assert (audit_default["enforcing"], audit_default["audit_only"]) == (0, 1) and rules_of(audit_default) == [("admission.audit-only", "minor")]
    enforce = lint(kyverno([kyverno_rule()], validationFailureAction="Enforce"))
    assert (enforce["enforcing"], enforce["audit_only"], enforce["finding_count"]) == (1, 0, 0)
    assert lint(kyverno([kyverno_rule()], validationFailureAction="enforce"))["enforcing"] == 1
    rule_audit = lint(kyverno([kyverno_rule(action="Audit")], validationFailureAction="Enforce"))
    assert (rule_audit["enforcing"], rule_audit["audit_only"]) == (0, 1)
    rule_enforce = lint(kyverno([kyverno_rule(action="Enforce")]))
    assert (rule_enforce["enforcing"], rule_enforce["audit_only"], rule_enforce["finding_count"]) == (1, 0, 0)
    mixed = lint(kyverno([kyverno_rule("a", "Enforce"), kyverno_rule("b", "Audit")]))
    assert (mixed["enforcing"], mixed["audit_only"]) == (1, 0) and rules_of(mixed) == [("admission.audit-only", "minor")]
    assert rules_of(lint(kyverno([kyverno_rule(action="Block")]))) == [("admission.invalid-action", "major")]
    mutate_only = lint(kyverno([{"name": "m", "mutate": {}}]))
    assert (mutate_only["enforcing"], mutate_only["audit_only"], mutate_only["finding_count"]) == (0, 0, 0)


def test_kyverno_policy_flags():
    assert rules_of(lint(kyverno([kyverno_rule()], validationFailureAction="Enforce", failurePolicy="Ignore"))) == [("admission.fail-open", "major")]
    overridden = kyverno([kyverno_rule()], validationFailureAction="Enforce", validationFailureActionOverrides=[{"action": "Audit", "namespaces": ["dev"]}])
    assert rules_of(lint(overridden)) == [("admission.enforce-overridden", "minor")]
    identity = kyverno([kyverno_rule(exclude={"any": [{"clusterRoles": ["cluster-admin"]}], "subjects": [{"kind": "User", "name": "x"}]})],
                       validationFailureAction="Enforce")
    result = lint(identity)
    assert rules_of(result) == [("admission.identity-exclusion", "minor")]
    assert result["findings"][0]["summary"] == "exclude lists clusterRoles, subjects: those identities bypass the rule"
    star = kyverno([kyverno_rule(exclude={"all": [{"resources": {"namespaces": ["*"]}}]})], validationFailureAction="Enforce")
    assert rules_of(lint(star)) == [("admission.namespace-exclusion", "major")]
    system = kyverno([kyverno_rule(exclude={"any": [{"resources": {"namespaces": ["kube-system"]}}]})], validationFailureAction="Enforce")
    assert rules_of(lint(system)) == []
    namespaced = kyverno([kyverno_rule()], kind="Policy", validationFailureAction="Enforce")
    assert lint(namespaced)["enforcing"] == 1


def constraint(action=None, **spec):
    return {"apiVersion": "constraints.gatekeeper.sh/v1beta1", "kind": "K8sRequiredLabels", "metadata": {"name": "c"},
            "spec": {**({"enforcementAction": action} if action else {}), **spec}}


def test_gatekeeper_enforcement_actions():
    for action, enforcing in ((None, 1), ("deny", 1)):
        result = lint(constraint(action))
        assert (result["enforcing"], result["audit_only"], result["finding_count"]) == (enforcing, 0, 0)
    for action in ("dryrun", "warn"):
        result = lint(constraint(action))
        assert (result["enforcing"], result["audit_only"]) == (0, 1) and rules_of(result) == [("admission.audit-only", "minor")]
    scoped_deny = lint(constraint("scoped", scopedEnforcementActions=[{"action": "warn", "enforcementPoints": [{"name": "audit"}]},
                                                                       {"action": "deny", "enforcementPoints": [{"name": "validation.gatekeeper.sh"}]}]))
    assert scoped_deny["enforcing"] == 1 and scoped_deny["finding_count"] == 0
    scoped_warn = lint(constraint("scoped", scopedEnforcementActions=[{"action": "warn"}]))
    assert (scoped_warn["enforcing"], scoped_warn["audit_only"]) == (0, 1)
    other = lint(constraint("block"))
    assert rules_of(other) == [("admission.invalid-action", "major")] and (other["enforcing"], other["audit_only"]) == (0, 0)


def test_admission_lint_ignores_unrelated_kinds_and_rejects_bad_manifests():
    result = lint({"kind": "ConfigMap", "metadata": {"name": "c"}}, {"apiVersion": "other.io/v1", "kind": "ClusterPolicy", "metadata": {"name": "x"}})
    assert (result["policy_count"], result["finding_count"]) == (0, 0)
    invalid("kpol_admission_policy_lint", {"policies": [{"metadata": {}}]}, "policies")


# --- kpol_cis_score -------------------------------------------------------------------------------------------


def cis(results, minimum=0):
    return ok("kpol_cis_score", {"results": results, "min_score_pct": minimum})


def rec(rec_id, status="PASS", scored=True, level=1):
    return {"id": rec_id, "status": status, "scored": scored, "level": level}


def test_cis_scores_only_scored_recommendations_and_counts_warn_as_not_passed():
    results = [rec("1.1.1"), rec("1.1.2"), rec("1.2.1", "FAIL"), rec("1.2.2", "WARN"), rec("1.2.3", "INFO"), rec("2.1.1", "FAIL", scored=False)]
    result = cis(results)
    assert (result["total"], result["scored_count"], result["manual_count"]) == (6, 4, 1)
    assert (result["passed"], result["failed"], result["warned"], result["score_pct"]) == (2, 1, 1, 50.0)
    assert result["sections"] == [{"section": "1.1", "passed": 2, "failed": 0, "warned": 0, "score_pct": 100.0},
                                  {"section": "1.2", "passed": 0, "failed": 1, "warned": 1, "score_pct": 0.0}]
    assert sorted(rules_of(result)) == [("cis.fail", "major"), ("cis.manual-fail", "info"), ("cis.warn", "minor")]
    assert result["verdict"] == "fail"


def test_cis_failure_severity_depends_on_profile_level():
    level2 = cis([rec("1.1.1"), rec("1.1.2", "FAIL", level=2)])
    assert rules_of(level2) == [("cis.fail", "minor")] and level2["verdict"] == "pass"
    level1 = cis([rec("1.1.1"), rec("1.1.2", "FAIL", level=1)])
    assert rules_of(level1) == [("cis.fail", "major")] and level1["verdict"] == "fail"


@pytest.mark.parametrize("minimum, verdict", [(74.99, "pass"), (75, "pass"), (75.01, "fail"), (100, "fail"), (0, "pass")])
def test_cis_threshold_boundary_is_inclusive(minimum, verdict):
    result = cis([rec("1.1.1"), rec("1.1.2"), rec("1.1.3"), rec("1.1.4", "FAIL", level=2)], minimum)
    assert result["score_pct"] == 75.0 and result["verdict"] == verdict
    assert ("cis.score-below-threshold" in rule_ids(result)) is (verdict == "fail")
    if verdict == "fail" and minimum == 75.01:
        assert next(f["summary"] for f in result["findings"] if f["id"] == "cis.score-below-threshold") == "score 75.0% is below the required 75.01%"


def test_cis_score_rounds_to_two_decimals():
    assert cis([rec("1.1.1"), rec("1.1.2"), rec("1.1.3", "FAIL", level=2)])["score_pct"] == 66.67


def test_cis_sections_sort_numerically():
    result = cis([rec("1.10.1"), rec("1.2.1"), rec("10.1.1"), rec("2.1.1"), rec("1.2.2.1")])
    assert [s["section"] for s in result["sections"]] == ["1.2", "1.10", "2.1", "10.1"]
    assert result["sections"][0]["passed"] == 2


def test_cis_rejects_bad_ids_duplicates_and_nothing_to_score():
    for bad in ("1", "a.b", "1..2", "1.2.", " 1.2", "1.2.3.4.5.6.7"):
        invalid("kpol_cis_score", {"results": [rec(bad)], "min_score_pct": 0}, "results")
    invalid("kpol_cis_score", {"results": [rec("1.1.1"), rec("1.1.1")], "min_score_pct": 0}, "results")
    invalid("kpol_cis_score", {"results": [rec("1.1.1", "INFO"), rec("1.1.2", scored=False)], "min_score_pct": 0}, "results")


# --- kpol_secret_exposure_check -------------------------------------------------------------------------------


def secrets(workloads=(), config=None):
    payload = {"workloads": list(workloads)}
    if config is not None:
        payload["encryption_config"] = config
    return ok("kpol_secret_exposure_check", payload)


def test_secret_env_injection_is_minor_and_volumes_are_preferred():
    container = {"name": "app", "image": "x", "env": [
        {"name": "DB_PASS", "valueFrom": {"secretKeyRef": {"name": "db", "key": "p"}}}, {"name": "PLAIN", "value": "1"},
        {"name": "CM", "valueFrom": {"configMapKeyRef": {"name": "c", "key": "k"}}}],
        "envFrom": [{"secretRef": {"name": "bulk"}}, {"configMapRef": {"name": "cm"}}]}
    result = secrets([pod(containers=[container])])
    assert sorted(f["summary"] for f in result["findings"]) == [
        "env DB_PASS read from secret db; mount a volume instead", "envFrom imports every key of secret bulk as environment variables"]
    assert rules_of(result) == [("secret.env-from-secret", "minor"), ("secret.env-secretkeyref", "minor")]
    assert result["verdict"] == "pass" and result["encryption_status"] == "not-provided"


@pytest.mark.parametrize("mode, expected", [(None, ("secret.default-mode-unset", "info")), (0o400, None), (0o440, None), (0o640, None),
                                            (0o644, ("secret.world-readable", "major")), (0o601, None), (0o001, None),
                                            (0o602, ("secret.world-writable", "major")), (0o604, ("secret.world-readable", "major")),
                                            (0o660, None), (0, None)])
def test_secret_volume_modes(mode, expected):
    volume = {"name": "s", "secret": {"secretName": "db", **({"defaultMode": mode} if mode is not None else {})}}
    result = secrets([pod(volumes=[volume])])
    assert rules_of(result) == ([expected] if expected else [])


def test_secret_item_modes_and_projected_volumes_are_checked():
    items = {"name": "s", "secret": {"secretName": "db", "defaultMode": 0o400, "items": [{"key": "k", "path": "p", "mode": 0o644}]}}
    assert rules_of(secrets([pod(volumes=[items])])) == [("secret.world-readable", "major")]
    projected = {"name": "p", "projected": {"defaultMode": 0o644, "sources": [{"secret": {"name": "db"}}, {"configMap": {"name": "c"}}]}}
    result = secrets([pod(volumes=[projected])])
    assert rules_of(result) == [("secret.world-readable", "major")] and "#volume/p" in result["findings"][0]["id"]
    only_config = {"name": "p", "projected": {"sources": [{"configMap": {"name": "c"}}]}}
    assert secrets([pod(volumes=[only_config])])["finding_count"] == 0


def test_secret_subpath_mounts_do_not_rotate():
    volume = {"name": "s", "secret": {"secretName": "db", "defaultMode": 0o400}}
    container = {"name": "app", "image": "x", "volumeMounts": [{"name": "s", "mountPath": "/a", "subPath": "k"}, {"name": "s", "mountPath": "/b"},
                                                                {"name": "other", "mountPath": "/c", "subPath": "k"}]}
    result = secrets([pod(containers=[container], volumes=[volume])])
    assert rules_of(result) == [("secret.subpath-no-rotation", "minor")]
    assert result["findings"][0]["summary"] == "subPath mount of secret volume s never receives secret updates"


def encryption(*providers, resources=("secrets",)):
    return {"apiVersion": "apiserver.config.k8s.io/v1", "kind": "EncryptionConfiguration",
            "resources": [{"resources": list(resources), "providers": list(providers)}]}


@pytest.mark.parametrize("providers, status, expected", [
    ([{"identity": {}}], "unencrypted", [("secret.encryption-identity-first", "blocker")]),
    ([{"aescbc": {"keys": []}}, {"identity": {}}], "weak", [("secret.encryption-identity-fallback", "info"), ("secret.encryption-local-keys", "info"),
                                                          ("secret.encryption-weak-provider", "minor")]),
    ([{"aesgcm": {"keys": []}}], "conditional", [("secret.encryption-local-keys", "info"), ("secret.encryption-rotation-required", "minor")]),
    ([{"secretbox": {"keys": []}}], "strong", [("secret.encryption-local-keys", "info")]),
    ([{"kms": {"name": "k", "endpoint": "unix:///s"}}], "conditional", [("secret.encryption-kms-v1", "minor")]),
    ([{"kms": {"apiVersion": "v1", "name": "k"}}], "conditional", [("secret.encryption-kms-v1", "minor")]),
    ([{"kms": {"apiVersion": "v2", "name": "k"}}, {"identity": {}}], "strong", [("secret.encryption-identity-fallback", "info")]),
    ([{"kms": {"apiVersion": "v2", "name": "k"}}], "strong", []),
])
def test_secret_encryption_provider_ranking(providers, status, expected):
    result = secrets(config=encryption(*providers))
    assert result["encryption_status"] == status
    assert sorted(rules_of(result)) == sorted(expected)


def test_secret_encryption_coverage_rules():
    missing = secrets(config=encryption({"aescbc": {}}, resources=("configmaps",)))
    assert rules_of(missing) == [("secret.encryption-missing", "blocker")] and missing["encryption_status"] == "unencrypted"
    for wildcard in ("*.", "*.*"):
        assert secrets(config=encryption({"secretbox": {}}, resources=(wildcard,)))["encryption_status"] == "strong"
    specific_first = {"apiVersion": "apiserver.config.k8s.io/v1", "kind": "EncryptionConfiguration", "resources": [
        {"resources": ["secrets"], "providers": [{"kms": {"apiVersion": "v2"}}]}, {"resources": ["*.*"], "providers": [{"identity": {}}]}]}
    assert secrets(config=specific_first)["encryption_status"] == "strong"
    wildcard_first = {"apiVersion": "apiserver.config.k8s.io/v1", "kind": "EncryptionConfiguration", "resources": [
        {"resources": ["*.*"], "providers": [{"identity": {}}]}, {"resources": ["secrets"], "providers": [{"kms": {"apiVersion": "v2"}}]}]}
    assert secrets(config=wildcard_first)["encryption_status"] == "unencrypted"


def test_secret_check_rejects_malformed_encryption_config():
    for config in ({"kind": "Other", "resources": []}, {"kind": "EncryptionConfiguration"}, {"kind": "EncryptionConfiguration", "resources": []},
                   encryption({"rot13": {}}), encryption({"aescbc": {}, "identity": {}}), encryption(), {"kind": "EncryptionConfiguration", "resources": ["x"]}):
        invalid("kpol_secret_exposure_check", {"workloads": [], "encryption_config": config}, "encryption_config")
    invalid("kpol_secret_exposure_check", {"workloads": [{"nokind": 1}]}, "workloads")


# --- kpol_sa_token_audit --------------------------------------------------------------------------------------


def sa_audit(workloads, accounts=None, required=None):
    payload = {"workloads": workloads}
    if accounts is not None:
        payload["service_accounts"] = accounts
    if required is not None:
        payload["token_required"] = required
    return ok("kpol_sa_token_audit", payload)


def test_sa_default_account_with_mounted_token_is_major():
    result = sa_audit([pod()])
    assert sorted(rules_of(result)) == [("sa.automount-enabled", "major"), ("sa.default-account", "minor")]
    assert result["verdict"] == "fail"


def test_sa_dedicated_account_still_mounting_a_token_is_minor():
    result = sa_audit([pod(serviceAccountName="builder")])
    assert rules_of(result) == [("sa.automount-enabled", "minor")] and result["verdict"] == "pass"
    legacy = sa_audit([pod(serviceAccount="builder")])
    assert rules_of(legacy) == [("sa.automount-enabled", "minor")]
    summaries = [f["summary"] for f in sa_audit([pod(serviceAccountName="builder")])["findings"] if f["id"].startswith("sa.automount-enabled")]
    assert summaries and all("automountServiceAccountToken: false" in s and "[REDACTED]" not in s for s in summaries)


def test_sa_automount_precedence_pod_then_service_account_then_default_true():
    sa_off = {"kind": "ServiceAccount", "metadata": {"name": "builder", "namespace": "prod"}, "automountServiceAccountToken": False}
    assert sa_audit([pod(serviceAccountName="builder")], [sa_off])["finding_count"] == 0
    assert rules_of(sa_audit([pod(serviceAccountName="builder", automountServiceAccountToken=True)], [sa_off])) == [("sa.automount-enabled", "minor")]
    sa_on = {**sa_off, "automountServiceAccountToken": True}
    assert sa_audit([pod(serviceAccountName="builder", automountServiceAccountToken=False)], [sa_on])["finding_count"] == 0
    assert sa_audit([pod(serviceAccountName="builder", automountServiceAccountToken=False)])["finding_count"] == 0
    other_namespace = {**sa_off, "metadata": {"name": "builder", "namespace": "other"}}
    assert rules_of(sa_audit([pod(serviceAccountName="builder")], [other_namespace])) == [("sa.automount-enabled", "minor")]
    assert sa_audit([pod(serviceAccountName="builder")], [sa_off])["service_account_count"] == 1


def test_sa_workloads_that_need_the_api_are_not_flagged_for_the_mount_but_not_for_sharing_default():
    assert sa_audit([pod(serviceAccountName="operator")], required=["web"])["finding_count"] == 0
    assert sa_audit([pod(serviceAccountName="operator")], required=["Pod/prod/web"])["finding_count"] == 0
    shared = sa_audit([pod()], required=["web"])
    assert sorted(rules_of(shared)) == [("sa.api-access-default-account", "major"), ("sa.default-account", "minor")]
    unrelated = sa_audit([pod(serviceAccountName="operator")], required=["other"])
    assert rules_of(unrelated) == [("sa.automount-enabled", "minor")]


def test_sa_projected_token_expiry_boundaries():
    def expiry(seconds):
        volume = {"name": "t", "projected": {"sources": [{"serviceAccountToken": {"path": "t", "audience": "api", "expirationSeconds": seconds}}]}}
        return rules_of(sa_audit([pod(serviceAccountName="b", automountServiceAccountToken=False, volumes=[volume])]))

    assert expiry(599) == [("sa.token-expiry-invalid", "blocker")]
    assert expiry(600) == []
    assert expiry(3600) == []
    assert expiry(86400) == []
    assert expiry(86401) == [("sa.token-long-lived", "minor")]
    assert expiry(2**32) == [("sa.token-long-lived", "minor")]
    assert expiry(2**32 + 1) == [("sa.token-expiry-invalid", "blocker")]


def test_sa_legacy_token_secrets_are_major():
    secret = {"kind": "Secret", "type": "kubernetes.io/service-account-token", "metadata": {"name": "tok", "namespace": "prod"}}
    plain = {"kind": "Secret", "type": "Opaque", "metadata": {"name": "x", "namespace": "prod"}}
    result = sa_audit([], [secret, plain])
    assert rules_of(result) == [("sa.legacy-token-secret", "major")] and result["findings"][0]["id"] == "sa.legacy-token-secret:Secret/prod/tok"


def test_sa_uses_the_workload_namespace_for_templates_and_rejects_bad_manifests():
    deployment = {"kind": "Deployment", "metadata": {"name": "d", "namespace": "prod"}, "spec": {"template": {"spec": pod(serviceAccountName="builder")["spec"]}}}
    sa_off = {"kind": "ServiceAccount", "metadata": {"name": "builder", "namespace": "prod"}, "automountServiceAccountToken": False}
    assert sa_audit([deployment], [sa_off])["finding_count"] == 0
    invalid("kpol_sa_token_audit", {"workloads": [], "service_accounts": [{"x": 1}]}, "service_accounts")


# --- kpol_image_admission_check -------------------------------------------------------------------------------

DIGEST = "sha256:" + "ab" * 32


def images(refs, allowed=("ghcr.io/acme",), digest=False, signature=False, verified=None):
    workloads = [pod(containers=[{"name": f"c{i}", "image": ref} for i, ref in enumerate(refs)])]
    payload = {"workloads": workloads, "allowed_registries": list(allowed), "require_digest": digest, "require_signature": signature}
    if verified is not None:
        payload["verified_digests"] = verified
    return ok("kpol_image_admission_check", payload)


@pytest.mark.parametrize("ref, allowed, passes", [
    ("ghcr.io/acme/app", ["ghcr.io/acme"], True), ("ghcr.io/acmeevil/app", ["ghcr.io/acme"], False), ("ghcr.io/acme", ["ghcr.io/acme"], True),
    ("ghcr.io/other/app", ["ghcr.io"], True), ("ghcr.io/acme/app", ["ghcr.io/acme/"], True), ("GHCR.IO/acme/app", ["ghcr.io/ACME"], True),
    ("nginx", ["docker.io/library"], True), ("nginx:1.27", ["docker.io"], True), ("bitnami/redis", ["docker.io/library"], False),
    ("bitnami/redis", ["docker.io/bitnami"], True), ("index.docker.io/library/nginx", ["docker.io/library"], True),
    ("registry-1.docker.io/library/nginx", ["docker.io"], True), ("localhost:5000/team/app:1", ["localhost:5000/team"], True),
    ("localhost/app", ["localhost"], True), ("registry.local:5000/app", ["registry.local"], False),
    ("registry.local:5000/app", ["registry.local:5000"], True), ("gcr.io/proj/app", ["ghcr.io/acme"], False)])
def test_image_registry_matching_at_path_boundaries(ref, allowed, passes):
    result = images([ref], allowed)
    assert ("image.registry-not-allowed" not in rule_ids(result)) is passes, result["findings"]


def test_image_reference_parsing_distinguishes_tag_digest_and_registry_port():
    result = images(["ghcr.io/acme/a", "ghcr.io/acme/b:latest", "ghcr.io/acme/c:1.2", f"ghcr.io/acme/d@{DIGEST}", f"ghcr.io/acme/e:1.2@{DIGEST}"])
    assert sorted(f["summary"] for f in result["findings"]) == [
        "ghcr.io/acme/a has no tag and no digest; pin it with @sha256:...",
        "ghcr.io/acme/b:latest uses the mutable tag latest; pin it with @sha256:...",
        "ghcr.io/acme/c:1.2 is pinned by tag 1.2 only; pin it with @sha256:..."]
    assert result["image_count"] == 5 and result["verdict"] == "pass"
    port = images(["ghcr.io:443/acme/a:1"], ["ghcr.io:443/acme"])
    assert [f["summary"] for f in port["findings"]] == ["ghcr.io:443/acme/a:1 is pinned by tag 1 only; pin it with @sha256:..."]


def test_image_require_digest_raises_unpinned_images_to_major():
    result = images(["ghcr.io/acme/a:1", f"ghcr.io/acme/b@{DIGEST}"], digest=True)
    assert rules_of(result) == [("image.not-pinned", "major")] and result["verdict"] == "fail"
    assert images(["ghcr.io/acme/a:1"], digest=False)["verdict"] == "pass"


def test_image_signature_requirement_uses_verified_digests_only():
    other = "sha256:" + "cd" * 32
    result = images([f"ghcr.io/acme/a@{DIGEST}", f"ghcr.io/acme/b@{other}", "ghcr.io/acme/c:1"], signature=True, verified=[DIGEST.upper().replace("SHA256", "sha256")])
    summaries = sorted(f["summary"] for f in result["findings"] if f["id"].startswith("image.unsigned"))
    assert summaries == [f"ghcr.io/acme/b@{other} has no verified signature: its digest is not in verified_digests",
                         "ghcr.io/acme/c:1 has no verified signature: a tag-only reference cannot carry signature evidence"]
    assert result["verdict"] == "fail"
    assert images([f"ghcr.io/acme/a@{DIGEST}"], signature=True, verified=[DIGEST])["finding_count"] == 0
    assert "image.unsigned" not in rule_ids(images(["ghcr.io/acme/c:1"], signature=False))
    assert rules_of(images([f"ghcr.io/acme/a@{DIGEST}"], signature=True)) == [("image.unsigned", "major")]


def test_image_checks_init_and_ephemeral_containers():
    workload = pod(containers=[{"name": "app", "image": "ghcr.io/x:1"}], initContainers=[{"name": "init", "image": "evil.io/x:1"}], ephemeralContainers=[{"name": "dbg", "image": "evil.io/y:1"}])
    result = ok("kpol_image_admission_check", {"workloads": [workload], "allowed_registries": ["ghcr.io"], "require_digest": False, "require_signature": False})
    ids = sorted(f["id"] for f in result["findings"] if f["id"].startswith("image.registry"))
    assert ids == ["image.registry-not-allowed:Pod/prod/web#ephemeralContainers/dbg", "image.registry-not-allowed:Pod/prod/web#initContainers/init"]


def test_image_check_rejects_bad_references_and_policy_inputs():
    base = {"allowed_registries": ["ghcr.io"], "require_digest": False, "require_signature": False}
    for ref in ("", "a b", "x@notadigest", "x@sha256:abc", "x:", "ghcr.io/"):
        invalid("kpol_image_admission_check", {"workloads": [pod(containers=[{"name": "c", "image": ref}])], **base}, "workloads")
    invalid("kpol_image_admission_check", {"workloads": [pod(containers=[{"name": "c"}])], **base}, "workloads")
    for entry in (" ", "ghcr.io/*", "ghcr io", "ghcr.io/a@sha256:x"):
        invalid("kpol_image_admission_check", {"workloads": [pod()], **{**base, "allowed_registries": [entry]}}, "allowed_registries")
    for digest in ("sha256:abc", "latest", "SHA256:" + "z" * 64):
        invalid("kpol_image_admission_check", {"workloads": [pod()], **base, "verified_digests": [digest]}, "verified_digests")


# --- kpol_runtime_rule_lint -----------------------------------------------------------------------------------


def frule(name="Shell in container", **over):
    base = {"rule": name, "desc": "a shell ran", "condition": "evt.type = execve and proc.name = bash", "output": "shell %proc.name",
            "priority": "NOTICE"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def falco(items, **extra):
    return ok("kpol_runtime_rule_lint", {"rules": items, **extra})


def test_falco_clean_rule_and_macro_counts():
    items = [{"required_engine_version": "0.40.0"}, {"list": "shells", "items": ["bash", "sh"]}, {"macro": "spawned", "condition": "evt.type = execve"},
             frule(condition="spawned and proc.name in (shells)")]
    result = falco(items)
    assert (result["rule_count"], result["macro_count"], result["finding_count"], result["verdict"], result["noisy_rules"]) == (1, 1, 0, "pass", [])


@pytest.mark.parametrize("key", ["desc", "condition", "output", "priority"])
def test_falco_missing_or_blank_required_fields_are_blockers(key):
    for value in (None, "", "   ", 5):
        result = falco([frule(**{key: value})])
        assert ("falco.missing-field", "blocker") in rules_of(result), (key, value)
        assert [f["summary"] for f in result["findings"] if f["id"].startswith("falco.missing-field")] == [f"{key} is required"]


def test_falco_priority_table_is_pinned():
    # falco_common.cpp: the eight priority names of the docs, and "info" accepted as a legacy alias of Informational
    assert importlib.import_module("swarm.tools.a24_tools").FALCO_PRIORITIES == (
        "emergency", "alert", "critical", "error", "warning", "notice", "informational", "debug", "info")


@pytest.mark.parametrize("priority", ["emergency", "ALERT", "Critical", "error", "WARNING", "notice", "Informational", "debug", "info", "INFO", "Info"])
def test_falco_accepts_each_priority_in_any_case(priority):
    assert "falco.invalid-priority" not in rule_ids(falco([frule(priority=priority)]))


@pytest.mark.parametrize("priority", ["warn", "fatal", "WARNING ", "high", "information", 3])
def test_falco_refuses_other_priorities(priority):
    result = falco([frule(priority=priority)])
    assert ("falco.invalid-priority", "blocker") in rules_of(result)
    assert result["verdict"] == "fail"


def test_falco_append_and_override_fragments_do_not_need_the_full_rule():
    assert falco([frule(), {"rule": "Shell in container", "append": True, "condition": "and not proc.pname = ci"}])["finding_count"] == 0
    assert falco([frule(), {"rule": "Shell in container", "override": {"condition": "append"}, "condition": "and x"}])["finding_count"] == 0
    assert rule_ids(falco([{"rule": "Lonely", "condition": "evt.type = open"}])) == {"falco.missing-field"}


def test_falco_bearer_token_after_a_space_is_masked_in_finding_id():
    token = "q8Lk2mPz9Rt4VbXw7Yn3Hc6Jd1Fg5Sa"  # opaque: no JWT shape, so only the Bearer rule can mask it
    result = falco([frule(name=f"Shell Bearer {token}", desc=None)])
    assert [f["id"] for f in result["findings"]] == ["falco.missing-field:Shell Bearer [REDACTED]"]
    assert token not in json.dumps(result)


def test_falco_duplicate_rule_names_are_minor_unless_appended():
    result = falco([frule(), frule()])
    assert rules_of(result) == [("falco.duplicate-rule", "minor")]
    assert result["findings"][0]["summary"] == "rule name defined 2 times without append or override"
    assert falco([frule(), frule(), {"rule": "Shell in container", "append": True}])["finding_count"] == 1
    assert falco([frule("a"), frule("b")])["finding_count"] == 0


@pytest.mark.parametrize("priority, enabled, flagged", [("emergency", False, True), ("alert", False, True), ("critical", False, True),
                                                       ("error", False, True), ("warning", False, False), ("notice", False, False),
                                                       ("critical", True, False), ("critical", None, False)])
def test_falco_disabled_detection_only_for_error_or_worse(priority, enabled, flagged):
    result = falco([frule(priority=priority, **({"enabled": enabled} if enabled is not None else {}))])
    assert (rules_of(result) == [("falco.disabled-detection", "minor")]) is flagged


def test_falco_exception_shapes():
    def shaped(*exceptions, **over):
        return rules_of(falco([frule(exceptions=list(exceptions), **over)]))

    good = {"name": "ci", "fields": ["proc.name", "fd.name"], "comps": ["=", "startswith"], "values": [["a", "/x"], ["b", "/y"]]}
    assert shaped(good) == []
    assert shaped({"name": "single", "fields": "proc.name", "values": ["a", "b"]}) == []
    assert shaped({"name": "one", "fields": ["proc.name"], "values": ["a", ["b"]]}) == []
    assert shaped({"fields": "proc.name", "values": ["a"]}) == [("falco.exception-shape", "blocker")]
    assert shaped(good, good) == [("falco.exception-shape", "blocker")]
    assert shaped({"name": "n", "values": ["a"]}) == [("falco.exception-shape", "blocker")]
    assert shaped({"name": "n", "fields": ["a", "b"], "comps": ["="], "values": [["x", "y"]]}) == [("falco.exception-shape", "blocker")]
    assert shaped({"name": "n", "fields": ["a", "b"], "values": [["x"]]}) == [("falco.exception-shape", "blocker")]
    assert shaped({"name": "n", "fields": ["a", "b"], "values": ["x"]}) == [("falco.exception-shape", "blocker")]
    result = falco([frule(exceptions="nope")])
    assert rules_of(result) == [("falco.exception-shape", "blocker")]
    appended = falco([{"rule": "Shell in container", "append": True, "exceptions": [{"name": "late", "values": ["x"]}]}])
    assert appended["finding_count"] == 0


@pytest.mark.parametrize("condition, flagged", [
    ("proc.name = bash", True), ("evt.type = execve and proc.name = bash", False), ("evt.type in (execve, execveat)", False),
    ("syscall.type = open", False), ("proc.name = bash and not user_known_shell", False), ('proc.name = "evt.type"', True),
    ("fd.name in (/etc/passwd, /etc/shadow)", True), ("proc.name in (shell_binaries) and container", False),
    ("proc.name startswith evt and fd.name glob /tmp/*", True), ("proc.name = bash and container.id != host", True),
    ("proc.name exists and not proc.pname exists", True)])
def test_falco_condition_without_evt_type_is_flagged_only_when_provably_unscoped(condition, flagged):
    result = falco([frule(condition=condition)])
    assert (rules_of(result) == [("falco.no-evt-type", "minor")]) is flagged, result["findings"]


def test_falco_macros_are_expanded_recursively_and_cycles_terminate():
    macros = [{"macro": "spawned", "condition": "evt.type in (execve, execveat)"}, {"macro": "in_container", "condition": "spawned and container.id != host"},
              {"macro": "plain", "condition": "proc.name = bash"}, {"macro": "loop_a", "condition": "loop_b and proc.name = x"},
              {"macro": "loop_b", "condition": "loop_a"}]
    assert falco([*macros, frule(condition="in_container")])["finding_count"] == 0
    assert rules_of(falco([*macros, frule(condition="plain")])) == [("falco.no-evt-type", "minor")]
    assert rules_of(falco([*macros, frule(condition="loop_a")])) == [("falco.no-evt-type", "minor")]
    appended = [{"macro": "m", "condition": "proc.name = bash"}, {"macro": "m", "append": True, "condition": "and evt.type = execve"}]
    assert falco([*appended, frule(condition="m")])["finding_count"] == 0


def test_falco_evt_type_check_skips_other_sources_and_opt_outs():
    assert falco([frule(condition="proc.name = bash", source="k8s_audit")])["finding_count"] == 0
    assert falco([frule(condition="proc.name = bash", source="syscall")])["finding_count"] == 1
    assert falco([frule(condition="proc.name = bash", warn_evttypes=False)])["finding_count"] == 0


def test_falco_noise_boundaries_over_the_window():
    def noisy(count, window=10, limit=5):
        return falco([frule()], alert_counts=[{"rule": "Shell in container", "count": count}], window_hours=window, max_alerts_per_hour=limit)

    assert noisy(50)["finding_count"] == 0 and noisy(50)["noisy_rules"] == []
    minor = noisy(51)
    assert rules_of(minor) == [("falco.noisy", "minor")] and minor["noisy_rules"] == ["Shell in container"] and minor["verdict"] == "pass"
    assert minor["findings"][0]["summary"] == "5.1 alerts/hour exceeds 5"
    assert rules_of(noisy(500)) == [("falco.noisy", "minor")]
    major = noisy(501)
    assert rules_of(major) == [("falco.noisy", "major")] and major["verdict"] == "fail"
    assert rules_of(noisy(1, window=0.5, limit=1.5)) == [("falco.noisy", "minor")]
    assert noisy(3, window=0.5, limit=6)["finding_count"] == 0


def test_falco_rejects_bad_items_and_incomplete_noise_inputs():
    invalid("kpol_runtime_rule_lint", {"rules": [5]}, "rules")
    invalid("kpol_runtime_rule_lint", {"rules": [{"unknown": 1}]}, "rules")
    invalid("kpol_runtime_rule_lint", {"rules": [{"rule": 5}]}, "rules")
    invalid("kpol_runtime_rule_lint", {"rules": [{"macro": "m", "condition": 5}]}, "rules")
    counts = [{"rule": "Shell in container", "count": 1}]
    invalid("kpol_runtime_rule_lint", {"rules": [frule()], "alert_counts": counts, "max_alerts_per_hour": 1}, "window_hours")
    invalid("kpol_runtime_rule_lint", {"rules": [frule()], "alert_counts": counts, "window_hours": 1}, "max_alerts_per_hour")
    invalid("kpol_runtime_rule_lint", {"rules": [frule()], "alert_counts": [{"rule": "ghost", "count": 1}], "window_hours": 1, "max_alerts_per_hour": 1}, "alert_counts")
    invalid("kpol_runtime_rule_lint", {"rules": [frule()], "alert_counts": counts + counts, "window_hours": 1, "max_alerts_per_hour": 1}, "alert_counts")


# --- kpol_tenant_isolation_check ------------------------------------------------------------------------------


def namespace(name="team-a", **labels):
    return {"kind": "Namespace", "metadata": {"name": name, "labels": labels}}


GOOD_LABELS = {"pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/enforce-version": "v1.31",
               "pod-security.kubernetes.io/warn": "restricted", "pod-security.kubernetes.io/audit": "restricted"}


def quota(hard, ns="team-a"):
    return {"kind": "ResourceQuota", "metadata": {"name": "q", "namespace": ns}, "spec": {"hard": hard}}


def netpol(name="np", ns="team-a", **spec):
    return {"kind": "NetworkPolicy", "metadata": {"name": name, "namespace": ns}, "spec": {"podSelector": {}, **spec}}


def isolated_tenant(ns="team-a"):
    return [namespace(ns, **GOOD_LABELS), quota({"requests.cpu": "4", "requests.memory": "8Gi", "pods": "50"}, ns),
            {"kind": "LimitRange", "metadata": {"name": "lr", "namespace": ns}, "spec": {}},
            netpol("deny-ingress", ns, policyTypes=["Ingress"]), netpol("deny-egress", ns, policyTypes=["Egress"])]


def tenants(manifests, names=("team-a",), level="restricted"):
    return ok("kpol_tenant_isolation_check", {"tenant_namespaces": list(names), "manifests": manifests, "required_psa_level": level})


def test_tenant_fully_isolated_namespace_passes():
    result = tenants(isolated_tenant())
    assert (result["tenant_count"], result["isolated_count"], result["finding_count"], result["verdict"]) == (1, 1, 0, "pass")


def test_tenant_with_nothing_configured_fails_every_control():
    result = tenants([])
    assert sorted(rules_of(result)) == sorted([
        ("tenant.namespace-missing", "major"), ("tenant.quota-missing", "major"), ("tenant.limitrange-missing", "minor"),
        ("tenant.ingress-default-deny-missing", "major"), ("tenant.egress-default-deny-missing", "minor")])
    assert (result["isolated_count"], result["verdict"]) == (0, "fail")


@pytest.mark.parametrize("enforce, required, expected", [
    ("restricted", "restricted", []), ("baseline", "restricted", [("tenant.psa-enforce-weak", "major")]),
    ("privileged", "restricted", [("tenant.psa-enforce-weak", "major")]), ("restricted", "baseline", []), ("baseline", "baseline", []),
    ("privileged", "baseline", [("tenant.psa-enforce-weak", "major")]), ("strict", "baseline", [("tenant.psa-enforce-invalid", "major")])])
def test_tenant_psa_enforce_level_ordering(enforce, required, expected):
    labels = {**GOOD_LABELS, "pod-security.kubernetes.io/enforce": enforce}
    manifests = [namespace(**labels), *isolated_tenant()[1:]]
    assert rules_of(tenants(manifests, level=required)) == expected


def test_tenant_psa_missing_enforce_unpinned_version_and_missing_warn_audit():
    only_warn = {k: v for k, v in GOOD_LABELS.items() if not k.endswith("/enforce")}
    assert rules_of(tenants([namespace(**only_warn), *isolated_tenant()[1:]])) == [("tenant.psa-enforce-missing", "major")]
    unpinned = {k: v for k, v in GOOD_LABELS.items() if not k.endswith("enforce-version")}
    assert rules_of(tenants([namespace(**unpinned), *isolated_tenant()[1:]])) == [("tenant.psa-version-unpinned", "info")]
    latest = {**GOOD_LABELS, "pod-security.kubernetes.io/enforce-version": "latest"}
    assert rules_of(tenants([namespace(**latest), *isolated_tenant()[1:]])) == [("tenant.psa-version-unpinned", "info")]
    no_audit = {k: v for k, v in GOOD_LABELS.items() if not k.endswith("/audit")}
    result = tenants([namespace(**no_audit), *isolated_tenant()[1:]])
    assert rules_of(result) == [("tenant.psa-warn-audit-missing", "info")] and result["verdict"] == "pass"


@pytest.mark.parametrize("hard, expected", [
    ({"requests.cpu": "1", "requests.memory": "1Gi", "pods": "5"}, []), ({"limits.cpu": "1", "limits.memory": "1Gi", "pods": "5"}, []),
    ({"cpu": "1", "memory": "1Gi", "pods": "5"}, []), ({"requests.cpu": "1", "limits.memory": "1Gi", "pods": "5"}, []),
    ({"requests.memory": "1Gi", "pods": "5"}, [("tenant.quota-incomplete", "major")]), ({"requests.cpu": "1", "pods": "5"}, [("tenant.quota-incomplete", "major")]),
    ({"requests.cpu": "1", "requests.memory": "1Gi"}, [("tenant.quota-no-pod-count", "minor")]),
    ({"pods": "5"}, [("tenant.quota-incomplete", "major")]), ({}, [("tenant.quota-incomplete", "major"), ("tenant.quota-no-pod-count", "minor")])])
def test_tenant_quota_must_bound_cpu_and_memory(hard, expected):
    manifests = [*isolated_tenant()[:1], quota(hard), *isolated_tenant()[2:]]
    assert sorted(rules_of(tenants(manifests))) == sorted(expected)


def test_tenant_quota_message_names_what_is_unbounded_and_quotas_merge():
    manifests = [*isolated_tenant()[:1], quota({"requests.cpu": "1", "pods": "5"}), *isolated_tenant()[2:]]
    assert tenants(manifests)["findings"][0]["summary"] == "ResourceQuota does not bound memory"
    second = {**quota({"requests.memory": "1Gi"}), "metadata": {"name": "q2", "namespace": "team-a"}}
    assert tenants([*manifests, second])["finding_count"] == 0
    elsewhere = quota({"requests.cpu": "1", "requests.memory": "1Gi", "pods": "5"}, ns="team-b")
    assert "tenant.quota-missing" in rule_ids(tenants([namespace(**GOOD_LABELS), elsewhere]))


def test_tenant_network_policy_default_deny_semantics():
    base = [*isolated_tenant()[:3]]

    def result(*policies):
        return rules_of(tenants([*base, *policies]))

    assert result(netpol(policyTypes=["Ingress", "Egress"])) == []
    assert result(netpol()) == [("tenant.egress-default-deny-missing", "minor")]
    assert result(netpol(policyTypes=["Egress"])) == [("tenant.ingress-default-deny-missing", "major")]
    assert result(netpol(egress=[{"to": [{"podSelector": {}}]}])) == [("tenant.egress-default-deny-missing", "minor")]
    assert result(netpol(policyTypes=["Ingress"], podSelector={"matchLabels": {"app": "x"}})) == [
        ("tenant.ingress-default-deny-missing", "major"), ("tenant.egress-default-deny-missing", "minor")]
    assert result(netpol(policyTypes=["Ingress"], ingress=[])) == [("tenant.egress-default-deny-missing", "minor")]
    assert result(netpol(policyTypes=["Ingress"], ingress=[{"from": [{"podSelector": {}}]}])) == [
        ("tenant.ingress-default-deny-missing", "major"), ("tenant.egress-default-deny-missing", "minor")]


def test_tenant_policy_types_default_to_ingress_plus_egress_only_when_egress_rules_exist():
    base = [*isolated_tenant()[:3]]
    egress_rules = rules_of(tenants([*base, netpol(egress=[{"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]}])]))
    assert egress_rules == [("tenant.egress-default-deny-missing", "minor")]
    empty_both = rules_of(tenants([*base, netpol(policyTypes=["Ingress", "Egress"])]))
    assert empty_both == []


def test_tenant_allow_all_ingress_cancels_default_deny():
    allow_all = netpol("allow", ingress=[{}])
    for rule_form in ({}, {"from": []}, {"ports": []}, {"from": [], "ports": []}):
        result = tenants([*isolated_tenant(), netpol("allow", policyTypes=["Ingress"], ingress=[rule_form])])
        assert sorted(rules_of(result)) == [("tenant.ingress-default-deny-missing", "major"), ("tenant.netpol-allow-all-ingress", "major")], rule_form
    scoped = netpol("allow", podSelector={"matchLabels": {"app": "web"}}, policyTypes=["Ingress"], ingress=[{}])
    assert tenants([*isolated_tenant(), scoped])["finding_count"] == 0
    ported = netpol("allow", policyTypes=["Ingress"], ingress=[{"ports": [{"port": 80}]}])
    cancelled = tenants([*isolated_tenant(), ported])  # a rule with no from admits every source on port 80: not isolated
    assert sorted(rules_of(cancelled)) == [("tenant.ingress-default-deny-missing", "major"), ("tenant.netpol-allow-all-ingress", "major")]
    assert cancelled["isolated_count"] == 0
    assert allow_all["spec"]["ingress"] == [{}]
    allow_egress = netpol("allow-egress", policyTypes=["Egress"], egress=[{}])
    assert rules_of(tenants([*isolated_tenant(), allow_egress])) == [("tenant.egress-default-deny-missing", "minor")]


def test_tenant_ingress_from_every_namespace_is_cross_tenant():
    wide = netpol("wide", podSelector={"matchLabels": {"app": "web"}}, policyTypes=["Ingress"], ingress=[{"from": [{"namespaceSelector": {}}]}])
    result = tenants([*isolated_tenant(), wide])
    assert rules_of(result) == [("tenant.netpol-all-namespaces", "major")]
    with_pods = netpol("wide2", podSelector={"matchLabels": {"app": "web"}}, policyTypes=["Ingress"],
                       ingress=[{"from": [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"a": "b"}}}]}])
    assert rules_of(tenants([*isolated_tenant(), with_pods])) == [("tenant.netpol-all-namespaces", "major")]
    labelled = netpol("ok", podSelector={"matchLabels": {"app": "web"}}, policyTypes=["Ingress"],
                      ingress=[{"from": [{"namespaceSelector": {"matchLabels": {"team": "a"}}}, {"podSelector": {}}]}])
    assert tenants([*isolated_tenant(), labelled])["finding_count"] == 0
    egress_only = netpol("e", podSelector={"matchLabels": {"app": "web"}}, policyTypes=["Egress"], egress=[{"to": [{"namespaceSelector": {}}]}])
    assert tenants([*isolated_tenant(), egress_only])["finding_count"] == 0


def test_tenant_cross_tenant_role_bindings():
    def binding_for(subject, ns="team-a"):
        return {"kind": "RoleBinding", "metadata": {"name": "rb", "namespace": ns}, "roleRef": {"kind": "ClusterRole", "name": "edit"}, "subjects": [subject]}

    both = ("team-a", "team-b")
    manifests = [*isolated_tenant("team-a"), *isolated_tenant("team-b")]

    def flagged(subject):
        result = tenants([*manifests, binding_for(subject)], both)
        return [f["summary"] for f in result["findings"]]

    assert flagged({"kind": "ServiceAccount", "name": "ci", "namespace": "team-b"}) == ["grants access to ServiceAccount ci of namespace team-b outside this tenant"]
    assert flagged({"kind": "Group", "name": "system:serviceaccounts:team-b"}) == ["grants access to Group system:serviceaccounts:team-b outside this tenant"]
    assert flagged({"kind": "Group", "name": "system:serviceaccounts"}) == ["grants access to Group system:serviceaccounts outside this tenant"]
    assert flagged({"kind": "ServiceAccount", "name": "ci", "namespace": "team-a"}) == []
    assert flagged({"kind": "ServiceAccount", "name": "ci", "namespace": "shared-tools"}) == []
    assert flagged({"kind": "Group", "name": "system:serviceaccounts:team-a"}) == []
    assert flagged({"kind": "User", "name": "alice"}) == []
    result = tenants([*manifests, binding_for({"kind": "ServiceAccount", "name": "ci", "namespace": "team-b"})], both)
    assert (result["tenant_count"], result["isolated_count"]) == (2, 1)


def test_tenant_namespaces_without_namespace_field_default_to_default():
    quota_in_default = {"kind": "ResourceQuota", "metadata": {"name": "q"}, "spec": {"hard": {"requests.cpu": "1", "requests.memory": "1", "pods": "1"}}}
    assert "tenant.quota-missing" not in rule_ids(tenants([quota_in_default], ("default",)))


def test_tenant_rejects_bad_manifests():
    invalid("kpol_tenant_isolation_check", {"tenant_namespaces": ["a"], "manifests": [{"metadata": {}}], "required_psa_level": "baseline"}, "manifests")
    bad_binding = {"kind": "RoleBinding", "metadata": {"name": "rb", "namespace": "a"}, "roleRef": {}, "subjects": "x"}
    invalid("kpol_tenant_isolation_check", {"tenant_namespaces": ["a"], "manifests": [bad_binding], "required_psa_level": "baseline"}, "manifests")
    assert tenants([], ("a", "a", "b"))["tenant_count"] == 2


# --- kpol_audit_policy_check ----------------------------------------------------------------------------------


def audit_policy(*rules, **extra):
    return {"apiVersion": "audit.k8s.io/v1", "kind": "Policy", "rules": list(rules), **extra}


def arule(level, group=None, resources=None, **selectors):
    out = {"level": level, **selectors}
    if group is not None:
        out["resources"] = [{"group": group, **({"resources": resources} if resources is not None else {})}]
    return out


def audit(policy):
    return ok("kpol_audit_policy_check", {"policy": policy})


GOOD_AUDIT = audit_policy(
    arule("Metadata", "", ["secrets", "configmaps", "serviceaccounts/token"]),
    arule("Metadata", "authentication.k8s.io", ["tokenreviews"]),
    arule("RequestResponse", "rbac.authorization.k8s.io"), arule("RequestResponse", "admissionregistration.k8s.io"),
    {"level": "Metadata"}, omitStages=["RequestReceived"], omitManagedFields=True)


def test_audit_complete_policy_has_no_findings():
    result = audit(GOOD_AUDIT)
    assert (result["rule_count"], result["failed_probes"], result["finding_count"], result["verdict"]) == (5, 0, 0, "pass")


def test_audit_probe_table_rows_under_and_over_audited():
    probes = [
        # id, group, resources, minimum, maximum, under severity, over severity
        ("secrets", "", ["secrets"], "Metadata", "Metadata", "major", "blocker"),
        ("service-account-tokens", "", ["serviceaccounts/token"], "Metadata", "Request", "major", "blocker"),
        ("token-reviews", "authentication.k8s.io", ["tokenreviews"], "None", "Metadata", None, "major"),
        ("rbac-writes", "rbac.authorization.k8s.io", ["roles", "rolebindings", "clusterroles", "clusterrolebindings"], "Request", "RequestResponse", "major", None),
        ("pod-exec", "", ["pods/exec", "pods/attach", "pods/portforward"], "Metadata", "RequestResponse", "major", None),
        ("pod-changes", "", ["pods"], "Metadata", "RequestResponse", "minor", None),
        ("admission-config", "admissionregistration.k8s.io",
         ["validatingwebhookconfigurations", "mutatingwebhookconfigurations", "validatingadmissionpolicies", "validatingadmissionpolicybindings"],
         "Request", "RequestResponse", "major", None),
        ("namespace-lifecycle", "", ["namespaces"], "Metadata", "RequestResponse", "minor", None),
    ]
    order = ["None", "Metadata", "Request", "RequestResponse"]

    def findings_for(probe, group, resources, level):
        result = audit(audit_policy(arule(level, group, resources)))
        return {f["id"].split(":")[0]: f["severity"] for f in result["findings"] if f["id"].startswith(f"audit.{probe}.")}

    for probe, group, resources, minimum, maximum, under, over in probes:
        assert findings_for(probe, group, resources, minimum) == {}, probe
        assert findings_for(probe, group, resources, maximum) == {}, probe
        if under:
            assert findings_for(probe, group, resources, order[order.index(minimum) - 1]) == {f"audit.{probe}.under-audited": under}, probe
        if over:
            assert findings_for(probe, group, resources, order[order.index(maximum) + 1]) == {f"audit.{probe}.over-audited": over}, probe


def test_audit_summary_lists_the_offending_requests_and_rules():
    result = audit(audit_policy(arule("RequestResponse", "", ["secrets"]), {"level": "Metadata"}))
    summary = next(f["summary"] for f in result["findings"] if f["id"].startswith("audit.secrets.over-audited"))
    assert summary == ("needs at most Metadata: get secrets -> RequestResponse (rules[0]); list secrets -> RequestResponse (rules[0]); "
                       "watch secrets -> RequestResponse (rules[0]); create secrets -> RequestResponse (rules[0]) and 3 more. "
                       "secret writes must be audited at Metadata (the request body carries the secret data) and reads at Metadata or Request "
                       "(only RequestResponse logs the secrets returned)")
    assert result["verdict"] == "fail"


def test_audit_requests_matching_no_rule_are_not_audited():
    result = audit(audit_policy(arule("Metadata", "", ["pods"])))
    assert result["failed_probes"] == 6
    assert "no rule" in next(f["summary"] for f in result["findings"] if f["id"].startswith("audit.secrets.under-audited"))
    assert "audit.no-catch-all" in rule_ids(result)


def test_audit_first_matching_rule_wins_and_other_users_never_shadow_the_probe():
    shadow = audit(audit_policy(arule("None", userGroups=["system:authenticated"]), {"level": "Metadata"}))
    assert shadow["failed_probes"] == 7
    assert "audit.unreachable-rule" not in rule_ids(shadow)
    other_user = audit(audit_policy(arule("None", users=["system:kube-proxy"]), arule("None", userGroups=["system:nodes"]), *GOOD_AUDIT["rules"],
                                    omitStages=["RequestReceived"], omitManagedFields=True))
    assert other_user["finding_count"] == 0
    probe_user = audit(audit_policy(arule("None", users=["kpol-probe-user"]), *GOOD_AUDIT["rules"]))
    assert probe_user["failed_probes"] == 7


def test_audit_rule_selectors_namespaces_verbs_resources_and_subresources():
    base = {"admission-config", "rbac-writes"}  # a Metadata catch-all alone leaves these two under-audited

    def failed(*rules):
        findings = audit(audit_policy(*rules, {"level": "Metadata"}))["findings"]
        return {f["id"].split(".")[1] for f in findings if f["id"].count(".") == 2}  # audit.<probe>.<under|over>-audited

    namespaced = base | {"secrets", "service-account-tokens", "pod-exec", "pod-changes"}
    core_all = base | {"secrets", "service-account-tokens", "pod-exec", "pod-changes", "namespace-lifecycle"}
    assert failed() == base
    assert failed(arule("None", namespaces=["default"])) == namespaced
    assert failed(arule("None", namespaces=["kube-system"])) == base
    assert failed(arule("None", namespaces=[""])) == base | {"namespace-lifecycle"}
    assert failed(arule("None", verbs=["get"], group="", resources=["secrets"])) == base | {"secrets"}
    assert failed(arule("None", verbs=["watch"], group="", resources=["secrets"])) == base | {"secrets"}
    assert failed(arule("None", verbs=["proxy"])) == base
    assert failed(arule("None", group="", resources=["pods"])) == base | {"pod-changes"}
    assert failed(arule("None", group="", resources=["pods/exec"])) == base | {"pod-exec"}
    assert failed(arule("None", group="", resources=["pods/*"])) == base | {"pod-exec", "pod-changes"}
    assert failed(arule("None", group="", resources=["*/exec"])) == base | {"pod-exec"}
    assert failed(arule("None", group="", resources=["*"])) == core_all
    assert failed(arule("None", group="")) == core_all
    assert failed(arule("None", group="apps", resources=["*"])) == base
    named = {"level": "None", "resources": [{"group": "", "resources": ["secrets"], "resourceNames": ["x"]}]}
    assert failed(named) == base
    assert failed({"level": "None", "nonResourceURLs": ["/healthz*"]}) == base
    assert failed({"level": "None", "nonResourceURLs": ["/healthz*"], "namespaces": ["default"]}) == namespaced


def test_audit_structure_findings():
    nothing = audit(audit_policy({"level": "None"}))
    assert ("audit.nothing-logged", "blocker") in rules_of(nothing)
    no_catch_all = audit(audit_policy(*GOOD_AUDIT["rules"][:-1], omitStages=["RequestReceived"], omitManagedFields=True))
    assert rules_of(no_catch_all) == [("audit.pod-exec.under-audited", "major"), ("audit.namespace-lifecycle.under-audited", "minor"),
                                      ("audit.no-catch-all", "minor"), ("audit.pod-changes.under-audited", "minor")]
    none_catch_all = audit(audit_policy(*GOOD_AUDIT["rules"][:-1], {"level": "None"}, omitStages=["RequestReceived"], omitManagedFields=True))
    assert ("audit.no-catch-all", "minor") in rules_of(none_catch_all)
    unreachable = audit(audit_policy({"level": "Metadata"}, arule("RequestResponse", "rbac.authorization.k8s.io"), arule("Metadata", "", ["pods"])))
    result = next(f for f in unreachable["findings"] if f["id"] == "audit.unreachable-rule")
    assert result["summary"] == "2 rule(s) after the selector-less rules[0] can never match" and result["severity"] == "minor"


def test_audit_informational_findings_about_stages_and_managed_fields():
    bare = audit(audit_policy(*GOOD_AUDIT["rules"]))
    assert sorted(rules_of(bare)) == [("audit.managed-fields-logged", "info"), ("audit.request-received-logged", "info")]
    assert bare["verdict"] == "pass"
    per_rule = audit(audit_policy(*[{**r, "omitStages": ["RequestReceived", "Panic"]} for r in GOOD_AUDIT["rules"]], omitManagedFields=True))
    assert per_rule["finding_count"] == 0
    metadata_only = audit(audit_policy({"level": "Metadata"}, omitStages=["RequestReceived"]))
    assert "audit.managed-fields-logged" not in rule_ids(metadata_only)
    partial = audit(audit_policy(*[{**r, "omitStages": ["RequestReceived"]} for r in GOOD_AUDIT["rules"][:-1]], {"level": "Metadata"}, omitManagedFields=True))
    assert "audit.request-received-logged" in rule_ids(partial)


def test_audit_rejects_malformed_policies():
    for policy in ({**GOOD_AUDIT, "apiVersion": "audit.k8s.io/v1beta1"}, {**GOOD_AUDIT, "kind": "Other"}, {**GOOD_AUDIT, "rules": []}, {**GOOD_AUDIT, "rules": "x"},
                   audit_policy({"level": "Verbose"}), audit_policy({"level": "None", "users": "bob"}), audit_policy({"level": "None", "verbs": [1]}),
                   audit_policy({"level": "None", "omitStages": ["Done"]}), audit_policy({"level": "None"}, omitStages=["Done"]),
                   audit_policy({"level": "None"}, omitStages="RequestReceived"), audit_policy({"level": "None", "resources": "pods"}),
                   audit_policy({"level": "None", "resources": ["pods"]}), audit_policy({"level": "None", "resources": [{"group": 1}]}),
                   audit_policy({"level": "None", "resources": [{"group": "", "resources": "pods"}]}), audit_policy("rule")):
        invalid("kpol_audit_policy_check", {"policy": policy}, "policy")


# --- kpol_exception_register_check ----------------------------------------------------------------------------

NOW = "2026-10-20T00:00:00Z"


def exc(**over):
    base = {"id": "EXC-1", "policy": "pss-restricted", "scope": "ns/legacy", "owner": "team-a", "approved_by": "sec-lead",
            "granted_at": "2026-10-01T00:00:00Z", "expires_at": "2026-12-01T00:00:00Z"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def register(*entries, now=NOW, warn=14, longest=90, review=30):
    return ok("kpol_exception_register_check", {"exceptions": list(entries), "now": now, "warn_days": warn, "max_duration_days": longest, "review_interval_days": review})


def test_exception_clean_entry_and_summary_fields():
    result = register(exc(), exc(id="EXC-2", expires_at="2026-12-01T01:00:00-01:00"))
    assert (result["exception_count"], result["active_count"], result["expired_count"], result["expiring_soon_count"], result["no_expiry_count"]) == (2, 2, 0, 0, 0)
    assert (result["finding_count"], result["verdict"], result["earliest_expiry"]) == (0, "pass", "2026-12-01T00:00:00Z")


def test_exception_expiry_boundary_is_inclusive_of_now():
    expired = register(exc(expires_at="2026-10-20T00:00:00Z"))
    assert rules_of(expired) == [("exception.expired", "major")] and (expired["expired_count"], expired["active_count"], expired["earliest_expiry"]) == (1, 0, "")
    assert expired["findings"][0]["id"] == "exception.expired:EXC-1 (pss-restricted / ns/legacy)"
    assert rules_of(register(exc(expires_at="2026-10-19T23:59:59Z"))) == [("exception.expired", "major")]
    assert rules_of(register(exc(expires_at="2026-10-20T00:00:01Z"))) == [("exception.expiring-soon", "minor")]
    offset = register(exc(expires_at="2026-10-20T02:00:00+02:00"))
    assert rules_of(offset) == [("exception.expired", "major")]


def test_exception_expiring_soon_window_is_inclusive():
    on_edge = register(exc(expires_at="2026-11-03T00:00:00Z"))
    assert rules_of(on_edge) == [("exception.expiring-soon", "minor")] and on_edge["expiring_soon_count"] == 1
    assert register(exc(expires_at="2026-11-03T00:00:01Z"))["finding_count"] == 0
    assert register(exc(expires_at="2026-10-20T00:00:01Z"), warn=0)["finding_count"] == 0
    assert register(exc(expires_at="2026-12-01T00:00:00Z"), warn=365)["expiring_soon_count"] == 1


def test_exception_without_expiry_is_major_and_still_active():
    result = register(exc(expires_at=None))
    assert rules_of(result) == [("exception.no-expiry", "major")] and (result["no_expiry_count"], result["active_count"], result["earliest_expiry"]) == (1, 1, "")


def test_exception_lifetime_limit_boundary():
    assert register(exc(granted_at="2026-10-01T00:00:00Z", expires_at="2026-12-30T00:00:00Z"))["finding_count"] == 0
    over = register(exc(granted_at="2026-10-01T00:00:00Z", expires_at="2026-12-30T00:00:01Z"))
    assert rules_of(over) == [("exception.too-long", "major")]
    assert over["findings"][0]["summary"] == "lifetime exceeds 90 day(s)"
    assert rules_of(register(exc(expires_at="2026-12-01T00:00:00Z"), longest=60)) == [("exception.too-long", "major")]
    assert register(exc(expires_at="2026-12-01T00:00:00Z"), longest=61)["finding_count"] == 0


def test_exception_review_staleness_boundary_uses_last_review_else_grant():
    assert register(exc(granted_at="2026-09-20T00:00:00Z", expires_at="2026-11-30T00:00:00Z"))["finding_count"] == 0
    stale = register(exc(granted_at="2026-09-19T23:59:59Z", expires_at="2026-11-30T00:00:00Z"))
    assert rules_of(stale) == [("exception.stale-review", "minor")]
    reviewed = exc(granted_at="2026-08-01T00:00:00Z", expires_at="2026-11-30T00:00:00Z", last_reviewed_at="2026-09-20T00:00:00Z")
    assert register(reviewed, longest=200)["finding_count"] == 0
    assert rules_of(register({**reviewed, "last_reviewed_at": "2026-09-19T23:59:59Z"}, longest=200)) == [("exception.stale-review", "minor")]
    expired_and_old = exc(granted_at="2026-08-01T00:00:00Z", expires_at="2026-10-19T00:00:00Z")
    assert rule_ids(register(expired_and_old, longest=100)) == {"exception.expired"}


def test_exception_approver_hygiene():
    assert rules_of(register(exc(approved_by=""))) == [("exception.no-approver", "major")]
    assert rules_of(register(exc(approved_by="   "))) == [("exception.no-approver", "major")]
    assert rules_of(register(exc(approved_by="TEAM-A "))) == [("exception.self-approved", "major")]
    assert register(exc(approved_by="team-b"))["finding_count"] == 0


def test_exception_granted_in_the_future_is_minor():
    result = register(exc(granted_at="2026-10-25T00:00:00Z", expires_at="2026-11-25T00:00:00Z"))
    assert rules_of(result) == [("exception.future-grant", "minor")]


def test_exception_timestamps_are_normalised_across_offsets():
    result = register(exc(expires_at="2026-12-01T05:30:00+05:30"), exc(id="EXC-2", expires_at="2026-12-01T00:00:00+00:00"))
    assert result["earliest_expiry"] == "2026-12-01T00:00:00Z"
    assert register(exc(expires_at="2026-12-01T01:00:00+01:00"), now="2026-10-20T01:00:00+01:00")["finding_count"] == 0


def test_exception_register_rejects_bad_timestamps_order_and_duplicates():
    for entry in (exc(granted_at="2026-10-01T00:00:00"), exc(granted_at="2026-10-01"), exc(granted_at="not a date"), exc(expires_at="2026-12-01 00:00"),
                  exc(expires_at="2026-10-01T00:00:00Z"), exc(expires_at="2026-09-01T00:00:00Z"), exc(last_reviewed_at="2026-09-30T00:00:00Z"),
                  exc(last_reviewed_at="2026-10-01T00:00:00")):
        invalid("kpol_exception_register_check", {"exceptions": [entry], "now": NOW, "warn_days": 14, "max_duration_days": 90, "review_interval_days": 30}, "exceptions")
    invalid("kpol_exception_register_check", {"exceptions": [exc(), exc()], "now": NOW, "warn_days": 14, "max_duration_days": 90, "review_interval_days": 30}, "exceptions")
    for now in ("2026-10-20T00:00:00", "2026-10-20", "soon"):
        invalid("kpol_exception_register_check", {"exceptions": [exc()], "now": now, "warn_days": 14, "max_duration_days": 90, "review_interval_days": 30}, "now")


# --- cross-cutting limits and remaining INVALID_INPUT fields -------------------------------------------------------


def test_findings_are_trimmed_to_a_serialised_size_budget_but_the_count_stays_true():
    long_names = pod(containers=[{"name": "c" * 200 + str(i), "image": "x"} for i in range(200)])
    result = pss([long_names])
    assert result["finding_count"] == 800
    assert 200 < len(result["findings"]) < 500
    assert len(json.dumps(result["findings"])) <= 100_000
    assert [f["id"] for f in result["findings"]] == sorted((f["id"] for f in result["findings"]))


def test_effective_access_permission_list_is_capped_at_200_entries():
    many = role("many", [rule(["get"], [f"res{i:03d}"]) for i in range(250)])
    result = access([many], [binding("b", "many", [USER])], USER)
    assert result["permission_count"] == 250 and len(result["permissions"]) == 200
    assert result["permissions"][0]["resources"] == ["res000"] and result["permissions"][-1]["resources"] == ["res199"]


def test_sa_audit_rejects_malformed_workloads_naming_the_field():
    invalid("kpol_sa_token_audit", {"workloads": [{"x": 1}]}, "workloads")
    invalid("kpol_secret_exposure_check", {"workloads": [{"kind": "Deployment", "spec": {}}]}, "workloads")
    invalid("kpol_image_admission_check", {"workloads": [{"kind": "Pod", "spec": "x"}], "allowed_registries": ["a.io"], "require_digest": False,
                                           "require_signature": False}, "workloads")


def test_exception_register_accepts_a_lowercase_z_suffix_and_fractional_seconds():
    result = register(exc(expires_at="2026-12-01T00:00:00z"), exc(id="EXC-2", expires_at="2026-12-01T00:00:00.500Z"))
    assert result["finding_count"] == 0 and result["earliest_expiry"] == "2026-12-01T00:00:00Z"


def test_rbac_scan_role_without_rules_and_unnamed_objects_are_handled():
    bare = {"kind": "Role", "metadata": {"namespace": "n"}}
    assert scan([bare])["finding_count"] == 0
    unnamed = scan([{"kind": "ClusterRole", "rules": [rule(["*"], ["*"], ["*"])]}])
    assert unnamed["findings"][0]["id"] == "rbac.admin-equivalent:ClusterRole/(unnamed)"


# --- independent review: regressions for defects found against primary sources ---------------------------------------


def test_pss_null_selinux_fields_are_unset_not_violations():
    # a JSON null is an unset field; PSS allows Undefined/"" for seLinuxOptions.type|user|role
    nulls = {"seLinuxOptions": {"type": None, "user": None, "role": None}}
    workload = pod(containers=[{"name": "app", "image": "x", "securityContext": dict(nulls)}], securityContext=dict(nulls))
    assert pss([workload], "baseline")["finding_count"] == 0


def test_pss_run_as_user_false_is_not_root():
    assert "pss.run-as-user" not in rule_ids(pss([pod(containers=[hardened(runAsUser=False)])]))
    assert "pss.run-as-user" in rule_ids(pss([pod(containers=[hardened(runAsUser=0)])]))
    assert "pss.run-as-user" in rule_ids(pss([pod(containers=[hardened(runAsUser=0.0)])]))


def test_pss_apparmor_annotation_value_is_withheld():
    canary = "ghp_" + "A1b2C3d4" * 4 + "ZZZZ"
    workload = {**pod(), "metadata": {"name": "web", "namespace": "prod",
                                      "annotations": {"container.apparmor.security.beta.kubernetes.io/app": canary}}}
    result = pss([workload], "baseline")
    assert rules_of(result) == [("pss.apparmor", "major")] and canary not in json.dumps(result)


def test_rbac_scan_subresource_wildcards_cover_exec_proxy_and_token():
    assert role_rules(["create"], ["*/exec"]) == [("rbac.pod-exec", "major")]
    assert role_rules(["get"], ["*/proxy"]) == [("rbac.nodes-proxy", "major")]
    assert role_rules(["create"], ["*/token"]) == [("rbac.token-request", "major")]
    assert role_rules(["create"], ["*/log"]) == []


def test_rbac_scan_a_forged_bootstrap_label_does_not_hide_a_binding():
    # anyone can set the label, so only the names the API server creates are trusted
    forged = binding("evil", "cluster-admin", [{"kind": "User", "name": "mallory"}], **{"labels": {"kubernetes.io/bootstrapping": "rbac-defaults"}})
    assert rules_of(scan(bindings=[forged])) == [("rbac.cluster-admin-binding", "blocker")]
    forged_role = role("backdoor", [rule(["*"], ["*"], ["*"])], labels={"kubernetes.io/bootstrapping": "rbac-defaults"})
    assert rules_of(scan([forged_role])) == [("rbac.admin-equivalent", "blocker")]
    genuine = binding("system:node", "system:node", [{"kind": "Group", "name": "system:nodes"}], **{"labels": {"kubernetes.io/bootstrapping": "rbac-defaults"}})
    assert scan(bindings=[genuine])["finding_count"] == 0


def test_rbac_scan_default_admin_and_edit_bound_cluster_wide_are_major():
    # RBAC docs: edit/admin let the subject read Secrets and run pods as any service account of the namespace
    for name in ("admin", "edit"):
        assert rules_of(scan(bindings=[binding("b", name, [USER])])) == [("rbac.broad-default-role-binding", "major")]
        assert scan(bindings=[binding("b", name, [USER], kind="RoleBinding", namespace="ns")])["finding_count"] == 0
    assert scan(bindings=[binding("b", "view", [USER])])["finding_count"] == 0


def test_rbac_scan_aggregated_role_without_supplied_children_keeps_its_live_rules():
    parent = role("agg", [rule(["*"], ["configmaps"])], aggregationRule={"clusterRoleSelectors": [{"matchLabels": {"agg": "yes"}}]})
    assert rules_of(scan([parent])) == [("rbac.wildcard-verb", "major")]
    child = role("child", [rule(["get"], ["pods"])])
    child["metadata"]["labels"] = {"agg": "yes"}
    assert scan([parent, child])["finding_count"] == 0  # a matching child replaces the stale own rules


def test_rbac_scan_selector_with_unhashable_key_is_not_a_crash():
    parent = role("agg", [], aggregationRule={"clusterRoleSelectors": [{"matchExpressions": [{"key": {"x": 1}, "operator": "In", "values": ["a"]}]}]})
    assert scan([parent, role("c", [rule(["get"], ["pods"])])])["state"] == "SUCCESS"


def test_effective_access_cluster_admin_needs_a_rule_not_narrowed_by_resource_names():
    narrow = role("narrow", [rule(["*"], ["*"], ["*"], names=["only-this"])])
    result = access([narrow], [binding("b", "narrow", [USER])], USER, [{"verb": "delete", "resource": "nodes", "resource_name": "other"},
                                                                         {"verb": "delete", "resource": "nodes", "resource_name": "only-this"}])
    assert result["access_level"] == "cluster-wide" and [c["allowed"] for c in result["checks"]] == [False, True]


def test_effective_access_unsupplied_cluster_admin_role_is_the_bootstrap_definition():
    result = access([], [binding("b", "cluster-admin", [USER])], USER, [{"verb": "delete", "resource": "nodes"}])
    assert (result["access_level"], result["roles"], result["unresolved_roles"], result["checks"][0]["allowed"]) == \
        ("cluster-admin", ["ClusterRole/cluster-admin"], [], True)
    other = access([], [binding("b", "custom", [USER])], USER)
    assert (other["access_level"], other["unresolved_roles"]) == ("none", ["ClusterRole/custom"])


def test_effective_access_system_masters_is_cluster_admin_without_any_binding():
    for subject, groups in (({"kind": "Group", "name": "system:masters"}, None), (USER, ["system:masters"])):
        result = access([], [], subject, [{"verb": "delete", "resource": "namespaces"}], groups=groups)
        assert result["access_level"] == "cluster-admin" and result["checks"][0]["granted_by"] == "Group/system:masters via cluster"
        assert result["binding_count"] == 0 and result["roles"] == []


def test_effective_access_aggregated_live_dump_without_children_uses_its_rules():
    parent = role("agg", [rule(["get"], ["pods"])], aggregationRule={"clusterRoleSelectors": [{"matchLabels": {"agg": "yes"}}]})
    result = access([parent], [binding("b", "agg", [USER])], USER, [{"verb": "get", "resource": "pods"}])
    assert result["checks"][0]["allowed"] is True and result["permission_count"] == 1


def test_effective_access_long_names_never_push_the_result_over_the_output_limit():
    names = [f"r{i:04d}-" + "a" * 240 for i in range(300)]
    roles_ = [role(n, [rule(["get"], ["pods"])]) for n in names]
    bindings_ = [binding(f"b{i}", n, [USER]) for i, n in enumerate(names)] + [binding(f"u{i}", f"missing{i:04d}-" + "m" * 240, [USER]) for i in range(200)]
    result = access(roles_, bindings_, USER)
    assert result["state"] == "SUCCESS" and len(json.dumps(result, separators=(",", ":"))) < 200_000
    assert result["roles"][-1].startswith("... ") and result["unresolved_roles"][-1].startswith("... ")
    assert len(result["roles"]) + int(result["roles"][-1].split()[1]) - 1 == 300


def test_admission_vap_binding_with_deny_and_warn_is_invalid_and_not_enforcing():
    # kubernetes validation: "must not contain both Deny and Warn"
    result = lint(vap(), vap_binding(["Deny", "Warn"]))
    assert rules_of(result) == [("admission.invalid-action", "major")]
    assert (result["enforcing"], result["audit_only"]) == (0, 0)
    assert lint(vap(), vap_binding(["Deny", "Audit"]))["enforcing"] == 1


@pytest.mark.parametrize("points, enforcing", [
    (["validation.gatekeeper.sh"], 1), (["vap.k8s.io"], 1), (["*"], 1), (["audit.gatekeeper.sh"], 0), (["gator.gatekeeper.sh"], 0),
    (["audit.gatekeeper.sh", "gator.gatekeeper.sh"], 0), ([], 0), (["audit.gatekeeper.sh", "validation.gatekeeper.sh"], 1)])
def test_gatekeeper_scoped_deny_enforces_only_at_an_admission_point(points, enforcing):
    # Gatekeeper enforcement-points docs: audit and gator never block a live request; an entry without points is enforced nowhere
    scoped = constraint("scoped", scopedEnforcementActions=[{"action": "deny", "enforcementPoints": [{"name": n} for n in points]}])
    result = lint(scoped)
    assert (result["enforcing"], result["audit_only"]) == (enforcing, 1 - enforcing)
    assert ("admission.audit-only" in rule_ids(result)) is (enforcing == 0)


def test_gatekeeper_scoped_warn_at_admission_and_deny_in_shift_left_is_audit_only():
    scoped = constraint("scoped", scopedEnforcementActions=[
        {"action": "warn", "enforcementPoints": [{"name": "validation.gatekeeper.sh"}]},
        {"action": "deny", "enforcementPoints": [{"name": "gator.gatekeeper.sh"}]}])
    assert lint(scoped)["audit_only"] == 1


def test_kyverno_rule_level_failure_action_overrides_are_reported():
    rule_ = kyverno_rule(action="Enforce")
    rule_["validate"]["failureActionOverrides"] = [{"action": "Audit", "namespaces": ["dev"]}]
    result = lint(kyverno([rule_]))
    assert result["enforcing"] == 1 and rules_of(result) == [("admission.enforce-overridden", "minor")]


def test_admission_invalid_values_are_withheld_when_long_or_odd():
    canary = "https://user:hunter2@internal.example/hook?token=ghp_" + "x" * 30
    result = lint(vwc(hook(failurePolicy=canary, sideEffects=canary)))
    assert {f["id"].split(":")[0] for f in result["findings"]} == {"admission.invalid-failure-policy", "admission.side-effects"}
    assert "hunter2" not in json.dumps(result) and "ghp_" not in json.dumps(result)
    short = lint(vwc(hook(failurePolicy="Maybe")))
    assert short["findings"][0]["summary"] == "failurePolicy 'Maybe' must be Fail or Ignore"


def test_cis_rounding_is_half_up_and_a_score_below_the_threshold_never_prints_as_it():
    # 1 pass of 800 scored = 0.125 %: half up gives 0.13 (round() on the binary float gave 0.12)
    results = [rec(f"1.{i // 100}.{i % 100 + 1}", "PASS" if i == 0 else "FAIL", level=2) for i in range(800)]
    assert cis(results)["score_pct"] == 0.13
    # 1694 of 1993 = 84.9974...%: two places would print 85.0 next to a threshold of 85
    results = [rec(f"{i // 1000 + 1}.{i // 100 % 10}.{i % 100 + 1}", "PASS" if i < 1694 else "FAIL", level=2) for i in range(1993)]
    below = cis(results, 85)
    assert below["score_pct"] == 85.0 and below["verdict"] == "fail"
    assert next(f["summary"] for f in below["findings"] if f["id"] == "cis.score-below-threshold") == "score 84.997491% is below the required 85%"


def test_falco_noisy_rate_never_prints_as_the_limit_it_exceeds():
    # 5.004 alerts/hour rounds to 5.0 at two places, which would read as "5.0 exceeds 5"
    result = falco([frule()], alert_counts=[{"rule": "Shell in container", "count": 5004}], window_hours=1000, max_alerts_per_hour=5)
    assert result["findings"][0]["summary"] == "5.004 alerts/hour exceeds 5"


def test_falco_noisy_rule_list_is_capped_and_counted():
    names = [f"{i:04d}-" + "é" * 240 for i in range(500)]
    result = falco([frule(name) for name in names], alert_counts=[{"rule": n, "count": 10**9} for n in names], window_hours=1, max_alerts_per_hour=1)
    assert result["state"] == "SUCCESS" and result["noisy_rules"][-1].startswith("... ")
    assert len(result["noisy_rules"]) - 1 + int(result["noisy_rules"][-1].split()[1]) == 500


def test_sa_audit_non_string_names_are_not_a_crash():
    workload = {"kind": "Pod", "metadata": {"name": {"x": 1}, "namespace": "prod"}, "spec": {"containers": [{"name": "app", "image": "x"}]}}
    assert sa_audit([workload], required=["web"])["state"] == "SUCCESS"


def test_secret_exposure_non_string_mount_names_are_not_a_crash():
    workload = pod(containers=[{"name": "app", "image": "x", "volumeMounts": [{"name": ["x"], "mountPath": "/m", "subPath": "s"}]}])
    assert secrets([workload])["state"] == "SUCCESS"


@pytest.mark.parametrize("digest, valid", [
    ("sha256:" + "a" * 64, True), ("sha256:" + "a" * 63, False), ("sha256:" + "a" * 65, False), ("sha256:" + "a" * 32, False),
    ("sha512:" + "b" * 128, True), ("sha512:" + "b" * 64, False), ("sha384:" + "c" * 96, True), ("sha256:" + "A" * 64, False),
    ("md5:" + "a" * 32, False), ("sha256:" + "g" * 64, False)])
def test_image_digest_must_be_a_complete_registered_digest(digest, valid):
    payload = {"workloads": [pod(containers=[{"name": "app", "image": f"ghcr.io/acme/app@{digest}"}])], "allowed_registries": ["ghcr.io/acme"],
               "require_digest": True, "require_signature": False}
    result = call("kpol_image_admission_check", payload)
    if valid:
        assert result["state"] == "SUCCESS" and result["finding_count"] == 0
    else:
        assert result["state"] == "INVALID_INPUT" and result["field"] == "workloads"
        assert digest not in json.dumps(result)


def test_image_verified_digest_must_be_complete_but_case_is_ignored():
    invalid("kpol_image_admission_check", {"workloads": [pod()], "allowed_registries": ["ghcr.io"], "require_digest": False, "require_signature": True,
                                           "verified_digests": ["sha256:" + "a" * 32]}, "verified_digests")
    shouted = "sha256:" + "AB" * 32
    result = images([f"ghcr.io/acme/a@{DIGEST}"], signature=True, verified=[shouted])
    assert result["finding_count"] == 0


@pytest.mark.parametrize("ref", [
    "https://user:hunter2@registry.example/app:1", "user:hunter2@registry.example/app:1", "registry.example/app@hunter2-" + "x" * 40,
    "registry.example/app:ghp_ token", "registry.example/app:" + "x" * 129, "UPPER.example/App:1", "registry.example//app"])
def test_image_malformed_reference_is_refused_without_echoing_it(ref):
    payload = {"workloads": [pod(containers=[{"name": "app", "image": ref}])], "allowed_registries": ["registry.example"],
               "require_digest": False, "require_signature": False}
    result = call("kpol_image_admission_check", payload)
    assert result["state"] == "INVALID_INPUT" and result["field"] == "workloads"
    assert "hunter2" not in json.dumps(result) and ref not in json.dumps(result)


def test_image_docker_hub_normalisation_and_uppercase_registry_hosts():
    one = images(["docker.io/nginx:1", "nginx:1", "index.docker.io/library/nginx:1", "library/nginx:1"], allowed=("docker.io/library",))
    assert "image.registry-not-allowed" not in rule_ids(one)
    upper = images(["Registry.Example/app:1"], allowed=("registry.example",))
    assert "image.registry-not-allowed" not in rule_ids(upper)


def test_image_allowed_registry_with_a_scheme_is_refused_without_echo():
    result = call("kpol_image_admission_check", {"workloads": [pod()], "allowed_registries": ["https://user:hunter2@ghcr.io/acme"],
                                                  "require_digest": False, "require_signature": False})
    assert result["state"] == "INVALID_INPUT" and result["field"] == "allowed_registries" and "hunter2" not in json.dumps(result)


def test_audit_probe_group_wildcard_matches_like_the_apiserver_checker():
    # k8s.io/apiserver audit policy checker: `gr.Group == apiGroup || gr.Group == "*"`
    policy = audit_policy(arule("Metadata", "*", ["secrets", "serviceaccounts/token", "pods", "pods/exec", "pods/attach", "pods/portforward", "namespaces"]),
                          arule("None", "authentication.k8s.io", ["tokenreviews"]), arule("RequestResponse", "*", ["roles", "rolebindings", "clusterroles",
                                                                                                                       "clusterrolebindings"]),
                          arule("Request", "*", ["validatingwebhookconfigurations", "mutatingwebhookconfigurations", "validatingadmissionpolicies",
                                                  "validatingadmissionpolicybindings"]), {"level": "Metadata"})
    assert audit(policy)["failed_probes"] == 0


def test_audit_secret_reads_and_token_creation_may_be_logged_at_request():
    # Request logs the request body only: nothing secret in a get/list/watch or a TokenRequest; RequestResponse logs the response
    request_level = audit(audit_policy(arule("Request", "", ["secrets"], verbs=["get", "list", "watch"]), arule("Metadata", "", ["secrets"]),
                                       arule("Request", "", ["serviceaccounts/token"]), *GOOD_AUDIT["rules"]))
    assert not [f for f in request_level["findings"] if f["id"].startswith("audit.secrets.") or f["id"].startswith("audit.service-account-tokens.")]
    writes = audit(audit_policy(arule("Request", "", ["secrets"], verbs=["create", "update", "patch"]), *GOOD_AUDIT["rules"]))
    assert "audit.secrets.over-audited" in rule_ids(writes)
    full = audit(audit_policy(arule("RequestResponse", "", ["secrets"], verbs=["get"]), arule("RequestResponse", "", ["serviceaccounts/token"]),
                              *GOOD_AUDIT["rules"]))
    assert {"audit.secrets.over-audited", "audit.service-account-tokens.over-audited"} <= rule_ids(full)


def test_audit_non_string_level_is_invalid_input_not_a_crash():
    for level in ({}, [[]], 1):
        invalid("kpol_audit_policy_check", {"policy": audit_policy({"level": level})}, "policy")


def test_tenant_unhashable_enforce_label_is_a_finding_not_a_crash():
    manifests = isolated_tenant()
    manifests[0] = namespace("team-a", **{**GOOD_LABELS, "pod-security.kubernetes.io/enforce": ["restricted"]})
    result = tenants(manifests)
    assert "tenant.psa-enforce-invalid" in rule_ids(result)


def test_exception_register_extreme_dates_are_invalid_input_not_a_crash():
    invalid("kpol_exception_register_check", {"exceptions": [exc(granted_at="0001-01-01T00:00:00+14:00", expires_at="9999-12-31T23:59:59-12:00")],
                                              "now": NOW, "warn_days": 1, "max_duration_days": 3650, "review_interval_days": 3650}, "exceptions")
    invalid("kpol_exception_register_check", {"exceptions": [exc()], "now": "9999-12-31T23:59:59-12:00", "warn_days": 1,
                                              "max_duration_days": 3650, "review_interval_days": 3650}, "now")


def test_exception_register_boundaries_to_the_second():
    base = dict(granted_at="2026-10-01T00:00:00Z", last_reviewed_at="2026-10-01T00:00:00Z")
    expired = register(exc(expires_at="2026-10-20T00:00:00Z", **base))
    assert expired["expired_count"] == 1 and expired["active_count"] == 0
    live = register(exc(expires_at="2026-10-20T00:00:01Z", **base))
    assert (live["expired_count"], live["expiring_soon_count"]) == (0, 1)
    edge = register(exc(expires_at="2026-11-03T00:00:00Z", **base))  # exactly warn_days (14) away
    assert edge["expiring_soon_count"] == 1
    beyond = register(exc(expires_at="2026-11-03T00:00:01Z", **base))
    assert beyond["expiring_soon_count"] == 0
    lifetime = register(exc(granted_at="2026-08-01T00:00:00Z", expires_at="2026-10-30T00:00:00Z"))  # exactly 90 days
    assert "exception.too-long" not in rule_ids(lifetime)
    longer = register(exc(granted_at="2026-08-01T00:00:00Z", expires_at="2026-10-30T00:00:01Z"))
    assert "exception.too-long" in rule_ids(longer)


# --- secret safety: a canary placed where a real secret would sit never reaches any result ---------------------------------

CANARIES = ["AKIAIOSFODNN7EXAMPLE", "ghp_" + "Q" * 36, "0x" + "ab" * 32, "abandon ability able about above absent absorb abstract absurd abuse access accident",
            "https://admin:hunter2@vault.internal.example/v1/secret", "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", "postgres://app:s3cr3t-pw@db:5432/prod"]


@pytest.mark.parametrize("canary", CANARIES)
def test_no_tool_echoes_a_canary_secret_placed_in_a_free_text_or_secret_bearing_field(canary):
    secret_pod = pod(containers=[{
        "name": "app", "image": "ghcr.io/acme/app:1", "command": ["run", canary], "args": [canary],
        "env": [{"name": "TOKEN", "value": canary}, {"name": "FROM", "valueFrom": {"secretKeyRef": {"name": "creds", "key": "k"}}}],
        "securityContext": {"seLinuxOptions": {"type": canary}, "procMount": canary, "seccompProfile": {"type": canary}, "capabilities": {"add": [canary]}},
        "ports": [{"containerPort": 1, "hostPort": canary}]}],
        securityContext={"sysctls": [{"name": canary, "value": canary}]},
        volumes=[{"name": "v", "secret": {"secretName": "creds", "defaultMode": 0o644}}, {"name": "c", "configMap": {"name": canary}}])
    secret_pod["metadata"]["annotations"] = {"note": canary, "container.apparmor.security.beta.kubernetes.io/app": canary}
    secret_pod["metadata"]["labels"] = {"token": canary}
    token_secret = {"kind": "Secret", "type": "kubernetes.io/service-account-token", "metadata": {"name": "tok", "namespace": "prod"},
                    "data": {"token": canary}, "stringData": {"token": canary}}
    results = [
        call("kpol_pss_check", {"workloads": [secret_pod], "level": "restricted"}),
        call("kpol_secret_exposure_check", {"workloads": [secret_pod], "encryption_config": {
            "kind": "EncryptionConfiguration", "resources": [{"resources": ["secrets"], "providers": [
                {"aescbc": {"keys": [{"name": "k1", "secret": canary}]}}, {"identity": {}}]}]}}),
        call("kpol_secret_exposure_check", {"workloads": [secret_pod], "encryption_config": {
            "kind": "EncryptionConfiguration", "resources": [{"resources": ["secrets"], "providers": [{canary: {}}]}]}}),
        call("kpol_sa_token_audit", {"workloads": [secret_pod], "service_accounts": [token_secret], "token_required": [canary]}),
        call("kpol_image_admission_check", {"workloads": [secret_pod], "allowed_registries": ["ghcr.io/acme"], "require_digest": True,
                                             "require_signature": True, "verified_digests": ["sha256:" + "a" * 64]}),
        call("kpol_image_admission_check", {"workloads": [pod(containers=[{"name": "app", "image": "user:" + canary.replace(" ", "") + "@registry.example/app:1"}])],
                                             "allowed_registries": ["registry.example/" + canary.replace(" ", "")], "require_digest": True,
                                             "require_signature": False}),
        call("kpol_tenant_isolation_check", {"tenant_namespaces": ["prod"], "required_psa_level": "restricted", "manifests": [
            namespace("prod", **{"pod-security.kubernetes.io/enforce": canary}), token_secret, secret_pod]}),
        call("kpol_admission_policy_lint", {"policies": [
            vwc(hook(failurePolicy=canary, sideEffects=canary, timeoutSeconds=canary, clientConfig={"url": canary, "caBundle": canary})),
            kyverno([kyverno_rule(action=canary, exclude={"subjects": [{"kind": "User", "name": canary}]})], validationFailureAction=canary),
            constraint(canary), vap(), vap_binding([canary], paramRef={"name": canary})]}),
        call("kpol_runtime_rule_lint", {"rules": [frule(priority=canary, output=canary, desc=canary, condition=f"proc.cmdline contains \"{canary}\""),
                                                  {"macro": "m", "condition": f"proc.env contains '{canary}'"}]}),
        call("kpol_rbac_risk_scan", {"roles": [role("r", [rule(["*"], ["secrets"], names=[canary])])],
                                     "bindings": [binding("b", "r", [{"kind": "User", "name": "alice", "annotations": canary}])]}),
        call("kpol_cis_score", {"results": [rec(canary)], "min_score_pct": 0}),
        call("kpol_audit_policy_check", {"policy": audit_policy(arule("Metadata", "", [canary], users=[canary], verbs=[canary]), omitManagedFields=True)}),
        call("kpol_exception_register_check", {"exceptions": [exc(granted_at=canary)], "now": canary, "warn_days": 1, "max_duration_days": 1, "review_interval_days": 1}),
    ]
    for result in results:
        assert canary not in json.dumps(result), result


def test_rbac_bootstrap_label_on_a_system_name_or_a_cluster_admin_binding_suppresses_nothing():
    labels = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    assert rules_of(scan(bindings=[binding("system:debug-binding", "cluster-admin", [USER], **{"labels": labels})])) == [("rbac.cluster-admin-binding", "blocker")]
    assert rules_of(scan(bindings=[binding("cluster-admin", "cluster-admin", [USER], **{"labels": labels})])) == [("rbac.cluster-admin-binding", "blocker")]
    masters = [{"kind": "Group", "name": "system:masters"}]
    assert rules_of(scan(bindings=[binding("cluster-admin", "cluster-admin", masters, **{"labels": labels})])) == [
        ("rbac.cluster-admin-binding", "blocker"), ("rbac.system-masters", "blocker")]


def test_a_labelled_edit_with_pods_exec_create_is_flagged():
    edit = role("edit", [rule(["create"], ["pods/exec"])])
    edit["metadata"]["labels"] = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    assert rules_of(scan([edit])) == [("rbac.pod-exec", "major")]


def test_a_labelled_edit_with_secrets_list_is_flagged():
    edit = role("edit", [rule(["list"], ["secrets"])])
    edit["metadata"]["labels"] = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    assert rules_of(scan([edit])) == [("rbac.secret-read", "major")]


def test_a_labelled_view_with_star_exec_create_is_flagged():
    view = role("view", [rule(["create"], ["*/exec"])])
    view["metadata"]["labels"] = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    assert rules_of(scan([view])) == [("rbac.pod-exec", "major")]


def test_rbac_resource_names_narrow_admin_equivalence_to_a_wildcard_verb():
    narrow = role("narrow", [rule(["*"], ["*"], ["*"], names=["only-this"])])
    assert rules_of(scan([narrow])) == [("rbac.wildcard-verb", "major")]
    assert rules_of(scan([role("open", [rule(["*"], ["*"], ["*"])])])) == [("rbac.admin-equivalent", "blocker")]


def test_audit_rule_naming_objects_with_no_resource_list_matches_no_probe():
    named = {"level": "None", "resources": [{"group": "", "resourceNames": ["x"]}]}

    def probes(*rules):
        findings = audit(audit_policy(*rules, {"level": "Metadata"}))["findings"]
        return sorted(f["id"].split(".")[1] for f in findings if f["id"].count(".") == 2)

    assert probes(named) == probes()


def test_pss_reports_no_compliance_level_when_no_pod_spec_was_checked():
    service = {"kind": "Service", "apiVersion": "v1", "metadata": {"name": "s", "namespace": "p"}, "spec": {"ports": [{"port": 80}]}}
    result = pss([service])
    assert (result["workload_count"], result["verdict"]) == (0, "pass") and "highest_compliant_level" not in result


def test_pss_empty_workload_list_is_refused_by_the_registry_as_invalid_input_on_workloads():
    result = call("kpol_pss_check", {"workloads": [], "level": "restricted"})
    assert (result["state"], result["field"]) == ("INVALID_INPUT", "workloads")
    assert "highest_compliant_level" not in result


def test_null_failure_policy_and_enforcement_action_read_as_their_api_defaults():
    assert lint(vwc(hook(failurePolicy=None)))["finding_count"] == 0
    assert lint(vwc(hook(failurePolicy="Maybe")))["finding_count"] == 1
    nulled = {"apiVersion": "constraints.gatekeeper.sh/v1beta1", "kind": "K8sRequiredLabels", "metadata": {"name": "c"},
              "spec": {"enforcementAction": None}}
    assert (lint(nulled)["finding_count"], lint(nulled)["enforcing"]) == (0, 1)


def test_string_typed_rule_fields_fail_closed_as_invalid_input_naming_roles():
    # N1: a bare string such as "*" is not a list; it must never read as no grant and pass with 0 findings
    for field in ("apiGroups", "resources", "verbs", "resourceNames"):
        rule_ = {"apiGroups": [""], "resources": ["pods"], "verbs": ["get"]}
        rule_[field] = "*"
        result = call("kpol_rbac_risk_scan", {"roles": [role("root", [rule_])], "bindings": []})
        assert (result["state"], result["field"]) == ("INVALID_INPUT", "roles"), field
        assert "*" not in result["message"]


def test_a_labelled_admin_with_string_wildcards_does_not_pass():
    labels = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    stringly = role("admin", [{"apiGroups": "*", "resources": "*", "verbs": "*"}])
    stringly["metadata"]["labels"] = labels
    result = call("kpol_rbac_risk_scan", {"roles": [stringly], "bindings": []})
    assert (result["state"], result["field"]) == ("INVALID_INPUT", "roles")


def test_a_labelled_view_with_bind_and_escalate_is_flagged_not_hidden():
    labels = {"kubernetes.io/bootstrapping": "rbac-defaults"}
    hidden = role("view", [rule(["bind", "escalate"], ["roles", "clusterroles"], [RBAC_GROUP])])
    hidden["metadata"]["labels"] = labels
    result = scan([hidden])
    assert rules_of(result) == [("rbac.bind", "major"), ("rbac.escalate", "major")]
    assert result["verdict"] == "fail"


def test_kyverno_null_validation_failure_action_matches_the_omitted_default():
    omitted = lint(kyverno([kyverno_rule()]))
    nulled = lint(kyverno([kyverno_rule()], validationFailureAction=None))
    assert rules_of(nulled) == rules_of(omitted) == [("admission.audit-only", "minor")]
    assert (nulled["enforcing"], nulled["audit_only"]) == (omitted["enforcing"], omitted["audit_only"])


def _labelled(name: str, labels: dict, rules: list, aggregation: dict | None = None) -> dict:
    doc = {"kind": "ClusterRole", "apiVersion": "rbac.authorization.k8s.io/v1", "metadata": {"name": name, "labels": labels}, "rules": rules}
    if aggregation is not None:
        doc["aggregationRule"] = aggregation
    return doc


def test_shared_aggregated_roles_are_walked_once_not_once_per_path():
    # 60 roles: two aggregated roles per level, both selecting both roles of the next level, over two identical leaves. The old walk
    # copied the rules once per path (2^29 copies) and timed out; each role is now visited once and its rule is listed once
    depth = 29
    roles = [_labelled(f"agg-{i}-{s}", {"tier": str(i)}, [], {"clusterRoleSelectors": [{"matchLabels": {"tier": str(i + 1)}}]})
             for i in range(depth) for s in "ab"]
    roles += [_labelled(f"leaf-{s}", {"tier": str(depth)}, [rule(["get"], ["pods"])]) for s in "ab"]
    result = access(roles, [binding("top", "agg-0-a", [USER])], USER)
    assert (result["permission_count"], [p["resource_names"] for p in result["permissions"]]) == (1, [[]])


def test_aggregation_graph_over_the_step_budget_is_refused_with_its_reason():
    # 499 roles that all select each other, plus one leaf: resolving them needs far more steps than the budget, so the call is refused
    roles = [_labelled(f"c{i}", {"tier": "x"}, [], {"clusterRoleSelectors": [{"matchLabels": {"tier": "x"}}]}) for i in range(499)]
    roles.append(_labelled("leaf", {"tier": "x"}, [rule(["get"], ["pods"])]))
    refused = call("kpol_rbac_risk_scan", {"roles": roles, "bindings": []})
    assert refused["state"] == "INVALID_INPUT" and refused["field"] == "roles"
    assert "the aggregation graph is too large" in refused["message"]


def test_public_masks_a_password_whose_key_and_value_are_separate_words():  # thread: whole-string spans, not per-word checks
    from swarm.tools import a24_tools

    assert a24_tools._public("password: hunter2") == "password: [REDACTED]"
    assert a24_tools._public("user password: hunter2 end") == "user password: [REDACTED] end"
    assert a24_tools._public("password=hunter2") == "password=[REDACTED]"
    assert "hunter2" not in a24_tools._public("binding subject password: hunter2")


def test_public_keeps_only_a_known_boolean_setting_and_masks_a_boolean_looking_secret():  # thread: exception limited to known settings
    from swarm.tools import a24_tools

    assert a24_tools._public("automountServiceAccountToken: false") == "automountServiceAccountToken: false"
    assert a24_tools._public("automountServiceAccountToken: false and password: hunter2") == (
        "automountServiceAccountToken: false and password: [REDACTED]")
    assert a24_tools._public("password=false") == "password=[REDACTED]"
    assert a24_tools._public("password: true") == "password: [REDACTED]"
    assert a24_tools._public("accessToken: null") == "accessToken: [REDACTED]"


def test_public_masks_a_secret_after_a_long_name_in_bounded_time(monkeypatch):  # thread: the key lookup must not rescan the whole prefix
    from swarm.tools import a24_tools

    reads = []
    real_text, real_spans = a24_tools.redact_text, a24_tools.redact_spans

    def counted(real):
        def wrapper(text, *args, **kwargs):
            reads.append(len(text))
            return real(text, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(a24_tools, "redact_text", counted(real_text))
    monkeypatch.setattr(a24_tools, "redact_spans", counted(real_spans))
    text = "A" * 32_000 + " password=false"
    masked = a24_tools._public(text)
    assert masked == "A" * 32_000 + " password=[REDACTED]"
    # counted work instead of a wall-clock bound: the text gets one detection pass and one span pass, never a pass per position
    assert reads and len(reads) <= 2 and sum(reads) <= 2 * len(text), reads


def test_a_credential_pasted_into_a_role_name_is_masked_in_the_finding_id_and_summary():  # thread: _finding copies manifest names
    token = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    result = scan([role(token, [rule(["*"], ["*"], ("*",))])])
    assert [f["id"] for f in result["findings"]] == ["rbac.admin-equivalent:ClusterRole/[REDACTED]"]
    assert token not in json.dumps(result)
