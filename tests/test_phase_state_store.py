"""phase_state_store: guarded transitions, atomic STATE file, conflict/drift."""
import pytest
from swarm.errors import SwarmError
from swarm.phase_state_store import PhaseStateStore, parse_frontmatter, serialize_state_md


@pytest.fixture
def store(tmp_path):
    return PhaseStateStore(tmp_path / "tasks.db")


@pytest.fixture
def digest():
    return "a" * 64


def test_happy_path_passed_requires_digest_then_succeeds(store, digest):
    store.put_phase_state({"phase_id": "9", "stage": "verification", "verdict": "unverified",
                           "correlation_id": "c1"})
    with pytest.raises(SwarmError):
        store.transition_phase("9", "passed", correlation_id="c1")
    got = store.transition_phase("9", "passed", correlation_id="c1", evidence_digest=digest)
    assert got["verdict"] == "passed"
    assert store.get_phase_state("9")["evidence"]["verifier_digest"] == digest


def test_debt_requires_user_acceptance(store, digest):
    store.put_phase_state({"phase_id": "9", "stage": "verification", "verdict": "unverified",
                           "correlation_id": "c1"})
    with pytest.raises(SwarmError):
        store.transition_phase("9", "passed_with_recorded_debt", correlation_id="c1",
                               evidence_digest=digest)
    got = store.transition_phase(
        "9", "passed_with_recorded_debt", correlation_id="c1", evidence_digest=digest,
        debt=[{"item": "x", "owner": "a", "accepted_by": "user", "accepted_at": "2026-01-01T00:00:00Z"}])
    assert got["debt"][0]["accepted_by"] == "user"


def test_illegal_transition_is_e_contract(store):
    store.put_phase_state({"phase_id": "9", "stage": "executing", "verdict": "unverified",
                           "correlation_id": "c1"})
    with pytest.raises(SwarmError) as ei:
        store.transition_phase("9", "passed", correlation_id="c1")
    assert ei.value.code.value == "E-CONTRACT"


def test_conflict_user_ruling_required_to_leave_hold(store, digest):
    store.put_phase_state({"phase_id": "14.4", "stage": "blocked_hold", "verdict": "gaps_found",
                           "correlation_id": "c1", "holds": [{"kind": "blocked_hold", "reason": "B2"}]})
    with pytest.raises(SwarmError):
        store.transition_phase("14.4", "verification", correlation_id="c1")
    got = store.transition_phase("14.4", "verification", correlation_id="c1", ruling_ref="ruling-1")
    assert got["evidence"]["ruling_ref"] == "ruling-1"


def test_atomic_state_file_roundtrip_and_orphan_temp_hygiene(store, tmp_path):
    p = tmp_path / "STATE.md"
    store.write_state(p, {"status": "gaps_found", "percent": "33", "__body__": "# State\nbody\n"})
    text = p.read_text()
    front, body = parse_frontmatter(text)
    assert front["status"] == "gaps_found" and front["percent"] == "33"
    assert body.startswith("# State")
    # no orphan temp files left behind (atomic publish hygiene)
    assert list(tmp_path.glob(".*.tmp")) == []
    # drift signal: hand-edited percent mismatch is visible to readers
    assert parse_frontmatter(serialize_state_md({"percent": "39"}, body))[0]["percent"] == "39"
