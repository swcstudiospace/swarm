"""Replay the recorded omp contract vectors against the current omp/src sources.

Each record in ``vectors.json`` cites ``source`` as ``path:line`` or ``path:start-end``,
relative to the repository root. A record matches when its ``value``, rendered as text,
appears in the cited start line or in the three lines after it:

* a string value is matched as-is;
* a list value matches when every element appears (each element rendered as above);
* any other value (dict, number, bool, null) is matched as ``json.dumps(value)``.

The rule is never relaxed: a record that does not match fails the test and is named in
the assertion message. The replay only reads files; it writes nothing and spawns nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
VECTORS_PATH = TESTS_DIR / "vectors.json"
WINDOW_AFTER = 3  # the cited line plus this many following lines
SOURCE_RE = re.compile(r"^(?P<path>[^:]+):(?P<start>\d+)(?:-(?P<end>\d+))?$")


def load_records() -> list[dict]:
    with VECTORS_PATH.open(encoding="utf-8") as handle:
        records = json.load(handle)
    assert isinstance(records, list), "vectors.json must hold a JSON array"
    return records


def render(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value)


def needles_for(value: object) -> list[str]:
    if isinstance(value, list):
        return [render(item) for item in value]
    return [render(value)]


def source_window(source: str, cache: dict[str, list[str]]) -> tuple[str, str] | str:
    """Return (location, window_text) for a record source, or an error string."""
    match = SOURCE_RE.match(source or "")
    if match is None:
        return f"unparseable source {source!r}"
    rel_path = match.group("path")
    start = int(match.group("start"))
    location = f"{rel_path}:{start}-{start + WINDOW_AFTER}"
    if rel_path not in cache:
        target = REPO_ROOT / rel_path
        if not target.is_file():
            return f"source file missing: {rel_path}"
        cache[rel_path] = target.read_text(encoding="utf-8").splitlines()
    lines = cache[rel_path]
    if start < 1 or start > len(lines):
        return f"line {start} out of range in {rel_path} ({len(lines)} lines)"
    window = "\n".join(lines[start - 1 : start + WINDOW_AFTER])
    return location, window


def check_record(record: dict, cache: dict[str, list[str]]) -> str | None:
    """Return None when the record matches its source, else the mismatch reason."""
    if "value" not in record:
        return "record has no value"
    located = source_window(record.get("source", ""), cache)
    if isinstance(located, str):
        return located
    location, window = located
    needles = needles_for(record["value"])
    if not needles:
        return f"empty list value cannot be matched in {location}"
    missing = [needle for needle in needles if needle not in window]
    if missing:
        shown = ", ".join(repr(needle) for needle in missing)
        return f"value not found in {location}: {shown}"
    return None


def replay() -> dict[str, str | None]:
    """Map each record id to None (matched) or its mismatch reason."""
    results: dict[str, str | None] = {}
    cache: dict[str, list[str]] = {}
    for record in load_records():
        record_id = str(record.get("id", "<no id>"))
        if record_id in results:
            results[record_id] = f"duplicate record id {record_id}"
            continue
        results[record_id] = check_record(record, cache)
    return results


def test_every_vector_matches_its_source() -> None:
    results = replay()
    assert results, "vectors.json has no records"
    mismatches = [
        f"[{record_id}] {reason}"
        for record_id, reason in results.items()
        if reason is not None
    ]
    assert not mismatches, (
        f"{len(mismatches)} of {len(results)} vector records do not match source:\n"
        + "\n".join(mismatches)
    )
