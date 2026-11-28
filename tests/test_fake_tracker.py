"""fake_tracker: hard-deny live, create-or-return, lost-reply, read-only Notion."""
import pytest
from swarm.errors import SwarmError
from swarm.fake_tracker import FakeNotionAdapter, FakeTrackerAdapter, fake_id_for


def test_live_urls_denied_before_any_transport():
    for url in ("https://linear.app/x", "https://notion.example/page", "https://api.linear.app", ""):
        with pytest.raises(SwarmError) as ei:
            FakeTrackerAdapter(url)
        assert ei.value.code.value == "E-DEP"
    with pytest.raises(SwarmError):
        FakeNotionAdapter("https://app.notion.com/x")


def test_create_or_return_idempotent_and_lost_reply():
    t = FakeTrackerAdapter("fake://linear")
    _ = t.create_or_return(idempotency_key="k1", fake_id=fake_id_for("9"), body={"status_fake": "open"})
    b = t.create_or_return(idempotency_key="k1", fake_id=fake_id_for("9"), body={"status_fake": "open"})
    assert b["deduped"] is True and len(t.list_all()) == 1
    # lost reply: stored once, retry still dedupes
    t2 = FakeTrackerAdapter("fake://linear", lost_reply=True)
    with pytest.raises(SwarmError):
        t2.create_or_return(idempotency_key="k2", fake_id=fake_id_for("9"), body={})
    got = t2.create_or_return(idempotency_key="k2", fake_id=fake_id_for("9"), body={})
    assert got["deduped"] is True and len(t2.list_all()) == 1


def test_offline_transport_is_transient_dep():
    t = FakeTrackerAdapter("fake://linear", online=False)
    with pytest.raises(SwarmError) as ei:
        t.create_or_return(idempotency_key="k", fake_id=fake_id_for("9"), body={})
    assert ei.value.code.value == "E-DEP"
    # offline does not break reads/seeds (reconciliation can still run)
    t.seed("TBD-LIN-0099", status_fake="open")
    assert t.get("TBD-LIN-0099")["status_fake"] == "open"


def test_update_unknown_fake_id_is_input_error():
    t = FakeTrackerAdapter("fake://linear")
    with pytest.raises(SwarmError) as ei:
        t.update(idempotency_key="k", fake_id="TBD-LIN-v24-9-P", body={"status_fake": "done"})
    assert ei.value.code.value == "E-INPUT"


def test_notion_is_read_only():
    n = FakeNotionAdapter("fake://notion")
    n.add_page("p", "ctx")
    assert n.read_page("p") == "ctx"
    with pytest.raises(SwarmError) as ei:
        n.write_page("p", "x")
    assert ei.value.code.value == "E-POLICY"
