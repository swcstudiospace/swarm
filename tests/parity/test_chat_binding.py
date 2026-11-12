"""Chat-thread binding through the real ``swarm._native`` extension (Phase 14.6 CR-04).

Only the cases that are deterministic from Python run here. The Python binding runs only the built-in executor, which holds for
no time, so queue-full refusal, fairness across threads, the queued and in-flight cancels and an abandoned shutdown cannot be
made to happen on purpose from Python (DEC-5). Those are covered by the Rust tests in ``gsd-pool``. No test sleeps: waits use
ticket timeouts only.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest

native = pytest.importorskip("swarm._native")
if not hasattr(native, "ChatPool"):  # pragma: no cover - the extension always exports it
    pytest.skip("swarm._native does not export ChatPool", allow_module_level=True)

WAIT_MS = 5000
FLOOD = 50
PAIR_LISTS = ("flushed", "dropped", "abandoned")


def _index(msg_id: str) -> int:
    return int(msg_id[1:])


def test_invalid_config_is_refused_with_e_input() -> None:
    with pytest.raises(ValueError, match=r"^E-INPUT: "):
        native.ChatPool(native.Governor(), workers=0)


@pytest.mark.parametrize("grace_ms", [10000, 0])
def test_shutdown_report_keeps_per_thread_fifo(grace_ms: int) -> None:
    pool = native.ChatPool(native.Governor(), per_thread_queue=128)
    pool.open_thread("a", "w")
    tickets: dict[str, Any] = {}
    for index in range(FLOOD):
        msg_id = f"m{index}"
        outcome, _seq, ticket = pool.submit("a", msg_id, "s", b"x")
        assert outcome == "enqueued"
        tickets[msg_id] = ticket
    report = pool.shutdown(grace_ms)

    ids = [f"m{index}" for index in range(FLOOD)]
    flushed = [msg for _thread, msg in report["flushed"]]
    dropped = [msg for _thread, msg in report["dropped"]]
    assert flushed == sorted(flushed, key=_index), "flushed must be in submission order"
    assert dropped == sorted(dropped, key=_index), "dropped must be in submission order"
    if flushed and dropped:
        assert max(map(_index, flushed)) < min(map(_index, dropped)), "every flushed id precedes every dropped id"

    statuses = {msg: tickets[msg].wait(WAIT_MS) for msg in ids}
    for msg, result in statuses.items():
        assert result["status"] != "pending", f"{msg} is not terminal after shutdown"
    for msg in flushed:
        assert statuses[msg]["status"] == "ok"
    for msg in dropped:
        assert statuses[msg]["status"] == "failed"
        assert statuses[msg]["code"] == "E-CAPACITY"
    # One thread runs one message at a time in FIFO order, so the completed messages are a prefix of submission order.
    ordered = [statuses[msg]["status"] for msg in ids]
    completed = ordered.count("ok")
    assert ordered == ["ok"] * completed + ["failed"] * (FLOOD - completed)


def test_dedupe_returns_already_accepted_with_the_first_seq() -> None:
    pool = native.ChatPool(native.Governor())
    pool.open_thread("a", "w")
    first_outcome, first_seq, first_ticket = pool.submit("a", "m1", "s", b"x")
    second_outcome, second_seq, second_ticket = pool.submit("a", "m1", "s", b"x")
    assert first_outcome == "enqueued"
    assert first_ticket is not None
    assert second_outcome == "already_accepted"
    assert second_ticket is None
    assert second_seq == first_seq
    pool.shutdown(10000)


def test_capacity_refuses_the_thread_past_max_threads() -> None:
    pool = native.ChatPool(native.Governor(), max_threads=1)
    pool.open_thread("a", "w")
    with pytest.raises(ValueError, match=r"^E-CAPACITY: "):
        pool.open_thread("b", "w")
    pool.shutdown(10000)


def test_duplicate_open_at_full_capacity_is_refused_with_e_input() -> None:
    pool = native.ChatPool(native.Governor(), max_threads=1)
    pool.open_thread("a", "w")
    with pytest.raises(ValueError, match=r"^E-INPUT: "):
        pool.open_thread("a", "w")
    pool.shutdown(10000)


def test_budget_refusal_raises_e_timeout_and_the_wire_call_grants_nothing() -> None:
    gov = native.Governor()
    within = gov.reserve_budget_checked("s1", "t1", 1, 0, 0, 0.0)
    assert within["granted"] is True
    assert gov.budget_snapshot("s1")["tokens_used"] == 1

    # The default session budget is 100 000 tokens, so this reservation is refused.
    with pytest.raises(ValueError, match=r"^E-TIMEOUT: "):
        gov.reserve_budget_checked("s1", "t1", 100001, 0, 0, 0.0)
    wire = gov.reserve_budget("s1", "t1", 100001, 0, 0, 0.0)
    assert wire["granted"] is False
    assert wire["tokens_granted"] == 0
    assert gov.budget_snapshot("s1")["tokens_used"] == 1


def test_cancel_after_completion_is_an_unknown_message() -> None:
    pool = native.ChatPool(native.Governor())
    pool.open_thread("a", "w")
    _outcome, _seq, ticket = pool.submit("a", "m1", "s", b"x")
    result = ticket.wait(WAIT_MS)
    assert result["status"] == "ok"
    assert result["payload"] == b"x"
    assert result["code"] is None
    with pytest.raises(ValueError, match=r"^E-INPUT: "):
        pool.cancel_message("a", "m1")
    pool.shutdown(10000)


def test_shutdown_reports_each_pair_once_and_fails_every_dropped_ticket() -> None:
    pool = native.ChatPool(native.Governor())
    pool.open_thread("A", "w")
    pool.open_thread("B", "w")
    tickets: dict[tuple[str, str], Any] = {
        ("A", "m1"): pool.submit("A", "m1", "s", b"a1")[2],
        ("B", "m1"): pool.submit("B", "m1", "s", b"b1")[2],
        ("A", "m2"): pool.submit("A", "m2", "s", b"a2")[2],
    }
    report = pool.shutdown(10000)

    pairs = [tuple(pair) for key in PAIR_LISTS for pair in report[key]]
    assert all(count == 1 for count in Counter(pairs).values()), "a pair appears in more than one list"
    assert set(pairs) <= set(tickets)
    for pair, ticket in tickets.items():
        assert ticket is not None
        assert ticket.wait(WAIT_MS)["status"] != "pending", f"{pair} is not terminal after shutdown"
    for thread, msg in report["dropped"]:
        result = tickets[(thread, msg)].wait(WAIT_MS)
        assert result["status"] == "failed"
        assert result["code"] == "E-CAPACITY"



def test_shutdown_report_has_exactly_the_four_lists() -> None:
    pool = native.ChatPool(native.Governor())
    report = pool.shutdown(10000)
    assert report == {"flushed": [], "dropped": [], "abandoned": [], "cancelled": []}


def test_open_thread_after_shutdown_is_refused_with_e_capacity() -> None:
    pool = native.ChatPool(native.Governor())
    pool.shutdown(10000)
    with pytest.raises(ValueError, match=r"^E-CAPACITY: "):
        pool.open_thread("a", "w")


def test_zero_drop_grace_is_refused_with_e_input() -> None:
    with pytest.raises(ValueError, match=r"^E-INPUT: "):
        native.ChatPool(native.Governor(), drop_grace_ms=0)


def test_dedupe_window_not_above_per_thread_queue_is_refused_from_python() -> None:
    with pytest.raises(ValueError, match=r"^E-INPUT: "):
        native.ChatPool(native.Governor(), per_thread_queue=8, dedupe_window=8)


def test_pool_refuses_more_workers_than_the_bound() -> None:
    with pytest.raises(ValueError, match=r"^E-INPUT: "):
        native.ChatPool(native.Governor(), workers=257)


def test_leaving_the_with_block_shuts_the_pool_down() -> None:
    with native.ChatPool(native.Governor()) as pool:
        pool.open_thread("a", "w")
    with pytest.raises(ValueError, match=r"^E-CAPACITY: "):
        pool.submit("a", "m1", "s", b"x")


def test_exception_in_the_with_block_propagates_and_the_pool_is_shut_down() -> None:
    pool = native.ChatPool(native.Governor())
    with pytest.raises(RuntimeError, match="boom"):
        with pool:
            pool.open_thread("a", "w")
            raise RuntimeError("boom")
    with pytest.raises(ValueError, match=r"^E-CAPACITY: "):
        pool.submit("a", "m1", "s", b"x")
