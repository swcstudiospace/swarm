"""sync_queue: offline enqueue, FIFO replay, transient-only retry, DLQ/redrive."""
import pytest
from swarm.errors import ErrorCode, SwarmError
from swarm.sync_queue import SyncQueue, backoff_seconds


def _intent(key, corr="c1", phase="9"):
    return {"idempotency_key": key, "phase_id": phase, "op": "create_primary",
            "tracker": "linear", "correlation_id": corr}


@pytest.fixture
def queue(tmp_path):
    return SyncQueue(tmp_path / "tasks.db")


def test_offline_enqueue_succeeds_then_fifo_replay(queue):
    queue.enqueue(_intent("k1"))
    queue.enqueue(_intent("k2"))
    assert [i["idempotency_key"] for i in queue.pending()] == ["k1", "k2"]
    seen = []
    res = queue.deliver_all(lambda it: seen.append(it["idempotency_key"]))
    assert seen == ["k1", "k2"]  # FIFO order
    assert all(r["outcome"] == "delivered" for r in res) and queue.pending() == []


def test_enqueue_same_key_dedupes(queue):
    a = queue.enqueue(_intent("k1"))
    b = queue.enqueue(_intent("k1"))
    assert b["deduped"] is True and a["intent_id"] == b["intent_id"]
    assert len(queue.pending()) == 1


def test_transient_retry_then_success_with_backoff(queue):
    queue.enqueue(_intent("k1"))
    calls = {"n": 0}
    def transport(it):
        calls["n"] += 1
        if calls["n"] < 4:
            raise SwarmError(ErrorCode.E_TIMEOUT, "slow", field="transport")
    res = queue.deliver_all(transport)
    assert res[0]["outcome"] == "delivered" and res[0]["attempts"] == 4
    assert len(res[0]["backoffs"]) == 3 and res[0]["backoffs"][0] < res[0]["backoffs"][1] + 0.3
    assert calls["n"] == 4


def test_transient_exhaust_goes_to_dlq_and_redrive_redelivers(queue):
    queue.enqueue(_intent("k1"))
    def always(it):
        raise SwarmError(ErrorCode.E_TIMEOUT, "down", field="transport")
    res = queue.deliver_all(always)
    assert res[0]["outcome"] == "dead_lettered" and res[0]["attempts"] == 5
    dlq = queue.list_dead_letter()
    assert len(dlq) == 1 and dlq[0]["entry_state"] == "pending_redrive"
    # redrive: same key, new attempt series
    queue.redrive(dlq[0]["dlq_id"])
    assert queue.list_dead_letter() == [] and len(queue.pending()) == 1
    seen = []
    res2 = queue.deliver_all(lambda it: seen.append(it["idempotency_key"]))
    assert res2[0]["outcome"] == "delivered" and seen == ["k1"]


def test_nontransient_dlqs_on_first_attempt_no_retry(queue):
    queue.enqueue(_intent("k1"))
    calls = {"n": 0}
    def bad(it):
        calls["n"] += 1
        raise SwarmError(ErrorCode.E_INPUT, "bad body", field="body")
    res = queue.deliver_all(bad)
    assert calls["n"] == 1 and res[0]["outcome"] == "dead_lettered" and res[0]["backoffs"] == []


def test_discard_requires_user(queue):
    queue.enqueue(_intent("k1"))
    queue.deliver_all(lambda it: (_ for _ in ()).throw(
        SwarmError(ErrorCode.E_INPUT, "bad", field="body")))
    dlq = queue.list_dead_letter()[0]
    with pytest.raises(SwarmError):
        queue.discard(dlq["dlq_id"], approved_by="agent")
    got = queue.discard(dlq["dlq_id"], approved_by="user")
    assert got["entry_state"] == "discarded" and queue.list_dead_letter() == []


def test_backoff_is_deterministic_bounded():
    b1 = backoff_seconds(1, "k1")
    b2 = backoff_seconds(1, "k1")
    assert b1 == b2 and 1.0 <= b1 < 1.3
    assert backoff_seconds(10, "k1") < 30.3  # capped
