"""Regressions for review findings D3-D7: bad or hostile inputs return INVALID_INPUT instead of raising or running away.

D3 A07 huge integer percentage; D4 A23 malformed later netpol rule is not skipped; D5 A24 cyclic macro set is bounded;
D6 A26 cyclic base graph is bounded; D7 A24 deep macro chain does not escape as RecursionError.
"""
from __future__ import annotations

from swarm.tools.a07_tools import data_explain_regression_check
from swarm.tools.a23_tools import netpol_coverage, netpol_reachability
from swarm.tools.a24_tools import runtime_rule_lint
from swarm.tools.a26_tools import upgrade_safety

# --- D3: A07 exact percentage -------------------------------------------------------------------------------------


def test_huge_integer_percentage_is_invalid_input_not_an_exception() -> None:
    result = data_explain_regression_check({"queries": [{"id": "q", "baseline": 1, "shadow": 10**400}]})

    assert result == {
        "state": "INVALID_INPUT",
        "field": "queries[0].shadow",
        "message": "shadow is out of range for a percentage change",
    }


# --- D4: A23 netpol rules are validated eagerly, whatever their order -----------------------------------------------

WEB = {"namespace": "shop", "name": "web-0", "labels": {"app": "web"}}
API = {"namespace": "shop", "name": "api-0", "labels": {"app": "api"}, "ip": "10.0.0.6",
       "ports": [{"name": "http", "containerPort": 8080}]}
PODS = [WEB, API]


def _ref(pod: dict) -> dict:
    return {"namespace": pod["namespace"], "name": pod["name"]}


def _policy(ingress: list) -> dict:
    return {"metadata": {"name": "allow", "namespace": "shop"}, "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": ingress}}


def test_reachability_rejects_malformed_later_rule_even_after_an_earlier_rule_matches() -> None:
    first_matches = {"from": [{"podSelector": {"matchLabels": {"app": "web"}}}]}
    result = netpol_reachability({
        "network_policies": [_policy([first_matches, {"from": 5}])],
        "pods": PODS,
        "source": _ref(WEB),
        "destination": _ref(API),
        "port": 8080,
    })

    assert result["state"] == "INVALID_INPUT" and result["field"] == "network_policies"


def test_coverage_rejects_malformed_later_rule_even_after_a_world_open_rule() -> None:
    world_open = {"from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}
    result = netpol_coverage({"network_policies": [_policy([world_open, {"from": 5}])], "pods": PODS})

    assert result["state"] == "INVALID_INPUT" and result["field"] == "network_policies"


# --- D5 and D7: A24 macro expansion is bounded and a deep chain is an input error ----------------------------------


def _falco_rule(condition: str) -> dict:
    return {"rule": "r", "desc": "d", "condition": condition, "output": "o", "priority": "WARNING"}


def test_cyclic_macro_set_of_forty_is_invalid_input() -> None:
    macros = [{"macro": f"m{i}", "condition": f"m{(i - 1) % 40} or m{(i - 1) % 40}"} for i in range(40)]
    result = runtime_rule_lint({"rules": [*macros, _falco_rule("m39")]})

    assert result["state"] == "INVALID_INPUT" and result["field"] == "rules"


def test_short_acyclic_macro_chain_keeps_its_normal_result() -> None:
    macros = [{"macro": "m0", "condition": "evt.type = execve"},
              *[{"macro": f"m{i}", "condition": f"m{i - 1} and proc.name = p{i}"} for i in range(1, 6)]]
    result = runtime_rule_lint({"rules": [*macros, _falco_rule("m5")]})

    assert (result["state"], result["finding_count"], result["verdict"]) == ("SUCCESS", 0, "pass")


def test_linear_macro_chain_of_3000_is_invalid_input_not_a_recursion_error() -> None:
    macros = [{"macro": "m0", "condition": "evt.type = execve"},
              *[{"macro": f"m{i}", "condition": f"m{i - 1} and proc.name = bash"} for i in range(1, 3000)]]
    result = runtime_rule_lint({"rules": [*macros, _falco_rule("m2999")]})

    assert result["state"] == "INVALID_INPUT" and result["field"] == "rules"


# --- D6: A26 cyclic base-contract graph is bounded ------------------------------------------------------------------


def test_cyclic_base_graph_of_forty_contracts_is_invalid_input() -> None:
    contracts = [f"contract C{i} is C{(i + 1) % 40}, C{(i + 2) % 40} {{}}" for i in range(40)]
    source = "// SPDX-License-Identifier: MIT\npragma solidity 0.8.28;\n" + "\n".join(contracts) + "\n"
    result = upgrade_safety({"source": source, "pattern": "uups"})

    assert result["state"] == "INVALID_INPUT" and result["field"] == "source"
