"""AgentScript.record_external_reply (14.3 D1): a reply another engine produced gets the run-log record `main` writes."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from collections.abc import Callable
from typing import Any

import pytest
from conftest import ROOT

from swarm import substrate_tee
from swarm.errors import ErrorCode, SwarmError
from swarm.script_base import AgentScript, Ctx

# fields that differ between two runs of the same call
StateDirs = Callable[[str], Path]
VOLATILE = ("ts", "msg_id")
DEFAULTED = ("agent", "script", "status")


def _orch_status_script() -> AgentScript:
    spec = importlib.util.spec_from_file_location("orch_status_under_test", ROOT / "scripts" / "orch_status.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return AgentScript("A01", "orch_status", mod.run, add_args=mod.add_args)


def _toy_script() -> AgentScript:
    def run(args: argparse.Namespace, ctx: Ctx) -> dict[str, Any]:
        return {"summary": f"item {args.item}"}

    def add_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--item", default="none")

    return AgentScript("A99", "toy", run, add_args=add_args)


def _records(state_dir: Path) -> list[dict[str, Any]]:
    log = state_dir / "events.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _stable(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in r.items() if k not in VOLATILE} for r in records]


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StateDirs:
    """Points SWARM_DIR at a named state dir and returns it; calling it again switches to another dir."""

    def use(name: str) -> Path:
        d = tmp_path / name / ".swarm"
        monkeypatch.setenv("SWARM_DIR", str(d))
        return d

    return use


@pytest.fixture
def tee_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(substrate_tee, "tee", lambda record, root=None: calls.append(record))
    return calls


# argv, expected event type, expected exit code of the Python path
PARITY = {
    "status ok": (["--json"], "script.orch_status", 0),
    "ids carried": (["--json", "--task-id", "T-1", "--correlation-id", "corr-1"], "script.orch_status", 0),
    "dry run": (["--json", "--dry-run", "--transition", "T-1", "DONE"], "script.orch_status", 0),
    "error reply": (["--json", "--history", "no-such-task", "--task-id", "T-1"], "script.orch_status.error", 2),
}


@pytest.mark.parametrize(("argv", "event_type", "exit_code"), PARITY.values(), ids=PARITY.keys())
def test_external_reply_record_equals_the_python_path_record(
        argv: list[str], event_type: str, exit_code: int, state: StateDirs, capsys: pytest.CaptureFixture[str],
        tee_calls: list[dict[str, Any]]) -> None:
    script = _orch_status_script()
    python_dir = state("python")
    assert script.main(argv) == exit_code
    reply = json.loads(capsys.readouterr().out)
    python_records = _records(python_dir)
    assert [r["type"] for r in python_records] == [event_type]
    python_tees = len(tee_calls)
    # a Rust reply: the same body, without the keys `main` fills in
    external = {k: v for k, v in reply.items() if k not in DEFAULTED}

    external_dir = state("external")
    script.record_external_reply(argv, external, exit_code)

    external_records = _records(external_dir)
    assert _stable(external_records) == _stable(python_records)
    assert len(tee_calls) == 2 * python_tees  # the dry run stays local on both paths, the others are teed on both
    for rec in external_records:
        assert rec["ts"] and rec["msg_id"]
    assert external_records[0]["msg_id"] != python_records[0]["msg_id"]


@pytest.mark.parametrize(("argv", "teed"), [(["--json"], 1), (["--json", "--dry-run"], 0)], ids=["live", "dry-run"])
@pytest.mark.parametrize("exit_code", [0, 1, 2], ids=["exit0", "exit1", "exit2"])
def test_dry_run_is_not_teed_and_is_still_written_locally(
        argv: list[str], teed: int, exit_code: int, state: StateDirs, tee_calls: list[dict[str, Any]]) -> None:
    log_dir = state("run")
    _toy_script().record_external_reply(argv, {"summary": "s"}, exit_code)
    assert len(tee_calls) == teed
    assert len(_records(log_dir)) == 1


@pytest.mark.parametrize(("exit_code", "event_type", "status"), [(0, "script.toy", "ok"), (1, "script.toy", "ok"), (2, "script.toy.error", "error")])
def test_exit_code_picks_the_event_name_and_the_status_default(exit_code: int, event_type: str, status: str, state: StateDirs) -> None:
    log_dir = state("run")
    _toy_script().record_external_reply(["--json"], {"summary": "s"}, exit_code)
    [record] = _records(log_dir)
    assert record["type"] == event_type
    assert record["source"] == "A99@toy"
    assert record["payload"] == {"summary": "s", "agent": "A99", "script": "toy", "status": status}


def test_the_replys_own_agent_script_and_status_are_kept_and_the_reply_is_not_mutated(state: StateDirs) -> None:
    log_dir = state("run")
    reply = {"agent": "A01", "script": "other", "status": "fail", "summary": "s"}
    _toy_script().record_external_reply(["--json"], reply, 1)
    [record] = _records(log_dir)
    assert record["payload"] == {"agent": "A01", "script": "other", "status": "fail", "summary": "s"}
    bare = {"summary": "s"}
    _toy_script().record_external_reply(["--json"], bare, 0)
    assert bare == {"summary": "s"}


def test_ids_from_argv_land_on_the_record(state: StateDirs) -> None:
    log_dir = state("run")
    _toy_script().record_external_reply(["--json", "--task-id", "T-9", "--correlation-id", "corr-9"], {}, 0)
    [record] = _records(log_dir)
    assert (record["task_id"], record["correlation_id"]) == ("T-9", "corr-9")


REJECTED = {
    "unknown flag": ["--bogus"],
    "missing value": ["--task-id"],
    "help": ["--help"],
    "abbreviation": ["--dry"],
}


@pytest.mark.parametrize("argv", REJECTED.values(), ids=REJECTED.keys())
def test_an_argv_the_parser_rejects_raises_and_writes_nothing(
        argv: list[str], state: StateDirs, tee_calls: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]) -> None:
    log_dir = state("run")
    with pytest.raises(SwarmError) as caught:
        _toy_script().record_external_reply(argv, {"summary": "s"}, 0)
    assert caught.value.code == ErrorCode.E_INPUT
    assert _records(log_dir) == []
    assert tee_calls == []
    assert capsys.readouterr().err == ""  # the fallback path prints the usage text once, not twice


@pytest.mark.parametrize("exit_code", [-1, 3, 255])
def test_an_exit_code_without_a_record_raises_and_writes_nothing(exit_code: int, state: StateDirs) -> None:
    log_dir = state("run")
    with pytest.raises(SwarmError):
        _toy_script().record_external_reply(["--json"], {}, exit_code)
    assert _records(log_dir) == []


INVALID_IDS = {
    "traversal task id": ["--task-id", "../x"],
    "leading dash correlation id": ["--correlation-id=-x"],
    "empty task id": ["--task-id", ""],
}


@pytest.mark.parametrize("argv", INVALID_IDS.values(), ids=INVALID_IDS.keys())
def test_an_invalid_id_raises_for_a_result_and_is_dropped_for_an_error_as_main_does(argv: list[str], state: StateDirs) -> None:
    log_dir = state("run")
    for exit_code in (0, 1):
        with pytest.raises(SwarmError):
            _toy_script().record_external_reply(argv, {}, exit_code)
    assert _records(log_dir) == []

    _toy_script().record_external_reply(argv, {"status": "error"}, 2)
    [record] = _records(log_dir)
    assert record["type"] == "script.toy.error"
    assert (record["task_id"], record["correlation_id"]) == (None, None)


def test_existing_records_are_kept_byte_for_byte_and_each_call_appends_one_line(state: StateDirs) -> None:
    log_dir = state("run")
    script = _toy_script()
    script.record_external_reply(["--json"], {"summary": "first"}, 0)
    first = (log_dir / "events.jsonl").read_bytes()

    script.record_external_reply(["--json"], {"summary": "second"}, 0)
    second = (log_dir / "events.jsonl").read_bytes()

    assert second.startswith(first)
    assert len(_records(log_dir)) == 2
    assert [r["payload"]["summary"] for r in _records(log_dir)] == ["first", "second"]
    assert second.endswith(b"\n")


def test_a_write_failure_propagates_and_leaves_the_existing_log_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied")
    monkeypatch.setenv("SWARM_DIR", str(blocker / ".swarm"))
    with pytest.raises(OSError):
        _toy_script().record_external_reply(["--json"], {}, 0)
    assert blocker.read_text() == "occupied"
