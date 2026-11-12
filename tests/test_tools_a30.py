"""Behaviour tests for the A30 Web3 Custody & Compliance Engineer tools (custody_*: multisig, timelock, ceremony, shards,
signer policy, wallet tiers, key rotation, tx policy, sanctions, Travel Rule, audit trail, recovery drills, secret scan).

Every assertion is on output values for specific inputs, boundaries, INVALID_INPUT fields and callers. The registry
document is the live registry plus this lane's intake fragment while it is unmerged, so the file passes before and
after `scripts/_registry_intake.py --apply` merges and deletes the fragment.

tests/conftest.py evicts swarm* modules between tests: modules are resolved through importlib at call time.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import importlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT, SLUG, PREFIX = "A30", "a30-custody-compliance", "custody_"
INTAKE = ROOT / "registry" / "intake"
TOOL_NAMES = [
    "custody_multisig_policy", "custody_timelock_check", "custody_ceremony_checklist", "custody_shard_distribution",
    "custody_signer_policy_lint", "custody_wallet_tier_caps", "custody_key_rotation", "custody_tx_policy_eval",
    "custody_sanctions_screening", "custody_travel_rule", "custody_audit_trail", "custody_recovery_drill", "custody_secret_scan",
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


# --- helpers --------------------------------------------------------------------------------------------------------


def mod():
    return importlib.import_module("swarm.tools.a30_tools")


def sev(result: dict) -> dict:
    """finding id -> severity"""
    return {f["id"]: f["severity"] for f in result["findings"]}


def b58check(payload: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    raw = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    number, out = int.from_bytes(raw, "big"), ""
    while number:
        number, digit = divmod(number, 58)
        out = alphabet[digit] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + out


KEY32 = bytes(range(1, 33))  # a throwaway byte pattern, not a real key


def wif(version: int = 0x80, compressed: bool = True) -> str:
    return b58check(bytes([version]) + KEY32 + (b"\x01" if compressed else b""))


def xprv(version: str = "0488ade4", key_prefix: bytes = b"\x00") -> str:
    return b58check(bytes.fromhex(version) + b"\x00" + b"\x00" * 4 + b"\x00" * 4 + b"\x11" * 32 + key_prefix + KEY32)


ADDR_A = "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"  # EIP-55 specification test vectors
ADDR_B = "0xfB6916095ca1df60bB79Ce92cE3Ea74c37c5d359"
ADDR_C = "0xdbF03B407c01E7cD3CBea99509d93f8DDDC8C6FB"
ADDR_D = "0xD1220A0cf47c7B9Be7A2E6BA89F429762e7b9aDb"


# --- reference tables: Keccak-256, EIP-55, selectors -----------------------------------------------------------------


def test_keccak256_matches_published_vectors_across_the_sponge_block_boundary():
    keccak = mod()._keccak256
    assert keccak(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    assert keccak(b"transfer(address,uint256)")[:4].hex() == "a9059cbb"
    # rate is 136 bytes: 135 leaves one padding byte (0x81), 136 forces a whole extra block (reference: pycryptodome keccak-256)
    assert keccak(b"a" * 135).hex() == "34367dc248bbd832f4e3e69dfaac2f92638bd0bbd18f2912ba4ef454919cf446"
    assert keccak(b"a" * 136).hex() == "a6c4d403279fe3e0af03729caada8374b5ca54d8065329a3ebcaeb4b60aa386e"
    assert keccak(b"a" * 137).hex() == "d869f639c7046b4929fc92a4d988a8b22c55fbadb802c0c66ebcd484f1915f39"
    assert keccak(b"a" * 272).hex() == "cf7fcd4f705ee749930d19ca84561a9bf62516bd90a471545fa2f49fdc7e63c8"


@pytest.mark.parametrize("address", [ADDR_A, ADDR_B, ADDR_C, ADDR_D])
def test_eip55_checksum_reproduces_the_specification_vectors(address):
    assert mod()._eip55(address[2:].lower()) == address[2:]


@pytest.mark.parametrize(("selector", "signature"), [
    ("095ea7b3", "approve(address,uint256)"),
    ("39509351", "increaseAllowance(address,uint256)"),
    ("d505accf", "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)"),
    ("a22cb465", "setApprovalForAll(address,bool)"),
    ("f2fde38b", "transferOwnership(address)"),
    ("715018a6", "renounceOwnership()"),
    ("3659cfe6", "upgradeTo(address)"),
    ("4f1ef286", "upgradeToAndCall(address,bytes)"),
    ("a9059cbb", "transfer(address,uint256)"),
    ("23b872dd", "transferFrom(address,address,uint256)"),
])
def test_selector_table_rows_are_pinned(selector, signature):
    assert mod()._SIGNATURE_BY_SELECTOR[selector] == signature
    assert (selector in mod()._RISKY_SELECTORS) == (signature not in ("transfer(address,uint256)", "transferFrom(address,address,uint256)"))


# --- custody_multisig_policy ------------------------------------------------------------------------------------------


def signers(n=3, entities=None, regions=None, devices=None):
    entities = entities or [f"entity{i}" for i in range(n)]
    regions = regions or [f"R{i}" for i in range(n)]
    devices = devices or ["hardware"] * n
    return [{"signer_id": f"s{i}", "entity": entities[i], "region": regions[i], "device": devices[i]} for i in range(n)]


def multisig(threshold=2, **kw):
    return {"threshold": threshold, "signers": kw.pop("signers", signers()), **kw}


def test_multisig_sound_two_of_three():
    result = ok("custody_multisig_policy", multisig())
    assert result == {
        "state": "SUCCESS", "signer_count": 3, "threshold": 2, "loss_tolerance": 1, "compromise_tolerance": 1,
        "distinct_entities": 3, "distinct_regions": 3, "min_entities_for_quorum": 2, "hot_signers": 0, "tiers_checked": 0,
        "verdict": "pass", "findings": [],
    }


def test_multisig_threshold_shapes():
    one = ok("custody_multisig_policy", multisig(1))
    assert sev(one)["threshold-one"] == "blocker" and sev(one)["entity-controls-quorum"] == "blocker"
    assert sev(one)["no-majority-quorum"] == "minor" and one["verdict"] == "fail"
    full = ok("custody_multisig_policy", multisig(3))
    assert sev(full) == {"no-loss-tolerance": "major"} and full["loss_tolerance"] == 0 and full["verdict"] == "fail"
    solo = ok("custody_multisig_policy", multisig(1, signers=signers(1)))
    assert sev(solo) == {"single-signer": "blocker"}


@pytest.mark.parametrize(("n", "m", "flagged"), [(4, 2, True), (5, 3, False), (6, 3, True), (6, 4, False), (5, 2, True)])
def test_multisig_majority_quorum_boundary_is_two_m_at_most_n(n, m, flagged):
    result = ok("custody_multisig_policy", multisig(m, signers=signers(n)))
    assert ("no-majority-quorum" in sev(result)) is flagged


def test_multisig_entity_quorum_uses_the_fewest_entities_and_ignores_case_and_padding():
    heavy = ok("custody_multisig_policy", multisig(3, signers=signers(5, entities=["Acme", "acme ", "ACME", "b", "c"])))
    assert sev(heavy) == {"entity-controls-quorum": "blocker"} and heavy["min_entities_for_quorum"] == 1 and heavy["distinct_entities"] == 3
    spread = ok("custody_multisig_policy", multisig(3, signers=signers(5, entities=["a", "a", "b", "b", "c"])))
    assert spread["findings"] == [] and spread["min_entities_for_quorum"] == 2


def test_multisig_single_region_and_hot_signer_boundary():
    region = ok("custody_multisig_policy", multisig(signers=signers(3, regions=["DE", "de", "DE"])))
    assert sev(region) == {"single-region": "minor"} and region["verdict"] == "warn" and region["distinct_regions"] == 1
    one_hot = ok("custody_multisig_policy", multisig(signers=signers(3, devices=["software", "hardware", "hsm"])))
    assert one_hot["hot_signers"] == 1 and one_hot["findings"] == []
    two_hot = ok("custody_multisig_policy", multisig(signers=signers(3, devices=["software", "mobile", "hardware"])))
    assert sev(two_hot) == {"hot-signers-reach-quorum": "major"} and two_hot["hot_signers"] == 2
    contract = ok("custody_multisig_policy", multisig(signers=signers(3, devices=["contract", "contract", "hsm"])))
    assert contract["hot_signers"] == 0


def test_multisig_tiers():
    clean = ok("custody_multisig_policy", multisig(tiers=[{"min_value_usd": 1000000, "required_signatures": 3}, {"min_value_usd": 0, "required_signatures": 2}]))
    assert clean["tiers_checked"] == 2 and clean["findings"] == []
    assert sev(ok("custody_multisig_policy", multisig(tiers=[{"min_value_usd": 1000, "required_signatures": 2}]))) == {"tier-gap-below-lowest": "major"}
    assert sev(ok("custody_multisig_policy", multisig(tiers=[{"min_value_usd": 0, "required_signatures": 4}]))) == {"tier-unsatisfiable:1": "blocker"}
    down = ok("custody_multisig_policy", multisig(3, signers=signers(4), tiers=[{"min_value_usd": 0, "required_signatures": 3}, {"min_value_usd": 1000, "required_signatures": 2}]))
    assert sev(down) == {"tier-not-monotonic:2": "major", "tier-below-onchain-threshold:2": "minor"}


def test_multisig_rejects_inconsistent_inputs_naming_the_field():
    invalid("custody_multisig_policy", multisig(4), "threshold")
    dup = signers(3)
    dup[1]["signer_id"] = "S0"
    invalid("custody_multisig_policy", multisig(signers=dup), "signers")
    invalid("custody_multisig_policy", multisig(tiers=[{"min_value_usd": 5, "required_signatures": 2}, {"min_value_usd": 5, "required_signatures": 3}]), "tiers")


def test_secret_shaped_labels_are_refused_without_echoing_the_value():
    leaked = signers(3)
    leaked[0]["signer_id"] = wif()
    result = invalid("custody_multisig_policy", multisig(signers=leaked), "signers")
    assert wif() not in json.dumps(result) and "wif-private-key" in result["message"]


# --- custody_timelock_check -------------------------------------------------------------------------------------------


def timelock(**kw):
    base = {"delay_seconds": 172800, "value_at_risk_usd": 5000000,
            "delay_bands": [{"min_value_usd": 0, "min_delay_seconds": 3600}, {"min_value_usd": 1000000, "min_delay_seconds": 172800}],
            "response_seconds": 7200, "canceller_count": 2, "guardian_can_cancel": True, "admin_renounced": True,
            "execution_window_seconds": 1209600}
    return {**base, **kw}


def test_timelock_meeting_the_band_exactly_passes():
    assert ok("custody_timelock_check", timelock()) == {
        "state": "SUCCESS", "required_delay_seconds": 172800, "delay_margin_seconds": 0, "delay_hours": 48.0,
        "verdict": "pass", "findings": [],
    }


def test_timelock_delay_below_required_by_one_second():
    result = ok("custody_timelock_check", timelock(delay_seconds=172799))
    assert sev(result) == {"delay-below-required": "major"} and result["delay_margin_seconds"] == -1 and result["verdict"] == "fail"


def test_timelock_band_selection_is_inclusive_and_takes_the_longest_applicable_delay():
    assert ok("custody_timelock_check", timelock(value_at_risk_usd=1000000))["required_delay_seconds"] == 172800
    assert ok("custody_timelock_check", timelock(value_at_risk_usd=999999.99))["required_delay_seconds"] == 3600
    only_high = timelock(value_at_risk_usd=500, delay_bands=[{"min_value_usd": 1000, "min_delay_seconds": 600}])
    gap = ok("custody_timelock_check", only_high)
    assert gap["required_delay_seconds"] == 0 and sev(gap) == {"band-gap-below-lowest": "major"} and gap["verdict"] == "fail"
    odd = timelock(value_at_risk_usd=5000, delay_bands=[{"min_value_usd": 0, "min_delay_seconds": 86400}, {"min_value_usd": 1000, "min_delay_seconds": 3600}])
    assert ok("custody_timelock_check", odd)["required_delay_seconds"] == 86400


def test_timelock_zero_delay_is_a_blocker_beside_the_derived_findings():
    result = ok("custody_timelock_check", timelock(delay_seconds=0))
    assert sev(result) == {"no-delay": "blocker", "delay-below-required": "major", "delay-shorter-than-response": "major"}


def test_timelock_delay_must_cover_the_response_time():
    bands = [{"min_value_usd": 0, "min_delay_seconds": 3600}]
    short = ok("custody_timelock_check", timelock(delay_seconds=7199, delay_bands=bands))
    assert sev(short) == {"delay-shorter-than-response": "major"} and short["delay_margin_seconds"] == -1
    exact = ok("custody_timelock_check", timelock(delay_seconds=7200, delay_bands=bands))
    assert exact["findings"] == [] and exact["delay_margin_seconds"] == 0 and exact["delay_hours"] == 2.0


@pytest.mark.parametrize(("cancellers", "guardian", "expected"), [
    (0, False, {"no-cancel-path": "major"}), (1, False, {"single-canceller": "minor"}), (2, False, {}),
    (0, True, {}), (1, True, {}),
])
def test_timelock_cancel_path(cancellers, guardian, expected):
    assert sev(ok("custody_timelock_check", timelock(canceller_count=cancellers, guardian_can_cancel=guardian))) == expected


def test_timelock_admin_and_expiry_findings():
    result = ok("custody_timelock_check", timelock(admin_renounced=False, execution_window_seconds=0))
    assert sev(result) == {"admin-not-renounced": "major", "no-expiry": "minor"}
    assert ok("custody_timelock_check", timelock(execution_window_seconds=0))["verdict"] == "warn"


def test_timelock_rejects_duplicate_band_starts():
    invalid("custody_timelock_check", timelock(delay_bands=[{"min_value_usd": 5, "min_delay_seconds": 1}, {"min_value_usd": 5, "min_delay_seconds": 2}]), "delay_bands")


# --- custody_ceremony_checklist ---------------------------------------------------------------------------------------

KEYGEN_ITEMS = [
    "participants-identified", "procedure-reviewed", "ceremony-record-signed", "hardware-integrity-verified",
    "offline-environment-attested", "entropy-source-attested", "public-key-recorded-out-of-band", "backup-restore-verified",
    "backup-custody-receipted", "media-sanitized",
]


def checklist(items, ceremony_type="key_generation", min_witnesses=2):
    return {"ceremony_type": ceremony_type, "min_witnesses": min_witnesses, "items": items}


def done(name, witnesses=2, evidence="rec-1", flag=True):
    return {"item": name, "done": flag, "evidence_ref": evidence, "witness_count": witnesses}


def test_ceremony_complete_and_partial():
    full = ok("custody_ceremony_checklist", checklist([done(n) for n in KEYGEN_ITEMS]))
    assert full["required_items"] == 10 and full["completed_items"] == 10 and full["completion_pct"] == 100.0
    assert full["missing_items"] == [] and full["verdict"] == "pass"
    partial = ok("custody_ceremony_checklist", checklist([done(n) for n in KEYGEN_ITEMS[:3]]))
    assert partial["completed_items"] == 3 and partial["completion_pct"] == 30.0 and partial["missing_items"] == KEYGEN_ITEMS[3:]
    assert sev(partial)["item-open:media-sanitized"] == "major" and partial["verdict"] == "fail"


@pytest.mark.parametrize(("ceremony", "count"), [("key_generation", 10), ("key_rotation", 9), ("shard_distribution", 8), ("recovery_restore", 7)])
def test_ceremony_catalogue_sizes(ceremony, count):
    result = ok("custody_ceremony_checklist", checklist([], ceremony))
    assert result["required_items"] == count and len(result["missing_items"]) == count and result["completion_pct"] == 0.0


def test_ceremony_completion_pct_rounds_to_two_places():
    result = ok("custody_ceremony_checklist", checklist([done(n) for n in ("participants-identified", "procedure-reviewed", "ceremony-record-signed")], "recovery_restore"))
    assert result["completion_pct"] == 42.86


def test_ceremony_done_needs_evidence_and_witnesses_only_where_physical():
    items = [done("participants-identified", evidence="  "), done("hardware-integrity-verified", witnesses=1),
             done("media-sanitized", witnesses=2), done("procedure-reviewed", witnesses=0)]
    result = ok("custody_ceremony_checklist", checklist(items))
    found = sev(result)
    assert found["missing-evidence:participants-identified"] == "major"
    assert found["insufficient-witnesses:hardware-integrity-verified"] == "major"
    assert "media-sanitized" not in " ".join(found) and "procedure-reviewed" not in " ".join(found)
    assert result["completed_items"] == 2


def test_ceremony_witness_minimum_is_inclusive_and_zero_disables_it():
    at_min = ok("custody_ceremony_checklist", checklist([done("media-sanitized", witnesses=2)]))
    assert "insufficient-witnesses:media-sanitized" not in sev(at_min)
    below = ok("custody_ceremony_checklist", checklist([done("media-sanitized", witnesses=1)]))
    assert sev(below)["insufficient-witnesses:media-sanitized"] == "major"
    none_needed = ok("custody_ceremony_checklist", checklist([done("media-sanitized", witnesses=0)], min_witnesses=0))
    assert "insufficient-witnesses:media-sanitized" not in sev(none_needed)


def test_ceremony_not_done_item_is_open_even_with_evidence():
    result = ok("custody_ceremony_checklist", checklist([done("media-sanitized", flag=False)]))
    assert sev(result)["item-open:media-sanitized"] == "major"


def test_ceremony_refuses_unknown_duplicate_and_secret_items_naming_the_field():
    invalid("custody_ceremony_checklist", checklist([done("new-key-added-onchain-before-old-removed")]), "items")
    invalid("custody_ceremony_checklist", checklist([done("media-sanitized"), done("media-sanitized")]), "items")
    leaked = invalid("custody_ceremony_checklist", checklist([done("media-sanitized", evidence=wif(0xEF, False))]), "items")
    assert wif(0xEF, False) not in json.dumps(leaked)


# --- custody_shard_distribution ---------------------------------------------------------------------------------------


def shards(custodians=None, orgs=None, countries=None, n=5):
    custodians = custodians or ["alice", "bob", "carol", "dave", "erin"][:n]
    orgs = orgs or [f"org-{c}" for c in custodians]
    countries = countries or ["DE", "CH", "US", "SG", "GB"][:n]
    return [{"shard_id": f"sh{i}", "custodian": custodians[i], "organization": orgs[i], "country": countries[i]} for i in range(len(custodians))]


def test_shards_independent_three_of_five():
    assert ok("custody_shard_distribution", {"threshold": 3, "shards": shards()}) == {
        "state": "SUCCESS", "shard_count": 5, "threshold": 3, "loss_tolerance": 2, "compromise_tolerance": 2,
        "min_custodians_for_quorum": 3, "min_organizations_for_quorum": 3, "min_countries_for_quorum": 3, "verdict": "pass", "findings": [],
    }


def test_shards_threshold_extremes():
    one = ok("custody_shard_distribution", {"threshold": 1, "shards": shards()})
    assert sev(one) == {"threshold-one": "blocker"}
    full = ok("custody_shard_distribution", {"threshold": 5, "shards": shards()})
    assert sev(full) == {"no-loss-tolerance": "major"} and full["loss_tolerance"] == 0


def test_shards_single_custodian_or_organization_can_reconstruct():
    cust = ok("custody_shard_distribution", {"threshold": 2, "shards": shards(custodians=["a", "a", "b", "c"], orgs=["x", "x", "y", "z"], countries=["DE", "CH", "US", "SG"])})
    assert sev(cust) == {"single-custodian-quorum": "blocker"} and cust["min_custodians_for_quorum"] == 1
    org = ok("custody_shard_distribution", {"threshold": 3, "shards": shards(orgs=["x", "x", "x", "y", "z"])})
    assert sev(org) == {"single-organization-quorum": "blocker"} and org["min_custodians_for_quorum"] == 3 and org["min_organizations_for_quorum"] == 1


def test_shards_jurisdiction_quorum():
    one = ok("custody_shard_distribution", {"threshold": 3, "shards": shards(countries=["DE", "DE", "DE", "US", "SG"])})
    assert sev(one) == {"single-jurisdiction-quorum": "major"} and one["min_countries_for_quorum"] == 1
    two = ok("custody_shard_distribution", {"threshold": 3, "shards": shards(countries=["DE", "DE", "US", "US", "SG"])})
    assert two["findings"] == [] and two["min_countries_for_quorum"] == 2


def test_shards_custodian_holding_a_minority_of_a_quorum_is_a_minor_finding():
    result = ok("custody_shard_distribution", {"threshold": 3, "shards": shards(custodians=["a", "a", "b", "c", "d"], orgs=["x", "x", "y", "z", "w"])})
    assert sev(result) == {"custodian-holds-multiple:a": "minor"} and result["min_custodians_for_quorum"] == 2


def test_shards_rejects_inconsistent_inputs_naming_the_field():
    invalid("custody_shard_distribution", {"threshold": 6, "shards": shards()}, "threshold")
    twin = shards()
    twin[1]["shard_id"] = "SH0"
    invalid("custody_shard_distribution", {"threshold": 3, "shards": twin}, "shards")
    for bad in ("de", "D1", "D", "DEU"):
        row = shards()
        row[0]["country"] = bad
        invalid("custody_shard_distribution", {"threshold": 3, "shards": row}, "shards")
    two_orgs = shards(custodians=["a", "a", "b", "c", "d"], orgs=["x", "y", "y", "z", "w"])
    invalid("custody_shard_distribution", {"threshold": 3, "shards": two_orgs}, "shards")
    leaked = shards()
    leaked[0]["custodian"] = xprv()
    invalid("custody_shard_distribution", {"threshold": 3, "shards": leaked}, "shards")


# --- custody_signer_policy_lint ---------------------------------------------------------------------------------------


def signer_policy(**kw):
    base = {"per_tx_limit_usd": 250000, "daily_limit_usd": 1000000, "balance_usd": 20000000, "velocity_max_tx_per_hour": 10,
            "allowlist_enforced": True, "allowlist_cooloff_seconds": 172800, "allowlist_reattest_days": 90, "signing_threshold": 3,
            "policy_change_approvals": 4, "approval_expiry_seconds": 3600}
    return {**base, **kw}


def test_signer_policy_clean_evaluates_every_rule():
    assert ok("custody_signer_policy_lint", signer_policy()) == {"state": "SUCCESS", "rules_evaluated": 14, "verdict": "pass", "findings": []}


@pytest.mark.parametrize(("override", "expected"), [
    ({"per_tx_limit_usd": 0}, {"no-per-tx-limit": "major"}),
    ({"daily_limit_usd": 0}, {"no-daily-limit": "major"}),
    ({"per_tx_limit_usd": 1000000}, {}),
    ({"per_tx_limit_usd": 1000001}, {"per-tx-exceeds-daily": "minor"}),
    ({"per_tx_limit_usd": 20000000, "daily_limit_usd": 30000000}, {"per-tx-limit-not-binding": "minor", "daily-limit-not-binding": "minor"}),
    ({"per_tx_limit_usd": 19999999.99}, {"per-tx-exceeds-daily": "minor"}),
    ({"daily_limit_usd": 20000000}, {"daily-limit-not-binding": "minor"}),
    ({"velocity_max_tx_per_hour": 0}, {"no-velocity-limit": "minor"}),
    ({"allowlist_cooloff_seconds": 0}, {"no-cooloff": "major"}),
    ({"allowlist_cooloff_seconds": 86399}, {"short-cooloff": "minor"}),
    ({"allowlist_cooloff_seconds": 86400}, {}),
    ({"allowlist_reattest_days": 0}, {"allowlist-never-reattested": "minor"}),
    ({"allowlist_reattest_days": 366}, {"allowlist-reattest-too-long": "minor"}),
    ({"allowlist_reattest_days": 365}, {}),
    ({"policy_change_approvals": 2}, {"weak-policy-change-quorum": "major"}),
    ({"policy_change_approvals": 3}, {}),
    ({"approval_expiry_seconds": 0}, {"approvals-never-expire": "major"}),
    ({"approval_expiry_seconds": 86401}, {"long-approval-expiry": "minor"}),
    ({"approval_expiry_seconds": 86400}, {}),
])
def test_signer_policy_each_rule_and_its_boundary(override, expected):
    assert sev(ok("custody_signer_policy_lint", signer_policy(**override))) == expected


def test_signer_policy_allowlist_rules_only_apply_when_enforced():
    result = ok("custody_signer_policy_lint", signer_policy(allowlist_enforced=False, allowlist_cooloff_seconds=0, allowlist_reattest_days=0))
    assert sev(result) == {"allowlist-not-enforced": "major"} and result["rules_evaluated"] == 10


def test_signer_policy_unset_limits_skip_the_comparisons_between_limits():
    result = ok("custody_signer_policy_lint", signer_policy(per_tx_limit_usd=0))
    assert sev(result) == {"no-per-tx-limit": "major"} and result["rules_evaluated"] == 12
    no_balance = ok("custody_signer_policy_lint", signer_policy(balance_usd=0))
    assert no_balance["rules_evaluated"] == 12 and no_balance["findings"] == []


# --- custody_wallet_tier_caps -----------------------------------------------------------------------------------------


def tiers(hot, warm, cold, hot_cap=5, warm_cap=20, cold_cap=100, hot_abs=None, min_cold=70):
    hot_row = {"tier": "hot", "balance_usd": hot, "max_pct_of_total": hot_cap}
    if hot_abs is not None:
        hot_row["max_balance_usd"] = hot_abs
    return {"tiers": [{"tier": "cold", "balance_usd": cold, "max_pct_of_total": cold_cap}, hot_row,
                      {"tier": "warm", "balance_usd": warm, "max_pct_of_total": warm_cap}], "min_cold_pct": min_cold}


def test_wallet_tiers_within_caps_lists_tiers_in_hot_warm_cold_order():
    result = ok("custody_wallet_tier_caps", tiers(50000, 450000, 9500000, hot_abs=100000))
    assert result["total_usd"] == 10000000.0 and result["hot_pct"] == 0.5 and result["cold_pct"] == 95.0
    assert [t["tier"] for t in result["tiers"]] == ["hot", "warm", "cold"]
    assert result["tiers"][0] == {"tier": "hot", "balance_usd": 50000.0, "pct_of_total": 0.5, "cap_usd": 100000.0, "excess_usd": 0.0, "within_cap": True}
    assert result["sweep_required_usd"] == 0.0 and result["findings"] == [] and result["verdict"] == "pass"


def test_wallet_tiers_absolute_cap_binds_below_the_percentage_cap():
    result = ok("custody_wallet_tier_caps", tiers(200000, 450000, 9350000, hot_abs=100000))
    assert sev(result) == {"hot-over-cap": "major"} and result["tiers"][0]["excess_usd"] == 100000.0 and result["sweep_required_usd"] == 100000.0
    assert result["tiers"][0]["within_cap"] is False


def test_wallet_tiers_percentage_cap_boundary_is_inclusive():
    at_cap = ok("custody_wallet_tier_caps", tiers(500000, 500000, 9000000))
    assert at_cap["findings"] == [] and at_cap["tiers"][0]["within_cap"] is True
    over = ok("custody_wallet_tier_caps", tiers(600000, 400000, 9000000))
    assert sev(over) == {"hot-over-cap": "major"} and over["tiers"][0]["cap_usd"] == 500000.0 and over["tiers"][0]["excess_usd"] == 100000.0


def test_wallet_tiers_cold_minimum_is_strict_and_cold_overage_is_minor():
    exact = ok("custody_wallet_tier_caps", tiers(1000000, 2000000, 7000000, hot_cap=100, warm_cap=100))
    assert exact["cold_pct"] == 70.0 and exact["findings"] == []
    below = ok("custody_wallet_tier_caps", tiers(1100000, 2000000, 6900000, hot_cap=100, warm_cap=100))
    assert sev(below) == {"cold-below-minimum": "major"}
    cold_over = ok("custody_wallet_tier_caps", tiers(1000000, 0, 9000000, hot_cap=100, cold_cap=80))
    assert sev(cold_over) == {"cold-over-cap": "minor"} and cold_over["sweep_required_usd"] == 1000000.0


def test_wallet_tiers_missing_cold_tier_and_empty_wallet():
    hot_only = ok("custody_wallet_tier_caps", {"tiers": [{"tier": "hot", "balance_usd": 100, "max_pct_of_total": 100}], "min_cold_pct": 10})
    assert hot_only["cold_pct"] == 0.0 and sev(hot_only) == {"cold-below-minimum": "major"}
    empty = ok("custody_wallet_tier_caps", {"tiers": [{"tier": "hot", "balance_usd": 0, "max_pct_of_total": 5}], "min_cold_pct": 70})
    assert empty["total_usd"] == 0.0 and empty["hot_pct"] == 0.0 and empty["findings"] == []


def test_wallet_tiers_rejects_a_repeated_tier():
    payload = tiers(1, 1, 1)
    payload["tiers"].append({"tier": "hot", "balance_usd": 1, "max_pct_of_total": 5})
    invalid("custody_wallet_tier_caps", payload, "tiers")


# --- custody_key_rotation ---------------------------------------------------------------------------------------------

AS_OF = datetime(2026, 10, 20, tzinfo=timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def rotation(keys, warn_days=30, as_of=None):
    return {"as_of": as_of or iso(AS_OF), "warn_days": warn_days, "keys": keys}


def key(key_id="k1", age_days=0.0, max_age_days=365, rotated=False, extra_seconds=0):
    created = AS_OF - timedelta(days=age_days * 2 if rotated else age_days, seconds=extra_seconds)
    row = {"key_id": key_id, "created_at": iso(created), "last_rotated_at": "", "max_age_days": max_age_days}
    if rotated:
        row["last_rotated_at"] = iso(AS_OF - timedelta(days=age_days, seconds=extra_seconds))
    return row


def test_rotation_example_ages_and_ordering():
    keys = [{"key_id": "signer-1", "created_at": "2025-01-01T00:00:00Z", "last_rotated_at": "", "max_age_days": 365},
            {"key_id": "signer-2", "created_at": "2025-01-01T00:00:00Z", "last_rotated_at": "2026-09-01T00:00:00Z", "max_age_days": 365}]
    result = ok("custody_key_rotation", rotation(keys))
    assert result["keys"] == [
        {"key_id": "signer-1", "age_days": 657.0, "days_remaining": -292.0, "status": "overdue"},
        {"key_id": "signer-2", "age_days": 49.0, "days_remaining": 316.0, "status": "ok"},
    ]
    assert (result["key_count"], result["overdue_count"], result["due_soon_count"], result["ok_count"]) == (2, 1, 0, 1)
    assert sev(result) == {"rotation-overdue:signer-1": "major"} and result["verdict"] == "fail"


def test_rotation_age_exactly_at_the_maximum_is_not_overdue_one_second_more_is():
    at_max = ok("custody_key_rotation", rotation([key(age_days=365)], warn_days=0))
    assert at_max["keys"][0]["status"] == "due_soon" and at_max["keys"][0]["days_remaining"] == 0.0
    late = ok("custody_key_rotation", rotation([key(age_days=365, extra_seconds=1)], warn_days=0))
    assert late["keys"][0]["status"] == "overdue"


def test_rotation_warning_window_boundary_is_inclusive():
    due = ok("custody_key_rotation", rotation([key(age_days=365, max_age_days=395)], warn_days=30))
    assert due["keys"][0]["status"] == "due_soon" and sev(due) == {"rotation-due-soon:k1": "minor"} and due["verdict"] == "warn"
    fine = ok("custody_key_rotation", rotation([key(age_days=365, max_age_days=395)], warn_days=29))
    assert fine["keys"][0]["status"] == "ok" and fine["findings"] == []


def test_rotation_more_than_twice_the_maximum_is_a_blocker():
    twice = ok("custody_key_rotation", rotation([key(age_days=730)]))
    assert sev(twice) == {"rotation-overdue:k1": "major"}
    beyond = ok("custody_key_rotation", rotation([key(age_days=730, extra_seconds=1)]))
    assert sev(beyond) == {"rotation-overdue:k1": "blocker"}


def test_rotation_age_runs_from_the_last_rotation_and_honours_offsets():
    rotated = ok("custody_key_rotation", rotation([key(age_days=10, rotated=True)]))
    assert rotated["keys"][0]["age_days"] == 10.0
    row = {"key_id": "k", "created_at": "2026-10-10T02:00:00+02:00", "last_rotated_at": "", "max_age_days": 365}
    assert ok("custody_key_rotation", rotation([row]))["keys"][0]["age_days"] == 10.0


def test_rotation_rejects_bad_times_and_duplicates_naming_the_field():
    good = key()
    invalid("custody_key_rotation", rotation([good], as_of="2026-10-20T00:00:00"), "as_of")
    invalid("custody_key_rotation", rotation([{**good, "created_at": "2025-01-01"}]), "keys")
    invalid("custody_key_rotation", rotation([{**good, "last_rotated_at": iso(AS_OF - timedelta(days=400)), "created_at": iso(AS_OF - timedelta(days=300))}]), "keys")
    invalid("custody_key_rotation", rotation([{**good, "created_at": iso(AS_OF + timedelta(seconds=1))}]), "keys")
    invalid("custody_key_rotation", rotation([good, {**good, "key_id": "K1"}]), "keys")
    invalid("custody_key_rotation", rotation([{**good, "key_id": xprv()}]), "keys")


# --- custody_tx_policy_eval -------------------------------------------------------------------------------------------


def tx_policy(**kw):
    return {"allowed_chain_ids": [1, 10, 8453], "allowlist": [ADDR_A, ADDR_B], "denylist": [ADDR_D], "per_tx_limit_usd": 500000,
            "daily_limit_usd": 1000000, "hold_above_usd": 100000, "required_approvals": 3, "cooloff_seconds": 172800,
            "max_tx_per_hour": 10, "allow_delegatecall": False, **kw}


def tx(policy=None, **kw):
    base = {"chain_id": 1, "to": ADDR_A, "value_usd": 25000, "operation": "call", "selector": "0xa9059cbb", "approvals": 3,
            "destination_listed_seconds": 604800, "spent_24h_usd": 10000, "tx_count_1h": 1}
    return {**base, **kw, "policy": policy or tx_policy()}


def test_tx_policy_allows_a_compliant_transfer():
    assert ok("custody_tx_policy_eval", tx()) == {
        "state": "SUCCESS", "decision": "allow", "deny_count": 0, "hold_count": 0,
        "selector_signature": "transfer(address,uint256)", "findings": [],
    }


@pytest.mark.parametrize(("override", "rule", "severity"), [
    ({"chain_id": 137}, "chain-not-allowed", "blocker"),
    ({"to": "0x" + "0" * 40}, "destination-zero-address", "blocker"),
    ({"to": ADDR_D}, "destination-denylisted", "blocker"),
    ({"operation": "delegatecall"}, "delegatecall-blocked", "blocker"),
    ({"value_usd": 500000.01, "policy": tx_policy(hold_above_usd=600000)}, "per-tx-limit-exceeded", "blocker"),
    ({"spent_24h_usd": 975000.01}, "daily-limit-exceeded", "blocker"),
    ({"to": ADDR_C}, "destination-not-allowlisted", "major"),
    ({"destination_listed_seconds": 172799}, "destination-cooloff", "major"),
    ({"value_usd": 100000}, "manual-review-threshold", "major"),
    ({"selector": "0x095ea7b3"}, "risky-selector", "major"),
    ({"approvals": 2}, "insufficient-approvals", "major"),
    ({"tx_count_1h": 10}, "velocity-exceeded", "major"),
])
def test_tx_policy_each_rule_fires_alone_with_its_effect(override, rule, severity):
    result = ok("custody_tx_policy_eval", tx(**override))
    assert sev(result) == {rule: severity}
    assert result["decision"] == ("deny" if severity == "blocker" else "hold")


@pytest.mark.parametrize("override", [
    {"value_usd": 99999.99}, {"destination_listed_seconds": 172800}, {"tx_count_1h": 9}, {"approvals": 4}, {"spent_24h_usd": 975000},
    {"selector": ""}, {"selector": "0xdeadbeef"},
])
def test_tx_policy_boundaries_that_do_not_fire(override):
    result = ok("custody_tx_policy_eval", tx(**override))
    assert result["decision"] == "allow" and result["findings"] == []


def test_tx_policy_limits_are_strict_greater_than():
    at_limit = ok("custody_tx_policy_eval", tx(value_usd=500000, spent_24h_usd=0))
    assert sev(at_limit) == {"manual-review-threshold": "major"}
    daily = ok("custody_tx_policy_eval", tx(value_usd=25000, spent_24h_usd=975000))
    assert daily["decision"] == "allow"


def test_tx_policy_deny_outranks_hold_and_counts_both():
    result = ok("custody_tx_policy_eval", tx(chain_id=137, approvals=0, to=ADDR_C))
    assert result["decision"] == "deny" and result["deny_count"] == 1 and result["hold_count"] == 2
    assert [f["severity"] for f in result["findings"]] == ["blocker", "major", "major"]


def test_tx_policy_delegatecall_can_be_allowed_by_policy():
    assert ok("custody_tx_policy_eval", tx(operation="delegatecall", policy=tx_policy(allow_delegatecall=True)))["decision"] == "allow"


def test_tx_policy_denied_destinations_are_not_also_reported_as_unlisted():
    result = ok("custody_tx_policy_eval", tx(to=ADDR_D))
    assert sev(result) == {"destination-denylisted": "blocker"}


def test_tx_policy_address_spelling_is_normalised():
    assert ok("custody_tx_policy_eval", tx(to=ADDR_A.lower()))["decision"] == "allow"
    assert ok("custody_tx_policy_eval", tx(to="0x" + ADDR_A[2:].upper()))["decision"] == "allow"


@pytest.mark.parametrize(("selector", "signature"), [
    ("0x095ea7b3", "approve(address,uint256)"), ("0x39509351", "increaseAllowance(address,uint256)"),
    ("0xd505accf", "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)"), ("0xa22cb465", "setApprovalForAll(address,bool)"),
    ("0xf2fde38b", "transferOwnership(address)"), ("0x715018a6", "renounceOwnership()"), ("0x3659cfe6", "upgradeTo(address)"),
    ("0x4f1ef286", "upgradeToAndCall(address,bytes)"), ("0X095EA7B3".replace("0X", "0x"), "approve(address,uint256)"),
])
def test_tx_policy_risky_selectors_hold_and_are_named(selector, signature):
    result = ok("custody_tx_policy_eval", tx(selector=selector))
    assert result["decision"] == "hold" and result["selector_signature"] == signature and sev(result) == {"risky-selector": "major"}


def test_tx_policy_plain_and_unknown_selectors():
    assert ok("custody_tx_policy_eval", tx(selector="0x23b872dd"))["selector_signature"] == "transferFrom(address,address,uint256)"
    assert ok("custody_tx_policy_eval", tx(selector="0xdeadbeef"))["selector_signature"] == "unknown"
    assert ok("custody_tx_policy_eval", tx(selector=""))["selector_signature"] == ""


def test_tx_policy_rejects_malformed_inputs_naming_the_field():
    flipped = ADDR_A[:3] + ADDR_A[3].swapcase() + ADDR_A[4:]
    invalid("custody_tx_policy_eval", tx(to=flipped), "to")
    invalid("custody_tx_policy_eval", tx(to="0x" + "g" * 40), "to")
    invalid("custody_tx_policy_eval", tx(to="5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed00"), "to")
    for bad in ("a9059cbb", "0x1234", "0xa9059cbbzz", "0xzzzzzzzz"):
        invalid("custody_tx_policy_eval", tx(selector=bad), "selector")
    invalid("custody_tx_policy_eval", tx(policy=tx_policy(allowlist=[flipped])), "policy")
    invalid("custody_tx_policy_eval", tx(policy=tx_policy(denylist=[ADDR_A])), "policy")


# --- custody_sanctions_screening --------------------------------------------------------------------------------------


def screening(**kw):
    row = {"subject_id": "cp1", "list_name": "ofac-sdn", "list_updated_at": "2026-10-20T06:00:00Z", "screened_at": "2026-10-20T08:00:00Z",
           "result": "clear", "action": "released", **kw}
    return row


def screen(*rows, **kw):
    return {"as_of": "2026-10-20T12:00:00Z", "max_list_age_hours": 24, "max_screening_age_hours": 24, "screenings": list(rows), **kw}


def test_sanctions_clean_record_passes_with_counts():
    assert ok("custody_sanctions_screening", screen(screening())) == {
        "state": "SUCCESS", "screened": 1, "stale_lists": 0, "counts": {"clear": 1, "potential_match": 0, "confirmed_match": 0, "error": 0},
        "omitted_findings": 0, "verdict": "pass", "findings": [],
    }


def test_sanctions_list_age_boundary():
    exact = ok("custody_sanctions_screening", screen(screening(list_updated_at="2026-10-19T08:00:00Z")))
    assert exact["findings"] == [] and exact["stale_lists"] == 0
    stale = ok("custody_sanctions_screening", screen(screening(list_updated_at="2026-10-19T07:59:59Z")))
    assert sev(stale) == {"stale-list:cp1@ofac-sdn": "major"} and stale["stale_lists"] == 1


def test_sanctions_screening_age_boundary_applies_to_clear_results():
    fresh = screening(list_updated_at="2026-10-19T10:00:00Z", screened_at="2026-10-19T12:00:00Z")
    assert ok("custody_sanctions_screening", screen(fresh))["findings"] == []
    old = screening(list_updated_at="2026-10-19T10:00:00Z", screened_at="2026-10-19T11:59:59Z")
    result = ok("custody_sanctions_screening", screen(old))
    assert sev(result) == {"stale-screening:cp1@ofac-sdn": "minor"} and result["verdict"] == "warn"
    old_match = screening(list_updated_at="2026-10-19T10:00:00Z", screened_at="2026-10-19T11:59:59Z", result="potential_match", action="held")
    assert sev(ok("custody_sanctions_screening", screen(old_match))) == {"potential-match-under-review:cp1@ofac-sdn": "info"}


@pytest.mark.parametrize(("result", "action", "rule", "severity"), [
    ("error", "released", "released-on-error", "blocker"),
    ("error", "none", "error-unactioned", "major"),
    ("error", "reported", "error-unactioned", "major"),
    ("error", "held", "error-retry-pending", "minor"),
    ("error", "blocked", "error-retry-pending", "minor"),
    ("potential_match", "released", "potential-match-released", "blocker"),
    ("potential_match", "none", "potential-match-unactioned", "major"),
    ("potential_match", "held", "potential-match-under-review", "info"),
    ("potential_match", "blocked", "potential-match-under-review", "info"),
    ("potential_match", "reported", "potential-match-unactioned", "major"),
    ("confirmed_match", "released", "confirmed-match-not-blocked", "blocker"),
    ("confirmed_match", "none", "confirmed-match-not-blocked", "blocker"),
    ("confirmed_match", "held", "confirmed-match-not-blocked", "blocker"),
    ("confirmed_match", "blocked", "confirmed-match-report-pending", "minor"),
    ("confirmed_match", "reported", "confirmed-match-not-blocked", "blocker"),
    ("clear", "held", "clear-but-withheld", "info"),
    ("clear", "blocked", "clear-but-withheld", "info"),
])
def test_sanctions_action_for_each_match_state(result, action, rule, severity):
    out = ok("custody_sanctions_screening", screen(screening(result=result, action=action)))
    assert sev(out) == {f"{rule}:cp1@ofac-sdn": severity}


@pytest.mark.parametrize("action", ["none", "released", "reported"])
def test_sanctions_clear_with_other_actions_has_no_finding(action):
    assert ok("custody_sanctions_screening", screen(screening(action=action)))["findings"] == []


def test_sanctions_counts_stale_lists_and_orders_worst_first():
    rows = [screening(subject_id="a"), screening(subject_id="b", result="confirmed_match", action="released"),
            screening(subject_id="c", result="error", action="none", list_updated_at="2026-10-01T00:00:00Z"),
            screening(subject_id="d", result="potential_match", action="held")]
    result = ok("custody_sanctions_screening", screen(*rows))
    assert result["counts"] == {"clear": 1, "potential_match": 1, "confirmed_match": 1, "error": 1} and result["stale_lists"] == 1
    assert [f["severity"] for f in result["findings"]] == ["blocker", "major", "major", "info"]
    assert result["verdict"] == "fail"


def test_sanctions_findings_are_capped_with_the_omitted_count():
    rows = [screening(subject_id=f"s{i:03d}", result="potential_match", action="none") for i in range(120)]
    result = ok("custody_sanctions_screening", screen(*rows))
    assert len(result["findings"]) == 100 and result["omitted_findings"] == 20 and result["screened"] == 120


def test_sanctions_rejects_inconsistent_times_and_secret_labels_naming_the_field():
    invalid("custody_sanctions_screening", screen(screening(screened_at="2026-10-20T12:00:01Z")), "screenings")
    invalid("custody_sanctions_screening", screen(screening(list_updated_at="2026-10-20T09:00:00Z")), "screenings")
    invalid("custody_sanctions_screening", screen(screening(screened_at="2026-10-20T08:00:00")), "screenings")
    invalid("custody_sanctions_screening", screen(screening(), as_of="2026-10-20"), "as_of")
    invalid("custody_sanctions_screening", screen(screening(subject_id=wif())), "screenings")


# --- custody_travel_rule ----------------------------------------------------------------------------------------------


def transfer(**kw):
    return {"amount": 5000, "threshold": 1000, "comparison": "at_or_above", "originator_name": "Acme Treasury Ltd",
            "originator_account": ADDR_A, "originator_identifier_kind": "customer_id", "originator_identifier": "CUST-1001",
            "beneficiary_name": "Globex GmbH", "beneficiary_account": ADDR_B, **kw}


def test_travel_rule_complete_transfer():
    assert ok("custody_travel_rule", transfer()) == {
        "state": "SUCCESS", "applies": True, "status": "complete", "required_fields": 5, "present_fields": 5, "missing_fields": [], "findings": [],
    }


@pytest.mark.parametrize(("comparison", "amount", "applies"), [
    ("at_or_above", 1000, True), ("above", 1000, False), ("at_or_above", 999.99, False), ("above", 1000.01, True),
])
def test_travel_rule_threshold_comparison_modes(comparison, amount, applies):
    result = ok("custody_travel_rule", transfer(comparison=comparison, amount=amount, originator_name=""))
    assert result["applies"] is applies
    assert result["status"] == ("incomplete" if applies else "not_applicable")


def test_travel_rule_below_threshold_does_not_require_the_full_set_but_reports_missing_minimal_data():
    # FATF INR.16 para 5(a) (FATF Recommendations 2012, as amended 2023): below a de minimis threshold the originator name,
    # the beneficiary name and an account or reference for each still accompany the transfer; the full set (para 6) does not.
    result = ok("custody_travel_rule", transfer(amount=10, originator_name="", beneficiary_account=""))
    assert result["applies"] is False and result["status"] == "not_applicable" and result["required_fields"] == 0
    assert result["present_fields"] == 3 and result["missing_fields"] == []
    assert sev(result) == {"below-threshold-missing-beneficiary-account": "info", "below-threshold-missing-originator-name": "info"}
    assert ok("custody_travel_rule", transfer(amount=10))["findings"] == []


def test_travel_rule_missing_fields_are_sorted_and_blank_means_missing():
    result = ok("custody_travel_rule", transfer(beneficiary_name="  ", beneficiary_account="", originator_identifier_kind="none", originator_identifier=""))
    assert result["missing_fields"] == ["beneficiary_account", "beneficiary_name", "originator_identifier"]
    assert result["present_fields"] == 2 and result["status"] == "incomplete"
    assert sev(result) == {"missing-beneficiary-account": "major", "missing-beneficiary-name": "major", "missing-originator-identifier": "major"}
    blank_id = ok("custody_travel_rule", transfer(originator_identifier=" "))
    assert blank_id["missing_fields"] == ["originator_identifier"]


def test_travel_rule_rejects_an_identifier_without_a_kind_and_secret_values():
    invalid("custody_travel_rule", transfer(originator_identifier_kind="none"), "originator_identifier_kind")
    invalid("custody_travel_rule", transfer(originator_name=wif()), "originator_name")
    invalid("custody_travel_rule", transfer(beneficiary_account=xprv()), "beneficiary_account")


# --- custody_audit_trail ----------------------------------------------------------------------------------------------


def event_hash(e):
    body = f"{e['seq']}|{e['at']}|{e['actor']}|{e['action']}|{','.join(sorted(e['approvers']))}|{e['prev_hash']}"
    return hashlib.sha256(body.encode()).hexdigest()


def chain(specs, genesis="0" * 64):
    events, prev = [], genesis
    for spec in specs:
        event = {"seq": len(events) + 1, "at": "2026-10-19T09:00:00Z", "actor": "alice", "action": "limit.update", "approvers": ["bob", "carol"], **spec}
        event["seq"] = spec.get("seq", event["seq"])
        event["prev_hash"] = spec.get("prev_hash", prev)
        event["hash"] = spec.get("hash", event_hash(event))
        events.append(event)
        prev = events[-1]["hash"]
    return events


def trail(events, **kw):
    return {"events": events, "approval_required_actions": ["limit.update", "signer.add"], "min_approvals": 2, **kw}


GOOD = [{}, {"at": "2026-10-19T10:00:00Z", "actor": "bob", "action": "signer.add", "approvers": ["alice", "carol"]},
        {"at": "2026-10-19T11:00:00Z", "actor": "carol", "action": "report.export", "approvers": []}]


def test_audit_hash_formula_is_pinned():
    first = chain([{}])[0]
    assert first["hash"] == "6040c281b93cd56c53b55e8ccb989525599c449cb6e2dc309f18dc59b471485b"


def test_audit_valid_chain_is_order_independent():
    events = chain(GOOD)
    forward = ok("custody_audit_trail", trail(events))
    assert forward == {"state": "SUCCESS", "event_count": 3, "chain_valid": True, "first_break_seq": -1, "omitted_findings": 0,
                       "verdict": "pass", "findings": []}
    assert ok("custody_audit_trail", trail(list(reversed(events)))) == forward


def test_audit_tampered_content_breaks_the_hash_at_that_event():
    events = chain(GOOD)
    events[1]["action"] = "signer.remove"
    result = ok("custody_audit_trail", trail(events))
    assert sev(result) == {"hash-mismatch:2": "blocker"} and result["chain_valid"] is False and result["first_break_seq"] == 2


def test_audit_relinked_prev_hash_breaks_the_chain():
    events = chain(GOOD)
    events[2]["prev_hash"] = "1" * 64
    result = ok("custody_audit_trail", trail(events))
    assert sev(result) == {"chain-broken:3": "blocker", "hash-mismatch:3": "blocker"} and result["first_break_seq"] == 3


def test_audit_deleted_event_is_a_sequence_gap_and_a_broken_link():
    events = chain(GOOD)
    del events[1]
    result = ok("custody_audit_trail", trail(events))
    assert sev(result) == {"seq-gap:3": "blocker", "chain-broken:3": "blocker"} and result["first_break_seq"] == 3


def test_audit_duplicate_sequence_number():
    events = chain(GOOD[:2])
    events.append({**events[1]})
    result = ok("custody_audit_trail", trail(events))
    assert sev(result) == {"duplicate-seq:2": "blocker"} and result["first_break_seq"] == 2


def test_audit_genesis_hash_default_and_explicit_excerpt():
    wrong = chain([{}], genesis="2" * 64)
    assert sev(ok("custody_audit_trail", trail(wrong))) == {"genesis-mismatch:1": "blocker"}
    assert ok("custody_audit_trail", trail(wrong, genesis_hash="2" * 64))["chain_valid"] is True
    excerpt = chain([{"seq": 40}, {"seq": 41}], genesis="3" * 64)
    assert ok("custody_audit_trail", trail(excerpt, genesis_hash="3" * 64))["findings"] == []


def test_audit_time_regression_is_major_but_does_not_break_the_chain():
    events = chain([{}, {"at": "2026-10-19T10:00:00+02:00"}])
    result = ok("custody_audit_trail", trail(events))
    assert sev(result) == {"time-regression:2": "major"} and result["chain_valid"] is True and result["verdict"] == "fail"
    equal = chain([{}, {"at": "2026-10-19T09:00:00Z"}])
    assert ok("custody_audit_trail", trail(equal))["findings"] == []


def test_audit_missing_who_and_what():
    result = ok("custody_audit_trail", trail(chain([{"actor": " ", "action": ""}])))
    assert sev(result) == {"missing-actor:1": "major", "missing-action:1": "major"}


@pytest.mark.parametrize(("approvers", "expected"), [
    ([], {"insufficient-approvals:1": "major"}),
    (["bob"], {"insufficient-approvals:1": "major"}),
    (["bob", "carol"], {}),
    (["bob", "carol", "dave"], {}),
    (["bob", "ALICE"], {"self-approval:1": "major", "insufficient-approvals:1": "major"}),
    (["bob", "BOB"], {"insufficient-approvals:1": "major"}),
])
def test_audit_approvals_must_be_independent_of_the_actor(approvers, expected):
    assert sev(ok("custody_audit_trail", trail(chain([{"approvers": approvers}])))) == expected


def test_audit_self_approval_is_flagged_even_when_no_approval_is_required():
    result = ok("custody_audit_trail", trail(chain([{"approvers": ["alice", "bob"]}]), min_approvals=0))
    assert sev(result) == {"self-approval:1": "major"}
    assert ok("custody_audit_trail", trail(chain([{"action": "report.export", "approvers": []}])))["findings"] == []


def test_audit_findings_are_capped_with_the_omitted_count():
    result = ok("custody_audit_trail", trail(chain([{"actor": ""}] * 120)))
    assert result["event_count"] == 120 and len(result["findings"]) == 100 and result["omitted_findings"] == 20


def test_audit_rejects_malformed_events_naming_the_field():
    base = chain([{}])
    invalid("custody_audit_trail", trail([{**base[0], "hash": base[0]["hash"].upper()}]), "events")
    invalid("custody_audit_trail", trail([{**base[0], "hash": "abc"}]), "events")
    invalid("custody_audit_trail", trail(chain([{"actor": "a|b"}])), "events")
    invalid("custody_audit_trail", trail(chain([{"approvers": ["x,y"]}])), "events")
    invalid("custody_audit_trail", trail(chain([{"at": "2026-10-19T09:00:00"}])), "events")
    invalid("custody_audit_trail", trail(chain([{"actor": wif()}])), "events")
    invalid("custody_audit_trail", trail(base, genesis_hash="G" * 64), "genesis_hash")


# --- custody_recovery_drill -------------------------------------------------------------------------------------------


def drill(scenario="signer_key_loss", age_days=80, extra_seconds=0, passed=True, rto=90):
    return {"scenario": scenario, "last_drill_at": iso(AS_OF - timedelta(days=age_days, seconds=extra_seconds)), "passed": passed, "measured_rto_minutes": rto}


def drills(required, *records, max_age_days=180, target=240):
    return {"as_of": iso(AS_OF), "max_age_days": max_age_days, "target_rto_minutes": target, "required_scenarios": required, "drills": list(records)}


def test_recovery_example_current_and_never():
    result = ok("custody_recovery_drill", drills(["signer_key_loss", "full_restore"], drill()))
    assert result["counts"] == {"current": 1, "overdue": 0, "failed": 0, "rto_exceeded": 0, "never": 1} and result["required_count"] == 2
    assert result["scenarios"] == [{"scenario": "full_restore", "status": "never"},
                                   {"scenario": "signer_key_loss", "status": "current", "age_days": 80.0}]
    assert sev(result) == {"never-drilled:full_restore": "major"} and result["verdict"] == "fail"


def test_recovery_never_drilled_scenario_has_no_age_days_and_passes_the_output_schema():
    result = ok("custody_recovery_drill", drills(["full_restore"]))
    assert result["scenarios"] == [{"scenario": "full_restore", "status": "never"}]
    assert "age_days" not in result["scenarios"][0] and result["verdict"] == "fail"
    assert sev(result) == {"never-drilled:full_restore": "major"}


def test_recovery_age_boundary_is_inclusive_of_the_maximum():
    assert ok("custody_recovery_drill", drills(["signer_key_loss"], drill(age_days=180)))["counts"]["current"] == 1
    late = ok("custody_recovery_drill", drills(["signer_key_loss"], drill(age_days=180, extra_seconds=1)))
    assert late["counts"]["overdue"] == 1 and sev(late) == {"drill-overdue:signer_key_loss": "major"}


def test_recovery_rto_boundary_is_inclusive_of_the_target():
    assert ok("custody_recovery_drill", drills(["site_loss"], drill("site_loss", rto=240)))["counts"]["current"] == 1
    slow = ok("custody_recovery_drill", drills(["site_loss"], drill("site_loss", rto=240.01)))
    assert slow["counts"]["rto_exceeded"] == 1 and sev(slow) == {"rto-exceeded:site_loss": "major"}


def test_recovery_status_precedence_failed_then_rto_then_overdue():
    both = ok("custody_recovery_drill", drills(["site_loss"], drill("site_loss", age_days=400, passed=False, rto=999)))
    assert both["scenarios"][0]["status"] == "failed" and sev(both) == {"drill-failed:site_loss": "blocker"}
    slow_and_old = ok("custody_recovery_drill", drills(["site_loss"], drill("site_loss", age_days=400, rto=999)))
    assert slow_and_old["scenarios"][0]["status"] == "rto_exceeded"


def test_recovery_only_required_scenarios_are_reported_and_duplicates_collapse():
    result = ok("custody_recovery_drill", drills(["full_restore", "full_restore", "custodian_unavailable"], drill("signer_compromise"), drill("full_restore")))
    assert [s["scenario"] for s in result["scenarios"]] == ["custodian_unavailable", "full_restore"] and result["required_count"] == 2


def test_recovery_rejects_duplicate_drills_and_future_dates_naming_the_field():
    invalid("custody_recovery_drill", drills(["full_restore"], drill("full_restore"), drill("full_restore")), "drills")
    invalid("custody_recovery_drill", drills(["full_restore"], drill("full_restore", age_days=0, extra_seconds=-1)), "drills")
    invalid("custody_recovery_drill", {**drills(["full_restore"]), "as_of": "2026-10-20"}, "as_of")


# --- custody_secret_scan ----------------------------------------------------------------------------------------------


def scan(*files):
    return {"files": [{"path": path, "content": content} for path, content in files]}


def kinds(result):
    return [(f["kind"], f["location"], f["length"], f["severity"]) for f in result["findings"]]


def test_secret_scan_clean_files_pass():
    result = ok("custody_secret_scan", scan(("a.env", "RPC_URL=https://rpc.example.org\nTX_HASH=0x" + "ab" * 32 + "\nprivate_key_path=/etc/key.pem\n")))
    assert result == {"state": "SUCCESS", "files_scanned": 1, "finding_count": 0, "omitted_findings": 0, "verdict": "pass", "findings": []}


def test_secret_scan_hex_private_key_reports_location_and_length_never_the_value():
    value = bytes(range(32)).hex()
    result = ok("custody_secret_scan", scan((".env", f"A=1\nETH_PRIVATE_KEY=0x{value}\n"), ("cfg.yaml", f'private_key: "{value}"\n')))
    assert kinds(result) == [("hex-private-key", ".env:2", 64, "blocker"), ("hex-private-key", "cfg.yaml:1", 64, "blocker")]
    assert value not in json.dumps(result) and result["verdict"] == "fail" and result["finding_count"] == 2
    json_key = ok("custody_secret_scan", scan(("w.json", f'{{"privateKey": "0x{value}"}}')))
    assert kinds(json_key) == [("hex-private-key", "w.json:1", 64, "blocker")]


@pytest.mark.parametrize("line", [
    "PRIVATE_KEY=0x" + "0" * 64, "PRIVATE_KEY=" + "f" * 64, "tx_hash = 0x" + "12" * 32, "PRIVATE_KEY=0x" + "12" * 31, "PRIVATE_KEY=0x" + "12" * 33,
    "secret_key = hunter2",
])
def test_secret_scan_hex_rule_ignores_placeholders_hashes_and_wrong_lengths(line):
    assert ok("custody_secret_scan", scan(("x", line)))["findings"] == []


@pytest.mark.parametrize(("version", "compressed", "length"), [(0x80, True, 52), (0x80, False, 51), (0xEF, True, 52), (0xEF, False, 51)])
def test_secret_scan_wif_keys_are_found_by_checksum(version, compressed, length):
    token = wif(version, compressed)
    assert len(token) == length
    result = ok("custody_secret_scan", scan(("k.txt", f"line one\nkey {token} end")))
    assert kinds(result) == [("wif-private-key", "k.txt:2", length, "blocker")] and token not in json.dumps(result)


def test_secret_scan_wif_lookalikes_are_ignored():
    good = wif()
    corrupted = good[:-1] + ("2" if good[-1] != "2" else "3")
    wrong_version = wif(0x00)
    bad_flag = b58check(bytes([0x80]) + KEY32 + b"\x02")
    for token in (corrupted, wrong_version, bad_flag, good + "A", "x" + good):
        assert ok("custody_secret_scan", scan(("k.txt", f"k = {token}")))["findings"] == []


def test_secret_scan_extended_private_keys_and_versions():
    for version in ("0488ade4", "04358394", "049d7878", "04b2430c"):
        token = xprv(version)
        result = ok("custody_secret_scan", scan(("k.txt", f"\n\nroot: {token}\n")))
        assert kinds(result) == [("extended-private-key", "k.txt:3", len(token), "blocker")]
    good = xprv()
    assert ok("custody_secret_scan", scan(("k.txt", good[:-1] + ("2" if good[-1] != "2" else "3"))))["findings"] == []
    assert ok("custody_secret_scan", scan(("k.txt", "x" + good[1:].replace("prv", "pub", 1))))["findings"] == []
    unknown_version = xprv("0488ade5")  # valid checksum, starts with xprv, but not a BIP32 private-key version
    assert unknown_version.startswith("xprv") and ok("custody_secret_scan", scan(("k.txt", unknown_version)))["findings"] == []
    public_payload = xprv(key_prefix=b"\x02")  # a compressed public key where the 0x00 private-key marker belongs
    assert public_payload.startswith("xprv") and ok("custody_secret_scan", scan(("k.txt", public_payload)))["findings"] == []


def test_secret_scan_pem_private_key_blocks():
    body = "-----BEGIN PRIVATE KEY-----\nAAAAAAAA\n-----END PRIVATE KEY-----"
    result = ok("custody_secret_scan", scan(("id.pem", "# header\n" + body + "\nrest")))
    assert kinds(result) == [("pem-private-key", "id.pem:2", len(body), "blocker")]
    for label in ("RSA PRIVATE KEY", "EC PRIVATE KEY", "OPENSSH PRIVATE KEY", "PGP PRIVATE KEY BLOCK"):
        assert ok("custody_secret_scan", scan(("k", f"-----BEGIN {label}-----\nAA\n-----END {label}-----")))["findings"][0]["severity"] == "blocker"
    encrypted = ok("custody_secret_scan", scan(("k", "-----BEGIN ENCRYPTED PRIVATE KEY-----\nAA\n-----END ENCRYPTED PRIVATE KEY-----")))
    assert encrypted["findings"][0]["severity"] == "major"
    assert ok("custody_secret_scan", scan(("k", "-----BEGIN PUBLIC KEY-----\nAA\n-----END PUBLIC KEY-----")))["findings"] == []
    lone = ok("custody_secret_scan", scan(("k", "-----BEGIN PRIVATE KEY-----")))
    assert lone["findings"][0]["length"] == len("-----BEGIN PRIVATE KEY-----")


def test_secret_scan_mnemonic_assignments_need_a_valid_word_count_and_word_shapes():
    phrase = " ".join(["abandon"] * 11 + ["about"])
    result = ok("custody_secret_scan", scan(("w.py", f'x = 1\nMNEMONIC = "{phrase}"\n')))
    assert kinds(result) == [("mnemonic-phrase", "w.py:2", len(phrase), "blocker")] and phrase not in json.dumps(result)
    assert kinds(ok("custody_secret_scan", scan(("w.yml", f"seed_phrase: '{phrase}'")))) == [("mnemonic-phrase", "w.yml:1", len(phrase), "blocker")]
    eleven = " ".join(["abandon"] * 11)
    assert ok("custody_secret_scan", scan(("w", f'mnemonic = "{eleven}"')))["findings"] == []
    long_word = " ".join(["abandon"] * 11 + ["abcdefghi"])
    assert ok("custody_secret_scan", scan(("w", f'mnemonic = "{long_word}"')))["findings"] == []
    assert ok("custody_secret_scan", scan(("w", f'note = "{phrase}"')))["findings"] == []
    twenty_four = " ".join(["abandon"] * 23 + ["art"])
    assert len(ok("custody_secret_scan", scan(("w", f'mnemonic = "{twenty_four}"')))["findings"]) == 1
    thirteen = " ".join(["abandon"] * 13)
    assert ok("custody_secret_scan", scan(("w", f'mnemonic = "{thirteen}"')))["findings"] == []


def test_secret_scan_encrypted_keystore_is_major_with_the_span_inside_the_file():
    content = '{"version": 3, "crypto": {"cipher": "aes-128-ctr", "ciphertext": "00", "kdf": "scrypt"}}'
    result = ok("custody_secret_scan", scan(("ks.json", content)))
    start = content.index('"ciphertext"')
    assert kinds(result) == [("encrypted-keystore", "ks.json:1", len(content) - start, "major")]
    assert ok("custody_secret_scan", scan(("x.json", '{"ciphertext": "00"}')))["findings"] == []


def test_secret_scan_keystore_span_never_runs_past_a_short_file():
    # a keystore that starts near the head of a 46-character file: the reported span must end at the file end, not beyond it
    head = '{"ciphertext":"00","kdf":"s","p":"'
    content = head + "a" * (46 - len(head) - 2) + '"}'
    assert len(content) == 46 and content.index('"ciphertext"') == 1
    (finding,) = ok("custody_secret_scan", scan(("k.json", content)))["findings"]
    assert finding["id"] == "encrypted-keystore:k.json:1" and finding["length"] == len(content) - 1
    assert 1 + finding["length"] <= len(content)


def test_secret_scan_orders_findings_and_caps_the_list():
    value = bytes(range(32)).hex()
    many = "".join(f"PRIVATE_KEY=0x{value}\n" for _ in range(150))
    result = ok("custody_secret_scan", scan(("b.env", many), ("a.json", '{"ciphertext": "0", "kdf": "x"}')))
    assert result["finding_count"] == 151 and len(result["findings"]) == 100 and result["omitted_findings"] == 51
    assert result["findings"][0]["location"] == "b.env:1" and result["verdict"] == "fail"
    ordered = ok("custody_secret_scan", scan(("b.env", f"PRIVATE_KEY=0x{value}"), ("a.json", '{"ciphertext": "0", "kdf": "x"}')))
    assert [f["severity"] for f in ordered["findings"]] == ["blocker", "major"]


def test_secret_scan_rejects_a_repeated_path():
    invalid("custody_secret_scan", scan(("a", ""), ("a", "")), "files")


# --- independent-review regressions -----------------------------------------------------------------------------------
# Each test below failed against the first implementation (see the review report) and pins a value computed independently.

CANARIES = {
    "aws-key-id": "AKIAIOSFODNN7EXAMPLE",
    "github-token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8",
    "hex-key": "4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318",
    "mnemonic": "bag cat dog elf fox gym hat ink jam key lap map",
    "credentialed-url": "https://user:pa55w0rd@host.example/x",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "bearer": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
}
# identifier fields refuse every canary; a 64-digit hex string is accepted only where it is never echoed (evidence, travel data)
ECHOED = {k: v for k, v in CANARIES.items() if len(v) <= 64}


def _canary_cases(canary: str):
    """(tool, payload, expected INVALID_INPUT field) with the canary in one identifier-like field."""
    dup = [{"signer_id": "a", "entity": "e", "region": "r", "device": "hsm"}] * 2
    return [
        ("custody_multisig_policy", multisig(signers=[{**dup[0], "signer_id": canary}, {**dup[0], "signer_id": canary}]), "signers"),
        ("custody_multisig_policy", multisig(signers=[{**dup[0], "entity": canary}, {**dup[0], "signer_id": "b"}]), "signers"),
        ("custody_multisig_policy", multisig(signers=[{**dup[0], "region": canary}, {**dup[0], "signer_id": "b"}]), "signers"),
        ("custody_shard_distribution", {"threshold": 1, "shards": [{"shard_id": canary, "custodian": "c", "organization": "o", "country": "DE"}] * 2}, "shards"),
        ("custody_shard_distribution", {"threshold": 1, "shards": [{"shard_id": "a", "custodian": canary, "organization": "o", "country": "DE"},
                                                                    {"shard_id": "b", "custodian": canary, "organization": "p", "country": "DE"}]}, "shards"),
        ("custody_shard_distribution", {"threshold": 3, "shards": [{"shard_id": "a", "custodian": "c", "organization": canary, "country": "DE"},
                                                                    {"shard_id": "b", "custodian": "c", "organization": canary, "country": "DE"},
                                                                    {"shard_id": "x", "custodian": "z", "organization": "q", "country": "US"}]}, "shards"),
        ("custody_key_rotation", rotation([{**key(), "key_id": canary}]), "keys"),
        ("custody_sanctions_screening", screen(screening(subject_id=canary)), "screenings"),
        ("custody_sanctions_screening", screen(screening(list_name=canary)), "screenings"),
        ("custody_audit_trail", trail(chain([{"actor": canary}])), "events"),
        ("custody_audit_trail", trail(chain([{"action": canary}]), approval_required_actions=[canary]), "events"),
        ("custody_audit_trail", trail(chain([{"approvers": [canary]}])), "events"),
        ("custody_ceremony_checklist", checklist([done(canary)]), "items"),
    ]


@pytest.mark.parametrize("label", sorted(ECHOED))
def test_secret_shaped_identifiers_are_refused_by_every_tool_and_never_echoed(label):
    canary = ECHOED[label]
    for name, payload, field in _canary_cases(canary):
        result = call(name, payload)
        assert result["state"] == "INVALID_INPUT" and result["field"] == field, (name, result)
        assert canary not in json.dumps(result), (name, result)


@pytest.mark.parametrize("label", sorted(CANARIES))
def test_secret_shaped_evidence_and_party_data_never_appear_in_any_result(label):
    canary = CANARIES[label]
    for name, payload in [
        ("custody_ceremony_checklist", checklist([done("media-sanitized", evidence=canary[:128])])),
        ("custody_travel_rule", transfer(originator_name=canary[:128])),
        ("custody_travel_rule", transfer(beneficiary_account=canary[:128], amount=1)),
        ("custody_travel_rule", transfer(originator_identifier=canary)),
    ]:
        result = call(name, payload)
        assert result["state"] in ("SUCCESS", "INVALID_INPUT") and canary not in json.dumps(result), (name, result)
    for refused in ("aws-key-id", "github-token", "mnemonic", "credentialed-url", "jwt", "bearer"):
        if label == refused:
            invalid("custody_travel_rule", transfer(originator_name=canary[:128]), "originator_name")
            invalid("custody_ceremony_checklist", checklist([done("media-sanitized", evidence=canary[:128])]), "items")


def test_a_sha256_digest_is_accepted_as_evidence_but_not_as_an_identifier():
    digest = CANARIES["hex-key"]
    assert ok("custody_ceremony_checklist", checklist([done("media-sanitized", evidence="sha256:" + digest)]))["completed_items"] == 1
    invalid("custody_key_rotation", rotation([{**key(), "key_id": "0x" + digest}]), "keys")


def test_unknown_item_and_bad_country_messages_do_not_echo_the_input():
    result = invalid("custody_ceremony_checklist", checklist([done("zzz-unlisted-step")]), "items")
    assert "zzz-unlisted-step" not in json.dumps(result) and "media-sanitized" in result["message"]
    result = invalid("custody_shard_distribution", {"threshold": 1, "shards": [{"shard_id": "a", "custodian": "c", "organization": "o", "country": "zz"}]}, "shards")
    assert "zz" not in result["message"]


def test_blank_identifiers_are_refused():
    blank = signers(2)
    blank[0]["entity"] = "  "
    invalid("custody_multisig_policy", multisig(signers=blank), "signers")
    invalid("custody_key_rotation", rotation([{**key(), "key_id": " "}]), "keys")
    invalid("custody_sanctions_screening", screen(screening(subject_id="\t")), "screenings")


def test_secret_scan_never_echoes_a_secret_shaped_path():
    value = bytes(range(32)).hex()
    for label, canary in ECHOED.items():
        if label == "hex-key":
            continue  # a 64-digit hex file name is a content-addressed artifact, not a secret: the path stays readable
        path = canary if label == "mnemonic" else f"keys/{canary}.env"  # a phrase is recognised as the whole value
        result = ok("custody_secret_scan", scan((path, f"PRIVATE_KEY=0x{value}\n")))
        assert canary not in json.dumps(result) and value not in json.dumps(result), label
        assert kinds(result) == [("hex-private-key", "<redacted-path-1>:1", 64, "blocker")], label
        duplicate = invalid("custody_secret_scan", scan((canary, ""), (canary, "")), "files")
        assert canary not in json.dumps(duplicate), label
    digest_path = f"artifacts/{CANARIES['hex-key']}.json"
    assert kinds(ok("custody_secret_scan", scan((digest_path, f"PRIVATE_KEY=0x{value}"))))[0][1] == f"{digest_path}:1"
    ok_path = ok("custody_secret_scan", scan(("keys/ok.env", f"PRIVATE_KEY=0x{value}")))
    assert "keys/ok.env line 1" in ok_path["findings"][0]["summary"]


# audit trail: approvals and required actions are compared on the normalised identity (NFKC, trimmed, case-folded)

@pytest.mark.parametrize("approvers", [["bob", "bob "], ["bob", "BOB"], ["bob", "\uff42ob"], ["bob", "  "], ["bob", "bob\t"]])
def test_audit_one_person_spelled_twice_is_one_approval(approvers):
    result = ok("custody_audit_trail", trail(chain([{"approvers": approvers}])))
    assert sev(result) == {"insufficient-approvals:1": "major"}


@pytest.mark.parametrize("approver", ["alice ", "ALICE", " Alice", "\uff41lice"])
def test_audit_self_approval_is_found_through_spelling_variants(approver):
    result = ok("custody_audit_trail", trail(chain([{"approvers": [approver, "bob", "carol"]}])))
    assert sev(result) == {"self-approval:1": "major"}


@pytest.mark.parametrize("action", ["LIMIT.UPDATE", "limit.update ", " Limit.Update"])
def test_audit_required_actions_match_on_the_normalised_name(action):
    result = ok("custody_audit_trail", trail(chain([{"action": action, "approvers": []}])))
    assert sev(result) == {"insufficient-approvals:1": "major"}


def test_multisig_unicode_variants_of_one_entity_are_one_entity():
    composed, decomposed = "Caf\u00e9 Custody", "Cafe\u0301 Custody"
    result = ok("custody_multisig_policy", multisig(signers=signers(3, entities=[composed, decomposed, "other"])))
    assert result["distinct_entities"] == 2 and result["min_entities_for_quorum"] == 1
    assert sev(result)["entity-controls-quorum"] == "blocker"
    shard = ok("custody_shard_distribution", {"threshold": 2, "shards": shards(custodians=["Zo\u00eb", "Zoe\u0308", "x"], orgs=["O", "O", "P"], n=3)})
    assert shard["min_custodians_for_quorum"] == 1 and sev(shard)["single-custodian-quorum"] == "blocker"


def test_multisig_a_quorum_inside_one_region_is_flagged_but_all_in_one_region_keeps_its_own_finding():
    two_of_three_in_de = ok("custody_multisig_policy", multisig(signers=signers(3, regions=["DE", "de", "US"])))
    assert sev(two_of_three_in_de) == {"region-controls-quorum": "minor"} and two_of_three_in_de["verdict"] == "warn"
    spread = ok("custody_multisig_policy", multisig(signers=signers(3, regions=["DE", "FR", "US"])))
    assert spread["findings"] == []
    one_region = ok("custody_multisig_policy", multisig(signers=signers(3, regions=["DE", "DE", "DE"])))
    assert sev(one_region) == {"single-region": "minor"}


@pytest.mark.parametrize(("signature", "selector"), [
    ("grantRole(bytes32,address)", "2f2ff15d"), ("revokeRole(bytes32,address)", "d547741f"), ("acceptOwnership()", "79ba5097"),
    ("addOwnerWithThreshold(address,uint256)", "0d582f13"), ("removeOwner(address,address,uint256)", "f8dc5dd9"),
    ("swapOwner(address,address,address)", "e318b52b"), ("changeThreshold(uint256)", "694e80c3"), ("enableModule(address)", "610b5925"),
    ("disableModule(address,address)", "e009cfde"), ("setGuard(address)", "e19a9dd9"), ("setFallbackHandler(address)", "f08a0323"),
])
def test_signer_set_and_authority_changing_selectors_are_held_for_review(signature, selector):
    assert mod()._keccak256(signature.encode()).hex()[:8] == selector
    result = ok("custody_tx_policy_eval", tx(selector="0x" + selector))
    assert result["decision"] == "hold" and result["selector_signature"] == signature
    assert sev(result) == {"risky-selector": "major"}


def test_secret_scan_unquoted_prefixed_and_backtick_mnemonic_assignments_are_found():
    phrase = " ".join(["abandon"] * 11 + ["about"])
    cases = {
        "a.env": f"MNEMONIC={phrase}\n", "b.env": f'DEPLOYER_MNEMONIC="{phrase}"\n', "c.env": f"WALLET_SEED_PHRASE='{phrase}'\n",
        "d.yml": f"mnemonic: {phrase}\n", "e.js": f"mnemonic: `{phrase}`,\n", "f.env": f"MNEMONIC_PHRASE={phrase} # dev only\n",
    }
    result = ok("custody_secret_scan", scan(*cases.items()))
    assert [(f["location"], f["length"]) for f in result["findings"]] == [(f"{p}:1", len(phrase)) for p in sorted(cases)]
    assert phrase not in json.dumps(result)
    for ignored in (f"MNEMONIC={phrase} extra words follow here", "MNEMONIC=" + " ".join(["abandon"] * 30), f"note={phrase}"):
        assert ok("custody_secret_scan", scan(("x", ignored)))["findings"] == []


@pytest.mark.parametrize("name", ["PK", "deployer_pk", "SECRET", "client_secret", "SEED", "PRIVKEY", "private_key_hex"])
def test_secret_scan_hex_value_under_more_key_like_names(name):
    value = bytes(range(32)).hex()
    result = ok("custody_secret_scan", scan(("x.env", f"{name}={value}\n")))
    assert kinds(result) == [("hex-private-key", "x.env:1", 64, "blocker")] and value not in json.dumps(result)


@pytest.mark.parametrize("line", ["PKG_NAME=" + "ab" * 32, "SPIKE=" + "ab" * 32, "tx_hash=0x" + "ab" * 32, "HASH_SECRETARY=" + "ab" * 32])
def test_secret_scan_hex_names_do_not_match_inside_other_words(line):
    assert ok("custody_secret_scan", scan(("x", line)))["findings"] == []


@pytest.mark.parametrize(("version", "name"), [("0295b005", "Yprv"), ("02aa7a99", "Zprv"), ("024285b5", "Uprv"), ("02575048", "Vprv"),
                                               ("049d7878", "yprv"), ("04b2430c", "zprv"), ("044a4e28", "uprv"), ("045f18bc", "vprv"), ("04358394", "tprv")])
def test_secret_scan_every_slip132_private_version_is_found(version, name):
    token = xprv(version)
    assert token.startswith(name)
    assert kinds(ok("custody_secret_scan", scan(("k", f"x {token} y")))) == [("extended-private-key", "k:1", len(token), "blocker")]


def test_secret_scan_stays_linear_on_many_hits_in_large_files(monkeypatch):
    # Counted work, not wall time: the scanner may make a fixed number of whole-text searches per file (the END-marker search,
    # the newline index and two keystore probes). A whole-text search per hit would make the count grow with the ~48,700 hits.
    # The wall clock is a backstop.
    import time

    module = importlib.import_module("swarm.tools.a30_tools")
    whole_text_searches = []

    class CountingRe:
        def __getattr__(self, name):
            return getattr(re, name)

        def finditer(self, pattern, string, *args, **kwargs):
            if isinstance(pattern, str) and len(string) >= 100_000:
                whole_text_searches.append(pattern)
            return re.finditer(pattern, string, *args, **kwargs)

        def search(self, pattern, string, *args, **kwargs):
            if isinstance(pattern, str) and len(string) >= 100_000:
                whole_text_searches.append(pattern)
            return re.search(pattern, string, *args, **kwargs)

    monkeypatch.setattr(module, "re", CountingRe())
    pem = "-----BEGIN PRIVATE KEY-----\n" * (1_000_000 // 28)
    keys = ("PRIVATE_KEY=" + "ab12" * 16 + "\n") * (1_000_000 // 77)
    started = time.monotonic()
    result = ok("custody_secret_scan", scan(("a.pem", pem), ("b.env", keys)))
    assert result["files_scanned"] == 2 and result["finding_count"] == (1_000_000 // 28) + (1_000_000 // 77) and len(result["findings"]) == 100
    assert len(json.dumps(result)) < 256 * 1024
    assert whole_text_searches, "the counting double never ran"
    assert len(whole_text_searches) <= 4 * result["files_scanned"], whole_text_searches
    assert time.monotonic() - started < 60, "backstop only: the counted bound above is the check"


def test_travel_rule_below_threshold_still_reports_the_minimal_party_data_fatf_expects():
    # FATF INR.16 para 5(a): below a de minimis threshold the transfer still carries both names and an account or reference each
    result = ok("custody_travel_rule", transfer(amount=10, originator_name="", beneficiary_account="", originator_identifier_kind="none", originator_identifier=""))
    assert result["applies"] is False and result["status"] == "not_applicable" and result["required_fields"] == 0 and result["missing_fields"] == []
    assert sev(result) == {"below-threshold-missing-beneficiary-account": "info", "below-threshold-missing-originator-name": "info"}
    assert ok("custody_travel_rule", transfer(amount=10, originator_identifier_kind="none", originator_identifier=""))["findings"] == []


@pytest.mark.parametrize("line", ["privateKey: `0x{k}`", "'private_key' => '{k}'", "\"private_key\" => \"0x{k}\"", "private_key =\n  '{k}'"])
def test_secret_scan_hex_value_in_backticks_arrows_and_wrapped_assignments(line):
    value = bytes(range(32)).hex()
    result = ok("custody_secret_scan", scan(("x.rb", line.format(k=value))))
    assert [(f["kind"], f["length"]) for f in result["findings"]] == [("hex-private-key", 64)] and value not in json.dumps(result)


def test_rotation_never_reports_negative_zero_days():
    late = ok("custody_key_rotation", rotation([key(age_days=365, extra_seconds=1)], warn_days=0))
    row = late["keys"][0]
    assert row["status"] == "overdue" and row["days_remaining"] == 0.0 and '"days_remaining": 0.0' in json.dumps(late) and "-0.0" not in json.dumps(late)


def _leaf_paths(value, schema, path=()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaf_paths(item, (schema.get("properties") or {}).get(key, {}), path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _leaf_paths(item, schema.get("items") or {}, path + (index,))
    elif isinstance(value, str) and "enum" not in schema and "const" not in schema:
        yield path, schema


def _with(value, path, new):
    if not path:
        return new
    copy_ = copy.deepcopy(value)
    node = copy_
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = new
    return copy_


FUZZ_CANARIES = {
    "aws": "AKIAIOSFODNN7EXAMPLE", "github": "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8", "hex": CANARIES["hex-key"],
    "0xhex": "0x" + CANARIES["hex-key"], "url": "https://user:pa55w0rd@host.example/x", "words": CANARIES["mnemonic"],
    "pem": "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0w\n-----END PRIVATE KEY-----",
}


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_no_tool_echoes_a_secret_placed_in_any_free_text_field(name):
    tool = next(t for t in lane_tools() if t["name"] == name)
    sample = tool["examples"]["success"]
    checked = 0
    for path, schema in _leaf_paths(sample, tool["input_schema"]):
        for label, canary in FUZZ_CANARIES.items():
            limit = schema.get("maxLength")
            if limit is not None and len(canary) > limit:
                continue
            result = call(name, _with(sample, path, canary))
            assert canary not in json.dumps(result), (name, path, label, result)
            assert result["state"] in ("SUCCESS", "INVALID_INPUT"), (name, path, label, result)
            checked += 1
    assert checked > 0 or name in {"custody_timelock_check", "custody_signer_policy_lint", "custody_wallet_tier_caps"}


@pytest.mark.parametrize(("token", "kind"), [
    # BIP-32 test vectors 1 and 2 (chain m) and the Bitcoin wiki WIF example: published values, independent of this module's builders
    ("xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi", "extended-private-key"),
    ("xprv9s21ZrQH143K25QhxbucbDDuQ4naNntJRi4KUfWT7xo4EKsHt2QJDu7KXp1A3u7Bi1j8ph3EGsZ9Xvz9dGuVrtHHs7pXeTzjuxBrCmmhgC6", "extended-private-key"),
    ("5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ", "wif-private-key"),
])
def test_secret_scan_finds_published_key_vectors_and_ignores_the_public_extended_key(token, kind):
    assert kinds(ok("custody_secret_scan", scan(("k", f"value = {token}\n")))) == [(kind, "k:1", len(token), "blocker")]
    xpub = "xpub661MyMwAqRbcEZVB4dScxMAdx6d4nFc9nvyvH3v4gJL378CSRZiYmhRoP7mBy6gSPSCYk6SzXPTf3ND1cZAceL7SfJ1Z3GC8vBgp2epUt13"
    assert ok("custody_secret_scan", scan(("k", xpub)))["findings"] == []


def test_secret_scan_pem_header_tolerates_repeated_and_tab_separators():
    body = "-----BEGIN  RSA PRIVATE KEY-----\nMIIE\n-----END  RSA PRIVATE KEY-----"
    tabbed = "-----BEGIN\tEC PRIVATE KEY -----\nMIIE\n-----END\tEC PRIVATE KEY -----"
    result = ok("custody_secret_scan", scan(("k.pem", body + "\n"), ("t.pem", tabbed + "\n")))
    assert sorted(kinds(result)) == sorted([
        ("pem-private-key", "k.pem:1", len(body), "blocker"), ("pem-private-key", "t.pem:1", len(tabbed), "blocker"),
    ])


def test_secret_scan_mnemonic_phrase_split_over_lines_commas_or_a_json_array_is_found():
    words = ["abandon"] * 11 + ["about"]
    phrase = " ".join(words)
    split = " ".join(words[:6]) + "\n" + " ".join(words[6:])
    comma = ", ".join(words)
    array = '{"mnemonic": ["' + '", "'.join(words) + '"]}'
    result = ok("custody_secret_scan", scan(
        ("s.env", f"MNEMONIC={split}\n"), ("c.txt", f"mnemonic: {comma}\n"), ("k.json", array),
        ("p.txt", f"mnemonic: {phrase}\nthe quick brown fox jumps\n"),
    ))
    assert sorted(kinds(result)) == sorted([
        ("mnemonic-phrase", "s.env:1", len(split), "blocker"),
        ("mnemonic-phrase", "c.txt:1", len(comma), "blocker"),
        ("mnemonic-phrase", "k.json:1", len('", "'.join(words)), "blocker"),
        ("mnemonic-phrase", "p.txt:1", len(phrase), "blocker"),
    ])
    assert "abandon" not in json.dumps(result)


def test_secret_scan_mnemonic_search_is_bounded_on_long_word_runs(monkeypatch):
    # Counted work, not wall time: each mnemonic candidate may try at most one cut per line break of a 24-word phrase, and
    # each cut reads at most 24 words. An unbounded phrase or a re-read per cut exceeds these counts. The wall clock is a backstop.
    import time

    module = importlib.import_module("swarm.tools.a30_tools")
    word_passes, words_read = [], []
    real_word_re = module._MNEMONIC_WORD_RE

    class CountingWords:
        def __getattr__(self, name):
            return getattr(real_word_re, name)

        def finditer(self, string, pos=0, endpos=None):
            word_passes.append(string)
            for match in real_word_re.finditer(string, pos, len(string) if endpos is None else endpos):
                words_read.append(match)
                yield match

    monkeypatch.setattr(module, "_MNEMONIC_WORD_RE", CountingWords())
    words = ["abandon"] * 5000
    same_line = [" ".join(words), "mnemonic: " + " ".join(words), "mnemonic: abandon" + " " * 100_000 + "abandon",
                 "mnemonic: " + ("abandon" + " " * 31) * 500]
    started = time.monotonic()
    for text in same_line:
        assert ok("custody_secret_scan", scan(("w.txt", text)))["findings"] == []
    # one word per line: the first 24 words are a valid run and are reported; the scan must still finish promptly
    assert ok("custody_secret_scan", scan(("w.txt", "mnemonic:\n" + "\n".join(words))))["files_scanned"] == 1
    candidates = len(same_line) + 1
    assert word_passes, "the counting double never ran"
    assert len(word_passes) <= 24 * candidates, len(word_passes)
    assert len(words_read) <= 24 * 24 * candidates, len(words_read)
    assert time.monotonic() - started < 60, "backstop only: the counted bounds above are the check"
