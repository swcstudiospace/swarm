"""U12.2(4) / Phase 12: replay-after-failover dedup on the durable join ledger.

After a failover the replacement worker replays joins it may already have
emitted. ``swarm.join_ledger.emit`` appends at most once per ledger key
(flock-protected read-check-append), so a replay returns ``"deduplicated"``
and writes nothing. These tests exercise that contract through the public
``emit`` / ``read_ledger`` API only, on an isolated ``tmp_path`` ledger.
"""
from swarm.join_ledger import emit, ledger_path_for, read_ledger

KEY = {"task_id": "task-1", "stage": 2, "join_seq": 7, "attempt": 1}
KEY2 = {"task_id": "task-2", "stage": 2, "join_seq": 8, "attempt": 1}
VERDICT = {"verdict": "pass", "code": None, "detail": None}


def rec(task_id="task-1", stage=2, depth=0, creation=0, outcome=None):
    return {
        "key": {"stage": stage, "depth": depth, "creation": creation, "task_id": task_id},
        "outcome": outcome if outcome is not None else {"kind": "completed"},
        "provenance": {"producer": "worker-a", "attempt": 1, "verdict": dict(VERDICT)},
    }


def test_replay_after_failover_deduplicates_same_key(tmp_path):
    path = ledger_path_for(tmp_path, "corr-replay")
    assert emit(path, KEY, [rec()]) == "collected"
    # Failover replay of the same ledger key: no second line is written.
    replay = [rec(outcome={"kind": "failed", "code": "E_LATE", "message": "replayed"})]
    assert emit(path, KEY, replay) == "deduplicated"
    assert path.read_bytes().count(b"\n") == 1
    assert read_ledger(path) == [(KEY, [rec()])]


def test_two_different_keys_both_collect(tmp_path):
    path = ledger_path_for(tmp_path, "corr-two-keys")
    assert emit(path, KEY, [rec(task_id="task-1")]) == "collected"
    assert emit(path, KEY2, [rec(task_id="task-2")]) == "collected"
    assert path.read_bytes().count(b"\n") == 2
    assert read_ledger(path) == [
        (KEY, [rec(task_id="task-1")]),
        (KEY2, [rec(task_id="task-2")]),
    ]


def test_restart_equivalent_read_back_preserves_order_and_dedup(tmp_path):
    path = ledger_path_for(tmp_path, "corr-restart")
    assert emit(path, KEY, [rec(task_id="task-1")]) == "collected"
    assert emit(path, KEY2, [rec(task_id="task-2")]) == "collected"
    before = path.read_bytes()
    # Restart-equivalent: no in-memory state is carried over; the ledger on
    # disk alone decides. Pairs come back in file order and replays dedupe.
    assert read_ledger(path) == [
        (KEY, [rec(task_id="task-1")]),
        (KEY2, [rec(task_id="task-2")]),
    ]
    assert emit(path, KEY, [rec(task_id="task-1")]) == "deduplicated"
    assert emit(path, KEY2, [rec(task_id="task-2")]) == "deduplicated"
    assert path.read_bytes() == before
