"""Real compiled-entry consumers for the frozen 14.4 scheduler boundary.

No import skip, stand-in native entry, facade or Python scheduler is used. The only doubles
are the sixteen host ports. Rust-only panic/stderr injection lives in sched_run.rs cfg(test).
"""
from __future__ import annotations

import inspect
import math
import subprocess
import sys
import threading
import traceback
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest

from swarm.errors import ErrorCode, SwarmError

_native = pytest.importorskip("swarm._native")

NativeCleanupFence = _native.NativeCleanupFence

LOOP_SUMMARY = "E-INTERNAL: scheduler loop callback failed"
WORKER_SUMMARY = "E-INTERNAL: scheduler worker callback failed"
CANARY = "INERT_BINDING_OPAQUE_CANARY"
PORTS = (
    "begin_pass", "agent_for", "claims_for", "block", "lease", "observe_pass", "submit", "worker",
    "internal_failure", "complete", "set_retry_after", "random_unit", "cancel_state", "cancel_begin", "kill", "log",
)
OPTIONS: dict[str, Any] = {
    "max_concurrent": 2, "barrier": False, "once": True, "budget": 8, "max_rounds": 4,
    "fail_streak": 0, "cooldown_s": 0.0, "retry_cap_s": 1.0, "backoff_base_s": 0.001,
    "backoff_cap_s": 0.01, "wait_tick_s": 0.001, "internal_failure_limit": 2,
    "cancel_grace_s": 0.0, "shutdown_budget_s": 0.1, "kill_grace_s": 0.0, "kill_settle_s": 0.01,
}
SUMMARY_KEYS = (
    "rounds", "dispatched", "effective_parallel", "closed", "extension_down", "abandoned", "stopped", "waiting",
)


class Ports:
    def __init__(self, tasks: tuple[str, ...] = ("task",)) -> None:
        self.rows: list[Any] = [(task, None, None, None, None) for task in tasks]
        self.calls: list[tuple[str, tuple[Any, ...], int]] = []
        self.overrides: dict[str, Callable[..., Any]] = {}
        self.finished: list[tuple[str, str, str, str | None]] = []
        self.failures: list[str] = []
        self.retries: list[tuple[str, float]] = []
        self.observed: list[dict[str, Any]] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.slow = False
        self.cancel = False
        self.main_thread = threading.get_ident()

    def answer(self, name: str, args: tuple[Any, ...], result: Any) -> Any:
        self.calls.append((name, args, threading.get_ident()))
        if name in self.overrides:
            return self.overrides[name](*args)
        return result

    def begin_pass(self, round: int, in_flight: list[str]) -> Any:
        assert type(round) is int and type(in_flight) is list
        rows = [row for row in self.rows if row[0] not in in_flight]
        return self.answer("begin_pass", (round, in_flight), rows)

    def agent_for(self, task: str) -> Any:
        return self.answer("agent_for", (task,), ("agent", "slug", 2))

    def claims_for(self, task: str) -> Any:
        return self.answer("claims_for", (task,), ([], None))

    def block(self, task: str, reason: str) -> Any:
        result = self.answer("block", (task, reason), None)
        if result is None:
            self.rows = [row for row in self.rows if row[0] != task]
        return result

    def lease(self, task: str, agent: str) -> Any:
        return self.answer("lease", (task, agent), (True, "held", None))

    def observe_pass(self, info: dict[str, Any]) -> Any:
        assert tuple(info) == ("round", "ready", "submitted", "effective_parallel", "free", "in_flight", "skipped", "unsubmitted")
        assert all(type(info[key]) is int for key in tuple(info)[:5])
        assert type(info["in_flight"]) is list and type(info["skipped"]) is list
        assert all(type(pair) is tuple and len(pair) == 2 for pair in info["skipped"])
        assert type(info["unsubmitted"]) is list and all(type(pair) is tuple and len(pair) == 2 for pair in info["unsubmitted"])
        self.observed.append(info)
        return self.answer("observe_pass", (info,), None)

    def submit(self, task: str, agent: str) -> Any:
        self.started.set()
        return self.answer("submit", (task, agent), ("started", "sid-" + task))

    def worker(self, task: str, agent: str, sid: str) -> Any:
        assert threading.get_ident() != self.main_thread
        assert sid == "sid-" + task
        if self.slow:
            assert self.release.wait(3), "test worker must be released"
        return self.answer("worker", (task, agent, sid), (task, agent, "DONE", None))

    def internal_failure(self, task: str, agent: str, text: str) -> Any:
        self.failures.append(text)
        return self.answer("internal_failure", (task, agent, text), (task, agent, "FAILED", text))

    def complete(self, done: tuple[str, str, str, str | None]) -> Any:
        assert type(done) is tuple and len(done) == 4
        self.finished.append(done)
        self.rows = [row for row in self.rows if row[0] != done[0]]
        return self.answer("complete", (done,), (done[2] == "FAILED", None, None, False, False))

    def set_retry_after(self, task: str, wall: float) -> Any:
        assert type(wall) is float and math.isfinite(wall)
        self.retries.append((task, wall))
        return self.answer("set_retry_after", (task, wall), None)

    def random_unit(self) -> Any:
        return self.answer("random_unit", (), 0.0)

    def cancel_state(self) -> Any:
        return self.answer("cancel_state", (), (self.cancel and self.started.is_set(), True, None, 0.0))

    def cancel_begin(self, reason: str, grace_s: float, in_flight: int) -> Any:
        assert type(reason) is str and type(grace_s) is float and type(in_flight) is int
        return self.answer("cancel_begin", (reason, grace_s, in_flight), None)

    def kill(self, grace_s: float, reason: str) -> Any:
        assert type(grace_s) is float and type(reason) is str
        try:
            return self.answer("kill", (grace_s, reason), None)
        finally:
            self.release.set()

    def log(self, line: str) -> Any:
        return self.answer("log", (line,), None)


def run(ports: Ports, **changes: Any) -> dict[str, Any]:
    options = OPTIONS | changes
    try:
        return _native.run_scheduler(ports, cleanup_fence=NativeCleanupFence(), **options)
    finally:
        ports.release.set()


def constant(value: Any) -> Callable[..., Any]:
    return lambda *args: value


def fail(error: BaseException) -> Callable[..., Any]:
    def raising_callback(*args: Any) -> Any:
        raise error
    return raising_callback


def test_native_compiled_entry_tracer_started_and_failed() -> None:
    assert inspect.isbuiltin(_native.run_scheduler), "must exercise the compiled entry"
    p = Ports(("failed-setup", "started"))
    p.overrides["submit"] = lambda task, agent: (
        ("failed", (task, agent, "FAILED", "setup failed")) if task == "failed-setup" else ("started", "sid-" + task)
    )
    result = run(p)
    assert tuple(result) == SUMMARY_KEYS
    assert result == {
        "rounds": 1, "dispatched": 2, "effective_parallel": 2, "closed": True,
        "extension_down": False, "abandoned": [], "stopped": "closed", "waiting": [],
    }
    assert Counter(done[0] for done in p.finished) == {"failed-setup": 1, "started": 1}
    assert [args[0] for name, args, _ in p.calls if name == "worker"] == ["started"]
    assert [name for name, _, _ in p.calls].count("submit") == 2
    assert p.observed[0]["in_flight"] == ["failed-setup", "started"]
    assert p.observed[0]["skipped"] == []
    assert p.observed[0]["unsubmitted"] == []
    for name, _, thread in p.calls:
        assert (thread != p.main_thread) if name == "worker" else (thread == p.main_thread)


def test_native_observe_pass_order_and_lease_waiting() -> None:
    p = Ports(("later", "first"))
    p.rows = [("later", "P3", 0, 0.0, None), ("first", "P0", 1, 2.0, None)]
    p.overrides["lease"] = constant((False, "busy", "owner"))
    result = run(p, once=False)
    assert result["stopped"] == "lease-wait"
    assert result["waiting"] == ["first: busy (held by owner)", "later: busy (held by owner)"]
    assert p.observed[0]["skipped"] == [("first", "lease:busy"), ("later", "lease:busy")]
    assert p.finished == []


def test_native_optional_records_claim_modes_and_blocking() -> None:
    p = Ports(("missing", "invalid", "shared", "exclusive"))
    p.overrides["agent_for"] = lambda task: None if task == "missing" else ("agent", "slug", 0)
    p.overrides["claims_for"] = lambda task: (
        (None, "bad claim") if task == "invalid" else ([("src", "shared" if task == "shared" else "exclusive")], None)
    )
    # A zero class ceiling is clamped by the source to one, not substituted by the binding.
    # A NaN retry_after is E-INPUT now; None is the same non-holding value (select.rs: a NaN retry_after never holds a task back).
    p.rows = [("shared", None, None, None, None), ("exclusive", None, 0, 0.0, 0.0)]
    p.overrides["lease"] = constant(None)
    result = run(p)
    assert result["dispatched"] == 1
    assert p.observed[0]["skipped"] == [("exclusive", "class-ceiling:agent")]
    assert len([call for call in p.calls if call[0] == "lease"]) == 1
    for mode in ("missing", "invalid"):
        q = Ports((mode,))
        q.overrides.update(p.overrides)
        outcome = run(q, once=False)
        assert outcome["stopped"] == "nothing-ready"
        assert any(name == "block" for name, _, _ in q.calls)


NAN_READY_ROWS = [("task", None, None, float("nan"), None), ("task", None, None, None, float("nan"))]


@pytest.mark.parametrize("row", NAN_READY_ROWS, ids=["created_at", "retry_after"])
def test_native_nan_ready_times_are_e_input(row: tuple[Any, ...]) -> None:
    p = Ports()
    p.overrides["begin_pass"] = constant([row])
    with pytest.raises(ValueError, match="^E-INPUT: run_scheduler failed:"):
        run(p)
    assert [name for name, _, _ in p.calls].count("begin_pass") == 1


def test_native_infinite_ready_times_are_accepted() -> None:
    # +inf retry_after holds a task back forever (`until > now`); -inf never does. Both are real ordering/comparison values.
    held = Ports()
    held.rows = [("held", None, None, float("-inf"), float("inf"))]
    result = run(held)
    assert result["dispatched"] == 0 and result["stopped"] == "closed"
    assert [task for task, _ in held.observed[0]["skipped"]] == ["held"]
    assert held.observed[0]["skipped"][0][1].startswith("backoff:")
    free = Ports(("first", "second"))
    free.rows = [("first", None, None, float("inf"), float("-inf")), ("second", None, None, float("-inf"), None)]
    assert run(free)["dispatched"] == 2


def test_native_observe_pass_reports_granted_leases_left_unsubmitted_by_a_stop() -> None:
    p = Ports(("granted",))
    leased: list[str] = []

    def lease(task: str, agent: str) -> Any:
        leased.append(task)
        return (True, "held", None)

    p.overrides["lease"] = lease
    # The stop becomes visible only once the lease is granted, so admission leaves the granted pair unsubmitted.
    p.overrides["cancel_state"] = lambda: (bool(leased), False, None, 0.0)
    result = run(p)
    assert result["dispatched"] == 0
    assert [name for name, _, _ in p.calls].count("submit") == 0
    assert tuple(p.observed[0]) == (
        "round", "ready", "submitted", "effective_parallel", "free", "in_flight", "skipped", "unsubmitted",
    )
    assert p.observed[0]["submitted"] == 0
    assert p.observed[0]["unsubmitted"] == [("granted", "agent")]


def test_native_claim_modes_are_decoded_and_conflicts_keep_source_order() -> None:
    p = Ports(("shared", "exclusive"))
    p.overrides["claims_for"] = lambda task: ([("src", task)], None)
    result = run(p)
    assert result["dispatched"] == 1
    assert p.observed[0]["in_flight"] == ["shared"]
    assert p.observed[0]["skipped"] == [("exclusive", "claim-conflict:shared")]


@pytest.mark.parametrize("barrier", [False, True])
def test_native_existing_budget_and_round_barrier_control_passes(barrier: bool) -> None:
    p = Ports(("first", "second", "third", "fourth"))
    result = run(p, once=False, barrier=barrier, budget=3, max_rounds=2)
    assert result["stopped"] == "closed"
    assert result["rounds"] == 2
    assert result["dispatched"] == (4 if barrier else 3)
    assert len(p.finished) == result["dispatched"]
    assert p.observed[0]["in_flight"] == ["first", "second"]


def test_native_stalled_stop_comes_from_the_existing_idle_limit() -> None:
    p = Ports()
    p.overrides["begin_pass"] = constant(list(p.rows))
    p.overrides["agent_for"] = constant(None)
    result = run(p, once=False)
    assert result["stopped"] == "stalled" and result["rounds"] == 2


@pytest.mark.parametrize("sample", [-1.0, 0.0, 1.0])
def test_native_random_endpoints_are_accepted(sample: float) -> None:
    p = trigger("random_unit")
    p.overrides["random_unit"] = constant(sample)
    assert run(p)["dispatched"] == 1
    assert len(p.retries) == 1


@pytest.mark.parametrize("rate,expected", [(None, 2), (0.0, 1)])
def test_native_none_and_zero_rate_limit_are_distinct(rate: float | None, expected: int) -> None:
    p = Ports()
    p.overrides["complete"] = constant((False, rate, None, False, False))
    assert run(p)["effective_parallel"] == expected


@pytest.mark.parametrize("cancelled,expected", [(False, 1), (True, 2)])
def test_native_completion_cancelled_is_the_fifth_boolean(cancelled: bool, expected: int) -> None:
    p = Ports()
    p.overrides["complete"] = constant((True, None, None, False, cancelled))
    assert run(p, fail_streak=1)["effective_parallel"] == expected


def test_native_retry_random_log_and_extension_callbacks() -> None:
    p = Ports()
    p.overrides["complete"] = constant((True, 0.0, 0, True, False))
    result = run(p)
    assert result["extension_down"] is True
    assert len(p.retries) == 1 and p.retries[0][0] == "task"
    counts = Counter(name for name, _, _ in p.calls)
    assert counts["random_unit"] == counts["set_retry_after"] == 1
    assert counts["log"] >= 2


def test_native_cancel_ports_have_frozen_arguments_and_gil_is_released() -> None:
    p = Ports()
    p.cancel = p.slow = True
    result = run(p)
    assert result["stopped"] == "cancelled"
    begin = [args for name, args, _ in p.calls if name == "cancel_begin"]
    kill = [args for name, args, _ in p.calls if name == "kill"]
    assert begin == [("cancelled", 0.0, 1)]
    assert [reason for _, reason in kill] == ["cancelled"]
    assert len(p.finished) == 1
    assert result["abandoned"] == []


@pytest.mark.parametrize("missing", tuple(OPTIONS))
def test_native_all_sixteen_kwargs_are_required(missing: str) -> None:
    options = dict(OPTIONS)
    del options[missing]
    p = Ports()
    with pytest.raises(TypeError):
        _native.run_scheduler(p, cleanup_fence=NativeCleanupFence(), **options)
    assert p.calls == []


def test_native_rejects_positional_config_unknown_kwargs_and_missing_ports() -> None:
    p = Ports()
    with pytest.raises(TypeError):
        getattr(_native, "run_scheduler")(p, OPTIONS, cleanup_fence=NativeCleanupFence())
    with pytest.raises(TypeError):
        _native.run_scheduler(p, cleanup_fence=NativeCleanupFence(), **(OPTIONS | {"config": OPTIONS}))
    with pytest.raises(AttributeError):
        _native.run_scheduler(object(), cleanup_fence=NativeCleanupFence(), **OPTIONS)
    assert p.calls == []


@pytest.mark.parametrize("name", PORTS)
def test_native_resolves_all_callbacks_before_dispatch(name: str) -> None:
    p = Ports()
    setattr(p, name, 7)
    with pytest.raises(ValueError, match="^E-INPUT:"):
        run(p)
    assert p.calls == []


COUNTS = ("max_concurrent", "budget", "max_rounds", "fail_streak", "internal_failure_limit")
DURATIONS = tuple(key for key in OPTIONS if key.endswith("_s"))


@pytest.mark.parametrize("name", COUNTS)
@pytest.mark.parametrize("bad", [-1, -(10**100), True, 1.0, "1", None])
def test_native_config_counts_are_strict_and_negative_is_coded(name: str, bad: Any) -> None:
    p = Ports()
    with pytest.raises(ValueError, match="^E-INPUT:"):
        run(p, **{name: bad})
    assert p.calls == []


@pytest.mark.parametrize("name", tuple(key for key in COUNTS if key != "fail_streak"))
def test_native_config_positive_counts_reject_zero(name: str) -> None:
    p = Ports()
    with pytest.raises(ValueError, match="^E-INPUT:"):
        run(p, **{name: 0})
    assert not p.calls


@pytest.mark.parametrize("name", ("barrier", "once"))
@pytest.mark.parametrize("bad", [0, 1, None, "false"])
def test_native_config_booleans_do_not_coerce(name: str, bad: Any) -> None:
    p = Ports()
    with pytest.raises(ValueError, match="^E-INPUT:"):
        run(p, **{name: bad})
    assert not p.calls


@pytest.mark.parametrize("name", DURATIONS)
@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), -float("inf"), 1e9 + 1, 10**1000, True, "1", None])
def test_native_config_seconds_reject_negative_nonfinite_overflow_and_coercion(name: str, bad: Any) -> None:
    p = Ports()
    with pytest.raises(ValueError, match="^E-INPUT:"):
        run(p, **{name: bad})
    assert not p.calls


@pytest.mark.parametrize("name", ("retry_cap_s", "backoff_base_s", "backoff_cap_s", "wait_tick_s"))
def test_native_config_positive_durations_reject_zero(name: str) -> None:
    with pytest.raises(ValueError, match="^E-INPUT:"):
        run(Ports(), **{name: 0.0})


def test_native_unsigned_overflow_saturates_instead_of_wrapping() -> None:
    huge = 10**1000
    p = Ports(())
    result = run(p, **{name: huge for name in COUNTS})
    assert result["effective_parallel"] == 2**32 - 1
    assert p.observed[0]["free"] == 2**32 - 1
    q = Ports()
    q.overrides["agent_for"] = constant(("agent", "slug", huge))
    q.overrides["complete"] = constant((True, None, huge, False, False))
    assert run(q)["dispatched"] == 1
    assert len(q.retries) == 1


def test_native_config_valid_duration_endpoints_without_dispatch() -> None:
    assert run(Ports(()), **{name: 1e9 for name in DURATIONS})["stopped"] == "nothing-ready"
    zero_fields = ("cooldown_s", "cancel_grace_s", "shutdown_budget_s", "kill_grace_s", "kill_settle_s")
    assert run(Ports(()), **{name: 0.0 for name in zero_fields})["stopped"] == "nothing-ready"


def test_native_numeric_conversion_never_invokes_host_coercion() -> None:
    class Coercion:
        def __index__(self) -> int:
            raise AssertionError("host index coercion was invoked")

        def __float__(self) -> float:
            raise AssertionError("host float coercion was invoked")

    for name in ("budget", "wait_tick_s"):
        with pytest.raises(ValueError, match="^E-INPUT:"):
            run(Ports(), **{name: Coercion()})
    p = trigger("random_unit")
    p.overrides["random_unit"] = constant(Coercion())
    with pytest.raises(ValueError, match="^E-INPUT:"):
        run(p)


BAD_RECORDS: dict[str, list[Any]] = {
    "begin_pass": [None, (), [("task",)], [["task", None, None, None, None]], [(1, None, None, None, None)],
                   [("task", 7, None, None, None)], [("task", None, True, None, None)],
                   [("task", None, 10**100, None, None)], [("task", None, None, True, None)],
                   [("task", None, None, None, "later")]],
    "agent_for": [[], ("agent", "slug"), (1, "slug", 1), ("agent", 1, 1), ("agent", "slug", -1), ("agent", "slug", True)],
    "claims_for": [None, [], (None, None), ([], "error"), ([('src', 'bogus')], None),
                   ([['src', 'shared']], None), ([(1, "shared")], None), ((('src', 'shared'),), None), (None, 1)],
    "block": [False, (), "not None"],
    "lease": [[], (True, "held"), (1, "held", None), (True, 1, None), (True, "held", 1)],
    "observe_pass": [False, {}, ()],
    "submit": ["sid", (), ["started", "sid"], ("other", "sid"), ("started", None),
               ("failed", ("task", "agent", "FAILED")), ("failed", ("wrong", "agent", "FAILED", None)),
               ("failed", ("task", "agent", "FAILED", 7))],
    "worker": [None, [], ("task", "agent", "DONE"), ("wrong", "agent", "DONE", None),
               ("task", "wrong", "DONE", None), ("task", "agent", 1, None), ("task", "agent", "DONE", 7)],
    "internal_failure": [None, ("task", "agent", "FAILED"), ("wrong", "agent", "FAILED", None),
                         ("task", "agent", "FAILED", 7)],
    "complete": [None, (), (False, None, None, False), (1, None, None, False, False),
                 (False, True, None, False, False), (False, -1.0, None, False, False),
                 (False, float("nan"), None, False, False), (False, float("inf"), None, False, False),
                 (False, None, -1, False, False), (False, None, True, False, False),
                 (False, None, None, 1, False), (False, None, None, False, "cancelled")],
    "set_retry_after": [False, (), 1],
    "random_unit": [None, True, "0", -1.01, 1.01, float("nan"), float("inf"), 10**1000],
    "cancel_state": [None, (), (False, False, None), (0, False, None, 0.0), (False, 0, None, 0.0),
                     (False, False, 1, 0.0), (False, False, None, -1.0), (False, False, None, float("nan")),
                     (False, False, None, float("inf")), (False, False, None, True)],
    "cancel_begin": [False, (), {}],
    "kill": [False, (), {}],
    "log": [False, (), {}],
}


def trigger(name: str) -> Ports:
    p = Ports()
    if name == "block":
        p.overrides["agent_for"] = constant(None)
    elif name == "internal_failure":
        p.overrides["worker"] = fail(KeyboardInterrupt(CANARY))
    elif name in ("random_unit", "set_retry_after"):
        p.overrides["complete"] = constant((True, None, 1, False, False))
    elif name == "log":
        p.overrides["complete"] = constant((False, None, None, True, False))
    elif name == "cancel_begin":
        p.overrides["cancel_state"] = constant((True, True, None, 0.0))
    elif name == "kill":
        p.slow = p.cancel = True
    return p


@pytest.mark.parametrize("name,bad", [(name, bad) for name, bads in BAD_RECORDS.items() for bad in bads])
def test_native_every_callback_rejects_malformed_records(name: str, bad: Any) -> None:
    p = trigger(name)
    p.overrides[name] = constant(bad)
    if name == "worker":
        assert run(p)["dispatched"] == 1
        assert p.failures == [WORKER_SUMMARY]
        assert p.finished[0][2:] == ("FAILED", WORKER_SUMMARY)
    else:
        with pytest.raises(ValueError, match="^E-INPUT:"):
            run(p)
    assert any(call[0] == name for call in p.calls), "must actually reach the malformed port"


@pytest.mark.parametrize("name", PORTS)
def test_native_every_callback_enforces_python_arity(name: str) -> None:
    p = trigger(name)
    p.overrides[name] = lambda *, impossible_argument: None
    if name == "worker":
        run(p)
        assert p.failures == [WORKER_SUMMARY]
    else:
        with pytest.raises(TypeError):
            run(p)


@pytest.mark.parametrize("name", tuple(name for name in PORTS if name != "worker"))
def test_native_every_loop_port_reraises_original_exception(name: str) -> None:
    p = trigger(name)
    error = RuntimeError(CANARY)
    p.overrides[name] = fail(error)
    with pytest.raises(RuntimeError) as caught:
        run(p)
    assert caught.value is error
    assert "raising_callback" in [frame.name for frame in traceback.extract_tb(caught.value.__traceback__)]


@pytest.mark.parametrize("error", [RuntimeError(CANARY), KeyboardInterrupt(CANARY), SystemExit(CANARY),
                                   SwarmError(ErrorCode.E_POLICY, CANARY, retained="detail")])
def test_native_loop_pyerr_identity_traceback_opaque(error: BaseException) -> None:
    p = Ports()
    p.overrides["begin_pass"] = fail(error)
    with pytest.raises(type(error)) as caught:
        run(p)
    assert caught.value is error
    assert CANARY in str(caught.value), "caller-visible opaque exception is deliberately unchanged"
    assert "raising_callback" in [frame.name for frame in traceback.extract_tb(caught.value.__traceback__)]
    if isinstance(error, SwarmError):
        assert error.code is ErrorCode.E_POLICY and error.details == {"retained": "detail"}


class HostileFailure(BaseException):
    def __str__(self) -> str:
        raise AssertionError("must not stringify opaque worker errors")

    def __repr__(self) -> str:
        raise AssertionError("must not repr opaque worker errors")


@pytest.mark.parametrize("error", [RuntimeError(CANARY), KeyboardInterrupt(CANARY), SystemExit(CANARY), HostileFailure(CANARY)])
def test_native_worker_baseexception_safe_summary(error: BaseException) -> None:
    p = Ports(("fault", "sibling"))
    p.overrides["worker"] = lambda task, agent, sid: (
        fail(error)() if task == "fault" else (task, agent, "DONE", None)
    )
    result = run(p)
    assert result["dispatched"] == 2
    assert p.failures == [WORKER_SUMMARY]
    by_task = {record[0]: record for record in p.finished}
    assert by_task["fault"][2:] == ("FAILED", WORKER_SUMMARY)
    assert by_task["sibling"][2:] == ("DONE", None)
    assert all(CANARY not in line for line in p.failures)
    assert Counter(record[0] for record in p.finished) == {"fault": 1, "sibling": 1}


def test_native_porterror_summary_is_fixed() -> None:
    p = Ports()
    p.overrides["worker"] = fail(HostileFailure(CANARY))
    run(p)
    assert p.failures == [WORKER_SUMMARY]
    assert p.finished == [("task", "agent", "FAILED", WORKER_SUMMARY)]
    # The loop summary is an internal Rust property (also asserted by the embedded Rust
    # consumer). Python must see the original exception, not a sanitized reconstruction.
    q = Ports()
    error = RuntimeError(CANARY)
    q.overrides["begin_pass"] = fail(error)
    with pytest.raises(RuntimeError) as caught:
        run(q)
    assert caught.value is error and str(caught.value) != LOOP_SUMMARY


def test_native_nested_entry_and_unrelated_python_progress() -> None:
    p = Ports()
    nested: list[dict[str, Any]] = []
    ticks: list[int] = []
    stop = threading.Event()

    def progress() -> None:
        assert p.started.wait(3)
        ticks.append(1)
        p.release.set()
        stop.set()

    def begin(round: int, in_flight: list[str]) -> Any:
        nested.append(run(Ports(())))
        return p.rows

    p.overrides["begin_pass"] = begin
    p.slow = True
    thread = threading.Thread(target=progress)
    thread.start()
    try:
        assert run(p)["dispatched"] == 1
    finally:
        p.release.set()
        thread.join(3)
    assert not thread.is_alive() and stop.is_set() and ticks == [1]
    assert nested[0]["stopped"] == "nothing-ready"


@pytest.mark.parametrize("worker_outcome", ("returned", "raised"))
def test_native_cancel_state_checks_signals_on_the_loop_thread(worker_outcome: str) -> None:
    # A disposable interpreter keeps process signal mutation out of the shared pytest parent.
    child = subprocess.run(
        [sys.executable, "-c", '''
import os, signal, sys, threading, traceback, weakref
from swarm import _native
class SignalSeen(BaseException): pass
class WorkerSeen(BaseException):
    def __str__(self): raise AssertionError("must not stringify worker error")
    def __repr__(self): raise AssertionError("must not repr worker error")
error = SignalSeen("opaque signal")
ready = threading.Event()
release = threading.Event()
released_refs = threading.Event()
worker_threads = []
cleanup_threads = []
worker_failures = []
main_thread = threading.get_ident()
def disposed():
    cleanup_threads.append(threading.get_ident())
    released_refs.set()
class Ports:
    def begin_pass(self, round, in_flight): return [("t", None, None, None, None)]
    def agent_for(self, task): return ("a", "slug", 1)
    def claims_for(self, task): return ([], None)
    def block(self, *args): pass
    def lease(self, *args): return None
    def observe_pass(self, *args): pass
    def submit(self, *args): return ("started", "sid")
    def worker(self, *args):
        worker_threads.append(threading.get_ident())
        ready.set()
        assert release.wait(3)
        if sys.argv[1] == "raised":
            raise WorkerSeen("opaque worker")
        return ("t", "a", "DONE", None)
    def internal_failure(self, task, agent, text):
        assert sys.argv[1] == "raised", "not a worker fault"
        assert text == "E-INTERNAL: scheduler worker callback failed"
        worker_failures.append(text)
        return (task, agent, "FAILED", text)
    def complete(self, *args): return (False, None, None, False, False)
    def set_retry_after(self, *args): pass
    def random_unit(self): return 0.0
    def cancel_state(self): return (False, False, None, 0.0)
    def cancel_begin(self, *args): pass
    def kill(self, *args): pass
    def log(self, *args): pass
def handler(*args):
    assert threading.get_ident() == main_thread
    raise error
def send():
    assert ready.wait(3)
    os.kill(os.getpid(), signal.SIGUSR1)
signal.signal(signal.SIGUSR1, handler)
t = threading.Thread(target=send)
t.start()
ports = Ports()
fence = _native.NativeCleanupFence()
native_owner = weakref.ref(ports)
# Each of the sixteen captured bound methods owns this same instance. This finalizer
# cannot run until the worker callback, any opaque worker PyErr traceback, and the last
# native callback reference have all released it. It does not capture the instance.
finalizer = weakref.finalize(ports, disposed)
finalizer.atexit = False
try:
    _native.run_scheduler(ports, cleanup_fence=fence, max_concurrent=1, barrier=False, once=True, budget=1,
        max_rounds=1, fail_streak=0, cooldown_s=0., retry_cap_s=1., backoff_base_s=.001,
        backoff_cap_s=.01, wait_tick_s=.001, internal_failure_limit=1, cancel_grace_s=0.,
        shutdown_budget_s=.1, kill_grace_s=0., kill_settle_s=0.)
except SignalSeen as caught:
    assert caught is error
    assert "handler" in [frame.name for frame in traceback.extract_tb(caught.__traceback__)]
    assert fence.wait(0.0) is False, "worker still owns native references while blocked"
    print("SIGNAL_OPAQUE_OK")
else:
    raise AssertionError("loop did not check signals")
finally:
    # The loop exception leaves the native job detached. Drop the caller's reference
    # BEFORE releasing the worker, so only native ownership can trigger disposed().
    del ports
    assert native_owner() is not None and not released_refs.is_set()
    release.set()
    t.join(3)
    assert not t.is_alive(), "signal sender did not exit"
    # Releasing the worker and joining the sender are NOT native cleanup witnesses.
    # PythonPorts::drop disposes all bound methods in an attached host scope; on this
    # GIL-enabled CPython consumer, wait resumes after that scope releases the GIL.
    assert released_refs.wait(3), "native callback references were not released"
    assert native_owner() is None and not finalizer.alive
    assert len(worker_threads) == 1 and worker_threads[0] != main_thread
    assert cleanup_threads == worker_threads, "last native owner must release on the worker"
    assert worker_failures == (
        ["E-INTERNAL: scheduler worker callback failed"] if sys.argv[1] == "raised" else []
    )
assert fence.wait(3.0) is True, "native cleanup fence did not complete after the worker was released"
print("SIGNAL_NATIVE_REFS_RELEASED")
''', worker_outcome],
        capture_output=True, text=True, timeout=15,
    )
    assert child.returncode == 0, child.stdout + child.stderr
    assert "SIGNAL_OPAQUE_OK" in child.stdout
    assert "SIGNAL_NATIVE_REFS_RELEASED" in child.stdout


def test_native_run_scheduler_signature_is_exact() -> None:
    parameters = inspect.signature(_native.run_scheduler).parameters
    names = list(parameters)
    assert names == ["ports", "cleanup_fence", *OPTIONS]
    assert parameters["ports"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in names[1:])


def test_native_rejects_duplicate_and_extra_ports_before_claiming_the_fence() -> None:
    p = Ports()
    fence = NativeCleanupFence()
    with pytest.raises(TypeError):
        _native.run_scheduler(p, p, cleanup_fence=fence, **OPTIONS)
    with pytest.raises(TypeError):
        _native.run_scheduler(p, ports=p, cleanup_fence=fence, **OPTIONS)
    with pytest.raises(TypeError):
        _native.run_scheduler(cleanup_fence=fence, **OPTIONS)
    assert p.calls == []
    with pytest.raises(ValueError, match="^E-INPUT:"):
        fence.wait(0.0)
    assert _native.run_scheduler(p, cleanup_fence=fence, **OPTIONS)["dispatched"] == 1


def test_native_cleanup_fence_is_required_typed_and_single_use() -> None:
    p = Ports()
    with pytest.raises(TypeError):
        _native.run_scheduler(p, **OPTIONS)
    for wrong in (object(), None, "fence"):
        with pytest.raises(TypeError):
            _native.run_scheduler(p, cleanup_fence=wrong, **OPTIONS)
    assert p.calls == []
    fence = NativeCleanupFence()
    assert _native.run_scheduler(p, cleanup_fence=fence, **OPTIONS)["dispatched"] == 1
    assert fence.wait(3.0) is True
    assert fence.wait(0.0) is True
    q = Ports()
    with pytest.raises(ValueError, match="^E-INPUT:"):
        _native.run_scheduler(q, cleanup_fence=fence, **OPTIONS)
    assert q.calls == []
    assert fence.wait(0.0) is True


@pytest.mark.parametrize("bad", [-0.1, float("nan"), float("inf"), 1e9 + 1, True, False, "1", None, 10**1000])
def test_native_cleanup_fence_wait_rejects_invalid_timeouts(bad: Any) -> None:
    fence = NativeCleanupFence()
    with pytest.raises(ValueError, match="^E-INPUT:"):
        fence.wait(bad)
    # A fresh fence stays unclaimed: the failed validation mutated nothing.
    with pytest.raises(ValueError, match="^E-INPUT:"):
        fence.wait(0.0)


def test_native_cleanup_fence_wait_never_coerces_host_numbers() -> None:
    class Coercion:
        def __float__(self) -> float:
            raise AssertionError("host float coercion was invoked")

    with pytest.raises(ValueError, match="^E-INPUT:"):
        NativeCleanupFence().wait(Coercion())


def test_native_cleanup_fence_constructor_and_wait_arity_are_exact() -> None:
    fence = NativeCleanupFence()
    with pytest.raises(TypeError):
        fence.wait()
    # `timeout_s` is positional-or-keyword, as the stub declares. The fresh-handle rule still applies to it.
    with pytest.raises(ValueError, match="^E-INPUT:"):
        fence.wait(timeout_s=0.0)
    with pytest.raises(TypeError):
        fence.wait(0.0, 1)
    with pytest.raises(TypeError):
        NativeCleanupFence(1)
    with pytest.raises(TypeError):
        class Subclass(NativeCleanupFence):  # type: ignore[misc]
            pass


def test_native_cleanup_fence_completes_after_loop_error_is_restored() -> None:
    error = RuntimeError(CANARY)
    p = Ports()
    p.overrides["begin_pass"] = fail(error)
    fence = NativeCleanupFence()
    with pytest.raises(RuntimeError) as caught:
        _native.run_scheduler(p, cleanup_fence=fence, **OPTIONS)
    assert caught.value is error
    assert fence.wait(3.0) is True
