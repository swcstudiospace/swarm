"""issue_mapper: create/update idempotency, TBD, drift/orphan/conflict."""
import pytest
from swarm.errors import SwarmError
from swarm.fake_tracker import fake_id_for
from swarm.issue_mapper import IssueMapper, classify_mapping, derive_idempotency_key, find_orphans


@pytest.fixture
def mapper(tmp_path):
    return IssueMapper(tmp_path / "tasks.db")


@staticmethod
def _create(m, phase="14.4", corr="c1", k=0, body=None):
    kw = {"phase_id": phase, "correlation_id": corr, "body": body or {"title": "x"}}
    if k:
        return m.map_issue(op="create_child", child_index=k, **kw)
    return m.map_issue(op="create_primary", **kw)


def test_create_replay_is_deduped_no_new_row(mapper):
    a = _create(mapper)
    b = _create(mapper)
    assert b["deduped"] is True and a["primary_fake_id"] == b["primary_fake_id"]
    assert a["tbd_real_id"] is None  # TBD handling: fakes only
    assert len(mapper.list_maps()) == 1


def test_create_children_are_one_to_many_and_deterministic(mapper):
    _create(mapper)
    _create(mapper, k=1)
    _create(mapper, k=2)
    got = mapper.get_map("14.4")
    assert got["child_fake_ids"] == ["TBD-LIN-v24-14.4-W01", "TBD-LIN-v24-14.4-W02"]
    assert fake_id_for("14.4", child_index=1) == "TBD-LIN-v24-14.4-W01"
    # re-run whole mapping is a no-op
    again = _create(mapper, k=1)
    assert again["deduped"] is True
    assert len(mapper.list_maps()) == 1


def test_update_replay_same_key_new_body_new_key_same_id(mapper):
    _create(mapper)
    u1 = mapper.map_issue(phase_id="14.4", op="update_primary", correlation_id="c1",
                          body={"status_fake": "in_progress"})
    u2 = mapper.map_issue(phase_id="14.4", op="update_primary", correlation_id="c1",
                          body={"status_fake": "in_progress"})
    assert u2["deduped"] is True and u1["primary_fake_id"] == u2["primary_fake_id"]
    u3 = mapper.map_issue(phase_id="14.4", op="update_primary", correlation_id="c1",
                          body={"status_fake": "done"})
    assert u3["idempotency_keys"]["update_primary"] != u1["idempotency_keys"]["update_primary"]
    assert u3["primary_fake_id"] == u1["primary_fake_id"]


def test_key_stable_and_attempt_free():
    a = derive_idempotency_key(correlation_id="c", phase_id="6", op="create_primary",
                               subject="TBD-LIN-v24-6-P", body={"t": "x"})
    b = derive_idempotency_key(correlation_id="c", phase_id="6", op="create_primary",
                               subject="TBD-LIN-v24-6-P", body={"t": "x"})
    assert a == b and len(a) == 64


def test_conflict_vs_drift_classification():
    # Linear done vs STATE gaps_found = conflict (park for user ruling)
    assert classify_mapping("gaps_found", "done") == "conflict"
    # Linear open vs STATE passed = drift (mirror lags)
    assert classify_mapping("passed", "open") == "drift"
    assert classify_mapping("gaps_found", "open") == "ok"


def test_orphans_reported_never_deleted(mapper):
    _create(mapper, phase="6")
    orphans = find_orphans(mapped_fake_ids={"TBD-LIN-v24-6-P"},
                           tracker_fake_ids={"TBD-LIN-v24-6-P", "TBD-LIN-0099"},
                           phases={"6", "9"}, mapped_phases={"6"})
    kinds = {(o["field"], o["a_value"]) for o in orphans}
    assert ("fake_id", "TBD-LIN-0099") in kinds and ("phase_id", "9") in kinds
    assert all(o["disposition"] == "reported" for o in orphans)
    assert mapper.get_map("6") is not None  # nothing deleted


def test_real_id_requires_user_approval(mapper):
    _create(mapper, phase="6")
    with pytest.raises(SwarmError):
        mapper.record_real_id("6", "LIN-1", approved_by="agent")
    got = mapper.record_real_id("6", "LIN-1", approved_by="user")
    assert got["tbd_real_id"] == "LIN-1"
