"""swarm.event.v1, swarm.live.v1 and handover.v1 accept/reject fixtures, plus the validator keywords they rely on."""
from __future__ import annotations

import copy

import pytest

from swarm.lifecycle import RunEvent, RunState
from swarm.schema import load_schema, validate

# ---- validator keywords added for the v2.2 schemas ---------------------------------------------------------------


class TestValidatorKeywords:
    def test_const(self):
        sch = {"const": "x"}
        assert validate("x", sch) == []
        assert validate("y", sch) and validate(1, sch)
        assert validate(True, {"const": 1}), "bool is not the integer 1"

    def test_minimum_maximum_apply_to_numbers_only(self):
        sch = {"minimum": 0, "maximum": 1}
        assert validate(0, sch) == [] and validate(1, sch) == [] and validate(0.5, sch) == []
        assert "below the minimum" in validate(-0.1, sch)[0]
        assert "above the maximum" in validate(1.1, sch)[0]
        assert validate("text", sch) == [], "keyword is inert for non-numbers"

    def test_min_length(self):
        assert validate("a", {"minLength": 1}) == []
        assert "minLength" in validate("", {"minLength": 1})[0]
        assert validate(5, {"minLength": 1}) == []

    def test_validate_enforces_min_items(self):
        assert "minItems" in validate([], {"type": "array", "minItems": 1})[0]
        assert validate([1], {"type": "array", "minItems": 1}) == []
        assert validate("text", {"minItems": 1}) == [], "keyword is inert for non-arrays"

    def test_exclusive_minimum_is_strict(self):
        assert "exclusive minimum" in validate(0, {"exclusiveMinimum": 0})[0]
        assert validate(0.1, {"exclusiveMinimum": 0}) == []
        assert validate(True, {"exclusiveMinimum": 0}) == [], "keyword is inert for booleans"

    def test_unknown_type_name_raises(self):
        with pytest.raises(ValueError, match="unknown type"):
            validate(1, {"type": "integr"})

    def test_all_of_collects_every_branch(self):
        sch = {"allOf": [{"type": "object", "required": ["a"]}, {"type": "object", "required": ["b"]}]}
        assert validate({"a": 1, "b": 2}, sch) == []
        assert sorted(validate({}, sch)) == ["/a: required", "/b: required"]

    def test_if_then_only_fires_when_if_matches(self):
        sch = {"if": {"required": ["k"], "properties": {"k": {"const": 1}}}, "then": {"required": ["need"]}}
        assert validate({"k": 1, "need": 0}, sch) == []
        assert validate({"k": 1}, sch) == ["/need: required"]
        assert validate({"k": 2}, sch) == []
        assert validate({}, sch) == []

    def test_existing_keywords_unchanged(self):
        sch = load_schema("task.result.v1")
        assert validate({"task_id": "T1", "state": "FAILED"}, sch) == []
        assert validate({"task_id": "T1", "state": "DONE"}, sch)


# ---- swarm.event.v1 ----------------------------------------------------------------------------------------------

PAYLOADS = {
    "swarm.run.queued": {"run_id": "T7f3a-be.a1", "agent": "a05-backend", "attempt": 1, "priority": 2, "deps": ["T1"], "queue_depth": 3},
    "swarm.run.started": {"run_id": "T7f3a-be.a1", "agent": "a05-backend", "attempt": 1, "sid": None, "runtime": "omp",
                          "started_wall": 1_760_000_000.5, "deadline_wall": None, "budget_max_wall_s": 900},
    "swarm.run.state": {"run_id": "T7f3a-be.a1", "agent": "a05-backend", "from": "queued", "to": "starting",
                        "event": "slot_acquired", "reason": None, "wait_kind": None},
    "swarm.run.activity": {"run_id": "T7f3a-be.a1", "agent": "a05-backend", "tool": "edit", "text": "edit swarm/x.py",
                           "tool_calls": 12, "turns": 4, "tokens": 1200},
    "swarm.run.heartbeat": {"run_id": "T7f3a-be.a1", "agent": "a05-backend", "state": "running", "elapsed_s": 72.0,
                            "state_elapsed_s": 70.5, "since_activity_s": 3.2, "stalled": False, "tool_calls": 12,
                            "last_tool": "edit", "activity": "edit swarm/x.py"},
    "swarm.run.stalled": {"run_id": "T7f3a-be.a1", "agent": "a05-backend", "since_activity_s": 200.0, "threshold_s": 180},
    "swarm.run.finished": {"run_id": "T7f3a-be.a1", "agent": "a05-backend", "state": "completed", "outcome": "completed",
                           "duration_s": 91.2, "error_code": None, "error": None},
    "swarm.run.handover": {"run_id": "T7f3a-be.a1", "from_agent": "a05-backend", "to_agent": "a08-qa",
                           "handover_id": "H-1", "digest": "sha256:" + "ab" * 32, "bytes": 2048, "summarized": False},
    "swarm.scheduler.tick": {"round": 3, "ready": 5, "running": 4, "queued": 1, "waiting": 0, "free_slots": 0,
                             "max_parallel": 4, "effective_parallel": 4,
                             "per_agent": {"a05-backend": {"running": 2, "limit": 2}}, "skipped": {"T9": "claims conflict"}},
    "swarm.degraded": {"reason": "rate limit", "mode": "serial", "detail": None},
    "swarm.tool.call": {"tool": "qa_tier_plan", "caller": "a08-qa", "state": "SUCCESS", "duration_ms": 12.5},
}


def event(typ: str = "swarm.run.state", payload: dict | None = None) -> dict:
    return {"ts": "2026-10-18T10:00:00Z", "type": typ, "source": "telemetry", "correlation_id": "corr-1",
            "task_id": "T7f3a-be", "payload": copy.deepcopy(PAYLOADS[typ] if payload is None else payload), "msg_id": "ab12"}


EVENT = load_schema("swarm.event.v1")


def test_event_catalog_is_the_eleven_swarm_types():
    assert set(PAYLOADS) == set(EVENT["properties"]["type"]["enum"]) and len(PAYLOADS) == 11


def test_tool_call_event_error_state_is_optional_and_typed():
    ok = event("swarm.tool.call", {"tool": "t", "caller": "c", "state": "TIMEOUT", "duration_ms": 0, "error_state": "TIMEOUT"})
    assert validate(ok, EVENT) == []
    assert any("below the minimum" in e for e in validate(event("swarm.tool.call", {**PAYLOADS["swarm.tool.call"], "duration_ms": -1}), EVENT))
    assert validate(event("swarm.tool.call", {**PAYLOADS["swarm.tool.call"], "error_state": 5}), EVENT)
    assert "/payload/tool: required" in validate(event("swarm.tool.call", {"caller": "c"}), EVENT)


@pytest.mark.parametrize("typ", sorted(PAYLOADS))
def test_event_accepts_every_type(typ):
    assert validate(event(typ), EVENT) == []


@pytest.mark.parametrize("typ", sorted(PAYLOADS))
def test_event_each_required_payload_field_is_enforced(typ):
    for field in PAYLOADS[typ]:
        payload = {k: v for k, v in PAYLOADS[typ].items() if k != field}
        errs = validate(event(typ, payload), EVENT)
        assert f"/payload/{field}: required" in errs, (typ, field)


@pytest.mark.parametrize("priority", ["high", 2, 0.5, None])
def test_event_queued_priority_is_label_number_or_null(priority):
    ev = event("swarm.run.queued")
    ev["payload"]["priority"] = priority
    assert validate(ev, EVENT) == []
    ev["payload"]["priority"] = ["high"]
    assert validate(ev, EVENT)


def test_event_allows_extra_payload_fields_and_null_ids():
    ev = event("swarm.degraded")
    ev["payload"]["extra"] = {"anything": 1}
    ev["correlation_id"] = ev["task_id"] = None
    assert validate(ev, EVENT) == []


@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda e: e.update(type="swarm.run.exploded"), "not one of"),
    (lambda e: e.update(type="phase.init"), "not one of"),
    (lambda e: e.pop("msg_id"), "/msg_id: required"),
    (lambda e: e.pop("payload"), "/payload: required"),
    (lambda e: e.update(payload=[]), "expected object"),
    (lambda e: e.update(ts=""), "minLength"),
    (lambda e: e.update(task_id=7), "expected string|null"),
    (lambda e: e["payload"].update(**{"from": "sleeping"}), "not one of"),
    (lambda e: e["payload"].update(event="explode"), "not one of"),
    (lambda e: e["payload"].update(wait_kind="sleep"), "not one of"),
])
def test_event_rejects(mutate, needle):
    ev = event("swarm.run.state")
    mutate(ev)
    errs = validate(ev, EVENT)
    assert any(needle in e for e in errs), errs


@pytest.mark.parametrize(("attempt", "needle"), [(0, "below the minimum"), ("1", "expected integer")])
def test_event_attempt_is_a_positive_integer(attempt, needle):
    ev = event("swarm.run.queued")
    ev["payload"]["attempt"] = attempt
    assert any(needle in e for e in validate(ev, EVENT))


def test_event_run_state_enums_track_the_lifecycle_module():
    branch = next(b for b in EVENT["allOf"] if b["if"]["properties"]["type"]["const"] == "swarm.run.state")
    props = branch["then"]["properties"]["payload"]["properties"]
    assert props["from"]["enum"] == [s.value for s in RunState] == props["to"]["enum"]
    assert props["event"]["enum"] == [e.value for e in RunEvent]
    for ok in ("dependency", "input", None):
        assert validate(event("swarm.run.state", {**PAYLOADS["swarm.run.state"], "wait_kind": ok}), EVENT) == []


def test_event_type_specific_rules_do_not_leak_between_types():
    assert validate(event("swarm.degraded", {"reason": "r", "mode": "m", "detail": None}), EVENT) == []
    assert "/payload/run_id: required" in validate(event("swarm.run.queued", {"agent": "x"}), EVENT)
    assert not any("run_id" in e for e in validate(event("swarm.degraded", {"reason": "r", "mode": "m", "detail": None}), EVENT))


# ---- swarm.live.v1 -----------------------------------------------------------------------------------------------

LIVE = load_schema("swarm.live.v1")


def live_doc() -> dict:
    return {"schema": "swarm.live.v1", "correlation_id": "corr-1", "seq": 42, "updated_wall": 1_760_000_000.1,
            "swarm": {"state": "running", "elapsed_s": 252.4, "max_parallel": 4, "effective_parallel": 4, "running": 3,
                      "queued": 2, "waiting": 1, "completed": 4, "failed": 0, "cancelled": 0, "timed_out": 0,
                      "stalled": 1, "degraded": None},
            "runs": [{"run_id": "T7f3a-be.a1", "task_id": "T7f3a-be", "agent": "a05-backend", "state": "running",
                      "attempt": 1, "elapsed_s": 72.0, "state_elapsed_s": 70.5, "since_activity_s": 3.2, "stalled": False,
                      "activity": "edit swarm/x.py", "tool_calls": 12, "last_tool": "edit", "parent": None,
                      "handover_in": None, "handover_out": None, "error": None}]}


def test_live_accepts_design_example_and_degraded_marker():
    doc = live_doc()
    assert validate(doc, LIVE) == []
    doc["swarm"]["degraded"] = {"reason": "serial fallback", "mode": "degraded"}
    doc["runs"] = []
    assert validate(doc, LIVE) == []


@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda d: d.update(schema="swarm.live.v2"), "not the constant"),
    (lambda d: d.pop("seq"), "/seq: required"),
    (lambda d: d.update(seq=-1), "below the minimum"),
    (lambda d: d.update(seq=1.5), "expected integer"),
    (lambda d: d.update(updated_wall="now"), "expected number"),
    (lambda d: d["swarm"].pop("stalled"), "/swarm/stalled: required"),
    (lambda d: d["swarm"].update(running=-1), "below the minimum"),
    (lambda d: d["swarm"].update(max_parallel=0), "below the minimum"),
    (lambda d: d["swarm"].update(elapsed_s=-0.1), "below the minimum"),
    (lambda d: d["runs"][0].update(state="stalled"), "not one of"),
    (lambda d: d["runs"][0].update(state="RUNNING"), "not one of"),
    (lambda d: d["runs"][0].pop("agent"), "/runs/0/agent: required"),
    (lambda d: d["runs"][0].update(stalled="no"), "expected boolean"),
    (lambda d: d["runs"][0].update(elapsed_s=-5), "below the minimum"),
    (lambda d: d["runs"][0].update(attempt=0), "below the minimum"),
    (lambda d: d.update(runs={}), "expected array"),
])
def test_live_rejects(mutate, needle):
    doc = live_doc()
    mutate(doc)
    errs = validate(doc, LIVE)
    assert any(needle in e for e in errs), errs


def test_live_accepts_the_optional_producer_ownership_fields_and_rejects_bad_ones():
    doc = live_doc()
    doc.update(producer="scheduler", pid=4242, producer_updated=1_760_000_000.1)
    assert validate(doc, LIVE) == []
    doc.update(producer="derived")
    doc.pop("producer_updated")
    assert validate(doc, LIVE) == []
    for field, bad, needle in (("producer", "", "shorter than minLength"), ("producer", 7, "expected string"),
                               ("pid", 0, "below the minimum"), ("pid", "12", "expected integer"),
                               ("producer_updated", "now", "expected number")):
        broken = live_doc()
        broken.update(producer="scheduler", pid=4242, producer_updated=1.0)
        broken[field] = bad
        assert any(needle in e for e in validate(broken, LIVE)), (field, bad)
    assert "producer" not in LIVE["required"] and "pid" not in LIVE["required"]  # additive: old documents stay valid


def test_live_run_state_enum_tracks_the_lifecycle_module():
    assert LIVE["properties"]["runs"]["items"]["properties"]["state"]["enum"] == [s.value for s in RunState]


# ---- handover.v1 -------------------------------------------------------------------------------------------------

HANDOVER = load_schema("handover.v1")


def handover_doc() -> dict:
    return {"schema_version": "swarm.handover.v1", "handover_id": "H-20261018-1", "correlation_id": "corr-1",
            "task_id": "T7f3a-be", "from_agent": "a05-backend", "to_agent": "a08-qa", "created_wall": 1_760_000_000.0,
            "goal": "Ship the lifecycle module", "completed": ["state machine"], "remaining": ["wire scheduler"],
            "decisions": [{"decision": "projection over TaskState", "rationale": "D9"}],
            "artifacts": [{"kind": "file", "uri": "swarm/lifecycle.py", "digest": "sha256:" + "cd" * 32, "producer": "a05-backend"}],
            "open_questions": [], "blockers": [], "constraints": ["stdlib only"],
            "verification": {"status": "partial", "confidence": 0.6, "checks": ["pytest tests/test_lifecycle.py"]},
            "summarized": False, "bytes": 812}


def test_handover_accepts_valid_and_empty_collections():
    doc = handover_doc()
    assert validate(doc, HANDOVER) == []
    for key in ("completed", "remaining", "decisions", "artifacts", "open_questions", "blockers", "constraints"):
        doc[key] = []
    doc["verification"] = {"status": "unverified", "confidence": 0, "checks": []}
    assert validate(doc, HANDOVER) == []


@pytest.mark.parametrize("status", ["verified", "partial", "unverified"])
def test_handover_verification_statuses(status):
    doc = handover_doc()
    doc["verification"]["status"] = status
    assert validate(doc, HANDOVER) == []


@pytest.mark.parametrize("field", list(handover_doc()))
def test_handover_every_field_is_required(field):
    doc = handover_doc()
    del doc[field]
    assert f"/{field}: required" in validate(doc, HANDOVER)


@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda d: d.update(schema_version="swarm.handover.v2"), "not the constant"),
    (lambda d: d.update(schema_version=1), "not the constant"),
    (lambda d: d.update(goal=""), "minLength"),
    (lambda d: d.update(handover_id=""), "minLength"),
    (lambda d: d.update(to_agent=None), "expected string"),
    (lambda d: d.update(bytes=-1), "below the minimum"),
    (lambda d: d.update(summarized="yes"), "expected boolean"),
    (lambda d: d.update(completed="done"), "expected array"),
    (lambda d: d.update(blockers=[1]), "expected string"),
    (lambda d: d["verification"].update(status="done"), "not one of"),
    (lambda d: d["verification"].update(confidence=1.01), "above the maximum"),
    (lambda d: d["verification"].update(confidence=-0.01), "below the minimum"),
    (lambda d: d["verification"].pop("checks"), "/verification/checks: required"),
    (lambda d: d["decisions"][0].pop("rationale"), "/decisions/0/rationale: required"),
    (lambda d: d["artifacts"][0].pop("digest"), "/artifacts/0/digest: required"),
    (lambda d: d["artifacts"][0].update(uri=""), "minLength"),
    (lambda d: d["artifacts"][0].update(extra=1), "additional property not allowed"),
    (lambda d: d.update(surprise=1), "/surprise: additional property not allowed"),
])
def test_handover_rejects(mutate, needle):
    doc = handover_doc()
    mutate(doc)
    errs = validate(doc, HANDOVER)
    assert any(needle in e for e in errs), errs


def test_handover_non_object_is_rejected():
    assert validate([], HANDOVER) and validate(None, HANDOVER)
