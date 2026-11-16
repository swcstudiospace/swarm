"""Behaviour of the v2.2 A03 tools: arch_nfr_allocation, arch_breaking_readiness, arch_adr_supersession.

Handlers are called directly; the registry test merges the intake fragment into a copy of the registry
(only entries not already merged) so it passes both before and after `scripts/_registry_intake.py --apply`.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from swarm.tool_registry import load_document, validate_registry
from swarm.tools.a03_tools import adr_supersession, breaking_readiness, nfr_allocation

ROOT = Path(__file__).resolve().parent.parent
FRAGMENT = ROOT / "registry" / "intake" / "a03-architect.tools.json"
NEW_TOOLS = ("arch_nfr_allocation", "arch_breaking_readiness", "arch_adr_supersession")


def _nfr(nfr_id, kind="quantitative"):
    return {"id": nfr_id, "kind": kind}


def _bind(nfr_id, ref="API-Orders#/x", kind="clause", **extra):
    return {"nfr_id": nfr_id, "target_kind": kind, "target_ref": ref, **extra}


# ---- arch_nfr_allocation -------------------------------------------------------------------


def test_nfr_allocation_full_coverage_with_clause_and_component_budget():
    result = nfr_allocation({
        "nfrs": [_nfr("NFR-1"), _nfr("NFR-2"), _nfr("NFR-3", "qualitative")],
        "bindings": [
            _bind("NFR-1", limit=300, unit="ms"),
            _bind("NFR-2", "orders-svc", "component_budget", limit=99.5, unit="pct"),
            _bind("NFR-3", "API-Orders#/security/oidc"),
        ],
    })
    assert result["state"] == "SUCCESS"
    assert result["allocated"] == ["NFR-1", "NFR-2", "NFR-3"]
    assert result["coverage_pct"] == 100.0 and result["fully_allocated"] is True
    assert result["unallocated"] == [] and result["unmeasurable"] == [] and result["unknown_nfr_ids"] == []


def test_nfr_allocation_empty_bindings_fails_closed():
    result = nfr_allocation({"nfrs": [_nfr("NFR-1"), _nfr("NFR-2")], "bindings": []})
    assert result["unallocated"] == ["NFR-1", "NFR-2"]
    assert result["coverage_pct"] == 0.0 and result["fully_allocated"] is False


def test_nfr_allocation_quantitative_without_limit_is_unmeasurable_not_allocated():
    result = nfr_allocation({"nfrs": [_nfr("NFR-1")], "bindings": [_bind("NFR-1")]})
    assert result["unmeasurable"] == ["NFR-1"] and result["allocated"] == []
    assert result["fully_allocated"] is False


def test_nfr_allocation_null_limit_is_unmeasurable_not_allocated():
    result = nfr_allocation({"nfrs": [_nfr("NFR-1")], "bindings": [_bind("NFR-1", limit=None)]})
    assert result["unmeasurable"] == ["NFR-1"] and result["allocated"] == []
    assert result["fully_allocated"] is False


def test_nfr_allocation_zero_limit_counts_as_numeric():
    result = nfr_allocation({"nfrs": [_nfr("NFR-1")], "bindings": [_bind("NFR-1", limit=0, unit="errors")]})
    assert result["allocated"] == ["NFR-1"] and result["fully_allocated"] is True


def test_nfr_allocation_any_one_measurable_binding_is_enough():
    result = nfr_allocation({
        "nfrs": [_nfr("NFR-1")],
        "bindings": [_bind("NFR-1", "clause-a"), _bind("NFR-1", "svc", "component_budget", limit=5)],
    })
    assert result["allocated"] == ["NFR-1"]


def test_nfr_allocation_unknown_binding_blocks_full_allocation():
    result = nfr_allocation({
        "nfrs": [_nfr("NFR-1")],
        "bindings": [_bind("NFR-1", limit=1), _bind("NFR-9", "z", limit=1), _bind("NFR-8", "y", limit=1)],
    })
    assert result["unknown_nfr_ids"] == ["NFR-8", "NFR-9"]
    assert result["coverage_pct"] == 100.0 and result["fully_allocated"] is False


def test_nfr_allocation_partial_coverage_percentage_is_rounded():
    result = nfr_allocation({
        "nfrs": [_nfr("a"), _nfr("b"), _nfr("c")],
        "bindings": [_bind("a", limit=1)],
    })
    assert result["coverage_pct"] == 33.3
    assert result["unallocated"] == ["b", "c"]


def test_nfr_allocation_duplicate_binding_is_invalid_input():
    result = nfr_allocation({
        "nfrs": [_nfr("NFR-1")],
        "bindings": [_bind("NFR-1", limit=1), _bind("NFR-1", limit=2)],
    })
    assert result["state"] == "INVALID_INPUT" and result["field"] == "bindings"


def test_nfr_allocation_duplicate_or_blank_ids_and_refs_are_invalid():
    assert nfr_allocation({"nfrs": [_nfr("a"), _nfr("a")], "bindings": []})["field"] == "nfrs"
    assert nfr_allocation({"nfrs": [_nfr("  ")], "bindings": []})["field"] == "nfrs"
    blank_ref = nfr_allocation({"nfrs": [_nfr("a")], "bindings": [_bind("a", "   ", limit=1)]})
    assert blank_ref["state"] == "INVALID_INPUT" and blank_ref["field"] == "bindings"


def test_nfr_allocation_probes_do_not_reach_the_work():
    base = {"nfrs": [_nfr("a")], "bindings": []}
    assert nfr_allocation({**base, "probe": "partial"})["state"] == "PARTIAL_SUCCESS"
    assert nfr_allocation({**base, "probe": "sleep"})["state"] == "TIMEOUT"
    with pytest.raises(OSError):
        nfr_allocation({**base, "probe": "dependency"})


# ---- arch_breaking_readiness ---------------------------------------------------------------


def _change(**over):
    base = {
        "breaking_change": False,
        "external_facing": False,
        "consumers": ["A05", "A06"],
        "acks": [],
        "head_version": "1.4.0",
        "migration_plan_present": False,
    }
    return {**base, **over}


def test_readiness_internal_non_breaking_is_l2_and_ready():
    result = breaking_readiness(_change())
    assert result == {
        "state": "SUCCESS", "autonomy_level": "L2", "missing_acks": [], "missing": [], "ready": True, "needs": [],
    }


def test_readiness_breaking_without_acks_or_plan_lists_both():
    result = breaking_readiness(_change(breaking_change=True, head_version="2.0.0"))
    assert result["autonomy_level"] == "L3" and result["ready"] is False
    assert result["missing_acks"] == ["A05", "A06"]
    assert result["missing"] == ["migration_plan", "consumer_acks"]
    assert result["needs"][-1] == "human-approval: breaking_contract"
    assert "consumer ack of 2.0.0: A05" in result["needs"]


def test_readiness_breaking_fully_prepared_still_needs_human_approval():
    result = breaking_readiness(_change(
        breaking_change=True, head_version="2.0.0", migration_plan_present=True,
        acks=[{"consumer": "A05", "version": "2.0.0"}, {"consumer": "A06", "version": "2.0.0"}],
    ))
    assert result["missing_acks"] == [] and result["missing"] == []
    assert result["autonomy_level"] == "L3"
    assert result["ready"] is False and result["needs"] == ["human-approval: breaking_contract"]


def test_readiness_ack_for_an_old_version_is_rejected():
    result = breaking_readiness(_change(
        breaking_change=True, head_version="2.0.0", migration_plan_present=True,
        acks=[{"consumer": "A05", "version": "2.0.0"}, {"consumer": "A06", "version": "1.4.0"}],
    ))
    assert result["missing_acks"] == ["A06"]


def test_readiness_external_facing_non_breaking_is_l3_without_acks():
    result = breaking_readiness(_change(external_facing=True))
    assert result["autonomy_level"] == "L3" and result["missing_acks"] == [] and result["missing"] == []
    assert result["ready"] is False and result["needs"] == ["human-approval: breaking_contract"]


def test_readiness_non_breaking_ignores_missing_acks():
    result = breaking_readiness(_change(consumers=["A05"], acks=[]))
    assert result["missing_acks"] == [] and result["ready"] is True


def test_readiness_breaking_without_consumers_needs_only_plan_and_approval():
    result = breaking_readiness(_change(breaking_change=True, consumers=[], head_version="3.0.0"))
    assert result["missing"] == ["migration_plan"] and result["missing_acks"] == []


def test_readiness_malformed_input_names_the_field():
    assert breaking_readiness(_change(head_version="2.0"))["field"] == "head_version"
    assert breaking_readiness(_change(head_version="2.0.0-rc1"))["field"] == "head_version"
    assert breaking_readiness(_change(consumers=["A05", "A05"]))["field"] == "consumers"
    assert breaking_readiness(_change(consumers=[" "]))["field"] == "consumers"


def test_readiness_extra_acks_for_unlisted_consumers_are_ignored():
    result = breaking_readiness(_change(
        breaking_change=True, head_version="2.0.0", migration_plan_present=True,
        consumers=["A05"], acks=[{"consumer": "A05", "version": "2.0.0"}, {"consumer": "A09", "version": "2.0.0"}],
    ))
    assert result["missing_acks"] == []


# ---- arch_adr_supersession -----------------------------------------------------------------


def _adr(adr_id, status, supersedes=None):
    item = {"adr_id": adr_id, "status": status}
    if supersedes:
        item["supersedes"] = supersedes
    return item


def test_supersession_clean_chain_has_one_head_and_a_chain():
    result = adr_supersession({"adrs": [
        _adr("ADR-0001", "superseded"), _adr("ADR-0002", "superseded", "ADR-0001"),
        _adr("ADR-0003", "accepted", "ADR-0002"), _adr("ADR-0004", "accepted"),
    ]})
    assert result["valid"] is True
    assert result["heads"] == ["ADR-0003", "ADR-0004"]
    assert result["chains"] == ["ADR-0003 -> ADR-0002 -> ADR-0001"]
    assert result["dangling"] == [] and result["cycles"] == [] and result["forks"] == []


def test_supersession_dangling_target_is_reported():
    result = adr_supersession({"adrs": [_adr("ADR-0002", "accepted", "ADR-0001")]})
    assert result["dangling"] == ["ADR-0002 -> ADR-0001"] and result["valid"] is False


def test_supersession_superseded_without_successor_is_a_mismatch():
    result = adr_supersession({"adrs": [_adr("ADR-0001", "superseded")]})
    assert result["status_mismatches"] == ["ADR-0001 is superseded but no ADR supersedes it"]
    assert result["valid"] is False


def test_supersession_old_adr_still_accepted_is_a_mismatch():
    result = adr_supersession({"adrs": [_adr("ADR-0001", "accepted"), _adr("ADR-0002", "accepted", "ADR-0001")]})
    assert result["status_mismatches"] == ["ADR-0001 is accepted although ADR-0002 (accepted) supersedes it"]


def test_supersession_proposed_successor_leaves_old_adr_live():
    ok = adr_supersession({"adrs": [_adr("ADR-0001", "accepted"), _adr("ADR-0002", "proposed", "ADR-0001")]})
    assert ok["valid"] is True
    early = adr_supersession({"adrs": [_adr("ADR-0001", "superseded"), _adr("ADR-0002", "proposed", "ADR-0001")]})
    assert early["status_mismatches"] == ["ADR-0001 is superseded by ADR-0002, which is only proposed"]


def test_supersession_rejected_successor_is_a_mismatch():
    result = adr_supersession({"adrs": [_adr("ADR-0001", "accepted"), _adr("ADR-0002", "rejected", "ADR-0001")]})
    assert result["status_mismatches"] == ["ADR-0001 is superseded by ADR-0002, which is rejected"]


def test_supersession_two_successors_is_a_fork():
    result = adr_supersession({"adrs": [
        _adr("ADR-0001", "superseded"), _adr("ADR-0002", "accepted", "ADR-0001"),
        _adr("ADR-0003", "accepted", "ADR-0001"),
    ]})
    assert result["forks"] == ["ADR-0001 <- ADR-0002, ADR-0003"] and result["valid"] is False
    assert result["heads"] == ["ADR-0002", "ADR-0003"]


def test_supersession_cycle_and_self_link_are_found_once():
    loop = adr_supersession({"adrs": [
        _adr("A", "accepted", "B"), _adr("B", "accepted", "C"), _adr("C", "accepted", "A"), _adr("D", "accepted"),
    ]})
    assert loop["cycles"] == ["A, B, C"] and loop["valid"] is False and loop["heads"] == ["D"]
    selfish = adr_supersession({"adrs": [_adr("A", "accepted", "A")]})
    assert selfish["cycles"] == ["A"] and selfish["valid"] is False


def test_supersession_duplicate_or_blank_ids_are_invalid_input():
    dup = adr_supersession({"adrs": [_adr("A", "accepted"), _adr("A", "accepted")]})
    assert dup["state"] == "INVALID_INPUT" and dup["field"] == "adrs"
    assert adr_supersession({"adrs": [_adr(" ", "accepted")]})["field"] == "adrs"


def test_supersession_standalone_set_is_valid_without_chains():
    result = adr_supersession({"adrs": [_adr("ADR-0001", "accepted"), _adr("ADR-0002", "deprecated")]})
    assert result["valid"] is True and result["chains"] == [] and result["heads"] == ["ADR-0001", "ADR-0002"]


# ---- registry fragment ---------------------------------------------------------------------


def test_fragment_entries_validate_when_added_to_a_copy_of_the_registry(tmp_path):
    fragment = json.loads(FRAGMENT.read_text(encoding="utf-8")) if FRAGMENT.exists() else []
    document = copy.deepcopy(load_document())
    lane = next(agent for agent in document["agents"] if agent["id"] == "A03")
    present = {tool["name"] for tool in lane["registered_tools"]}
    if FRAGMENT.exists():
        assert [entry["name"] for entry in fragment] == list(NEW_TOOLS)
    for entry in fragment:
        if entry["name"] not in present:
            lane["registered_tools"].append(copy.deepcopy(entry))
    merged = tmp_path / "agents.json"
    merged.write_text(json.dumps(document), encoding="utf-8")
    assert validate_registry(merged) == []
    names = {tool["name"] for tool in lane["registered_tools"]}
    assert set(NEW_TOOLS) <= names or not FRAGMENT.exists()
