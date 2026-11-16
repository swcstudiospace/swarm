"""ACP round trip, denial, retry, dead-letter, and circuit breaker."""
import time

from swarm.acp import ACPStatus, Bus, routing_table

HAPPY = {
    "schema": "acp.v1",
    "sender": "A02",
    "recipient": "A03",
    "correlation_id": "corr-happy-1",
    "message_type": "requirements.spec",
    "payload": {"spec": "ping"},
    "timestamp": "2026-10-10T01:40:00Z",
    "timeout_hint_s": 1,
}


def test_routes_are_derived_from_the_manifest():
    table = routing_table()
    assert set(table) == {f"A{n:02d}" for n in range(1, 31)}
    assert table["A03"]["endpoint"] == "inproc://A03"
    assert "requirements.spec" in table["A02"]["produces"]
    assert "requirements.spec" in table["A03"]["accepts"]
    source = open("swarm/acp.py", encoding="utf-8").read()
    assert "A02 -> A03" not in source


def test_round_trip_echoes_the_correlation_id():
    bus = Bus(sleep=lambda _seconds: None)

    def handler(envelope):
        return {"correlation_id": envelope["correlation_id"], "accepted": True}

    bus.register("A03", handler)
    result = bus.request(HAPPY)
    assert result["state"] == "RESPONDED"
    assert result["ok"] is True
    assert result["response"]["correlation_id"] == "corr-happy-1"
    assert result["history"] == ["PENDING", "DELIVERED", "IN_PROGRESS", "RESPONDED"]


def test_malformed_envelope_is_rejected_without_retry():
    bus = Bus()
    broken = dict(HAPPY)
    del broken["correlation_id"]
    result = bus.request(broken)
    assert result["state"] == "INVALID_INPUT"
    assert result["attempts"] == 0
    assert any("correlation_id" in error for error in result["errors"])


def test_unauthorized_message_is_audited_and_not_retried():
    bus = Bus(sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("retried")))
    envelope = dict(HAPPY)
    envelope["message_type"] = "swarm.status"
    envelope["recipient"] = "A01"
    result = bus.request(envelope)
    assert result["state"] == "UNAUTHORIZED"
    assert result["attempts"] == 1
    assert bus.audit[-1]["caller"] == "A02"
    assert bus.audit[-1]["target"] == "A01"
    assert bus.audit[-1]["reason"]


def test_handler_invalid_input_does_not_retry():
    bus = Bus(sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("retried")))

    def handler(_envelope):
        raise ACPStatus("INVALID_INPUT", "spec field is empty")

    bus.register("A03", handler)
    result = bus.request(HAPPY)
    assert result["state"] == "INVALID_INPUT"
    assert result["attempts"] == 1
    assert result["retryable"] is False


def test_timeout_retries_then_dead_letters_the_full_envelope():
    delays = []
    bus = Bus(sleep=delays.append, max_attempts=3, backoff_s=0.01, breaker_threshold=5)

    def handler(_envelope):
        raise TimeoutError("slow")

    bus.register("A03", handler)
    envelope = dict(HAPPY)
    envelope["correlation_id"] = "corr-timeout-1"
    result = bus.request(envelope)
    assert result["state"] == "DEAD_LETTERED"
    assert result["attempts"] == 3
    assert delays == [0.01, 0.02]
    retained = bus.dead[-1]["envelope"]
    assert retained["correlation_id"] == "corr-timeout-1"
    assert retained["payload"] == {"spec": "ping"}
    assert retained["sender"] == "A02"


def test_slow_handler_hits_the_timeout_hint():
    bus = Bus(sleep=lambda _seconds: None, max_attempts=1, breaker_threshold=9)

    def handler(envelope):
        time.sleep(0.2)
        return {"correlation_id": envelope["correlation_id"]}

    bus.register("A03", handler)
    envelope = dict(HAPPY)
    envelope["correlation_id"] = "corr-slow"
    envelope["timeout_hint_s"] = 0.05
    result = bus.request(envelope)
    assert result["state"] == "DEAD_LETTERED"
    assert bus.dead[-1]["envelope"]["timeout_hint_s"] == 0.05


def test_circuit_breaker_fails_fast_while_local_tools_still_run():
    bus = Bus(sleep=lambda _seconds: None, max_attempts=1, breaker_threshold=1)

    def handler(_envelope):
        raise ConnectionError("down")

    bus.register("A03", handler)
    first = bus.request(HAPPY)
    assert first["state"] == "DEAD_LETTERED"
    assert "A03" in bus.open_circuits
    second = bus.request({**HAPPY, "correlation_id": "corr-open"})
    assert second["state"] == "FAILED"
    assert second["reason"] == "circuit open"
    assert second["attempts"] == 0
    local = bus.invoke_tool("A01", "orch_registry_ping", {"token": "ping"})
    assert local["state"] == "SUCCESS" and local["token"] == "ping"


def test_handler_exception_is_a_failed_delivery_and_is_not_retried():
    bus = Bus(sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("retried")))

    def handler(_envelope):
        raise RuntimeError("boom")

    bus.register("A03", handler)
    result = bus.request(HAPPY)
    assert result["state"] == "FAILED"
    assert result["retryable"] is False
    assert result["attempts"] == 1
    assert "boom" in result["reason"]


def test_response_must_echo_the_correlation_id():
    bus = Bus(sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("retried")))

    def missing(_envelope):
        return {}

    bus.register("A03", missing)
    omitted = bus.request(HAPPY)
    assert omitted["state"] == "FAILED"
    assert omitted["retryable"] is False
    assert "correlation_id" in omitted["reason"]

    def mismatched(_envelope):
        return {"correlation_id": "other"}

    bus.register("A03", mismatched)
    wrong = bus.request({**HAPPY, "correlation_id": "corr-echo"})
    assert wrong["state"] == "FAILED"
    assert wrong["retryable"] is False


def test_retries_keep_the_original_envelope():
    seen = []

    def handler(envelope):
        seen.append(envelope["payload"].get("spec"))
        envelope["payload"].clear()
        raise ConnectionError("down")

    bus = Bus(sleep=lambda _seconds: None, max_attempts=2, breaker_threshold=9)
    bus.register("A03", handler)
    result = bus.request(HAPPY)
    assert result["state"] == "DEAD_LETTERED"
    assert seen == ["ping", "ping"]
    assert result["envelope"]["payload"] == {"spec": "ping"}
    assert bus.dead[-1]["envelope"]["payload"] == {"spec": "ping"}
    assert HAPPY["payload"] == {"spec": "ping"}


def test_busy_handler_is_not_retried(monkeypatch):
    Busy = Bus.request.__globals__["Busy"]  # the class this Bus catches, whichever module graph it was imported in

    def boom(_fn, _arg, _timeout):
        raise Busy("call")

    # patch the globals of the Bus under test: conftest's swarm_dir fixture evicts swarm* modules, so a dotted-name
    # setattr could patch a fresh `swarm.acp` that this Bus never uses
    monkeypatch.setitem(Bus.request.__globals__, "invoke_bounded", boom)
    bus = Bus(sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("retried")))
    bus.register("A03", lambda envelope: {"correlation_id": envelope["correlation_id"]})
    result = bus.request(HAPPY)
    assert result["state"] == "DEAD_LETTERED"
    assert result["attempts"] == 1
    assert result["retryable"] is True


def test_independent_calls_wait_and_do_not_open_the_circuit(monkeypatch):
    import threading
    real = Bus.request.__globals__["invoke_bounded"]

    def slowed_failure(fn, arg, timeout):
        try:
            return real(fn, arg, timeout)
        except Exception:
            if arg["correlation_id"] == "first":
                time.sleep(1.5)  # the failed first call answers only after 1.5 s, past the 1 s poll below
            raise

    monkeypatch.setitem(Bus.request.__globals__, "invoke_bounded", slowed_failure)

    entered = threading.Event()
    release = threading.Event()
    seen = []

    def handler(envelope):
        seen.append(envelope["correlation_id"])
        if envelope["correlation_id"] == "first":
            entered.set()
            release.wait(2)
        return {"correlation_id": envelope["correlation_id"]}

    bus = Bus(sleep=lambda _seconds: None, max_attempts=1, breaker_threshold=3)
    bus.register("A03", handler)
    results = {}
    first_answered = threading.Event()

    def run(correlation_id, timeout):
        results[correlation_id] = bus.request({**HAPPY, "correlation_id": correlation_id, "timeout_hint_s": timeout})
        if correlation_id == "first":
            first_answered.set()

    first = threading.Thread(target=run, args=("first", 0.05))
    first.start()
    assert entered.wait(1)
    second = threading.Thread(target=run, args=("second", 2))
    third = threading.Thread(target=run, args=("third", 2))
    second.start()
    third.start()
    assert first_answered.wait(10), "the first call never answered"
    assert results["first"]["state"] == "DEAD_LETTERED"
    assert "second" not in seen
    assert "third" not in seen
    release.set()
    first.join(2)
    second.join(2)
    third.join(2)
    assert results["first"]["state"] == "DEAD_LETTERED"
    assert results["first"]["attempts"] == 1
    assert results["second"]["state"] == "RESPONDED"
    assert results["third"]["state"] == "RESPONDED"
    assert bus.open_circuits == set()
    assert seen.count("second") == 1
    assert seen.count("third") == 1


def test_expired_waiter_does_not_start_when_the_slot_frees(monkeypatch):
    from swarm.tools import support

    calls = []

    def fn(arg):
        calls.append(arg)
        return arg

    key = id(fn)
    support._running.add(key)
    started = time.monotonic()
    jumped = {"on": False}
    original = support.time.monotonic

    def monotonic():
        if jumped["on"]:
            return started + 10
        return original()

    def wait(timeout=None):
        jumped["on"] = True
        support._running.discard(key)
        return True

    monkeypatch.setattr(support.time, "monotonic", monotonic)
    monkeypatch.setattr(support._lane, "wait", wait)
    try:
        try:
            support.invoke_bounded(fn, "x", 0.2)
        except support.Waiting:
            pass
        else:
            raise AssertionError("expired waiter started")
    finally:
        support._running.discard(key)
    assert calls == []


def test_queue_timeout_is_retried_until_the_peer_is_free():
    import threading

    entered = threading.Event()
    release = threading.Event()
    seen = []

    def handler(envelope):
        correlation_id = envelope["correlation_id"]
        seen.append(correlation_id)
        if correlation_id == "holder":
            entered.set()
            release.wait(2)
        return {"correlation_id": correlation_id}

    bus = Bus(sleep=lambda _seconds: release.set(), max_attempts=2, breaker_threshold=1)
    bus.register("A03", handler)
    holder = threading.Thread(
        target=lambda: bus.request({**HAPPY, "correlation_id": "holder", "timeout_hint_s": 1})
    )
    holder.start()
    assert entered.wait(1)
    result = bus.request({**HAPPY, "correlation_id": "queued", "timeout_hint_s": 0.05})
    holder.join(2)
    assert result["state"] == "RESPONDED"
    assert result["attempts"] == 2
    assert "TIMED_OUT" in result["history"]
    assert seen.count("queued") == 1
    assert bus.dead == []
    assert bus.open_circuits == set()


def test_exhausted_queue_wait_is_dead_lettered_without_a_peer_failure():
    import threading

    entered = threading.Event()
    release = threading.Event()
    seen = []

    def handler(envelope):
        seen.append(envelope["correlation_id"])
        if envelope["correlation_id"] == "holder":
            entered.set()
            release.wait(2)
        return {"correlation_id": envelope["correlation_id"]}

    bus = Bus(sleep=lambda _seconds: None, max_attempts=2, breaker_threshold=1)
    bus.register("A03", handler)
    holder = threading.Thread(
        target=lambda: bus.request({**HAPPY, "correlation_id": "holder", "timeout_hint_s": 1})
    )
    holder.start()
    assert entered.wait(1)
    result = bus.request({**HAPPY, "correlation_id": "queued", "timeout_hint_s": 0.05})
    assert result["state"] == "DEAD_LETTERED"
    assert result["attempts"] == 2
    assert result["history"].count("TIMED_OUT") == 1
    assert "queued" not in seen
    assert bus.dead[-1]["envelope"]["correlation_id"] == "queued"
    assert bus.dead[-1]["envelope"]["payload"] == {"spec": "ping"}
    assert bus._failures.get("A03", 0) == 0
    assert bus.open_circuits == set()
    release.set()
    holder.join(2)


def test_slow_handler_is_not_started_again_while_it_is_still_running():
    starts = []

    def handler(envelope):
        starts.append(envelope["correlation_id"])
        time.sleep(0.3)
        return {"correlation_id": envelope["correlation_id"]}

    bus = Bus(sleep=lambda _seconds: (_ for _ in ()).throw(AssertionError("retried")), max_attempts=3)
    bus.register("A03", handler)
    result = bus.request({**HAPPY, "correlation_id": "corr-inflight", "timeout_hint_s": 0.05})
    assert result["state"] == "DEAD_LETTERED"
    assert result["attempts"] == 1
    assert starts == ["corr-inflight"]


def test_invoke_tool_uses_the_bus_registry():
    import json
    from pathlib import Path

    data = json.loads(Path("agents.json").read_text(encoding="utf-8"))
    tool = data["agents"][0]["registered_tools"][0]
    assert tool["name"] == "orch_registry_ping"
    tool["permitted_callers"] = ["A15"]
    bus = Bus(data=data)
    denied = bus.invoke_tool("A01", "orch_registry_ping", {"token": "ping"})
    assert denied["state"] == "UNAUTHORIZED"
    allowed = bus.invoke_tool("A15", "orch_registry_ping", {"token": "ping"})
    assert allowed["state"] == "SUCCESS"
    assert allowed["token"] == "ping"


def test_unauthorized_tool_is_audited():
    bus = Bus()
    result = bus.invoke_tool("A02", "orch_registry_ping", {"token": "ping"})
    assert result["state"] == "UNAUTHORIZED"
    assert result["caller"] == "A02"
    assert result["target"] == "orch_registry_ping"
    assert bus.audit[-1]["caller"] == "A02"
    assert bus.audit[-1]["target"] == "orch_registry_ping"
    assert bus.audit[-1]["reason"]
