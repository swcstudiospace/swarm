"""Stdlib-only validator for the Draft 2020-12 subset used by swarm/schemas/*.json.

Supported keywords: type, required, enum, const, properties, items, additionalProperties, minimum, maximum,
exclusiveMinimum, minItems, minLength, allOf, if/then. (const, minimum, maximum, minLength, allOf and if/then serve the v2.2
event, live and handover schemas; `if` is matched by "validates with no errors".) Annotation keywords are ignored; an
unknown type name raises ValueError.
Schemas are read-only at runtime; nothing in the swarm writes them.
"""
from __future__ import annotations
from collections.abc import Callable
from typing import Any
import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"

_TYPES: dict[str, Callable[[object], bool]] = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def load_schema(name: str) -> dict[str, Any]:
    with open(SCHEMA_DIR / f"{name}.json", encoding="utf-8") as f:
        doc: dict[str, Any] = json.load(f)
        return doc


def validate(obj: object, schema: dict[str, Any], path: str = "") -> list[str]:
    """Return a list of "<json-pointer>: message" errors; empty means valid."""
    where = path or "/"
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        unknown = [t for t in types if t not in _TYPES]
        if unknown:
            raise ValueError(f"unknown type name {unknown[0]!r} at {where}")
        if not any(_TYPES[t](obj) for t in types):
            return [f"{where}: expected {'|'.join(types)}, got {type(obj).__name__}"]
    errors: list[str] = []
    if "enum" in schema and obj not in schema["enum"]:
        errors.append(f"{where}: {obj!r} not one of {schema['enum']}")
    if "const" in schema and (obj != schema["const"] or type(obj) is not type(schema["const"])):
        errors.append(f"{where}: {obj!r} is not the constant {schema['const']!r}")
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        if "minimum" in schema and obj < schema["minimum"]:
            errors.append(f"{where}: {obj!r} is below the minimum {schema['minimum']!r}")
        if "maximum" in schema and obj > schema["maximum"]:
            errors.append(f"{where}: {obj!r} is above the maximum {schema['maximum']!r}")
        if "exclusiveMinimum" in schema and obj <= schema["exclusiveMinimum"]:
            errors.append(f"{where}: {obj!r} is not above the exclusive minimum {schema['exclusiveMinimum']!r}")
    if isinstance(obj, str) and "minLength" in schema and len(obj) < schema["minLength"]:
        errors.append(f"{where}: string shorter than minLength {schema['minLength']}")
    if isinstance(obj, list) and "minItems" in schema and len(obj) < schema["minItems"]:
        errors.append(f"{where}: {len(obj)} items is below minItems {schema['minItems']}")
    for sub in schema.get("allOf", []):
        errors += validate(obj, sub, path)
    if "if" in schema and "then" in schema and not validate(obj, schema["if"], path):
        errors += validate(obj, schema["then"], path)
    if isinstance(obj, dict):
        for key in schema.get("required", []):
            if key not in obj:
                errors.append(f"{path}/{key}: required")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, val in obj.items():
            if key in props:
                errors += validate(val, props[key], f"{path}/{key}")
            elif extra is False:
                errors.append(f"{path}/{key}: additional property not allowed")
            elif isinstance(extra, dict):
                errors += validate(val, extra, f"{path}/{key}")
    if isinstance(obj, list) and isinstance(schema.get("items"), dict):
        for i, val in enumerate(obj):
            errors += validate(val, schema["items"], f"{path}/{i}")
    return errors
