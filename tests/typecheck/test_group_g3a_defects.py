"""Regression tests for runtime defects fixed while typing the G3a modules (swarm/tools/a23_tools.py).

Three inputs that reach the handler as plain JSON crashed with TypeError or KeyError instead of a finding or INVALID_INPUT:

* a Gateway listener whose ``allowedRoutes.kinds`` is not a list (``5``) crashed the route acceptance check;
* a NetworkPolicy rule whose ``from`` is not a list (``5``) crashed the open-to-any-address check of ``netpol_coverage``;
* a ``netpol_reachability`` source or destination that is not an object, or has no name, crashed the pod lookup.
"""
from __future__ import annotations

from swarm.tools import a23_tools


def _gateway_payload(kinds: object) -> dict[str, object]:
    listener = {"name": "http", "port": 80, "protocol": "HTTP", "allowedRoutes": {"namespaces": {"from": "All"}, "kinds": kinds}}
    return {
        "gateways": [{"metadata": {"name": "gw", "namespace": "infra"}, "spec": {"gatewayClassName": "example", "listeners": [listener]}}],
        "reference_grants": [],
        "routes": [{"metadata": {"name": "web", "namespace": "app"}, "spec": {"parentRefs": [{"name": "gw", "namespace": "infra"}]}}],
    }


def test_gateway_route_check_treats_non_list_kinds_as_excluding_httproute() -> None:
    result = a23_tools.gateway_api_check(_gateway_payload(5))

    assert result["state"] == "SUCCESS"
    assert [f["id"] for f in result["findings"]].count("ROUTE-NOT-ACCEPTED") == 1
    assert result["verdict"] == "fail"


def test_gateway_route_check_still_accepts_a_listener_that_lists_httproute() -> None:
    result = a23_tools.gateway_api_check(_gateway_payload([{"kind": "HTTPRoute"}]))

    assert result["state"] == "SUCCESS"
    assert "ROUTE-NOT-ACCEPTED" not in [f["id"] for f in result["findings"]]


def test_netpol_coverage_rejects_a_non_list_from_as_invalid_input() -> None:
    payload = {
        "network_policies": [{"metadata": {"name": "p", "namespace": "a"},
                              "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": 5}]}}],
        "pods": [{"namespace": "a", "name": "x", "labels": {}}],
    }

    result = a23_tools.netpol_coverage(payload)

    assert result["state"] == "INVALID_INPUT"
    assert result["field"] == "network_policies"


def test_netpol_reachability_names_a_malformed_pod_reference() -> None:
    base = {
        "network_policies": [],
        "pods": [{"namespace": "a", "name": "x", "labels": {}}, {"namespace": "b", "name": "y", "labels": {}}],
        "port": 80,
    }
    destination = {"namespace": "b", "name": "y"}

    not_an_object = a23_tools.netpol_reachability({**base, "source": "x", "destination": destination})
    assert not_an_object["state"] == "INVALID_INPUT"
    assert not_an_object["field"] == "source"

    no_name = a23_tools.netpol_reachability({**base, "source": {"namespace": "a"}, "destination": destination})
    assert no_name["state"] == "INVALID_INPUT"
    assert no_name["field"] == "source"

    well_formed = a23_tools.netpol_reachability({**base, "source": {"namespace": "a", "name": "x"}, "destination": destination})
    assert well_formed["state"] == "SUCCESS"
    assert well_formed["reachable"] is True
