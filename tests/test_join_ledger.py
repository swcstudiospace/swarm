"""Durable join ledger: dedup on replay, read-back, malformed-line refusal, empty-log refusal, outcome round trip."""
import pytest

from swarm.join_ledger import emit, ledger_path_for, read_ledger

KEY = {"task_id": "task-1", "stage": 2, "join_seq": 7, "attempt": 1}
VERDICT = {"verdict": "pass", "code": None, "detail": None}


def rec(task_id="task-1", stage=2, depth=0, creation=0, outcome=None):
    return {
        "key": {"stage": stage, "depth": depth, "creation": creation, "task_id": task_id},
        "outcome": outcome if outcome is not None else {"kind": "completed"},
        "provenance": {"producer": "worker-a", "attempt": 1, "verdict": dict(VERDICT)},
    }


def test_replay_after_failover_is_deduplicated(tmp_path):
    path = ledger_path_for(tmp_path, "corr-replay")
    assert emit(path, KEY, [rec()]) == "collected"
    replay = [rec(outcome={"kind": "failed", "code": "E_LATE", "message": "replayed"})]
    assert emit(path, KEY, replay) == "deduplicated"
    assert path.read_bytes().count(b"\n") == 1


def test_read_ledger_returns_key_and_sorted_log(tmp_path):
    path = ledger_path_for(tmp_path, "corr-read")
    log = [rec(task_id="b", creation=1), rec(task_id="a", creation=0)]
    assert emit(path, KEY, log) == "collected"
    expected_log = [rec(task_id="a", creation=0), rec(task_id="b", creation=1)]
    assert read_ledger(path) == [(KEY, expected_log)]


def test_malformed_existing_line_raises_and_leaves_file_unchanged(tmp_path):
    path = ledger_path_for(tmp_path, "corr-malformed")
    assert emit(path, KEY, [rec()]) == "collected"
    with open(path, "ab") as fh:
        fh.write(b"{not json\n")
    before = path.read_bytes()
    other_key = {"task_id": "task-2", "stage": 2, "join_seq": 8, "attempt": 1}
    with pytest.raises(ValueError):
        emit(path, other_key, [rec(task_id="task-2")])
    assert path.read_bytes() == before
    with pytest.raises(ValueError):
        read_ledger(path)


def test_empty_log_raises_and_creates_no_file(tmp_path):
    path = ledger_path_for(tmp_path, "corr-empty")
    with pytest.raises(ValueError):
        emit(path, KEY, [])
    assert not path.exists()


def test_outcome_kinds_round_trip(tmp_path):
    path = ledger_path_for(tmp_path, "corr-outcomes")
    log = [
        rec(task_id="a", creation=0, outcome={"kind": "completed"}),
        rec(task_id="b", creation=1, outcome={"kind": "rate_limited", "retry_after_ns": None}),
        rec(task_id="c", creation=2, outcome={"kind": "rate_limited", "retry_after_ns": 1_500_000_000}),
        rec(task_id="d", creation=3, outcome={"kind": "failed", "code": "E_BOOM", "message": "boom"}),
    ]
    assert emit(path, KEY, log) == "collected"
    assert read_ledger(path) == [(KEY, log)]


@pytest.mark.parametrize("bad", ["", ".hidden", "..", "a/b", "a b", "x\n", "a" * 129, "é"])
def test_invalid_correlation_raises(tmp_path, bad):
    with pytest.raises(ValueError):
        ledger_path_for(tmp_path, bad)


def test_valid_correlation_maps_under_joins_directory(tmp_path):
    assert ledger_path_for(tmp_path, "a.b_c-d") == tmp_path / "joins" / "a.b_c-d.jsonl"
    assert ledger_path_for(tmp_path, "a" * 128).name == "a" * 128 + ".jsonl"
