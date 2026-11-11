"""Behaviour tests for the A19 Event-Driven & Serverless Engineer tools.

Tools: event_schema_compat, event_cloudevents_check, event_asyncapi_lint, event_partition_plan, event_lag_assessment,
event_retry_schedule, event_dlq_coverage, event_idempotency_window, event_ordering_check, event_function_sizing,
event_scaler_sanity, event_saga_coverage, event_retention_replay.

Every assertion is on output values for specific inputs, boundaries, INVALID_INPUT fields and callers. The registry
document is the live registry plus this lane's intake fragment while it is unmerged, so the file passes before and
after `scripts/_registry_intake.py --apply` merges and deletes the fragment.

tests/conftest.py evicts swarm* modules between tests: modules are resolved through importlib at call time.
"""
from __future__ import annotations

import copy
import functools
import importlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
AGENT, SLUG, PREFIX = "A19", "a19-event-driven", "event_"
INTAKE = ROOT / "registry" / "intake"
TOOL_NAMES = [
    "event_schema_compat", "event_cloudevents_check", "event_asyncapi_lint", "event_partition_plan", "event_lag_assessment",
    "event_retry_schedule", "event_dlq_coverage", "event_idempotency_window", "event_ordering_check",
    "event_function_sizing", "event_scaler_sanity", "event_saga_coverage", "event_retention_replay",
]


def _registry():
    return importlib.import_module("swarm.tool_registry")


def _fragment_entries() -> list[dict]:
    """Entries still waiting in the lane's intake fragment(s): <slug>.tools.json and <slug>.<shard>.tools.json."""
    entries: list[dict] = []
    for path in sorted(INTAKE.glob(f"{SLUG}.*tools.json")):
        entries.extend(json.loads(path.read_text(encoding="utf-8")))
    return entries


@functools.cache
def lane_document() -> dict:
    """The registry plus this lane's fragment entries that are not merged yet."""
    document = copy.deepcopy(_registry().load_document())
    agent = next(a for a in document["agents"] if a["id"] == AGENT)
    present = {t["name"] for t in agent["registered_tools"]}
    agent["registered_tools"].extend(copy.deepcopy(e) for e in _fragment_entries() if e["name"] not in present)
    return document


def lane_tools() -> list[dict]:
    return next(a for a in lane_document()["agents"] if a["id"] == AGENT)["registered_tools"]


def call(name: str, payload, caller: str = AGENT) -> dict:
    return _registry().call_tool(name, caller, payload, data=lane_document())


def ok(name: str, payload: dict) -> dict:
    result = call(name, payload)
    assert result["state"] == "SUCCESS", result
    return result


def invalid(name: str, payload, field: str) -> dict:
    result = call(name, payload)
    assert result["state"] == "INVALID_INPUT" and result["field"] == field, result
    return result


def fx(result: dict) -> list[tuple[str, str]]:
    """(severity, summary) of every finding, in the order returned."""
    return [(f["severity"], f["summary"]) for f in result["findings"]]


# --- registration ---------------------------------------------------------------------------------------------


def test_lane_registers_exactly_its_tools_as_read_only_owner_calls():
    tools = lane_tools()
    assert [t["name"] for t in tools] == TOOL_NAMES
    assert 10 <= len(tools) <= 15
    for tool in tools:
        suffix = tool["name"][len(PREFIX):]
        assert tool["name"].startswith(PREFIX) and tool["owner"] == AGENT and tool["permitted_callers"] == [AGENT]
        assert tool["side_effect_tier"] == "READ" and tool["idempotency"] == "idempotent"
        assert tool["entrypoint"] == f"swarm/tools/{AGENT.lower()}_tools.py"
        assert tool["handler"] == f"swarm.tools.{AGENT.lower()}_tools:{suffix}"
        assert "probe" in tool["input_schema"]["properties"] and "|" not in tool["purpose"]
        assert tool["output_schema"]["properties"]["state"] == {"type": "string", "const": "SUCCESS"}


def test_lane_entries_validate_in_a_copy_of_the_registry(tmp_path):
    copy_path = tmp_path / "agents.json"
    copy_path.write_text(json.dumps(lane_document()), encoding="utf-8")
    errors = _registry().validate_registry(copy_path)
    mine = [e for e in errors if AGENT in e or any(name in e for name in TOOL_NAMES)]
    assert mine == []


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_every_tool_refuses_other_callers_and_malformed_payloads(name):
    sample = next(t for t in lane_tools() if t["name"] == name)["examples"]["success"]
    assert call(name, sample, caller="A01")["state"] == "UNAUTHORIZED"
    assert call(name, {})["state"] == "INVALID_INPUT"
    assert call(name, [sample])["field"] == "payload"
    assert call(name, {**sample, "probe": "partial"}) == {"state": "PARTIAL_SUCCESS", "completed_fraction": 0.5}
    assert call(name, {**sample, "probe": "dependency"})["state"] == "DEPENDENCY_UNAVAILABLE"


# --- event_schema_compat: JSON Schema -------------------------------------------------------------------------


def compat(base, head, mode="backward", fmt="json-schema"):
    return ok("event_schema_compat", {"format": fmt, "mode": mode, "base_schema": json.dumps(base), "head_schema": json.dumps(head)})


def obj_schema(props, required=(), **extra):
    schema = {"type": "object", "properties": props, **extra}
    if required:
        schema["required"] = list(required)
    return schema


STR, INT = {"type": "string"}, {"type": "integer"}
MODERN = "https://json-schema.org/draft/2020-12/schema"  # declared draft where a $ref's sibling keywords apply


def test_schema_compat_optional_property_added_to_closed_schema_is_backward_only():
    base = obj_schema({"id": STR}, ["id"], additionalProperties=False)
    head = obj_schema({"id": STR, "note": STR}, ["id"], additionalProperties=False)
    result = compat(base, head, "full")
    assert (result["backward_compatible"], result["forward_compatible"], result["compatible"]) == (True, False, False)
    assert fx(result) == [("blocker", "[forward] $.note: accepted by the source but rejected by the closed target schema")]
    assert (result["breaking_changes"], result["verdict"]) == (1, "fail")
    assert compat(base, head, "backward")["verdict"] == "pass"
    assert compat(base, head, "backward")["findings"] == []


def test_schema_compat_open_content_model_makes_added_optional_property_forward_only():
    base = obj_schema({"id": STR}, ["id"])
    head = obj_schema({"id": STR, "note": STR}, ["id"])
    result = compat(base, head, "full")
    assert (result["backward_compatible"], result["forward_compatible"]) == (False, True)
    assert fx(result) == [("blocker", "[backward] $.note: the source is an open content model and accepts any value here, "
                                      "the target constrains it")]


def test_schema_compat_removing_a_property_in_an_open_model_is_backward_only():
    result = compat(obj_schema({"id": STR, "note": STR}, ["id"]), obj_schema({"id": STR}, ["id"]), "full")
    assert (result["backward_compatible"], result["forward_compatible"]) == (True, False)


def test_schema_compat_required_property_added_breaks_backward_only():
    base = obj_schema({"id": STR, "qty": INT}, ["id"], additionalProperties=False)
    head = obj_schema({"id": STR, "qty": INT}, ["id", "qty"], additionalProperties=False)
    result = compat(base, head, "full")
    assert (result["backward_compatible"], result["forward_compatible"]) == (False, True)
    assert fx(result) == [("blocker", "[backward] $.qty: required by the target but optional or undeclared in the source")]


def test_schema_compat_type_change_breaks_both_directions_and_widening_integer_to_number_is_one_way():
    both = compat(obj_schema({"v": STR}, additionalProperties=False), obj_schema({"v": INT}, additionalProperties=False), "full")
    assert (both["backward_compatible"], both["forward_compatible"]) == (False, False)
    assert fx(both) == [("blocker", "[backward] $.v: type ['string'] is not accepted by target type ['integer']"),
                        ("blocker", "[forward] $.v: type ['integer'] is not accepted by target type ['string']")]
    widen = compat(obj_schema({"v": INT}, additionalProperties=False), obj_schema({"v": {"type": "number"}}, additionalProperties=False), "full")
    assert (widen["backward_compatible"], widen["forward_compatible"]) == (True, False)
    assert fx(widen) == [("blocker", "[forward] $.v: type ['number'] is not accepted by target type ['integer']")]


def test_schema_compat_enum_widening_is_backward_compatible_and_narrowing_is_not():
    two = obj_schema({"s": {"type": "string", "enum": ["a", "b"]}}, additionalProperties=False)
    three = obj_schema({"s": {"type": "string", "enum": ["a", "b", "c"]}}, additionalProperties=False)
    result = compat(two, three, "full")
    assert (result["backward_compatible"], result["forward_compatible"]) == (True, False)
    assert fx(result) == [("major", '[forward] $.s: value(s) "c" accepted by the source are not in the target enum')]
    assert compat(two, obj_schema({"s": STR}, additionalProperties=False), "backward")["backward_compatible"] is True
    restricted = compat(obj_schema({"s": STR}, additionalProperties=False), two, "backward")
    assert fx(restricted) == [("major", "[backward] $.s: target restricts to an enum but the source accepts any value")]


def test_schema_compat_const_counts_as_single_value_enum_and_1_equals_1_point_0():
    base = obj_schema({"n": {"const": 1}}, additionalProperties=False)
    head = obj_schema({"n": {"enum": [1.0, 2]}}, additionalProperties=False)
    result = compat(base, head, "full")
    assert (result["backward_compatible"], result["forward_compatible"]) == (True, False)


@pytest.mark.parametrize(
    "base_bound, head_bound, backward_ok",
    [
        ({"minimum": 1}, {"minimum": 1}, True),
        ({"minimum": 2}, {"minimum": 1}, True),
        ({"minimum": 0}, {"minimum": 1}, False),
        ({}, {"minimum": 1}, False),
        ({"minimum": 1}, {"exclusiveMinimum": 1}, False),
        ({"exclusiveMinimum": 1}, {"minimum": 1}, True),
        ({"exclusiveMinimum": 1}, {"exclusiveMinimum": 1}, True),
        ({"maximum": 10}, {"maximum": 10}, True),
        ({"maximum": 11}, {"maximum": 10}, False),
        ({"maximum": 10}, {"exclusiveMaximum": 10}, False),
        ({"exclusiveMaximum": 10}, {"maximum": 10}, True),
        ({"maximum": 9}, {"exclusiveMaximum": 10}, True),
    ],
)
def test_schema_compat_numeric_bounds_boundaries(base_bound, head_bound, backward_ok):
    base = obj_schema({"n": {"type": "integer", **base_bound}}, additionalProperties=False)
    head = obj_schema({"n": {"type": "integer", **head_bound}}, additionalProperties=False)
    assert compat(base, head)["backward_compatible"] is backward_ok


def test_schema_compat_numeric_bound_message_names_direction_and_limit():
    base = obj_schema({"n": {"type": "integer", "minimum": 0}}, additionalProperties=False)
    head = obj_schema({"n": {"type": "integer", "minimum": 1}}, additionalProperties=False)
    assert fx(compat(base, head)) == [("major", "[backward] $.n: numeric lower bound loosened (target >= 1)")]
    assert fx(compat({"type": "integer"}, {"type": "integer", "exclusiveMaximum": 5})) == [
        ("major", "[backward] $: numeric upper bound removed (target < 5)")]


@pytest.mark.parametrize(
    "base_len, head_len, backward_ok",
    [
        ({"maxLength": 10}, {"maxLength": 10}, True),
        ({"maxLength": 5}, {"maxLength": 10}, True),
        ({"maxLength": 11}, {"maxLength": 10}, False),
        ({}, {"maxLength": 10}, False),
        ({"minLength": 3}, {"minLength": 3}, True),
        ({"minLength": 2}, {"minLength": 3}, False),
        ({}, {"minLength": 0}, True),
    ],
)
def test_schema_compat_string_length_boundaries(base_len, head_len, backward_ok):
    base = obj_schema({"s": {"type": "string", **base_len}}, additionalProperties=False)
    head = obj_schema({"s": {"type": "string", **head_len}}, additionalProperties=False)
    assert compat(base, head)["backward_compatible"] is backward_ok


def test_schema_compat_array_rules_items_length_and_uniqueness():
    arr = lambda **kw: obj_schema({"a": {"type": "array", **kw}}, additionalProperties=False)  # noqa: E731
    assert compat(arr(items=STR), arr(items={"type": "string", "maxLength": 5}))["backward_compatible"] is False
    assert fx(compat(arr(items=STR), arr(items={"type": "string", "maxLength": 5}))) == [
        ("major", "[backward] $.a[]: maxLength is above the target maximum 5 (string length)")]
    assert compat(arr(items=STR, maxItems=3), arr(items=STR, maxItems=3))["backward_compatible"] is True
    assert compat(arr(items=STR, maxItems=4), arr(items=STR, maxItems=3))["backward_compatible"] is False
    assert compat(arr(items=STR), arr(items=STR, minItems=1))["backward_compatible"] is False
    assert fx(compat(arr(items=STR), arr(items=STR, uniqueItems=True))) == [
        ("major", "[backward] $.a: target requires uniqueItems but the source does not")]
    assert compat(arr(items=STR, uniqueItems=True), arr(items=STR))["backward_compatible"] is True


def test_schema_compat_pattern_change_and_format_change():
    base = obj_schema({"s": {"type": "string", "pattern": "^a"}}, additionalProperties=False)
    head = obj_schema({"s": {"type": "string", "pattern": "^b"}}, additionalProperties=False)
    assert fx(compat(base, head)) == [("major", "[backward] $.s: pattern changed; old values cannot be proven to match the target pattern")]
    assert compat(base, base)["findings"] == []
    fmt = compat(obj_schema({"s": STR}, additionalProperties=False),
                 obj_schema({"s": {"type": "string", "format": "date-time"}}, additionalProperties=False))
    assert fx(fmt) == [("minor", "[backward] $.s: format changed to 'date-time' (annotation unless the validator asserts formats)")]
    assert (fmt["compatible"], fmt["verdict"]) == (True, "warn")


def test_schema_compat_nested_objects_refs_and_recursion():
    base = {"$ref": "#/definitions/Order", "definitions": {"Order": obj_schema({"line": {"$ref": "#/definitions/Line"}},
                                                                             additionalProperties=False),
                                                           "Line": obj_schema({"sku": STR}, ["sku"], additionalProperties=False)}}
    head = copy.deepcopy(base)
    head["definitions"]["Line"] = obj_schema({"sku": INT}, ["sku"], additionalProperties=False)
    assert fx(compat(base, head)) == [("blocker", "[backward] $.line.sku: type ['string'] is not accepted by target type ['integer']")]
    tree = {"$ref": "#/$defs/Node", "$defs": {"Node": obj_schema({"children": {"type": "array", "items": {"$ref": "#/$defs/Node"}}},
                                                                additionalProperties=False)}}
    assert compat(tree, copy.deepcopy(tree), "full")["findings"] == []


def test_schema_compat_any_of_alternatives():
    base = {"anyOf": [STR, INT]}
    narrow = {"anyOf": [STR]}
    assert fx(compat(base, narrow, "backward")) == [("major", "[backward] $<alt1>: not accepted by any alternative of the target anyOf/oneOf")]
    assert compat(base, narrow, "forward")["forward_compatible"] is True
    nowhere = compat(STR, {"anyOf": [INT, {"type": "boolean"}]})
    assert fx(nowhere) == [("major", "[backward] $: not accepted by any alternative of the target anyOf/oneOf")]


def test_schema_compat_reports_unanalysed_keywords_without_failing():
    base = {"type": "object", "allOf": [{"required": ["a"]}], "additionalProperties": False, "properties": {"a": STR}}
    result = compat(base, copy.deepcopy(base))
    assert fx(result) == [("minor", "$: keyword 'allOf' is not analysed; verify this part manually")]
    assert (result["compatible"], result["verdict"], result["breaking_changes"]) == (True, "warn", 0)


def test_schema_compat_additional_properties_schema_and_closed_models():
    base = obj_schema({}, additionalProperties=STR)
    assert fx(compat(base, obj_schema({}, additionalProperties=INT))) == [
        ("blocker", "[backward] $.*: type ['string'] is not accepted by target type ['integer']")]
    assert fx(compat(obj_schema({}), obj_schema({}, additionalProperties=False))) == [
        ("blocker", "[backward] $: the source accepts undeclared properties but the target is closed (additionalProperties false)")]
    assert fx(compat(obj_schema({}), obj_schema({}, additionalProperties=STR))) == [
        ("major", "[backward] $: the source accepts any undeclared property, the target constrains them")]


def test_schema_compat_boolean_schemas():
    assert compat(True, False)["findings"][0] == {"id": "SC-001", "severity": "blocker",
                                                  "summary": "[backward] $: no longer accepted (the target schema is `false`)"}
    assert compat(False, False)["findings"] == []


def test_schema_compat_findings_are_numbered_sorted_by_severity_then_text():
    base = obj_schema({"a": STR, "b": {"type": "string", "maxLength": 9}}, additionalProperties=False)
    head = obj_schema({"a": INT, "b": {"type": "string", "maxLength": 5}}, additionalProperties=False)
    result = compat(base, head)
    assert [(f["id"], f["severity"]) for f in result["findings"]] == [("SC-001", "blocker"), ("SC-002", "major")]
    assert result["breaking_changes"] == 2


@pytest.mark.parametrize(
    "base, head, field",
    [
        ("{not json", "{}", "base_schema"),
        ("{}", "[1,", "head_schema"),
        ("{}", '{"$ref": "#/definitions/missing"}', "head_schema"),
        ("[]", "{}", "base_schema"),
        ('{"type": "datetime"}', "{}", "base_schema"),
        ("{}", '{"$ref": "http://example.com/s.json"}', "head_schema"),
    ],
)
def test_schema_compat_json_schema_invalid_input_names_the_field(base, head, field):
    invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": base, "head_schema": head}, field)


# --- event_schema_compat: Avro --------------------------------------------------------------------------------


def avro_record(*fields, name="Order", **extra):
    return {"type": "record", "name": name, "fields": list(fields), **extra}


F_ID = {"name": "id", "type": "string"}


def test_avro_field_added_with_default_is_fully_compatible():
    head = avro_record(F_ID, {"name": "note", "type": "string", "default": ""})
    result = compat(avro_record(F_ID), head, "full", "avro")
    assert (result["backward_compatible"], result["forward_compatible"], result["compatible"]) == (True, True, True)
    assert result["findings"] == []


def test_avro_field_added_without_default_breaks_backward_and_removed_field_breaks_forward():
    base, head = avro_record(F_ID), avro_record(F_ID, {"name": "email", "type": "string"})
    added = compat(base, head, "full", "avro")
    assert (added["backward_compatible"], added["forward_compatible"]) == (False, True)
    assert fx(added) == [("blocker", "[backward] $.email: reader field has no default and the writer does not provide it")]
    removed = compat(head, base, "full", "avro")
    assert (removed["backward_compatible"], removed["forward_compatible"]) == (True, False)
    assert fx(removed) == [("blocker", "[forward] $.email: reader field has no default and the writer does not provide it")]


def test_avro_default_of_null_counts_as_a_default():
    head = avro_record(F_ID, {"name": "note", "type": ["null", "string"], "default": None})
    assert compat(avro_record(F_ID), head, "backward", "avro")["compatible"] is True


@pytest.mark.parametrize(
    "writer, reader, ok",
    [
        ("int", "long", True), ("int", "float", True), ("int", "double", True), ("long", "float", True),
        ("long", "double", True), ("float", "double", True), ("string", "bytes", True), ("bytes", "string", True),
        ("long", "int", False), ("double", "float", False), ("double", "long", False), ("string", "int", False),
        ("boolean", "int", False), ("null", "string", False), ("string", "string", True),
    ],
)
def test_avro_primitive_promotion_table(writer, reader, ok):
    result = compat(writer, reader, "backward", "avro")  # base is the writer, head the reader
    assert result["backward_compatible"] is ok
    if not ok:
        assert fx(result) == [("blocker", f"[backward] $: {writer} cannot be read as {reader}")]


def test_avro_enum_symbols_and_reader_default():
    base = {"type": "enum", "name": "Status", "symbols": ["A", "B"]}
    head = {"type": "enum", "name": "Status", "symbols": ["A", "B", "C"]}
    result = compat(base, head, "full", "avro")
    assert (result["backward_compatible"], result["forward_compatible"]) == (True, False)
    assert fx(result) == [("blocker", "[forward] $: enum symbol(s) C unknown to the reader, which has no default")]
    with_default = {**base, "default": "A"}
    assert compat(base, {**head}, "forward", "avro")["forward_compatible"] is False
    assert compat({**with_default}, head, "forward", "avro")["forward_compatible"] is True


def test_avro_union_widening_and_narrowing():
    result = compat("string", ["null", "string"], "full", "avro")
    assert (result["backward_compatible"], result["forward_compatible"]) == (True, False)
    assert fx(result) == [("blocker", "[forward] $<0:null>: null cannot be read as string")]
    miss = compat(["null", "string"], ["null", "int"], "backward", "avro")
    assert fx(miss) == [("blocker", "[backward] $<1:string>: no branch of the reader union accepts string")]


def test_avro_containers_fixed_aliases_and_names():
    assert compat({"type": "array", "items": "int"}, {"type": "array", "items": "long"}, "backward", "avro")["compatible"] is True
    assert fx(compat({"type": "map", "values": "long"}, {"type": "map", "values": "int"}, "backward", "avro")) == [
        ("blocker", "[backward] ${}: long cannot be read as int")]
    fixed = lambda size: {"type": "fixed", "name": "Hash", "size": size}  # noqa: E731
    assert fx(compat(fixed(16), fixed(32), "backward", "avro")) == [("blocker", "[backward] $: fixed size 16 differs from reader size 32")]
    renamed = avro_record(F_ID, name="Purchase", aliases=["Order"])
    assert compat(avro_record(F_ID), renamed, "backward", "avro")["compatible"] is True
    assert fx(compat(avro_record(F_ID), avro_record(F_ID, name="Other"), "backward", "avro")) == [
        ("blocker", "[backward] $: record name Order does not match reader name Other")]
    aliased = avro_record({"name": "identifier", "type": "string", "aliases": ["id"]})
    assert compat(avro_record(F_ID), aliased, "backward", "avro")["compatible"] is True


def test_avro_logical_type_change_and_namespaced_references_and_recursion():
    ts = lambda lt: avro_record({"name": "t", "type": {"type": "long", **({"logicalType": lt} if lt else {})}})  # noqa: E731
    assert fx(compat(ts(""), ts("timestamp-millis"), "backward", "avro")) == [
        ("major", "[backward] $.t: logicalType changed from none to timestamp-millis")]
    nested = {"type": "record", "name": "Outer", "namespace": "com.acme", "fields": [
        {"name": "a", "type": {"type": "record", "name": "Inner", "fields": [{"name": "x", "type": "int"}]}},
        {"name": "b", "type": "Inner"}]}
    assert compat(nested, copy.deepcopy(nested), "full", "avro")["findings"] == []
    linked = {"type": "record", "name": "Node", "fields": [{"name": "next", "type": ["null", "Node"], "default": None}]}
    assert compat(linked, copy.deepcopy(linked), "full", "avro")["compatible"] is True


@pytest.mark.parametrize(
    "base, head, field",
    [
        ('"nope"', '"string"', "base_schema"),
        ('"string"', '{"type": "record", "name": "R"}', "head_schema"),
        ('"string"', '{"type": "enum", "name": "E", "symbols": []}', "head_schema"),
        ('"string"', '{"type": "fixed", "name": "F", "size": "x"}', "head_schema"),
        ('"string"', '[]', "head_schema"),
        ('"string"', '5', "head_schema"),
        ('"string"', '{"type": "record", "fields": []}', "head_schema"),
        ('{"type": "record", "name": "R", "fields": [{"name": "a"}]}', '"string"', "base_schema"),
        ('"string"', '{"type": "mystery"}', "head_schema"),
    ],
)
def test_avro_invalid_schema_names_the_field(base, head, field):
    invalid("event_schema_compat", {"format": "avro", "mode": "backward", "base_schema": base, "head_schema": head}, field)


def test_schema_compat_mode_restricts_reported_findings():
    base, head = avro_record(F_ID), avro_record(F_ID, {"name": "email", "type": "string"})
    assert compat(base, head, "forward", "avro")["findings"] == []
    assert compat(base, head, "forward", "avro")["compatible"] is True
    assert compat(base, head, "backward", "avro")["compatible"] is False


# --- event_cloudevents_check ----------------------------------------------------------------------------------


def ce(attrs, has_data=False):
    return ok("event_cloudevents_check", {"attributes": [{"name": k, "value": v} for k, v in attrs], "has_data": has_data})


CE_BASE = [("specversion", "1.0"), ("id", "1"), ("source", "/orders"), ("type", "com.acme.order.created")]


def test_cloudevents_minimal_valid_event():
    result = ce(CE_BASE)
    assert result == {"state": "SUCCESS", "valid": True, "required_present": 4, "optional_present": 0,
                      "extension_attributes": [], "verdict": "pass", "findings": []}


def test_cloudevents_missing_required_attributes_are_blockers_in_sorted_order():
    result = ce([("specversion", "1.0"), ("type", "com.acme.x")])
    assert (result["valid"], result["required_present"], result["verdict"]) == (False, 2, "fail")
    assert fx(result) == [("blocker", "required attribute 'id' is missing"), ("blocker", "required attribute 'source' is missing")]


def test_cloudevents_empty_required_value_and_wrong_specversion():
    result = ce([("specversion", "0.3"), ("id", ""), ("source", "/s"), ("type", "a.b")])
    assert fx(result) == [("blocker", "required attribute 'id' must not be empty"),
                          ("blocker", "specversion is '0.3'; this check validates CloudEvents 1.0")]
    assert result["required_present"] == 3


def test_cloudevents_time_must_be_rfc3339_with_offset():
    for good in ("2026-10-20T10:00:00Z", "2026-10-20T10:00:00.123+02:00", "2026-10-20t10:00:00z", "2026-12-31T23:59:60Z"):
        assert ce(CE_BASE + [("time", good)])["findings"] == [], good
    for bad in ("2026-10-20 10:00:00Z", "2026-10-20T10:00:00", "2026-13-01T10:00:00Z", "2026-10-20T24:00:00Z",
                "2026-10-20T10:00:00+25:00", "2026-10-20T10:00:61Z", "yesterday", "2026-02-30T10:00:00Z"):
        assert fx(ce(CE_BASE + [("time", bad)])) == [("major", f"time {bad!r} is not an RFC 3339 timestamp")], bad


def test_cloudevents_dataschema_must_be_an_absolute_uri():
    assert ce(CE_BASE + [("dataschema", "https://example.com/s.json")])["findings"] == []
    assert ce(CE_BASE + [("dataschema", "urn:acme:schema:order")])["findings"] == []
    assert fx(ce(CE_BASE + [("dataschema", "/schemas/order.json")])) == [("major", "dataschema '/schemas/order.json' must be an absolute URI")]


def test_cloudevents_datacontenttype_and_data_presence():
    assert ce(CE_BASE + [("datacontenttype", "application/json; charset=utf-8")], has_data=True)["findings"] == []
    assert fx(ce(CE_BASE + [("datacontenttype", "json")], has_data=True)) == [
        ("major", "datacontenttype 'json' is not a valid RFC 2046 media type")]
    assert fx(ce(CE_BASE + [("datacontenttype", "application/json")])) == [("minor", "datacontenttype is set but the event carries no data")]
    assert fx(ce(CE_BASE, has_data=True)) == [("info", "data without datacontenttype: consumers must assume application/json")]


def test_cloudevents_attribute_name_rules_and_extension_listing():
    twenty, twenty_one = "a" * 20, "a" * 21
    result = ce(CE_BASE + [(twenty, "x"), ("zeta", "1"), ("alpha1", "2")])
    assert result["extension_attributes"] == [twenty, "alpha1", "zeta"] and result["findings"] == []
    assert fx(ce(CE_BASE + [(twenty_one, "x")])) == [
        ("minor", f"attribute name '{twenty_one}' is longer than the recommended 20 characters")]
    assert fx(ce(CE_BASE + [("Trace", "x")])) == [("blocker", "attribute name 'Trace' must use only lower-case a-z and digits 0-9")]
    assert fx(ce(CE_BASE + [("trace-id", "x")]))[0][0] == "blocker"


def test_cloudevents_type_prefix_subject_source_and_duplicates():
    assert fx(ce([("specversion", "1.0"), ("id", "1"), ("source", "/s"), ("type", "created")])) == [
        ("minor", "type 'created' should be prefixed with a reverse-DNS name (for example com.example.order.created)")]
    assert fx(ce(CE_BASE + [("subject", "")])) == [("major", "subject must not be empty when present")]
    assert fx(ce([("specversion", "1.0"), ("id", "1"), ("source", "has space"), ("type", "a.b")])) == [
        ("major", "source must be a URI-reference without whitespace")]
    assert fx(ce(CE_BASE + [("id", "2")])) == [("blocker", "attribute 'id' appears more than once")]
    result = ce(CE_BASE + [("subject", "s"), ("time", "2026-10-20T10:00:00Z")])
    assert (result["optional_present"], result["extension_attributes"]) == (2, [])


def test_cloudevents_payload_names_are_not_context_attributes():
    for name in ("data", "data_base64"):
        invalid("event_cloudevents_check", {"attributes": [{"name": name, "value": "x"}], "has_data": True}, "attributes")


# --- event_asyncapi_lint --------------------------------------------------------------------------------------


def aa(document):
    return ok("event_asyncapi_lint", {"document": json.dumps(document)})


MSG = {"payload": {"type": "object"}, "contentType": "application/json"}
V3 = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
      "channels": {"orders": {"address": "orders", "messages": {"created": MSG}}},
      "operations": {"publish": {"action": "send", "channel": {"$ref": "#/channels/orders"}}}}
V2 = {"asyncapi": "2.6.0", "info": {"title": "T", "version": "1"},
      "channels": {"orders/{id}": {"parameters": {"id": {"schema": {"type": "string"}}},
                                   "publish": {"operationId": "pub", "message": MSG},
                                   "subscribe": {"operationId": "sub", "message": {"oneOf": [MSG, {"$ref": "#/components/messages/M"}]}}}},
      "components": {"messages": {"M": MSG}}, "servers": {"prod": {"url": "broker:9092", "protocol": "kafka"}}}


def test_asyncapi_valid_v3_and_v2_documents():
    assert aa(V3) == {"state": "SUCCESS", "asyncapi_version": "3.0.0", "channels": 1, "operations": 1, "messages": 1,
                      "verdict": "pass", "findings": []}
    result = aa(V2)
    assert (result["asyncapi_version"], result["channels"], result["operations"], result["messages"]) == ("2.6.0", 1, 2, 3)
    assert result["findings"] == [] and result["verdict"] == "pass"


def test_asyncapi_v2_structural_problems():
    doc = copy.deepcopy(V2)
    doc["info"] = {"title": "T"}
    doc["channels"]["orders/{id}"]["parameters"] = {}
    doc["channels"]["orders/{id}"]["subscribe"]["operationId"] = "pub"
    doc["channels"]["empty"] = {}
    doc["channels"]["nomsg"] = {"publish": {"operationId": "x"}}
    doc["channels"]["nopayload"] = {"publish": {"operationId": "y", "message": {"contentType": "application/json"}}}
    doc["servers"]["bad"] = {"url": "x"}
    result = aa(doc)
    assert result["verdict"] == "fail"
    assert fx(result) == [
        ("blocker", "info.version is required"),
        ("blocker", "servers/bad: protocol is required"),
        ("major", "channels/orders/{id}/subscribe: operationId 'pub' duplicates channels/orders/{id}/publish"),
        ("major", "channels/orders/{id}: parameter {id} is not declared under parameters"),
        ("minor", "channels/empty: no publish or subscribe operation"),
        ("minor", "channels/nomsg/publish: operation has no message (the AsyncAPI 2.x schema does not require one, but consumers "
                  "get no contract)"),
        ("minor", "channels/nopayload/publish/message: message has no payload schema, so consumers have no contract"),
    ]


def test_asyncapi_v2_requires_channels_and_content_type_unless_default():
    no_channels = aa({"asyncapi": "2.0.0", "info": {"title": "T", "version": "1"}})
    assert fx(no_channels) == [("blocker", "channels is required and must be an object in AsyncAPI 2.x")]
    empty = aa({"asyncapi": "2.6.0", "info": {"title": "T", "version": "1"}, "channels": {}})
    assert fx(empty) == [("minor", "channels is empty: the document describes no channel")]  # {} is valid under the 2.6.0 JSON Schema
    doc = copy.deepcopy(V2)
    doc["channels"]["orders/{id}"]["publish"]["message"] = {"payload": {}}
    assert fx(aa(doc)) == [("minor", "channels/orders/{id}/publish/message: no contentType and no defaultContentType")]
    doc["defaultContentType"] = "application/json"
    assert aa(doc)["findings"] == []


def test_asyncapi_v3_operation_rules_and_servers():
    doc = copy.deepcopy(V3)
    doc["operations"] = {"a": {"action": "publish", "channel": {"$ref": "#/channels/orders"}},
                         "b": {"action": "send", "channel": "orders"},
                         "c": {"action": "receive", "channel": {"$ref": "#/components/channels/orders"}}}
    doc["channels"]["silent"] = {"address": "silent"}
    doc["servers"] = {"s": {"host": "h"}}
    result = aa(doc)
    assert fx(result) == [
        ("blocker", "operations/a: action must be send or receive"),
        ("blocker", "operations/b: channel must be a $ref to #/channels/..."),
        ("blocker", "servers/s: protocol is required"),
        ("major", "#/operations/c/channel: $ref '#/components/channels/orders' does not resolve"),
        ("major", "operations/c: channel $ref '#/components/channels/orders' must point at #/channels/..."),
        ("minor", "channels/silent: no messages declared"),
    ]
    assert result["operations"] == 3


def test_asyncapi_dangling_and_external_refs_version_and_info():
    doc = copy.deepcopy(V3)
    doc["channels"]["orders"]["messages"]["gone"] = {"$ref": "#/components/messages/Gone"}
    doc["channels"]["orders"]["messages"]["ext"] = {"$ref": "https://example.com/m.json"}
    assert fx(aa(doc)) == [
        ("major", "#/channels/orders/messages/gone: $ref '#/components/messages/Gone' does not resolve"),
        ("info", "#/channels/orders/messages/ext: external $ref 'https://example.com/m.json' is not resolved by this lint"),
    ]
    assert aa({"asyncapi": "1.2.0", "info": {"title": "T", "version": "1"}})["findings"][0] == {
        "id": "AA-001", "severity": "blocker", "summary": "AsyncAPI 1.2.0 is not supported by this lint (2.x and 3.x)"}
    assert fx(aa({"asyncapi": "3.0.0"})) == [("blocker", "info.title is required"), ("blocker", "info.version is required")]


@pytest.mark.parametrize("document", ["{nope", "[1]", '{"info": {}}', '{"asyncapi": "3"}', '{"asyncapi": 3}',
                                      '{"asyncapi": "3.0.0-' + "a" * 40 + '"}'])
def test_asyncapi_invalid_document_names_the_field(document):
    invalid("event_asyncapi_lint", {"document": document}, "document")


# --- event_partition_plan -------------------------------------------------------------------------------------


def plan(**over):
    payload = {"peak_msgs_per_s": 5000, "avg_msg_bytes": 1024, "partition_max_mib_per_s": 10, "consumer_max_msgs_per_s": 500,
               "headroom_pct": 20, "hot_key_share_pct": 0}
    payload.update(over)
    return ok("event_partition_plan", payload)


def test_partition_plan_uniform_load_is_bound_by_consumer_capacity():
    result = plan()
    assert result == {
        "state": "SUCCESS", "required_partitions": 12, "partitions_by_throughput": 1, "partitions_by_consumers": 12,
        "partitions_by_skew": 12, "evaluated_partitions": 12, "target_msgs_per_s": 6000.0, "throughput_mib_per_s": 5.8594,
        "hot_partition_msgs_per_s": 500.0, "skew_ratio": 1.0, "max_consumer_parallelism": 12, "verdict": "pass", "findings": []}


def test_partition_plan_hot_key_raises_the_count_and_flags_skew():
    result = plan(hot_key_share_pct=5)
    assert (result["required_partitions"], result["partitions_by_skew"], result["hot_partition_msgs_per_s"], result["skew_ratio"]) == (
        29, 29, 496.55, 2.4)
    assert fx(result) == [("minor", "key skew: the hottest partition carries 2.4x the mean load")]
    assert result["verdict"] == "warn"


def test_partition_plan_skew_factor_boundary_is_strictly_above_two():
    assert plan(peak_msgs_per_s=1000, headroom_pct=0, hot_key_share_pct=0, consumer_max_msgs_per_s=500, current_partitions=2)["skew_ratio"] == 1.0
    exactly_two = plan(peak_msgs_per_s=1000, headroom_pct=0, hot_key_share_pct=33.3333333333, current_partitions=4)
    assert exactly_two["skew_ratio"] == 2.0 and exactly_two["findings"] == []


def test_partition_plan_hot_key_larger_than_one_partition_cannot_be_fixed_by_partitions():
    result = plan(hot_key_share_pct=10)
    assert result["partitions_by_skew"] == -1 and result["required_partitions"] == 12
    assert fx(result)[0] == ("major", "the hottest key alone carries 600 msg/s, at or above one partition's capacity of 500 msg/s: "
                                     "no partition count fixes it, split the key or re-key")


def test_partition_plan_hot_key_exactly_at_capacity_is_infeasible_and_single_key_fits_one_partition():
    assert plan(peak_msgs_per_s=1000, headroom_pct=0, hot_key_share_pct=50)["partitions_by_skew"] == -1
    assert plan(peak_msgs_per_s=1000, headroom_pct=0, hot_key_share_pct=40)["partitions_by_skew"] == 6
    only = plan(peak_msgs_per_s=400, headroom_pct=0, hot_key_share_pct=100)
    assert (only["partitions_by_skew"], only["required_partitions"], only["verdict"]) == (1, 1, "pass")
    too_hot = plan(peak_msgs_per_s=600, headroom_pct=0, hot_key_share_pct=100)
    assert (too_hot["partitions_by_skew"], too_hot["required_partitions"]) == (-1, 2)


def test_partition_plan_write_budget_bounds_the_count_and_capacity():
    result = plan(peak_msgs_per_s=1000, avg_msg_bytes=1048576, headroom_pct=0, consumer_max_msgs_per_s=100000)
    assert (result["partitions_by_throughput"], result["partitions_by_consumers"], result["partitions_by_skew"]) == (100, 1, 100)
    assert result["required_partitions"] == 100 and result["throughput_mib_per_s"] == 1000.0


def test_partition_plan_current_partitions_findings():
    assert fx(plan(current_partitions=8)) == [
        ("major", "8 partitions is below the 12 needed for a uniform load (1 by throughput, 12 by consumer capacity)"),
        ("major", "the hottest partition would carry 750 msg/s, above one partition's capacity of 500 msg/s "
                  "(12 partitions keep it within capacity)"),
        ("info", "increasing the partition count remaps keys (hash mod N): per-key ordering breaks across the change")]
    equal = plan(current_partitions=12)
    assert equal["findings"] == [] and equal["evaluated_partitions"] == 12
    assert fx(plan(current_partitions=20)) == [("info", "20 partitions exceeds the 12 needed; partitions cannot normally be reduced later")]
    hot = plan(current_partitions=12, hot_key_share_pct=5)
    assert fx(hot)[0][0] == "major" and "the hottest partition would carry 794.17 msg/s" not in fx(hot)[0][1]
    assert hot["hot_partition_msgs_per_s"] == 775.0 and "29 partitions keep it within capacity" in fx(hot)[0][1]


# --- event_lag_assessment -------------------------------------------------------------------------------------


def lag(**over):
    payload = {"lag_msgs": 120000, "arrival_msgs_per_s": 400, "consumer_count": 4, "per_consumer_msgs_per_s": 200, "slo_s": 300}
    payload.update(over)
    return ok("event_lag_assessment", payload)


def test_lag_draining_within_slo():
    assert lag() == {
        "state": "SUCCESS", "effective_consumers": 4, "consume_rate_msgs_per_s": 800.0, "net_drain_msgs_per_s": 400.0,
        "estimated_delay_s": 150.0, "drain_time_s": 300.0, "recovery_time_s": 0.0, "trend": "draining", "slo_breach": False,
        "consumers_needed": 4, "needs_more_partitions": False, "verdict": "pass", "findings": []}


def test_lag_slo_breach_boundary_is_strictly_greater_and_recovery_time():
    assert lag(slo_s=150)["slo_breach"] is False
    breach = lag(slo_s=149)
    assert breach["slo_breach"] is True and breach["verdict"] == "fail"
    assert fx(breach) == [("major", "estimated delay 150 s exceeds the 149 s SLO")]
    assert lag(slo_s=100)["recovery_time_s"] == 100.0  # (120000 - 100 * 800) / 400
    assert lag(slo_s=100)["consumers_needed"] == 8  # 400 + 120000 / 100 = 1600 msg/s at 200 each


def test_lag_growing_stalled_and_steady_trends():
    growing = lag(arrival_msgs_per_s=900)
    assert (growing["trend"], growing["net_drain_msgs_per_s"], growing["drain_time_s"], growing["verdict"]) == ("growing", -100.0, -1.0, "fail")
    assert fx(growing)[0] == ("blocker", "consumers process 800 msg/s but 900 msg/s arrive: lag grows without bound")
    breach_growing = lag(arrival_msgs_per_s=900, slo_s=100)
    assert breach_growing["recovery_time_s"] == -1.0 and breach_growing["slo_breach"] is True
    stalled = lag(arrival_msgs_per_s=800)
    assert (stalled["trend"], stalled["drain_time_s"]) == ("stalled", -1.0)
    assert fx(stalled)[0] == ("major", "consumption equals arrival: the existing backlog never drains")
    steady = lag(lag_msgs=0)
    assert (steady["trend"], steady["estimated_delay_s"], steady["consumers_needed"], steady["verdict"]) == ("steady", 0.0, 2, "pass")
    assert lag(lag_msgs=0, arrival_msgs_per_s=900)["trend"] == "growing"


def test_lag_partitions_bound_parallelism():
    result = lag(partition_count=2)
    assert result["effective_consumers"] == 2 and result["consume_rate_msgs_per_s"] == 400.0
    assert result["trend"] == "stalled"
    assert ("minor", "2 consumer(s) are idle: only 2 partitions can be consumed in parallel") in fx(result)
    result = lag(partition_count=4, slo_s=100)
    assert result["needs_more_partitions"] is True
    assert ("major", "clearing the backlog within the SLO needs 8 consumers but only 4 partitions exist") in fx(result)
    healthy = lag(partition_count=4, lag_msgs=1000)
    assert healthy["needs_more_partitions"] is False and healthy["findings"] == []


# --- event_retry_schedule -------------------------------------------------------------------------------------


def retry(**over):
    payload = {"base_delay_s": 2, "multiplier": 2, "max_delay_s": 60, "max_attempts": 5, "jitter": "full",
               "retry_mode": "queue-redelivery", "processing_timeout_s": 30, "visibility_timeout_s": 180, "budget_s": 900}
    payload.update(over)
    return ok("event_retry_schedule", payload)


def test_retry_full_jitter_schedule_values():
    result = retry()
    assert result["schedule"] == [{"retry": i, "delay_min_s": 0.0, "delay_max_s": d} for i, d in enumerate([2.0, 4.0, 8.0, 16.0], 1)]
    assert (result["attempts"], result["retries"], result["total_delay_min_s"], result["total_delay_max_s"],
            result["worst_case_total_s"]) == (5, 4, 0.0, 30.0, 180.0)
    assert (result["fits_visibility"], result["fits_budget"], result["verdict"], result["findings"]) == (True, True, "pass", [])


def test_retry_equal_and_no_jitter_and_delay_cap():
    equal = retry(jitter="equal")
    assert [s["delay_min_s"] for s in equal["schedule"]] == [1.0, 2.0, 4.0, 8.0] and equal["total_delay_min_s"] == 15.0
    capped = retry(base_delay_s=10, multiplier=3, max_delay_s=50, jitter="none")
    assert [(s["delay_min_s"], s["delay_max_s"]) for s in capped["schedule"]] == [(10.0, 10.0), (30.0, 30.0), (50.0, 50.0), (50.0, 50.0)]
    assert fx(capped) == [("minor", "no jitter: consumers that failed together retry together (thundering herd)")]
    constant = retry(multiplier=1, base_delay_s=5, max_delay_s=5)
    assert fx(constant) == [("info", "constant delay: the schedule does not back off")]


def test_retry_single_attempt_has_no_schedule():
    result = retry(max_attempts=1, jitter="none")
    assert (result["schedule"], result["retries"], result["total_delay_max_s"], result["worst_case_total_s"]) == ([], 0, 0.0, 30.0)
    assert result["findings"] == []


def test_retry_in_process_mode_must_fit_the_visibility_timeout():
    assert retry(retry_mode="in-process", visibility_timeout_s=180)["fits_visibility"] is True
    result = retry(retry_mode="in-process", visibility_timeout_s=179)
    assert result["fits_visibility"] is False and result["verdict"] == "fail"
    assert fx(result) == [("blocker", "in-process retries take up to 180 s, beyond the 179 s visibility timeout: "
                                      "the message becomes visible again mid-retry")]


def test_retry_processing_must_not_outlive_the_visibility_timeout():
    assert retry(processing_timeout_s=180)["fits_visibility"] is True
    result = retry(processing_timeout_s=180.5, visibility_timeout_s=180)
    assert result["fits_visibility"] is False
    assert fx(result)[0] == ("blocker", "processing may take 180.5 s but the visibility timeout is 180 s: "
                                        "the message is redelivered while still being processed")


def test_retry_queue_redelivery_obeys_the_twelve_hour_visibility_maximum():
    assert retry(base_delay_s=43170, max_delay_s=43170, max_attempts=2, budget_s=1e6, visibility_timeout_s=60)["fits_visibility"] is True
    over = retry(base_delay_s=43171, max_delay_s=43171, max_attempts=2, budget_s=1e6, visibility_timeout_s=60)
    assert over["fits_visibility"] is False
    assert fx(over) == [("major", "the longest backoff 43171 s plus processing exceeds the 43200 s maximum message visibility timeout")]


def test_retry_budget_boundary():
    assert retry(budget_s=180)["fits_budget"] is True
    result = retry(budget_s=179)
    assert result["fits_budget"] is False
    assert fx(result) == [("major", "worst case 180 s exceeds the 179 s retry budget")]


def test_retry_max_delay_below_base_is_invalid():
    invalid("event_retry_schedule", {"base_delay_s": 10, "multiplier": 2, "max_delay_s": 5, "max_attempts": 3, "jitter": "none",
                                     "retry_mode": "in-process", "processing_timeout_s": 1, "visibility_timeout_s": 60,
                                     "budget_s": 60}, "max_delay_s")


# --- event_dlq_coverage ---------------------------------------------------------------------------------------


def consumer(**over):
    base = {"name": "billing", "queue": "billing-q", "dlq": "billing-dlq", "max_receive_count": 5, "redrive_runbook": True,
            "dlq_alarm": True, "fifo": False, "dlq_fifo": False, "source_retention_days": 4, "dlq_retention_days": 14}
    base.update(over)
    return base


def dlq(*consumers):
    return ok("event_dlq_coverage", {"consumers": list(consumers)})


def test_dlq_fully_covered_consumer():
    assert dlq(consumer()) == {"state": "SUCCESS", "consumers": 1, "covered": 1, "uncovered_consumers": [],
                               "verdict": "pass", "findings": []}


def test_dlq_missing_dlq_is_a_blocker_and_uncovered():
    result = dlq(consumer(name="b", dlq=""), consumer(name="a"))
    assert (result["consumers"], result["covered"], result["uncovered_consumers"], result["verdict"]) == (2, 1, ["b"], "fail")
    assert fx(result) == [("blocker", "b: no dead-letter queue configured")]


@pytest.mark.parametrize(
    "over, severity, text, covered",
    [
        ({"dlq": "billing-q"}, "blocker", "billing: the DLQ is the source queue itself", 0),
        ({"max_receive_count": 0}, "blocker", "billing: no max receive count, so messages never move to the DLQ", 0),
        ({"max_receive_count": 1001}, "major", "billing: max receive count 1001 exceeds the SQS limit of 1000, so the redrive policy is "
                                               "rejected and no message reaches the DLQ", 0),
        ({"max_receive_count": 1000}, "minor", "billing: max receive count 1000 keeps a poison message in flight for more than 20 attempts", 1),
        ({"max_receive_count": 21}, "minor", "billing: max receive count 21 keeps a poison message in flight for more than 20 attempts", 1),
        ({"max_receive_count": 1}, "minor", "billing: max receive count 1 sends a message to the DLQ after a single transient failure", 1),
        ({"fifo": True}, "blocker", "billing: queue and DLQ must both be FIFO or both standard", 0),
        ({"dlq_fifo": True}, "blocker", "billing: queue and DLQ must both be FIFO or both standard", 0),
        ({"redrive_runbook": False}, "major", "billing: no redrive path or runbook for messages in billing-dlq", 1),
        ({"dlq_alarm": False}, "minor", "billing: no alarm on DLQ depth for billing-dlq", 1),
        ({"dlq_retention_days": 4}, "major",
         "billing: DLQ retention 4 d must exceed source retention 4 d (retention counts from the original enqueue time)", 1),
        ({"dlq_retention_days": 3}, "major",
         "billing: DLQ retention 3 d must exceed source retention 4 d (retention counts from the original enqueue time)", 1),
    ],
)
def test_dlq_rules(over, severity, text, covered):
    result = dlq(consumer(**over))
    assert fx(result) == [(severity, text)]
    assert result["covered"] == covered


def test_dlq_receive_count_boundaries_20_is_accepted_and_retention_just_above_source_is_accepted():
    assert dlq(consumer(max_receive_count=20))["findings"] == []
    assert dlq(consumer(dlq_retention_days=4.5))["findings"] == []


def test_dlq_shared_dlq_is_reported_and_duplicate_names_are_invalid():
    result = dlq(consumer(name="a", queue="qa", dlq="shared"), consumer(name="b", queue="qb", dlq="shared"))
    assert fx(result) == [("info", "DLQ shared is shared by a, b: poison messages of different consumers mix")]
    assert result["covered"] == 2 and result["verdict"] == "pass"
    invalid("event_dlq_coverage", {"consumers": [consumer(), consumer()]}, "consumers")


# --- event_idempotency_window ---------------------------------------------------------------------------------


def idem(**over):
    payload = {"key_source": "producer-event-id", "dedup_window_s": 86400, "retry_window_s": 3600, "redelivery_window_s": 3600,
               "replay_window_s": 0, "atomic_claim": True, "naturally_idempotent": False}
    payload.update(over)
    return ok("event_idempotency_window", payload)


def test_idempotency_window_covers_the_longest_duplicate_path():
    result = idem()
    assert result == {"state": "SUCCESS", "required_window_s": 3600.0, "headroom_s": 82800.0, "covered": True,
                      "verdict": "pass", "findings": []}
    assert idem(replay_window_s=172800)["required_window_s"] == 172800.0
    assert idem(redelivery_window_s=7200, retry_window_s=100)["required_window_s"] == 7200.0


def test_idempotency_window_boundary_is_inclusive():
    assert idem(dedup_window_s=3600)["covered"] is True
    short = idem(dedup_window_s=3599)
    assert (short["covered"], short["headroom_s"], short["verdict"]) == (False, -1.0, "fail")
    assert fx(short) == [("blocker", "the dedup window of 3599 s is shorter than the 3600 s during which a duplicate can arrive")]


def test_idempotency_key_strategies():
    assert fx(idem(key_source="none")) == [("blocker", "no idempotency key: a redelivered message repeats its side effect")]
    assert idem(key_source="none")["covered"] is False
    natural = idem(key_source="none", naturally_idempotent=True)
    assert fx(natural) == [("info", "no idempotency key: safe only because the effect is naturally idempotent")]
    assert natural["covered"] is True and natural["verdict"] == "pass"
    assert fx(idem(key_source="broker-message-id")) == [
        ("major", "the broker message id changes when the producer retries a send and on replay: it is not a stable idempotency key")]
    assert fx(idem(key_source="payload-hash")) == [
        ("minor", "a payload hash collapses distinct but identical events (for example two equal commands)")]
    assert idem(key_source="business-key")["findings"] == []


def test_idempotency_atomic_claim_and_natural_idempotence_downgrade():
    race = idem(atomic_claim=False)
    assert fx(race) == [("major", "key check and effect are not one atomic claim (conditional write or unique constraint): "
                                  "concurrent redeliveries can both pass")]
    assert race["covered"] is False
    relaxed = idem(atomic_claim=False, naturally_idempotent=True, dedup_window_s=10)
    assert fx(relaxed) == [("minor", "the dedup window of 10 s is shorter than the 3600 s during which a duplicate can arrive")]
    assert relaxed["covered"] is True


# --- event_ordering_check -------------------------------------------------------------------------------------


def order(**over):
    payload = {"broker": "kafka", "order_required": True, "entity_key_fields": ["order_id"], "partition_key_fields": ["order_id"],
               "handler_concurrency_per_unit": 1, "retry_mode": "in-place", "producer_in_flight": 5, "idempotent_producer": True,
               "partitions_will_change": False}
    payload.update(over)
    return ok("event_ordering_check", payload)


@pytest.mark.parametrize(
    "broker, unit",
    [("kafka", "partition"), ("kinesis", "shard"), ("sqs-fifo", "message group"), ("sns-fifo", "message group"),
     ("pubsub", "ordering key"), ("rabbitmq", "queue"), ("sqs-standard", ""), ("sns-standard", ""), ("eventbridge", "")],
)
def test_ordering_unit_of_each_broker(broker, unit):
    result = order(broker=broker, order_required=False)
    assert result["ordering_unit"] == unit and result["ordered"] is bool(unit)


def test_ordering_kafka_by_entity_key_is_ordered():
    assert order() == {"state": "SUCCESS", "ordering_unit": "partition", "ordered": True, "verdict": "pass", "findings": []}


@pytest.mark.parametrize("broker", ["sqs-standard", "sns-standard", "eventbridge"])
def test_ordering_unordered_brokers_fail_when_order_is_required(broker):
    result = order(broker=broker)
    assert (result["ordered"], result["verdict"]) == (False, "fail")
    expected = (f"{broker} gives no ordering guarantee" if broker != "eventbridge" else
                "eventbridge (classic event bus) gives no ordering guarantee; the Custom Event Bus orders per event group for FIFO "
                "subscribers, otherwise use sqs-fifo, kinesis or kafka")
    assert fx(result) == [("blocker", expected)]


def test_ordering_routing_key_must_be_derived_from_the_entity_key():
    assert fx(order(partition_key_fields=[])) == [("blocker", "no partition key: events of one entity spread over several partitions")]
    assert fx(order(partition_key_fields=["order_id", "created_at"])) == [
        ("blocker", "routing key field(s) created_at are not part of the entity key: events of one entity can land in different partitions")]
    coarse = order(entity_key_fields=["customer_id", "order_id"], partition_key_fields=["customer_id"])
    assert coarse["ordered"] is True
    assert fx(coarse) == [("info", "the routing key is coarser than the entity key: order holds, but several entities share a partition (watch skew)")]
    assert order(entity_key_fields=["a", "b"], partition_key_fields=["b", "a"])["findings"] == []
    rabbit = order(broker="rabbitmq", partition_key_fields=[])
    assert rabbit["ordered"] is True and rabbit["findings"] == []


def test_ordering_consumer_retry_and_producer_behaviour():
    assert fx(order(handler_concurrency_per_unit=2)) == [
        ("major", "2 handlers work on one partition concurrently: processing order is not preserved")]
    assert fx(order(retry_mode="retry-queue")) == [
        ("major", "failed messages are re-queued behind later ones (retry topic/queue): order is broken on every retry")]
    assert order(retry_mode="none")["ordered"] is True
    assert fx(order(producer_in_flight=2, idempotent_producer=False)) == [
        ("major", "more than one in-flight request without idempotent producer: a retried batch can overtake a later one")]
    assert order(producer_in_flight=1, idempotent_producer=False)["ordered"] is True
    assert order(producer_in_flight=5, idempotent_producer=True)["ordered"] is True
    assert fx(order(producer_in_flight=6, idempotent_producer=True)) == [
        ("major", "idempotent producers preserve order only up to 5 in-flight requests")]
    assert order(broker="kinesis", producer_in_flight=9, idempotent_producer=False)["ordered"] is True
    assert fx(order(partitions_will_change=True)) == [
        ("major", "the partition count changes: keys are remapped, so ordering is not guaranteed across the change")]
    fifo = order(broker="sqs-fifo", partitions_will_change=True)  # message groups are not a fixed partition set
    assert fifo["ordered"] is True
    assert fx(fifo) == [("info", "partitions_will_change does not apply to sqs-fifo: its ordering unit (message group) is not a fixed "
                                 "partition set")]
    assert fx(order(broker="kinesis", partitions_will_change=True)) == [
        ("major", "resharding re-routes keys to child shards: read each parent shard to its end before its children, or order breaks")]


# --- event_function_sizing ------------------------------------------------------------------------------------


def size(**over):
    payload = {"platform": "aws-lambda", "memory_mb": 1024, "timeout_s": 30, "peak_events_per_s": 200, "batch_size": 10,
               "p95_duration_ms": 800, "p99_duration_ms": 1500, "headroom_pct": 20, "cold_start_ms": 600,
               "cold_start_budget_ms": 1000}
    payload.update(over)
    return ok("event_function_sizing", payload)


def test_function_sizing_concurrency_by_littles_law_and_vcpu():
    result = size()
    assert result == {"state": "SUCCESS", "concurrency_needed": 20, "max_safe_concurrency": -1,
                      "provisioned_concurrency_suggested": 0, "vcpu_equivalent": 0.58, "verdict": "pass", "findings": []}
    assert size(memory_mb=1769)["vcpu_equivalent"] == 1.0
    assert size(platform="generic", memory_mb=4096)["vcpu_equivalent"] == 0.0
    assert size(batch_size=1)["concurrency_needed"] == 192  # 200 x 1.2 x 0.8
    assert size(peak_events_per_s=0)["concurrency_needed"] == 1


def test_function_sizing_timeout_margin_boundaries():
    assert size(timeout_s=30, p99_duration_ms=24000)["findings"] == []
    near = size(timeout_s=30, p99_duration_ms=24001)
    assert fx(near) == [("minor", "p99 duration 24001 ms is above 80% of the 30 s timeout: little safety margin")]
    assert near["verdict"] == "warn"
    cut = size(timeout_s=30, p99_duration_ms=30000)
    assert fx(cut) == [("blocker", "p99 duration 30000 ms reaches the 30 s timeout: at least 1% of invocations are cut off")]
    assert size(timeout_s=30, p99_duration_ms=29999)["verdict"] == "warn"


def test_function_sizing_reserved_concurrency_and_downstream_limit():
    assert size(reserved_concurrency=20)["findings"] == []
    assert fx(size(reserved_concurrency=19)) == [("major", "reserved concurrency 19 is below the 20 needed at peak: events are throttled")]
    ok_limit = size(downstream_max_connections=200, connections_per_instance=2, reserved_concurrency=100)
    assert ok_limit["max_safe_concurrency"] == 100 and ok_limit["findings"] == []
    cap_high = size(downstream_max_connections=200, connections_per_instance=2, reserved_concurrency=101)
    assert fx(cap_high) == [("major", "reserved concurrency 101 x 2 connections exceeds the downstream limit of 200; at most 100 instances are safe")]
    short = size(downstream_max_connections=39, connections_per_instance=2)
    assert short["max_safe_concurrency"] == 19
    assert fx(short) == [("major", "the downstream limit allows 19 instances but peak needs 20: use a pooler or proxy, or buffer")]
    assert size(downstream_max_connections=40, connections_per_instance=2)["findings"] == []
    assert size(downstream_max_connections=1, connections_per_instance=2)["max_safe_concurrency"] == 0


def test_function_sizing_cold_start_budget_boundary_and_provisioned_suggestion():
    assert size(cold_start_ms=1000)["provisioned_concurrency_suggested"] == 0
    result = size(cold_start_ms=1000.5)
    assert result["provisioned_concurrency_suggested"] == 20
    assert fx(result) == [("major", "cold start 1000.5 ms exceeds the 1000 ms budget: provision 20 warm instance(s) or shrink the package")]


def test_function_sizing_queue_visibility_rules():
    assert size(source_visibility_timeout_s=180)["findings"] == []
    assert fx(size(source_visibility_timeout_s=179)) == [
        ("major", "source queue visibility timeout 179 s is below 6x the 30 s function timeout (180 s)")]
    generic = size(platform="generic", source_visibility_timeout_s=30)
    assert generic["findings"] == []
    assert fx(size(platform="generic", source_visibility_timeout_s=29)) == [
        ("blocker", "source visibility timeout 29 s is below the 30 s function timeout")]


@pytest.mark.parametrize(
    "over, field",
    [
        ({"memory_mb": 127}, "memory_mb"), ({"memory_mb": 10241}, "memory_mb"), ({"timeout_s": 901}, "timeout_s"),
        ({"p95_duration_ms": 900, "p99_duration_ms": 800}, "p99_duration_ms"),
        ({"downstream_max_connections": 10}, "connections_per_instance"),
        ({"connections_per_instance": 2}, "connections_per_instance"),
    ],
)
def test_function_sizing_invalid_input_names_the_field(over, field):
    payload = {"platform": "aws-lambda", "memory_mb": 1024, "timeout_s": 30, "peak_events_per_s": 200, "batch_size": 10,
               "p95_duration_ms": 800, "p99_duration_ms": 1500, "headroom_pct": 20, "cold_start_ms": 600,
               "cold_start_budget_ms": 1000, **over}
    invalid("event_function_sizing", payload, field)


def test_function_sizing_lambda_limits_are_inclusive_and_generic_has_none():
    assert size(memory_mb=128)["vcpu_equivalent"] == 0.07
    assert size(memory_mb=10240, timeout_s=900, p99_duration_ms=1500)["vcpu_equivalent"] == 5.79
    assert size(platform="generic", memory_mb=64, timeout_s=3600)["state"] == "SUCCESS"


# --- event_scaler_sanity --------------------------------------------------------------------------------------


def scaler(**over):
    payload = {"min_replicas": 1, "max_replicas": 20, "polling_interval_s": 15, "cooldown_period_s": 300, "replica_startup_s": 20,
               "max_reaction_s": 60, "target_backlog_per_replica": 100, "peak_backlog": 1500, "termination_grace_period_s": 60,
               "max_processing_s": 30}
    payload.update(over)
    return ok("event_scaler_sanity", payload)


def test_scaler_sane_configuration():
    assert scaler() == {"state": "SUCCESS", "needed_replicas": 15, "effective_max_replicas": 20, "reaction_time_s": 35.0,
                        "verdict": "pass", "findings": []}
    assert scaler(peak_backlog=1501)["needed_replicas"] == 16


def test_scaler_ceiling_versus_demand_boundary_and_partition_cap():
    assert scaler(max_replicas=15)["findings"] == []
    low = scaler(max_replicas=14)
    assert fx(low) == [("major", "peak backlog needs 15 replicas but the effective ceiling is 14 (max_replicas is 14): lag will grow")]
    parts = scaler(partitions=10)
    assert parts["effective_max_replicas"] == 10
    assert fx(parts) == [("major", "peak backlog needs 15 replicas but the effective ceiling is 10 (only 10 partitions can be consumed in "
                                   "parallel): lag will grow")]
    assert scaler(partitions=15)["findings"] == []


def test_scaler_bounds_and_scale_to_zero():
    assert fx(scaler(min_replicas=25, max_replicas=20)) == [("blocker", "min_replicas 25 is above max_replicas 20")]
    assert fx(scaler(min_replicas=20)) == [("minor", "min equals max (20): the scaler cannot scale")]
    zero = scaler(min_replicas=0)
    assert fx(zero) == [("info", "scale to zero: the first event waits up to 35 s for a replica")]
    assert zero["verdict"] == "pass"


def test_scaler_reaction_time_boundary_is_inclusive():
    assert scaler(polling_interval_s=40, replica_startup_s=20)["findings"] == []
    slow = scaler(polling_interval_s=41, replica_startup_s=20)
    assert slow["reaction_time_s"] == 61.0
    assert fx(slow) == [("major", "reaction time 61 s (polling 41 s + startup 20 s) exceeds the 60 s target")]


def test_scaler_cooldown_and_termination_grace_boundaries():
    # KEDA's cooldownPeriod only delays the scale to zero, so it is judged only when min_replicas is 0
    assert scaler(cooldown_period_s=14)["findings"] == []
    assert fx(scaler(min_replicas=0, cooldown_period_s=15)) == [("info", "scale to zero: the first event waits up to 35 s for a replica")]
    assert fx(scaler(min_replicas=0, cooldown_period_s=14)) == [
        ("minor", "the cooldown is shorter than the polling interval: scale to zero is only evaluated once per poll, so the effective "
                  "quiet period before it is the polling interval"),
        ("info", "scale to zero: the first event waits up to 35 s for a replica")]
    assert scaler(termination_grace_period_s=30, max_processing_s=30)["findings"] == []
    assert fx(scaler(termination_grace_period_s=29)) == [
        ("major", "termination grace 29 s is shorter than the longest message processing 30 s: scale-down kills in-flight work")]


# --- event_saga_coverage --------------------------------------------------------------------------------------


def step(name, kind, compensation="", idempotent=True, comp_idempotent=True):
    return {"name": name, "kind": kind, "compensation": compensation, "idempotent": idempotent, "compensation_idempotent": comp_idempotent}


def saga(steps, outbox=True):
    return ok("event_saga_coverage", {"steps": steps, "outbox": outbox})


GOOD_SAGA = [step("reserve", "compensable", "release"), step("charge", "compensable", "refund"), step("confirm", "pivot"),
             step("ship", "retriable"), step("notify", "retriable")]


def test_saga_well_formed_with_compensation_order_reversed():
    assert saga(GOOD_SAGA) == {
        "state": "SUCCESS", "steps": 5, "compensable_steps": 2, "compensated_steps": 2, "uncompensated_steps": [], "pivot": "confirm",
        "compensation_order": ["refund", "release"], "verdict": "pass", "findings": []}


def test_saga_missing_and_non_idempotent_compensation():
    steps = [step("a", "compensable", ""), step("b", "compensable", "undo-b", comp_idempotent=False), step("p", "pivot")]
    result = saga(steps)
    assert (result["compensable_steps"], result["compensated_steps"], result["uncompensated_steps"]) == (2, 1, ["a"])
    assert result["compensation_order"] == ["undo-b"]
    assert fx(result) == [("blocker", "a: compensable step has no compensating action"),
                          ("major", "b: compensation undo-b is not idempotent; it can be retried or redelivered")]


def test_saga_pivot_rules():
    two = saga([step("p1", "pivot"), step("p2", "pivot")])
    assert fx(two) == [("major", "2 pivot steps (p1, p2): a saga has one commit point")]
    after = saga([step("p", "pivot"), step("late", "compensable", "undo")])
    assert fx(after) == [("major", "late: compensable step after the pivot cannot be rolled back once the pivot committed")]
    assert after["compensation_order"] == []
    no_pivot = saga([step("a", "compensable", "undo-a"), step("b", "compensable", "undo-b")])
    assert (no_pivot["pivot"], no_pivot["compensation_order"], no_pivot["findings"]) == ("", ["undo-b", "undo-a"], [])


def test_saga_retriable_steps():
    before = saga([step("r", "retriable"), step("c", "compensable", "undo")])
    assert fx(before) == [("major", "r: retriable step before a later compensable or pivot step is not undone if that step fails")]
    non_idem = saga([step("c", "compensable", "undo"), step("p", "pivot"), step("r", "retriable", idempotent=False)])
    assert fx(non_idem) == [("major", "r: retriable step is retried until it succeeds and must be idempotent")]
    assert saga([step("c", "compensable", "undo"), step("p", "pivot"), step("r", "retriable")])["findings"] == []


def test_saga_outbox_and_unique_names():
    assert fx(saga(GOOD_SAGA, outbox=False)) == [
        ("major", "no transactional outbox: the state change and the event publish are a dual write that can diverge")]
    invalid("event_saga_coverage", {"steps": [step("x", "pivot"), step("x", "retriable")], "outbox": True}, "steps")


# --- event_retention_replay -----------------------------------------------------------------------------------


def replay(**over):
    payload = {"retention_s": 604800, "replay_from_age_s": 86400, "replay_events": 5000000, "replay_rate_per_s": 2000,
               "live_rate_per_s": 500, "consumers_idempotent": True, "compacted": False, "environment": "non-prod"}
    payload.update(over)
    return ok("event_retention_replay", payload)


def test_replay_plan_catch_up_time_and_headroom():
    assert replay() == {"state": "SUCCESS", "replay_duration_s": 3333.33, "restartable": True, "retention_headroom_s": 518400.0,
                        "requires_l3_approval": False, "verdict": "pass", "findings": []}
    assert replay(replay_events=0)["replay_duration_s"] == 0.0


def test_replay_restartable_boundary_is_inclusive():
    exact = replay(retention_s=1000, replay_from_age_s=900, replay_events=100, replay_rate_per_s=2, live_rate_per_s=0)
    assert (exact["replay_duration_s"], exact["restartable"], exact["verdict"]) == (50.0, True, "pass")
    fits = replay(retention_s=1000, replay_from_age_s=950, replay_events=100, replay_rate_per_s=2, live_rate_per_s=0)
    assert fits["restartable"] is True and fits["findings"] == []
    over = replay(retention_s=1000, replay_from_age_s=951, replay_events=100, replay_rate_per_s=2, live_rate_per_s=0)
    assert over["restartable"] is False
    assert fx(over) == [("major", "replay needs 50 s from a point 951 s old: the start offset expires after 1000 s, so a failed replay cannot restart")]


def test_replay_start_older_than_retention_is_a_blocker_and_age_equal_is_accepted():
    gone = replay(retention_s=1000, replay_from_age_s=1001, replay_events=10)
    assert fx(gone) == [("blocker", "replay starts 1001 s back but only 1000 s is retained: older events are gone")]
    assert gone["retention_headroom_s"] == -1.0
    edge = replay(retention_s=1000, replay_from_age_s=1000, replay_events=0)
    assert edge["findings"] == [] and edge["restartable"] is True
    assert edge["retention_headroom_s"] == 0.0


def test_replay_never_catches_up_when_rate_does_not_exceed_live_rate():
    for rate in (500, 400):
        result = replay(replay_rate_per_s=rate)
        assert (result["replay_duration_s"], result["restartable"], result["verdict"]) == (-1.0, False, "fail")
        assert fx(result) == [("blocker", f"replay rate {rate} evt/s does not exceed the live rate 500 evt/s: replay never catches up")]
    assert replay(replay_rate_per_s=501)["replay_duration_s"] == 5000000.0


def test_replay_compacted_log_ignores_age_limit_and_reports_state_only_replay():
    result = replay(compacted=True, retention_s=10, replay_from_age_s=1000)
    assert fx(result) == [("info", "compacted log: only the latest record per key is retained, so replay rebuilds state, not history")]
    assert result["restartable"] is True and result["verdict"] == "pass"
    assert replay(compacted=True, replay_rate_per_s=400)["restartable"] is False


def test_replay_requires_idempotent_consumers_and_flags_production_as_l3():
    assert fx(replay(consumers_idempotent=False)) == [("blocker", "consumers are not idempotent: a replay repeats their side effects")]
    prod = replay(environment="prod")
    assert prod["requires_l3_approval"] is True and prod["verdict"] == "pass"
    assert fx(prod) == [("info", "replay or backfill in production is autonomy level L3: explicit approval is required")]


# --- cross-cutting --------------------------------------------------------------------------------------------


def test_findings_are_capped_at_300_and_the_cap_is_reported():
    consumers = [consumer(name=f"c{i:03d}", queue=f"q{i}", dlq=f"d{i}", max_receive_count=1, redrive_runbook=False, dlq_alarm=False,
                          dlq_retention_days=1) for i in range(100)]
    result = dlq(*consumers)  # four findings per consumer: 400 in total
    assert len(result["findings"]) == 300
    assert result["findings"][-1] == {"id": "DL-TRUNC", "severity": "info", "summary": "101 further findings omitted"}
    assert result["findings"][0]["id"] == "DL-001" and result["findings"][298]["id"] == "DL-299"
    assert result["verdict"] == "fail" and result["covered"] == 100


def test_schema_compat_refuses_hostile_nesting_instead_of_recursing():
    deep = "[" * 150 + "]" * 150
    invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": deep, "head_schema": "{}"}, "base_schema")
    invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": "[" * 100000, "head_schema": "{}"}, "base_schema")
    chain = {f"d{i}": obj_schema({"n": {"$ref": f"#/definitions/d{i + 1}"}}, additionalProperties=False) for i in range(160)}
    chain["d160"] = STR
    document = {"$ref": "#/definitions/d0", "definitions": chain}
    invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": json.dumps(document),
                                    "head_schema": json.dumps(document)}, "base_schema")
    shallow = {k: v for k, v in chain.items() if int(k[1:]) >= 100}
    short = {"$ref": "#/definitions/d100", "definitions": shallow}
    assert compat(short, copy.deepcopy(short), "full")["compatible"] is True


# --- review regressions: JSON Schema soundness ----------------------------------------------------------------


def _backward(base, head, **kw):
    return compat(base, head, "backward", **kw)


def test_schema_compat_oneof_target_must_stay_exclusive():
    # "a" matches both alternatives of the head oneOf, so oneOf rejects it: the change is not backward compatible
    overlap = _backward(STR, {"oneOf": [STR, {"minLength": 0}]})
    assert overlap["backward_compatible"] is False
    assert fx(overlap) == [("major", "[backward] $: matches several alternatives of the target oneOf, which accepts exactly one")]
    unproven = _backward({"type": "array"}, {"oneOf": [{"minLength": 2}, {"maxLength": 5}]})
    assert unproven["backward_compatible"] is False
    assert _backward(STR, {"oneOf": [STR, INT]})["backward_compatible"] is True  # disjoint type sets
    kind = lambda k: {"type": "object", "required": ["kind"], "properties": {"kind": {"const": k}}}  # noqa: E731
    assert _backward(kind("a"), {"oneOf": [kind("a"), kind("b")]})["backward_compatible"] is True  # discriminator
    same = {"oneOf": [{"minLength": 1}, {"maxLength": 9}]}
    assert compat(same, copy.deepcopy(same), "full")["findings"] == []  # an unchanged oneOf is not re-judged


def test_schema_compat_unchanged_oneof_is_not_rejudged_when_draft_spellings_are_equivalent():
    # "draft-07/schema#" and "draft-07/schema" name one draft, so an unchanged oneOf is the same schema and is not re-judged;
    # genuinely different drafts keep the textual check and are still compared conservatively
    number_or_integer = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
    base = {"$schema": "http://json-schema.org/draft-07/schema#", "properties": {"x": number_or_integer}}
    head = {"$schema": "http://json-schema.org/draft-07/schema", "properties": {"x": copy.deepcopy(number_or_integer)}}
    assert compat(base, head, "full")["findings"] == []
    https = {**head, "$schema": "https://json-schema.org/draft-07/schema#"}
    assert compat(base, https, "full")["findings"] == []
    other_draft = {**head, "$schema": "http://json-schema.org/draft-04/schema#"}
    assert compat(base, other_draft, "full")["findings"] != []


def test_schema_compat_target_siblings_of_anyof_and_ref_still_apply():
    assert _backward(INT, {"type": "string", "anyOf": [{"minLength": 1}]})["backward_compatible"] is False
    defs = {"$schema": MODERN, "$defs": {"s": STR}}
    base = {"$ref": "#/$defs/s", **defs}
    head = {"$ref": "#/$defs/s", "maxLength": 3, **defs}
    assert fx(_backward(base, head)) == [("major", "[backward] $: maxLength is above the target maximum 3 (string length)")]
    assert _backward(head, base)["backward_compatible"] is True


def test_schema_compat_multiple_of_and_property_count_bounds():
    multiple = lambda m: {"type": "integer", **({"multipleOf": m} if m else {})}  # noqa: E731
    assert fx(_backward(multiple(0), multiple(5))) == [("major", "[backward] $: multipleOf 5 is not guaranteed by the source")]
    assert _backward(multiple(10), multiple(5))["backward_compatible"] is True
    assert _backward(multiple(5), multiple(10))["backward_compatible"] is False
    assert _backward(multiple(0), multiple(1))["backward_compatible"] is True  # every integer is a multiple of 1
    assert _backward({"type": "number"}, {"type": "number", "multipleOf": 0.5})["backward_compatible"] is False
    bag = lambda **kw: {"type": "object", **kw}  # noqa: E731
    assert fx(_backward(bag(), bag(minProperties=1))) == [("major", "[backward] $: minProperties is below the target minimum 1 (property count)")]
    assert fx(_backward(bag(maxProperties=3), bag(maxProperties=2))) == [
        ("major", "[backward] $: maxProperties is above the target maximum 2 (property count)")]
    assert _backward(bag(minProperties=2, maxProperties=2), bag(minProperties=1, maxProperties=3))["backward_compatible"] is True


def test_schema_compat_draft4_boolean_exclusive_bounds():
    closed = {"type": "integer", "minimum": 5}
    exclusive = {"type": "integer", "minimum": 5, "exclusiveMinimum": True}
    assert _backward(closed, exclusive)["backward_compatible"] is False
    assert _backward(exclusive, closed)["backward_compatible"] is True
    upper = {"type": "integer", "maximum": 9, "exclusiveMaximum": True}
    assert _backward({"type": "integer", "maximum": 9}, upper)["backward_compatible"] is False
    assert _backward(upper, {"type": "integer", "maximum": 9})["backward_compatible"] is True


def test_schema_compat_enum_and_const_together_are_intersected():
    assert _backward({"enum": [1]}, {"enum": [1, 2], "const": 3})["backward_compatible"] is False
    assert _backward({"enum": [1]}, {"enum": [1, 2], "const": 1})["backward_compatible"] is True


def test_schema_compat_unanalysed_keyword_that_differs_in_the_target_is_not_a_pass():
    base, head = {"type": "object"}, {"type": "object", "propertyNames": {"maxLength": 3}}
    narrowed = compat(base, head, "full")
    assert (narrowed["backward_compatible"], narrowed["forward_compatible"]) == (False, True)
    assert ("major", "[backward] $: keyword 'propertyNames' differs between the schemas and cannot be analysed: "
                     "compatibility is not proven") in fx(narrowed)
    allof = {"type": "object", "allOf": [{"required": ["a"]}]}
    assert compat({"type": "object"}, allof, "backward")["verdict"] == "fail"
    ref_allof = {"allOf": [{"$ref": "#/$defs/x"}], "$defs": {"x": STR}}
    assert compat(ref_allof, copy.deepcopy(ref_allof), "backward")["compatible"] is False  # a $ref may resolve differently


def test_schema_compat_pattern_properties_of_the_source_are_not_ignored_by_a_closed_target():
    pattern = obj_schema({}, additionalProperties=False, patternProperties={"^x-": {}})
    closed = obj_schema({}, additionalProperties=False)
    result = compat(pattern, closed, "backward")
    assert result["backward_compatible"] is False
    assert ("major", "[backward] $: the source accepts properties by pattern that the closed or constrained target does not "
                     "provably accept") in fx(result)


def test_schema_compat_breaking_change_count_is_capped_at_the_contract_maximum():
    props = {f"p{i}": STR for i in range(1500)}
    result = compat(obj_schema(props, additionalProperties=False), obj_schema({}, additionalProperties=False), "full")
    assert (result["breaking_changes"], len(result["findings"])) == (1000, 300)
    assert result["findings"][-1]["id"] == "SC-TRUNC" and result["verdict"] == "fail"


def test_schema_compat_refs_use_rfc6901_pointers_percent_decoding_and_anchors():
    base = {"type": "object", "properties": {"a": {"$ref": "#/definitions/a%20b"}, "b": {"$ref": "#/definitions/"}},
            "definitions": {"a b": STR, "": INT}}
    head = copy.deepcopy(base)
    head["definitions"][""] = STR
    assert fx(compat(base, head)) == [("blocker", "[backward] $.b: type ['integer'] is not accepted by target type ['string']")]
    anchored = {"$ref": "#node", "$defs": {"n": {"$anchor": "node", "type": "string"}}}
    assert compat(anchored, copy.deepcopy(anchored), "full")["compatible"] is True
    invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": '{"$ref": "#/a//b"}',
                                    "head_schema": "{}"}, "base_schema")


def test_avro_decimal_precision_and_scale_changes_are_flagged():
    dec = lambda p, s: avro_record({"name": "amt", "type": {"type": "bytes", "logicalType": "decimal", "precision": p, "scale": s}})  # noqa: E731
    result = compat(dec(10, 2), dec(10, 4), "backward", "avro")
    assert fx(result) == [("major", "[backward] $.amt: decimal precision/scale changed from (10,2) to (10,4)")]
    assert compat(dec(10, 2), dec(10, 2), "full", "avro")["findings"] == []
    fixed = lambda s: avro_record({"name": "h", "type": {"type": "fixed", "name": "H", "size": 8, "logicalType": "decimal",  # noqa: E731
                                                        "precision": 5, "scale": s}})
    assert fx(compat(fixed(1), fixed(2), "backward", "avro")) == [("major", "[backward] $.h: decimal precision/scale changed from (5,1) to (5,2)")]


# --- review regressions: planning math, domain facts and secret safety --------------------------------------------


def test_partition_plan_refuses_inputs_that_imply_more_partitions_than_the_contract_can_return():
    huge = {"peak_msgs_per_s": 10_000_000, "avg_msg_bytes": 100_000_000, "partition_max_mib_per_s": 0.01,
            "consumer_max_msgs_per_s": 10_000_000, "headroom_pct": 1000, "hot_key_share_pct": 0}
    result = invalid("event_partition_plan", huge, "partition_max_mib_per_s")
    assert "more than 1000000000000 partitions" in result["message"]
    assert ok("event_partition_plan", {**huge, "partition_max_mib_per_s": 0.011})["required_partitions"] == 953_674_316_407


def test_ordering_findings_are_informational_when_order_is_not_required():
    result = order(order_required=False, partition_key_fields=[], handler_concurrency_per_unit=3, retry_mode="retry-queue")
    assert (result["ordered"], result["verdict"]) == (False, "pass")
    assert [sev for sev, _ in fx(result)] == ["info", "info", "info"]
    assert all(text.startswith("order is not required here; for information: ") for _, text in fx(result))
    assert order(order_required=False)["findings"] == []


def test_ordering_needs_the_entity_key_for_a_keyed_broker_when_order_is_required():
    invalid("event_ordering_check", {"broker": "kafka", "order_required": True, "entity_key_fields": [], "partition_key_fields": ["a"],
                                     "handler_concurrency_per_unit": 1, "retry_mode": "none", "producer_in_flight": 1,
                                     "idempotent_producer": True, "partitions_will_change": False}, "entity_key_fields")
    assert order(broker="rabbitmq", entity_key_fields=[], partition_key_fields=[])["ordered"] is True


def test_saga_blank_compensation_is_no_compensation_and_every_step_kind_is_idempotence_checked():
    blank = saga([step("a", "compensable", "   "), step("p", "pivot")])
    assert (blank["compensated_steps"], blank["uncompensated_steps"], blank["compensation_order"]) == (0, ["a"], [])
    assert fx(blank) == [("blocker", "a: compensable step has no compensating action")]
    flawed = saga([step("c", "compensable", "undo", idempotent=False), step("p", "pivot", idempotent=False)])
    assert fx(flawed) == [("major", "p: the pivot is not idempotent: a redelivered command can commit twice"),
                          ("minor", "c: compensable step is not idempotent: a redelivered command repeats its effect")]


def test_idempotency_broker_message_id_is_never_reported_as_covered():
    result = idem(key_source="broker-message-id")
    assert result["covered"] is False and result["verdict"] == "fail"
    assert idem(key_source="broker-message-id", naturally_idempotent=True)["covered"] is True
    assert idem(key_source="producer-event-id")["covered"] is True


def test_cloudevents_year_zero_is_a_valid_rfc3339_date():
    assert ce(CE_BASE + [("time", "0000-02-29T00:00:00Z")])["findings"] == []
    assert fx(ce(CE_BASE + [("time", "0100-02-29T00:00:00Z")]))[0][0] == "major"  # year 100 is not a leap year


CANARIES = {
    "aws-key": ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    "github-token": ("ghp_aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5", "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bD1fH3jL5"),
    "private-key-hex": ("a3f1" * 16, "a3f1a3f1"),
    "mnemonic": ("far far far far far far far far far far far far", "far far far far"),
    "url-credentials": ("https://deploy:Sup3rS3cret@broker.internal.example/q", "Sup3rS3cret"),
    "bearer": ("Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0", "eyJhbGciOiJIUzI1NiJ9"),
    "password-pair": ("password=hunter2hunter2", "hunter2hunter2"),
}


def _leaks(result: dict, marker: str) -> bool:
    return marker in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize("kind", sorted(CANARIES))
def test_no_tool_echoes_a_secret_shaped_input_value(kind):
    secret, marker = CANARIES[kind]
    results = []
    # JSON Schema: property names, enum values, formats, bad type, dangling $ref (INVALID_INPUT message)
    base = obj_schema({secret: STR, "e": {"type": "string", "enum": ["a", secret]}, "f": {"type": "string", "format": secret}},
                      [secret], additionalProperties=False)
    head = obj_schema({"e": {"type": "string", "enum": ["a"]}, "f": {"type": "string", "format": secret + "x"}}, additionalProperties=False)
    results.append(compat(base, head, "full"))
    results.append(compat(head, base, "full"))
    for bad in ({"type": secret}, {"$ref": "#/definitions/" + secret}):
        results.append(call("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": json.dumps(bad),
                                                    "head_schema": "{}"}))
    # Avro: record, field, enum symbol and an unresolved reference
    rec = lambda name, fld, sym: {"type": "record", "name": name, "fields": [  # noqa: E731
        {"name": fld, "type": {"type": "enum", "name": "E", "symbols": ["A", sym]}}]}
    results.append(compat(rec("Order", secret, secret), rec("Other", secret + "y", "A"), "full", "avro"))
    results.append(call("event_schema_compat", {"format": "avro", "mode": "backward", "base_schema": json.dumps(secret), "head_schema": '"string"'}))
    # CloudEvents: names and values
    for name in ("time", "dataschema", "datacontenttype", "type", "specversion", "subject", "source"):
        attrs = [(k, v) for k, v in CE_BASE if k != name] + [(name, secret)]
        results.append(ce(attrs, has_data=True))
    if len(secret) <= 64:
        results.append(ce(CE_BASE + [(secret, "v"), (secret, "w")]))
    # AsyncAPI: channel, operation id, server, dangling and external refs
    doc = copy.deepcopy(V2)
    doc["channels"][secret] = {"publish": {"operationId": secret, "message": {"$ref": "#/components/messages/" + secret}},
                               "subscribe": {"operationId": secret}}
    doc["channels"][secret + "/{" + secret + "}"] = {"publish": {"operationId": "z"}}
    doc["servers"][secret] = {"url": "x"}
    doc["components"]["messages"]["M"] = {"payload": {"$ref": secret}}
    results.append(aa(doc))
    v3 = copy.deepcopy(V3)
    v3["operations"][secret] = {"action": "send", "channel": {"$ref": "#/channels/" + secret}}
    v3["operations"]["x"] = {"action": "send", "channel": {"$ref": secret}}
    v3["servers"] = {secret: {"host": "h"}}
    results.append(aa(v3))
    # DLQ, ordering and saga names
    results.append(dlq(consumer(name=secret, queue="q", dlq="d1", redrive_runbook=False, dlq_alarm=False),
                       consumer(name="other", queue="q2", dlq=secret, redrive_runbook=False),
                       consumer(name="third", queue="q3", dlq=secret)))
    results.append(order(entity_key_fields=["id"], partition_key_fields=["id", secret]))
    results.append(saga([step(secret, "compensable", secret, comp_idempotent=False), step(secret + "p", "pivot"),
                         step(secret + "q", "pivot")]))
    assert len(results) >= 12
    for result in results:
        assert not _leaks(result, marker), (kind, result)


def test_unicode_digits_are_not_rfc3339_or_semver_digits():
    arabic = "\u0662\u0660\u0662\u0666-10-20T10:00:00Z"  # 2026 written with Arabic-Indic digits
    assert fx(ce(CE_BASE + [("time", arabic)]))[0][0] == "major"
    invalid("event_asyncapi_lint", {"document": json.dumps({"asyncapi": "\u0663.\u0660.\u0660", "info": {"title": "T", "version": "1"}})}, "document")


def test_very_long_names_are_answered_quickly_and_never_echoed(monkeypatch):
    module = importlib.import_module("swarm.tools.a19_tools")
    shape_reads = []

    class Counted:
        def __init__(self, pattern):
            self.pattern = pattern

        def search(self, text, *args):
            shape_reads.append(len(text))
            return self.pattern.search(text, *args)

        def finditer(self, text, *args):
            shape_reads.append(len(text))
            return self.pattern.finditer(text, *args)

    monkeypatch.setattr(module, "_SECRET_SHAPES", tuple(Counted(shape) for shape in module._SECRET_SHAPES))
    monkeypatch.setattr(module, "_LONG_RUN", Counted(module._LONG_RUN))
    name = "a" * 150_000  # a quadratic secret-shape scan over such a name used to take minutes
    base = obj_schema({name: STR}, additionalProperties=False)
    result = compat(base, obj_schema({}, additionalProperties=False), "backward")
    assert fx(result) == [("blocker", f"[backward] $.<redacted: {len(name)} chars>: accepted by the source but rejected by the closed target schema")]
    channel = aa({"asyncapi": "2.6.0", "info": {"title": "T", "version": "1"}, "channels": {name: {}}})
    assert fx(channel) == [("minor", f"channels/<redacted: {len(name)} chars>: no publish or subscribe operation")]
    # counted instead of timed: no secret-shape pattern reads a text over the scan limit, so the 150,000-character name is never scanned
    assert max(shape_reads, default=0) <= module._SCAN_LIMIT, max(shape_reads, default=0)
    # the counting double is wired: a short name is read by the shape patterns, so its reads are counted
    aa({"asyncapi": "2.6.0", "info": {"title": "T", "version": "1"}, "channels": {"orders": {}}})
    assert shape_reads, "the counting double never ran"


# --- QA-03 regressions ----------------------------------------------------------------------------------------------


def _tuple_items(*entries):
    return {"type": "array", "items": list(entries)}


def test_schema_compat_ref_siblings_apply_in_addition_to_the_target():
    # the sibling `minimum: 0` must not replace the target's `minimum: 5`: the head still loosens the source's bound
    base = {"$schema": MODERN, "type": "number", "minimum": 1}
    head = {"$schema": MODERN, "$defs": {"P": {"type": "number", "minimum": 5}}, "$ref": "#/$defs/P", "minimum": 0}
    result = compat(base, head)
    assert (result["backward_compatible"], result["verdict"]) == (False, "fail")
    assert fx(result) == [("major", "[backward] $: numeric lower bound loosened (target >= 5)")]
    base = {"$schema": MODERN, **obj_schema({"a": STR, "b": STR}, ["b"], additionalProperties=False)}
    head = {"$schema": MODERN, "$defs": {"P": obj_schema({"a": STR, "b": STR}, ["a"], additionalProperties=False)}, "$ref": "#/$defs/P",
            "required": ["b"]}
    assert fx(compat(base, head)) == [
        ("blocker", "[backward] $.a: required by the target but optional or undeclared in the source")]


def test_schema_compat_ref_siblings_on_the_source_side_are_applied_too():
    base = {"$schema": MODERN, **obj_schema({"a": STR, "b": STR}, ["b"])}
    head = {"$schema": MODERN, "$defs": {"P": obj_schema({"a": STR, "b": STR}, ["a"])}, "$ref": "#/$defs/P", "required": ["b"]}
    result = compat(base, head, "full")
    assert (result["backward_compatible"], result["forward_compatible"]) == (False, True)


def test_schema_compat_tuple_form_items_are_compared_position_by_position():
    base = _tuple_items(STR, INT)
    assert fx(_backward(base, _tuple_items(STR, STR))) == [
        ("blocker", "[backward] $[1]: type ['integer'] is not accepted by target type ['string']")]
    assert _backward(base, _tuple_items(STR, STR))["backward_compatible"] is False
    assert _backward(base, _tuple_items(STR, INT))["backward_compatible"] is True
    assert _backward(base, {"type": "array", "items": STR})["backward_compatible"] is False  # tuple to single form


def test_schema_compat_ref_siblings_follow_the_declared_draft():
    # drafts 3 to 7 ignore the keywords beside a $ref, 2019-09 and later apply them; an undeclared or unrecognised draft is refused
    draft7 = "http://json-schema.org/draft-07/schema#"
    base = {"$schema": draft7, "$ref": "#/definitions/n", "minimum": 10, "definitions": {"n": {"type": "number"}}}
    assert _backward(base, {"$schema": draft7, "type": "number", "minimum": 10})["backward_compatible"] is False
    modern = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$ref": "#/$defs/n", "minimum": 10, "$defs": {"n": {"type": "number"}}}
    assert _backward(modern, {"type": "number", "minimum": 10})["backward_compatible"] is True
    unknown = {"$schema": "urn:example:draft", "$ref": "#/$defs/n", "minimum": 10, "$defs": {"n": {"type": "number"}}}
    invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": json.dumps(unknown), "head_schema": "{}"}, "base_schema")
    undeclared = {"$ref": "#/$defs/n", "minimum": 10, "$defs": {"n": {"type": "number"}}}  # a validator's default draft decides
    invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": json.dumps(undeclared), "head_schema": "{}"}, "base_schema")
    assert _backward({"$ref": "#/$defs/n", "$defs": {"n": {"type": "number"}}}, {"type": "number"})["backward_compatible"] is True  # no siblings: no question


def test_schema_compat_tuple_positions_past_the_source_maxitems_never_occur():
    # maxItems 1 means no array has a second item, so the second tuple entry may change; a reachable position still fails
    short = lambda second: {"type": "array", "items": [STR, second], "maxItems": 1}  # noqa: E731
    assert compat(short(INT), short(STR), "full")["compatible"] is True
    assert _backward(_tuple_items(STR, INT), _tuple_items(STR, STR))["backward_compatible"] is False


def test_schema_compat_const_is_not_a_keyword_of_draft_3_or_4():
    # draft 4 has no const, so the base accepts every integer and an enum head that narrows it is breaking; under draft 6 the const counts
    d4, d6 = "http://json-schema.org/draft-04/schema#", "http://json-schema.org/draft-06/schema#"
    base4 = {"$schema": d4, "type": "integer", "const": 1}
    head4 = {"$schema": d4, "type": "integer", "enum": [1]}
    assert fx(_backward(base4, head4)) == [("major", "[backward] $: target restricts to an enum but the source accepts any value")]
    assert _backward(head4, base4)["backward_compatible"] is True
    assert _backward({**base4, "$schema": d6}, {**head4, "$schema": d6})["backward_compatible"] is True


def test_schema_compat_identical_subtree_under_different_drafts_is_not_shortcut():
    # draft 4 ignores const, so the draft 4 base accepts {"x": 2} while the draft 7 head rejects it: identical text is not the same schema
    d4, d7 = "http://json-schema.org/draft-04/schema#", "http://json-schema.org/draft-07/schema#"
    base = {"$schema": d4, "type": "object", "properties": {"x": {"anyOf": [{"const": 1}]}}}
    head = {"$schema": d7, "type": "object", "properties": {"x": {"anyOf": [{"const": 1}]}}}
    result = _backward(base, head)
    assert result["backward_compatible"] is False
    assert fx(result) == [("major", "[backward] $.x<alt0>: not accepted by any alternative of the target anyOf/oneOf")]
    # the same pair under one declared draft is still a shortcut, so the identical union passes
    assert _backward({**base, "$schema": d7}, head)["backward_compatible"] is True


def test_schema_compat_keywords_beside_anyof_apply_to_each_alternative():
    # the source is a string AND (minLength 1): a string target is backward compatible, and the empty string is the forward break
    base = {"type": "string", "anyOf": [{"minLength": 1}]}
    assert _backward(base, STR)["backward_compatible"] is True and fx(_backward(base, STR)) == []
    assert fx(compat(base, STR, "full")) == [("major", "[forward] $: not accepted by any alternative of the target anyOf/oneOf")]
    # no instance is both a string and an integer, so the source accepts nothing and is compatible with any target
    assert _backward({"type": "string", "anyOf": [INT]}, STR)["backward_compatible"] is True


def test_schema_compat_pointer_array_index_takes_ascii_digits_without_leading_zero():
    base = {"allOf": [STR], "properties": {"a": {"$ref": "#/allOf/0"}}}
    assert compat(base, copy.deepcopy(base), "full")["compatible"] is True  # index 0 resolves
    for ref in ("#/allOf/\u00b2", "#/allOf/01"):  # superscript two passes str.isdigit(); RFC 6901 forbids a leading zero
        base["properties"]["a"]["$ref"] = ref
        result = invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": json.dumps(base),
                                                 "head_schema": json.dumps({"type": "object"})}, "base_schema")
        assert "does not resolve" in result["message"]


def test_asyncapi_cyclic_message_one_of_is_reported_once_not_recursed():
    loop = {"oneOf": [{"$ref": "#/components/messages/M"}]}
    doc3 = {"asyncapi": "3.0.0", "info": {"title": "t", "version": "1"},
            "channels": {"c": {"address": "a", "messages": {"m": {"$ref": "#/components/messages/M"}}}},
            "components": {"messages": {"M": loop}}}
    assert fx(aa(doc3)) == [("major", "channels/c/messages/m/oneOf/0: message $ref is cyclic: a oneOf refers back to itself")]
    doc2 = {"asyncapi": "2.6.0", "info": {"title": "t", "version": "1"},
            "channels": {"c": {"subscribe": {"operationId": "s", "message": {"$ref": "#/components/messages/M"}}}},
            "components": {"messages": {"M": loop}}, "servers": {"s": {"url": "u", "protocol": "kafka"}}}
    assert ("major", "channels/c/subscribe/message/oneOf/0: message $ref is cyclic: a oneOf refers back to itself") in fx(aa(doc2))


def test_asyncapi_version_is_echoed_only_through_the_redaction_rule():
    secret = "AKIAIOSFODNN7EXAMPLE"
    result = aa({"asyncapi": f"3.0.0-{secret}", "info": {"title": "t", "version": "1"}, "channels": {}})
    assert secret not in json.dumps(result) and result["asyncapi_version"] == "<redacted: 26 chars>"
    unsupported = aa({"asyncapi": f"1.0.0-{secret}", "info": {"title": "t", "version": "1"}})
    assert secret not in json.dumps(unsupported)
    assert fx(unsupported) == [("blocker", "AsyncAPI <redacted: 26 chars> is not supported by this lint (2.x and 3.x)")]


def test_retry_queue_redelivery_refuses_a_visibility_timeout_beyond_the_sqs_maximum():
    over = retry(base_delay_s=1, max_delay_s=2, max_attempts=2, processing_timeout_s=10, visibility_timeout_s=50000, budget_s=1e6)
    assert (over["fits_visibility"], over["verdict"]) == (False, "fail")
    assert fx(over) == [("major", "visibility timeout 50000 s exceeds the 43200 s SQS maximum")]
    edge = retry(base_delay_s=1, max_delay_s=2, max_attempts=2, processing_timeout_s=10, visibility_timeout_s=43200, budget_s=1e6)
    assert edge["fits_visibility"] is True and edge["findings"] == []


def test_ordering_without_an_entity_key_is_not_reported_as_ordered():
    result = order(order_required=False, entity_key_fields=[])
    assert (result["ordered"], result["verdict"]) == (False, "pass")
    assert fx(result) == [("info", "entity_key_fields is empty: per-entity order cannot be judged, so ordered is false")]


def test_avro_aliases_that_are_not_a_list_are_invalid_input_at_record_and_field_level():
    # the record and the field form of a non-list aliases used to raise TypeError out of the handler
    record = {"type": "record", "name": "R", "aliases": 5, "fields": [{"name": "a", "type": "string"}]}
    field = {"type": "record", "name": "R", "fields": [{"name": "a", "type": "string", "aliases": 5}]}
    for schema in (record, field):
        result = invalid("event_schema_compat", {"format": "avro", "mode": "backward", "base_schema": json.dumps(schema),
                                                 "head_schema": json.dumps(schema)}, "base_schema")
        assert "must be a list of strings" in result["message"]
    listed = {"type": "record", "name": "R", "aliases": ["Old"], "fields": [{"name": "a", "type": "string", "aliases": ["b"]}]}
    assert compat(listed, copy.deepcopy(listed), "full", "avro")["compatible"] is True


def test_asyncapi_channel_parameters_must_be_an_object_not_a_string_or_list():
    def document(parameters):
        return {"asyncapi": "2.6.0", "info": {"title": "T", "version": "1"},
                "channels": {"orders/{id}": {"parameters": parameters,
                                             "publish": {"operationId": "p", "message": MSG}}}}
    # a non-mapping parameters value is a major finding, not a crash
    assert ("major", "channels/orders/{id}: parameters must be an object, so no parameter of orders/{id} is declared") in fx(aa(document(5)))
    # a string is not a mapping: a substring of the string must not count as a declared parameter
    result = aa(document("id"))
    assert result["verdict"] == "fail"
    assert ("major", "channels/orders/{id}: parameter {id} is not declared under parameters") in fx(result)
    assert aa(document([1, 2]))["verdict"] == "fail"
    assert aa(document({"id": {"schema": {"type": "string"}}}))["verdict"] == "pass"


def _record_walks(monkeypatch, module):
    """Every lint walk, recorded by a counting double on its step charge; the last one's `steps` is the work it did."""
    walks = []
    real_charge = module._AsyncApiWalk.charge

    def counted_charge(self, steps):
        if not walks or walks[-1] is not self:
            walks.append(self)
        return real_charge(self, steps)

    monkeypatch.setattr(module._AsyncApiWalk, "charge", counted_charge)
    return walks


def test_asyncapi_one_of_chain_with_a_repeated_alternative_is_linear_not_exponential(monkeypatch):
    module = importlib.import_module("swarm.tools.a19_tools")
    walks = _record_walks(monkeypatch, module)
    # each level lists the next message twice: the unmemoised walk doubled per level and took minutes at depth 26
    depth = 26
    components = {f"M{depth}": MSG}
    for level in range(depth - 1, -1, -1):
        nxt = {"$ref": f"#/components/messages/M{level + 1}"}
        components[f"M{level}"] = {"oneOf": [nxt, dict(nxt)]}
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {"c": {"address": "a", "messages": {"m": {"$ref": "#/components/messages/M0"}}}},
           "components": {"messages": components}}
    result = aa(doc)
    assert (result["verdict"], result["findings"], result["messages"]) == ("pass", [], 1)
    # counted: a memoised walk charges a few steps per level (132 at depth 26 at HEAD); the unmemoised walk needed 2**26 paths
    assert walks and walks[-1].steps <= 12 * depth, walks[-1].steps if walks else None


def test_asyncapi_shared_leaf_keeps_its_findings_for_every_path_that_reaches_it():
    # a memoised walk must still report the leaf's minors under each oneOf path, as the unmemoised walk does
    doc = {"asyncapi": "3.0.0", "info": {"title": "t", "version": "1"},
           "channels": {"c": {"address": "a", "messages": {"m": {"$ref": "#/components/messages/Top"}}}},
           "components": {"messages": {"Top": {"oneOf": [{"$ref": "#/components/messages/Leaf"}, {"$ref": "#/components/messages/Leaf"}]},
                                        "Leaf": {"contentType": None}}}}
    assert fx(aa(doc)) == [
        ("minor", "channels/c/messages/m/oneOf/0: message has no payload schema, so consumers have no contract"),
        ("minor", "channels/c/messages/m/oneOf/0: no contentType and no defaultContentType"),
        ("minor", "channels/c/messages/m/oneOf/1: message has no payload schema, so consumers have no contract"),
        ("minor", "channels/c/messages/m/oneOf/1: no contentType and no defaultContentType"),
    ]


def test_retention_replay_duration_beyond_the_finite_range_is_invalid_input_not_an_overflow():
    result = invalid("event_retention_replay", {"retention_s": 1e9, "replay_from_age_s": 1e3, "replay_events": 1e12,
                                                "replay_rate_per_s": 5e-324, "live_rate_per_s": 0, "consumers_idempotent": True,
                                                "compacted": False, "environment": "non-prod"}, "replay_events")
    assert "finite duration" in result["message"]


def test_partition_skew_threshold_is_restored_and_drives_the_skew_finding():
    # SKEW_WARN was lost in a QA-03 edit: the skew branch raised NameError. Its HEAD value is 2 and the branch must run.
    module = importlib.import_module("swarm.tools.a19_tools")
    assert str(module.SKEW_WARN) == "2"
    result = plan(hot_key_share_pct=5)  # skew ratio 2.4 is above SKEW_WARN
    assert result["skew_ratio"] > 2 and result["partitions_by_skew"] == 29
    assert fx(result) == [("minor", "key skew: the hottest partition carries 2.4x the mean load")]


def test_asyncapi_two_channels_referencing_two_leaf_messages_count_two_messages():
    # R-3: memoised walks keyed by relative ids collapsed distinct leaves into one message
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {"c1": {"address": "a", "messages": {"m": {"$ref": "#/components/messages/A"}}},
                        "c2": {"address": "b", "messages": {"m": {"$ref": "#/components/messages/B"}}}},
           "components": {"messages": {"A": MSG, "B": dict(MSG)}}}
    result = aa(doc)
    assert (result["messages"], result["verdict"], result["findings"]) == (2, "pass", [])


def _ref_chain(refs: int) -> dict:
    """A document whose root is `refs` $ref hops away from a string schema."""
    definitions = {f"d{i}": {"$ref": f"#/definitions/d{i + 1}"} for i in range(refs - 1)}
    definitions[f"d{refs - 1}"] = STR
    return {"$ref": "#/definitions/d0", "definitions": definitions}


def test_schema_compat_accepts_a_21_hop_ref_chain():
    # N-G: a 21-hop chain is valid and must not be refused
    chain = _ref_chain(21)
    assert compat(chain, copy.deepcopy(chain), "full")["compatible"] is True


def test_schema_compat_refuses_a_ref_chain_beyond_64_hops_naming_the_schema():
    chain = _ref_chain(65)
    result = invalid("event_schema_compat", {"format": "json-schema", "mode": "backward", "base_schema": json.dumps(chain),
                                             "head_schema": json.dumps(chain)}, "base_schema")
    assert "64 hops" in result["message"]
    assert compat(_ref_chain(64), copy.deepcopy(_ref_chain(64)), "full")["compatible"] is True


def test_asyncapi_cyclic_three_channel_document_matches_the_unmemoised_walk():
    # S-3: a memoised result replayed under a path where an ancestor it was cut by is absent gave 4 messages; HEAD gives 6
    comps = {"T": {"oneOf": [{"$ref": "#/components/messages/X"}, {"contentType": "i"}]},
             "X": {"oneOf": [{"$ref": "#/components/messages/T"}, {"contentType": "j"}]}}
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {f"c{i}": {"address": f"a{i}", "messages": {"m": {"$ref": f"#/components/messages/{ref}"}}}
                        for i, ref in enumerate(["T", "X", "X"])},
           "components": {"messages": comps}}
    result = aa(doc)
    assert (result["messages"], result["verdict"]) == (6, "fail")
    assert fx(result) == [
        ("major", "channels/c0/messages/m/oneOf/0/oneOf/0: message $ref is cyclic: a oneOf refers back to itself"),
        ("major", "channels/c1/messages/m/oneOf/0/oneOf/0: message $ref is cyclic: a oneOf refers back to itself"),
        ("major", "channels/c2/messages/m/oneOf/0/oneOf/0: message $ref is cyclic: a oneOf refers back to itself"),
        ("minor", "channels/c0/messages/m/oneOf/0/oneOf/1: message has no payload schema, so consumers have no contract"),
        ("minor", "channels/c0/messages/m/oneOf/1: message has no payload schema, so consumers have no contract"),
        ("minor", "channels/c1/messages/m/oneOf/0/oneOf/1: message has no payload schema, so consumers have no contract"),
        ("minor", "channels/c1/messages/m/oneOf/1: message has no payload schema, so consumers have no contract"),
        ("minor", "channels/c2/messages/m/oneOf/0/oneOf/1: message has no payload schema, so consumers have no contract"),
        ("minor", "channels/c2/messages/m/oneOf/1: message has no payload schema, so consumers have no contract"),
    ]


def test_asyncapi_cyclic_document_past_the_step_budget_stops_with_a_reason():
    # S-6: every component is a oneOf of all the others plus a leaf; the unbounded walk grows about x14 per component (n=9 took
    # 16 s). Past its budgets the walk stops and the document is reported fail with the budget reason: that reason is the counted check.
    module = importlib.import_module("swarm.tools.a19_tools")
    n = 9
    comps = {f"C{i}": {"oneOf": [{"$ref": f"#/components/messages/C{j}"} for j in range(n) if j != i] + [{"contentType": "x"}]}
             for i in range(n)}
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {"c": {"address": "a", "messages": {"m": {"$ref": "#/components/messages/C0"}}}},
           "components": {"messages": comps}}
    result = aa(doc)
    assert result["verdict"] == "fail"
    stopped = [summary for severity, summary in fx(result) if severity == "major" and summary.startswith(module.ASYNCAPI_WALK_STOPPED)]
    assert len(stopped) == 1 and f"after {module.ASYNCAPI_EMIT_BUDGET} findings copied" in stopped[0]


def test_asyncapi_acyclic_document_of_500_messages_is_walked_in_full_and_passes():
    # the budget leaves realistic documents alone: 500 channels, one message each, walked in full and counted
    n = 500
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {f"c{i}": {"address": f"a{i}", "messages": {"m": {"$ref": f"#/components/messages/M{i}"}}} for i in range(n)},
           "components": {"messages": {f"M{i}": MSG for i in range(n)}}}
    result = aa(doc)
    assert (result["messages"], result["verdict"], result["findings"]) == (500, "pass", [])


def test_asyncapi_shared_pool_replayed_by_500_channels_is_warn_with_500_messages_as_at_head():
    # N-1: 500 channels each refer to one pool of 500 leaves, so the pool's 500 findings are replayed 500 times: 250000 findings,
    # a legal document. HEAD reports warn, 500 messages and 300 findings. Replays cost one step each, so the budget is not spent here.
    leaves = {f"L{i}": {"contentType": "x"} for i in range(500)}
    leaves["P"] = {"oneOf": [{"$ref": f"#/components/messages/L{i}"} for i in range(500)]}
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {f"c{i}": {"address": f"a{i}", "messages": {"m": {"$ref": "#/components/messages/P"}}} for i in range(500)},
           "components": {"messages": leaves}}
    result = aa(doc)
    assert (result["messages"], result["verdict"], len(result["findings"])) == (500, "warn", 300)
    assert all("stopped" not in finding["summary"] for finding in result["findings"])


def _message_chain(levels: int) -> dict:
    """A document whose message at each level is a oneOf of the next level and one inline alternative; the last level is complete."""
    comps = {f"M{levels}": MSG}
    for index in range(levels - 1, -1, -1):
        comps[f"M{index}"] = {"oneOf": [{"$ref": f"#/components/messages/M{index + 1}"}, {"contentType": "x"}]}
    return {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
            "channels": {"c": {"address": "a", "messages": {"m": {"$ref": "#/components/messages/M0"}}}},
            "components": {"messages": comps}}


def test_asyncapi_chain_of_600_levels_lints_as_at_head_and_a_deeper_chain_is_a_reason_not_an_exception():
    # N-2: one Python frame per level, so a 600-level chain lints as at HEAD (warn, 601 messages); past the depth limit the walk stops
    result = aa(_message_chain(600))
    assert (result["messages"], result["verdict"], len(result["findings"])) == (601, "warn", 300)
    deep = aa(_message_chain(1500))  # under the handler's 200000-character bound; a chain deeper than 700 levels still stops
    assert deep["verdict"] == "fail"
    stops = [summary for severity, summary in fx(deep) if severity == "major" and "deeper than 700 levels" in summary]
    assert len(stops) == 1 and stops[0].startswith("AsyncAPI message walk stopped under channels/c/messages/m")


def test_asyncapi_shared_pool_of_600_channels_lints_as_at_head():
    # N-4: 600 channels refer to one pool of 600 leaves, so 360000 findings are copied: HEAD gives warn, 600 messages, 300 findings
    leaves = {f"L{i}": {"contentType": "x"} for i in range(600)}
    leaves["P"] = {"oneOf": [{"$ref": f"#/components/messages/L{i}"} for i in range(600)]}
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {f"c{i}": {"address": f"a{i}", "messages": {"m": {"$ref": "#/components/messages/P"}}} for i in range(600)},
           "components": {"messages": leaves}}
    result = aa(doc)
    assert (result["messages"], result["verdict"], len(result["findings"])) == (600, "warn", 300)
    assert all("stopped" not in finding["summary"] for finding in result["findings"])


def test_asyncapi_stop_survives_truncation_and_says_the_message_count_is_partial():
    # N-5: 320 blockers would fill the 300 findings and cut the stop; the stop is pinned, so it stays in the output. 298 blockers are
    # shown with it and the truncation note. That is a choice, not a contract requirement: _MAX_FINDINGS (300) includes the stop and the
    # note. The registered findings maxItems is 301 (agents.json), so 299 blockers would also have been contract-legal.
    doc = _message_chain(800)
    doc["operations"] = {f"o{i}": {"action": "publish", "channel": {"$ref": "#/channels/c"}} for i in range(320)}
    result = aa(doc)
    summaries = [summary for _, summary in fx(result)]
    stops = [summary for summary in summaries if summary.startswith("AsyncAPI message walk stopped under channels/c/messages/m")]
    assert result["verdict"] == "fail" and len(result["findings"]) == 300
    assert len(stops) == 1 and stops[0].endswith("the message count and the findings list are both partial")
    assert sum(1 for severity, _ in fx(result) if severity == "blocker") == 298


def test_asyncapi_stopped_cyclic_document_reports_the_messages_reached_not_zero():
    # N-6: n=9 stops at the replay ceiling; the messages reached before the stop are counted, and the stop says both counts are partial
    n = 9
    comps = {f"C{i}": {"oneOf": [{"$ref": f"#/components/messages/C{j}"} for j in range(n) if j != i] + [{"contentType": "x"}]}
             for i in range(n)}
    doc = {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
           "channels": {"c": {"address": "a", "messages": {"m": {"$ref": "#/components/messages/C0"}}}},
           "components": {"messages": comps}}
    result = aa(doc)
    stops = [summary for _, summary in fx(result) if summary.startswith("AsyncAPI message walk stopped")]
    assert len(stops) == 1 and stops[0].endswith("the message count and the findings list are both partial")
    assert result["messages"] > 0 and result["verdict"] == "fail"


def test_stop_summary_with_60_character_names_keeps_the_whole_sentence():
    # R6-4: 60-character channel and message names push the stop reason past the 300-character summary; the origin is shortened, the
    # sentence is kept whole
    module = importlib.import_module("swarm.tools.a19_tools")
    doc = _message_chain(800)
    doc["channels"] = {"k" * 60: {"address": "a", "messages": {"j" * 60: {"$ref": "#/components/messages/M0"}}}}
    result = aa(doc)
    stops = [summary for _, summary in fx(result) if summary.startswith("AsyncAPI message walk stopped under channels/")]
    assert len(stops) == 1 and len(stops[0]) <= module._SUMMARY_MAX
    assert stops[0].endswith("the message count and the findings list are both partial")


def _pool(channels: int, leaves: int) -> dict:
    """`channels` channels, each referring to one pool of `leaves` leaves, every leaf without a payload (one minor per leaf)."""
    pool = {f"L{i}": {"contentType": "x"} for i in range(leaves)}
    pool["P"] = {"oneOf": [{"$ref": f"#/components/messages/L{i}"} for i in range(leaves)]}
    return {"asyncapi": "3.0.0", "info": {"title": "T", "version": "1"},
            "channels": {f"c{i}": {"address": f"a{i}", "messages": {"m": {"$ref": "#/components/messages/P"}}} for i in range(channels)},
            "components": {"messages": pool}}


def test_shared_pool_at_the_copy_ceiling_lints_as_at_head_and_one_channel_more_is_refused():
    # R7-4: the ceiling is 500,000 copies. 999 channels of a 500-leaf pool copy exactly that many and lint as HEAD does; 1,000 copy
    # 500,500 and are refused with the stop reason
    module = importlib.import_module("swarm.tools.a19_tools")
    assert module.ASYNCAPI_EMIT_BUDGET == 500000
    at = aa(_pool(999, 500))
    assert (at["messages"], at["verdict"], len(at["findings"])) == (500, "warn", 300)
    assert all(not summary.startswith(module.ASYNCAPI_WALK_STOPPED) for _, summary in fx(at))
    over = aa(_pool(1000, 500))
    stops = [summary for severity, summary in fx(over) if summary.startswith(module.ASYNCAPI_WALK_STOPPED)]
    assert over["verdict"] == "fail" and len(stops) == 1 and "after 500000 findings copied" in stops[0]
