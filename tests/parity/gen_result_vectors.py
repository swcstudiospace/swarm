#!/usr/bin/env python3
"""Vectors for the pure Rust result parser (`crates/gsd-route/src/result_parse.rs`), generated from the REAL Python oracle.

Every case is one agent output text and one assigned task id. The generator runs `swarm.results.parse_result(text)` and then
`swarm.results.validate_result(parsed, task_id=...)` exactly as `apply_result` callers do, and records what Python did:

* `parse.outcome`: `value` (a dict came back, `parse.value` is its text), `none` (Python returned None) or `raises` (an exception escaped,
  `parse.exception` names its class: the 4300-digit integer limit raises `ValueError`).
* `validate.outcome`: `ok`, `error` (`validate.message` is `str(SwarmError)`, so `E-CONTRACT: ...` with the 500 character cut applied) or
  `skipped` (parse raised). `validate_result(None)` is a recorded `error` ("no JSON result block from agent").

Encoding of a parsed value (`parse.value`): `json.dumps(value, indent=2)`, with Python's default `ensure_ascii` and no key sorting. That is
byte for byte what `gsd_route::to_python_json` writes, and because it keeps insertion order it also pins Python's duplicate-key resolution
(last value wins, first position kept).

`rust.parse` and `rust.validate` state what the Rust port MUST do for the case, derived from the Python outcome by a documented rule:

* `exact`: Rust returns the very result Python returned (the same dict text, `none`, `ok`, or the same error message).
* `not_started`: Rust returns the typed refusal and never an answer. Parse is `not_started` when Python raised, or when the dict it returned
  holds something Rust does not reproduce byte for byte: an integer outside the i64 range, a float that is NaN or infinite (`NaN`,
  `Infinity`, `-Infinity` and an overflowing literal such as `1e999`), a lone UTF-16 surrogate in a string or key, or containers nested more
  than 100 deep. Validate is `not_started` when Python's text embeds the `repr` of an object that Rust does not format (a float, a list, a
  dict, or a string with a non-ASCII character): an enum failure on `state`, or a `task_id` mismatch.

Determinism: the fuzz set is drawn from `random.Random(FUZZ_SEED)`; nothing reads a clock, the environment or the file system but the repository's
own schema and Python sources; keys are sorted where a mapping has no order of its own, and one case is written per line. The texts assume
CPython 3.12 with the default 4300-digit integer limit; another interpreter changes the file and the freshness test says so.

`python3 tests/parity/gen_result_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from swarm import results as results_module  # noqa: E402
from swarm.errors import SwarmError  # noqa: E402
from swarm.results import parse_result, validate_result  # noqa: E402
from swarm.schema import load_schema, validate  # noqa: E402
from swarm.taskstore import TaskState  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "result_parse.json"
SCHEMA = "gsd-route.result-parse-vectors.v1"
FUZZ_SEED = 14_5_035
FUZZ_CASES = 480
I64_MIN = -(2**63)
I64_MAX = 2**63 - 1
DEPTH_CAP = 100
SCHEMA_PATH = REPO / "swarm" / "schemas" / "task.result.v1.json"
SCHEMA_ENUM: list[str] = load_schema("task.result.v1")["properties"]["state"]["enum"]

Case = dict[str, Any]
Value = Any


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def space_ranges() -> list[list[int]]:
    """Inclusive code point ranges matched by Python's `re` `\\s` (what `JSON_BLOCK` uses for its optional whitespace)."""
    pattern = re.compile(r"\s")
    ranges: list[list[int]] = []
    for code in range(0x110000):
        if pattern.fullmatch(chr(code)):
            if ranges and ranges[-1][1] == code - 1:
                ranges[-1][1] = code
            else:
                ranges.append([code, code])
    return ranges


# ---------------------------------------------------------------------------------------------------------------------------------------
# What the Rust port is expected to refuse, stated independently of the Rust code (see the module docstring).


def _depth(value: Value) -> int:
    if isinstance(value, dict):
        return 1 + max((_depth(v) for v in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(v) for v in value), default=0)
    return 0


def _has_surrogate(text: str) -> bool:
    return any(0xD800 <= ord(ch) <= 0xDFFF for ch in text)


def _unsupported(value: Value) -> bool:
    """True when the parsed value holds something the Rust parser refuses instead of answering."""
    if _depth(value) > DEPTH_CAP:
        return True
    stack: list[Value] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, bool) or item is None:
            continue
        if isinstance(item, int):
            if not I64_MIN <= item <= I64_MAX:
                return True
        elif isinstance(item, float):
            if item != item or item in (float("inf"), float("-inf")):
                return True
        elif isinstance(item, str):
            if _has_surrogate(item):
                return True
        elif isinstance(item, dict):
            for key, val in item.items():
                if _has_surrogate(key):
                    return True
                stack.append(val)
        elif isinstance(item, list):
            stack.extend(item)
    return False


def _reproducible_repr(value: Value) -> bool:
    """Rust formats `repr` only for None, bool, int and ASCII str (the scalars an agent mistypes `state` or `task_id` with)."""
    if value is None or isinstance(value, (bool, int)):
        return True
    return isinstance(value, str) and value.isascii()


def _validate_label(parsed: dict[str, Any], task_id: str) -> str:
    errors = validate(parsed, load_schema("task.result.v1"))
    if errors:
        if "state" in parsed and parsed["state"] not in SCHEMA_ENUM and not _reproducible_repr(parsed["state"]):
            return "not_started"
        return "exact"
    if parsed["task_id"] != task_id and not (parsed["task_id"].isascii() and task_id.isascii()):
        return "not_started"
    return "exact"


def run_case(case_id: str, text: str, task_id: str, refused: bool = False) -> Case:
    """Run the real Python path for one text and record outcomes plus the derived Rust expectation.

    `refused` forces `not_started` for a case whose text Python rejects or reads as a non-object but that Rust abandons before the end
    (containers nested deeper than the cap: Rust stops at the cap instead of reading on to Python's verdict).
    """
    text.encode("utf-8")  # a lone surrogate code point cannot reach a Rust &str: the texts only ever carry escape SEQUENCES of one
    parse: Case
    validate_out: Case
    rust_parse = "not_started" if refused else "exact"
    rust_validate = "not_started" if refused else "exact"
    parsed: dict[str, Any] | None = None
    try:
        parsed = parse_result(text)
    except Exception as exc:  # noqa: BLE001 - the oracle's crash is the recorded outcome
        parse = {"outcome": "raises", "value": None, "exception": type(exc).__name__}
        validate_out = {"outcome": "skipped", "message": None}
        rust_parse, rust_validate = "not_started", "not_started"
        return _case(case_id, text, task_id, parse, validate_out, rust_parse, rust_validate)
    if parsed is None:
        parse = {"outcome": "none", "value": None, "exception": None}
    else:
        if _unsupported(parsed) or refused:
            rust_parse = "not_started"
            parse = {"outcome": "value", "value": None, "exception": None}
        else:
            parse = {"outcome": "value", "value": json.dumps(parsed, indent=2), "exception": None}
    try:
        validate_result(parsed, task_id=task_id)
        validate_out = {"outcome": "ok", "message": None}
    except SwarmError as exc:
        validate_out = {"outcome": "error", "message": str(exc)}
        if parsed is not None and rust_parse == "exact":
            rust_validate = _validate_label(parsed, task_id)
        elif parsed is not None:
            rust_validate = "not_started"
    if rust_parse == "not_started":
        rust_validate = "not_started"
    return _case(case_id, text, task_id, parse, validate_out, rust_parse, rust_validate)


def _case(case_id: str, text: str, task_id: str, parse: Case, validate_out: Case, rust_parse: str, rust_validate: str) -> Case:
    return {"id": case_id, "text": text, "task_id": task_id, "parse": parse, "validate": validate_out,
            "rust": {"parse": rust_parse, "validate": rust_validate}}


# ---------------------------------------------------------------------------------------------------------------------------------------
# Hand-written cases


def fence(body: str, opener: str = "```json\n", closer: str = "\n```") -> str:
    return opener + body + closer


def ok_result(task_id: str = "T1", state: str = "IN_REVIEW", **extra: Value) -> dict[str, Any]:
    return {"task_id": task_id, "state": state, **extra}


def dumps(value: Value) -> str:
    return json.dumps(value)


def hand_cases() -> list[tuple[str, str, str, bool]]:
    """(id, text, assigned task id, refused) for every hand-written case, in file order."""
    cases: list[tuple[str, str, str, bool]] = []

    def add(case_id: str, text: str, task_id: str = "T1", refused: bool = False) -> None:
        cases.append((case_id, text, task_id, refused))

    good = dumps(ok_result())
    # -- which fence spellings count and what is taken -------------------------------------------------------------------------------
    add("fence:plain", fence(good))
    add("fence:prose-around", "Done.\n\n" + fence(good) + "\n\nThanks.")
    add("fence:crlf", fence(good, "```json\r\n", "\r\n```"))
    add("fence:crlf-multiline-body", fence(json.dumps(ok_result(summary_md="a\nb"), indent=2).replace("\n", "\r\n"), "```json\r\n", "\r\n```\r\n"))
    add("fence:inline-no-newline", "```json" + good + "```")
    add("fence:space-and-tab-padding", "```json \t\n  " + good + " \t\n  ```")
    add("fence:blank-lines-before-brace", "```json\n\n\n" + good + "\n\n\n```")
    add("fence:uppercase-json", "```JSON\n" + good + "\n```")
    add("fence:json5", "```json5\n" + good + "\n```")
    add("fence:jsonc", "```jsonc\n" + good + "\n```")
    add("fence:space-before-json", "``` json\n" + good + "\n```")
    add("fence:tilde", "~~~json\n" + good + "\n~~~")
    add("fence:python-fence", "```python\n" + good + "\n```")
    add("fence:bare-fence-no-language", "```\n" + good + "\n```")
    add("fence:array-block", "```json\n[" + good + "]\n```")
    add("fence:string-block", '```json\n"hello"\n```')
    add("fence:four-backticks", "````json\n" + good + "\n````")
    add("fence:five-backtick-close", "```json\n" + good + "\n`````")
    add("fence:closing-fence-two-backticks", "```json\n" + good + "\n``")
    add("fence:unterminated", "```json\n" + good)
    add("fence:unterminated-with-bare-fallback", "```json\n" + good + "\n")
    add("fence:missing-open-brace", "```json\n" + good[1:] + "\n```")
    add("fence:missing-close-brace", "```json\n" + good[:-1] + "\n```")
    add("fence:text-after-brace-before-fence", "```json\n" + good + " trailing\n```")
    add("fence:closing-immediately-after-brace", "```json\n" + good + "```")
    add("fence:two-blocks-last-wins", fence(dumps(ok_result("A"))) + "\n\n" + fence(dumps(ok_result("B"))), "B")
    add("fence:two-blocks-last-wins-mismatch", fence(dumps(ok_result("A"))) + "\n\n" + fence(dumps(ok_result("B"))), "A")
    add("fence:three-blocks-middle-ignored", fence(dumps(ok_result("A"))) + "\n" + fence(dumps(ok_result("B"))) + "\n" + fence(dumps(ok_result("C"))), "C")
    add("fence:last-invalid-falls-back-to-earlier", fence(good) + "\ntext\n" + fence('{"task_id": "T1", "state":}'))
    add("fence:last-valid-earlier-invalid", fence('{"nope":') + "\ntext\n" + fence(good))
    add("fence:all-blocks-invalid", fence("{'a': 1}") + "\n" + fence("{b}"))
    add("fence:last-block-unterminated-earlier-kept", fence(dumps(ok_result("A"))) + "\ntext\n```json\n" + dumps(ok_result("B")), "A")
    add("fence:earlier-unterminated-bridges-into-next",
        '```json\n{"x": 1\ntext\n```json\n' + good + "\n```")
    add("fence:valid-block-beats-bare-object", '```json\n{"x": 1}\n```\n' + good, "T1")
    add("fence:brace-then-fence-inside-string",
        '```json\n{"task_id": "T1", "state": "FAILED", "summary_md": "a}\n``` b"}\n```')
    add("fence:triple-backticks-inside-string-valid",
        '```json\n' + dumps(ok_result(summary_md="use ``` here")) + "\n```")
    add("fence:json-fence-inside-string",
        '```json\n' + dumps(ok_result(summary_md="```json\n{}\n```")) + "\n```")
    add("fence:nested-objects", fence(dumps(ok_result(metrics={"a": {"b": {"c": 1}}}))))
    add("fence:object-with-inner-close-then-fence",
        '```json\n{"task_id": "T1", "state": "FAILED", "metrics": {"a": 1}\n```\n}\n```')
    add("fence:empty-object", fence("{}"))
    add("fence:empty-object-whitespace", fence("{ \n }"))
    add("fence:empty-block-no-brace", "```json\n```")
    add("fence:empty-text", "")
    add("fence:whitespace-only", " \n\t ")
    add("fence:prose-only", "I finished the task and everything is fine.")
    add("fence:prose-with-braces", "Result: {done} and {also done}")
    add("fence:two-objects-in-block", fence(good + " " + good))
    add("fence:object-then-garbage", fence(good + " }"))
    add("fence:fence-with-trailing-newlines-crlf-mixed", "```json\r\n" + good + "\n\r\n```\r\n\r\n")
    add("fence:backticks-right-after-json-word", "```json```" + good + "```")
    add("fence:json-word-twice", "```json ```json\n" + good + "\n```")
    # -- bare whole-text fallback ----------------------------------------------------------------------------------------------------
    add("bare:object", good)
    add("bare:object-padded", " \n\t" + good + "\r\n ")
    add("bare:object-with-prose-after", good + " and then some text")
    add("bare:list", "[" + good + "]")
    add("bare:string", '"text"')
    add("bare:number", "42")
    add("bare:null", "null")
    add("bare:true", "true")
    add("bare:nan", "NaN")
    add("bare:bom", "\ufeff" + good)
    add("bare:nbsp-padding", "\u00a0" + good)
    add("bare:ideographic-space-padding", good + "\u3000")
    add("bare:unterminated-object", good[:-1])
    add("bare:block-wins-over-bare", good + "\n" + fence(dumps(ok_result("B"))), "B")
    # -- Python `\s` versus JSON whitespace between fence and brace ---------------------------------------------------------------------
    for label, ch in [("tab", "\t"), ("vt", "\x0b"), ("ff", "\x0c"), ("fs", "\x1c"), ("gs", "\x1d"), ("rs", "\x1e"), ("us", "\x1f"),
                      ("nel", "\x85"), ("nbsp", "\xa0"), ("ogham", "\u1680"), ("en-quad", "\u2000"), ("em-space", "\u2003"),
                      ("hair", "\u200a"), ("line-sep", "\u2028"), ("para-sep", "\u2029"), ("nnbsp", "\u202f"), ("mmsp", "\u205f"),
                      ("ideographic", "\u3000"), ("zwsp", "\u200b"), ("bom", "\ufeff"), ("mongolian-vs", "\u180e"), ("nul", "\x00")]:
        add(f"space:{label}-before-brace", "```json" + ch + good + "\n```")
        add(f"space:{label}-before-close", "```json\n" + good + ch + "```")
    # -- JSON near misses inside a block ---------------------------------------------------------------------------------------------
    for label, body in [
        ("trailing-comma-object", '{"task_id": "T1", "state": "FAILED",}'),
        ("trailing-comma-array", '{"task_id": "T1", "state": "FAILED", "outputs": [],  "needs": [1,]}'),
        ("line-comment", '{"task_id": "T1", // c\n "state": "FAILED"}'),
        ("block-comment", '{"task_id": "T1", /* c */ "state": "FAILED"}'),
        ("single-quotes", "{'task_id': 'T1', 'state': 'FAILED'}"),
        ("unquoted-key", '{task_id: "T1", "state": "FAILED"}'),
        ("missing-colon", '{"task_id" "T1", "state": "FAILED"}'),
        ("missing-comma", '{"task_id": "T1" "state": "FAILED"}'),
        ("double-comma", '{"task_id": "T1",, "state": "FAILED"}'),
        ("leading-zero", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": 01}}'),
        ("plus-sign", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": +1}}'),
        ("leading-dot", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": .5}}'),
        ("trailing-dot", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": 1.}}'),
        ("hex", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": 0x10}}'),
        ("bare-exponent", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": 1e}}'),
        ("minus-only", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": -}}'),
        ("invalid-escape", '{"task_id": "T1", "state": "FAILED", "summary_md": "a\\xb"}'),
        ("short-unicode-escape", '{"task_id": "T1", "state": "FAILED", "summary_md": "a\\u12"}'),
        ("non-hex-unicode-escape", '{"task_id": "T1", "state": "FAILED", "summary_md": "a\\u12zz"}'),
        ("raw-newline-in-string", '{"task_id": "T1", "state": "FAILED", "summary_md": "a\nb"}'),
        ("raw-tab-in-string", '{"task_id": "T1", "state": "FAILED", "summary_md": "a\tb"}'),
        ("raw-del-in-string", '{"task_id": "T1", "state": "FAILED", "summary_md": "a\x7fb"}'),
        ("unterminated-string", '{"task_id": "T1", "state": "FAILED", "summary_md": "abc}'),
        ("non-string-key", '{1: "T1", "state": "FAILED"}'),
        ("nan", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": NaN}}'),
        ("infinity", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": Infinity}}'),
        ("minus-infinity", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": -Infinity}}'),
        ("lowercase-nan", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": nan}}'),
        ("capital-true", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": True}}'),
        ("undefined", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": undefined}}'),
        ("truex", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": truex}}'),
        ("nan-then-syntax-error", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": NaN,}}'),
        ("bigint-then-syntax-error", '{"task_id": "T1", "state": "FAILED", "metrics": {"n": 99999999999999999999,}}'),
        ("surrogate-then-syntax-error", '{"task_id": "T1", "state": "FAILED", "summary_md": "\\ud800",}'),
    ]:
        add(f"near:{label}", fence(body))
    add("near:nbsp-between-tokens", fence('{"task_id":\u00a0"T1", "state": "FAILED"}'))
    add("near:formfeed-between-tokens", fence('{"task_id": "T1",\x0c"state": "FAILED"}'))
    add("near:nan-in-earlier-block-ignored", fence('{"n": NaN}') + "\n" + fence(good))
    add("near:nan-in-later-block", fence(good) + "\n" + fence('{"task_id": "T1", "state": "FAILED", "metrics": {"n": NaN}}'))
    add("near:bigint-in-earlier-block-ignored", fence('{"n": 99999999999999999999}') + "\n" + fence(good))
    add("near:invalid-last-nan-first", fence('{"task_id": "T1", "state": "FAILED", "metrics": {"n": NaN}}') + "\n" + fence('{"x":'))
    # -- duplicate keys --------------------------------------------------------------------------------------------------------------
    for label, body in [
        ("last-wins", '{"task_id": "T1", "state": "FAILED", "state": "BLOCKED"}'),
        ("first-invalid-last-valid", '{"task_id": "T1", "state": "NOPE", "state": "BLOCKED"}'),
        ("first-valid-last-invalid", '{"task_id": "T1", "state": "BLOCKED", "state": "NOPE"}'),
        ("task-id-twice", '{"task_id": "A", "state": "FAILED", "task_id": "T1"}'),
        ("position-of-first", '{"b": 1, "a": 2, "b": 3, "task_id": "T1", "state": "FAILED"}'),
        ("nested", '{"task_id": "T1", "state": "FAILED", "metrics": {"k": 1, "k": {"z": 1, "z": 2}}}'),
        ("many", '{"task_id": "T1", "state": "FAILED", ' + ", ".join('"d": %d' % i for i in range(20)) + "}"),
        ("type-flip", '{"task_id": "T1", "task_id": 5, "state": "FAILED"}'),
        ("empty-key-twice", '{"": 1, "": 2, "task_id": "T1", "state": "FAILED"}'),
        ("escaped-key-equal", '{"task\\u005fid": "T1", "task_id": "T2", "state": "FAILED"}'),
    ]:
        add(f"dup:{label}", fence(body))
    # -- unicode and escapes ---------------------------------------------------------------------------------------------------------
    uni = "é日本語🙂\u2028\u007f\u0000 end"
    add("unicode:raw-and-escaped-summary", fence(json.dumps(ok_result(summary_md=uni), ensure_ascii=False)))
    add("unicode:ensure-ascii-summary", fence(json.dumps(ok_result(summary_md=uni), ensure_ascii=True)))
    add("unicode:surrogate-pair-escape", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\ud83d\\ude00"}'))
    add("unicode:lone-high-surrogate", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\ud800"}'))
    add("unicode:lone-low-surrogate", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\udc00"}'))
    add("unicode:high-then-ascii-escape", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\ud800\\u0041"}'))
    add("unicode:high-then-high", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\ud800\\ud800"}'))
    add("unicode:low-then-high", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\udc00\\ud800"}'))
    add("unicode:high-at-end-of-string", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "x\\ud83d"}'))
    add("unicode:lone-surrogate-in-key", fence('{"task_id": "T1", "state": "FAILED", "\\ud800": 1}'))
    add("unicode:lone-surrogate-in-earlier-block", fence('{"summary_md": "\\ud800"}') + "\n" + fence(good))
    add("unicode:surrogate-escape-uppercase-pair", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\uD83D\\uDE00"}'))
    add("unicode:escaped-slash-and-controls", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "\\/\\b\\f\\n\\r\\t\\"\\\\\\u0001"}'))
    add("unicode:nul-escape", fence('{"task_id": "T1", "state": "FAILED", "summary_md": "a\\u0000b"}'))
    add("unicode:non-ascii-key", fence(json.dumps({"task_id": "T1", "state": "FAILED", "ключ": 1, "键": {"é": [1]}}, ensure_ascii=False)))
    add("unicode:task-id-non-ascii-match", fence(json.dumps(ok_result("tâche-1"), ensure_ascii=False)), "tâche-1")
    add("unicode:task-id-non-ascii-mismatch", fence(json.dumps(ok_result("tâche-1"), ensure_ascii=False)), "tache-1")
    add("unicode:task-id-mismatch-assigned-non-ascii", fence(dumps(ok_result("tache-1"))), "tâche-1")
    add("unicode:task-id-astral-match", fence(dumps(ok_result("t-🙂"))), "t-🙂")
    add("unicode:task-id-escaped-vs-raw-match", fence('{"task_id": "t-\\u00e9", "state": "FAILED"}'), "t-é")
    add("unicode:state-non-ascii", fence(dumps(ok_result(state="ÉCHEC"))))
    add("unicode:state-ascii-lookalike-case", fence(dumps(ok_result(state="failed"))))
    # -- numbers ---------------------------------------------------------------------------------------------------------------------
    for label, literal in [
        ("zero", "0"), ("minus-zero", "-0"), ("minus-zero-float", "-0.0"), ("i64-max", str(I64_MAX)), ("i64-min", str(I64_MIN)),
        ("i64-max-plus-one", str(I64_MAX + 1)), ("i64-min-minus-one", str(I64_MIN - 1)), ("twenty-digits", "1" * 20),
        ("thirty-digits-negative", "-" + "9" * 30), ("float-twenty-digits", "12345678901234567890.0"),
        ("exp-upper", "1E2"), ("exp-signed", "1.0e+2"), ("exp-negative", "100e-2"), ("exp-big", "1e22"), ("exp-1e16", "1e16"),
        ("exp-1e15", "1e15"), ("exp-overflow", "1e999"), ("exp-overflow-negative", "-1e999"), ("exp-underflow", "1e-999"),
        ("min-subnormal", "5e-324"), ("max-float", "1.7976931348623157e308"), ("small", "0.00001"), ("tiny", "1.5e-7"),
        ("fraction", "0.1"), ("long-fraction", "3.141592653589793238462643383279"), ("integer-valued-float", "2.0"),
        ("minus-exp-zero", "-0e0"), ("four-thousand-digits", "7" * 4000), ("forty-three-hundred-digits", "7" * 4300),
        ("over-limit-digits", "7" * 4301), ("five-thousand-digits", "7" * 5000),
    ]:
        add(f"number:{label}", fence('{"task_id": "T1", "state": "FAILED", "metrics": {"n": %s, "list": [%s]}}' % (literal, literal)))
    add("number:bigint-as-state", fence('{"task_id": "T1", "state": %s}' % ("9" * 30)))
    add("number:float-as-state", fence('{"task_id": "T1", "state": 1.5}'))
    add("number:bigint-in-key-position-syntax", fence('{"task_id": "T1", "state": "FAILED", ' + "9" * 30 + ": 1}"))
    # -- depth and size ----------------------------------------------------------------------------------------------------------------
    for depth in (50, 99, 100, 101, 120, 400):
        nested = "[" * (depth - 1) + "]" * (depth - 1)  # the root object itself is depth 1 and holds `metrics`: depth - 1 more containers
        add(f"depth:metrics-list-{depth}", fence('{"task_id": "T1", "state": "FAILED", "metrics": %s}' % ("{" + '"a": ' + nested + "}" if depth > 1 else "{}")))
    add("depth:state-deep-list", fence('{"task_id": "T1", "state": ' + "[" * 50 + "]" * 50 + "}"))
    add("depth:object-chain-100", fence('{"task_id": "T1", "state": "FAILED", "metrics": ' + '{"a": ' * 98 + "1" + "}" * 98 + "}"))
    add("depth:object-chain-101", fence('{"task_id": "T1", "state": "FAILED", "metrics": ' + '{"a": ' * 99 + "1" + "}" * 99 + "}"))
    add("depth:unclosed-deep-no-closing-brace", fence('{"task_id": "T1", "state": "FAILED", "metrics": ' + "[" * 300))
    add("depth:unclosed-deep-syntax-error-past-cap", fence('{"task_id": "T1", "state": "FAILED", "metrics": ' + "[" * 300 + "}"), refused=True)
    add("depth:deep-bare-list-text", "[" * 150 + "]" * 150, refused=True)
    add("depth:deep-then-valid-earlier", fence(good) + "\n" + fence('{"a": ' + "[" * 200 + "]" * 200 + "}"))
    add("size:huge-summary-30k", fence(dumps(ok_result(summary_md="x" * 30_000))))
    add("size:huge-summary-unicode", fence(json.dumps(ok_result(summary_md="é🙂" * 8_000), ensure_ascii=False)))
    add("size:many-keys", fence(dumps({**ok_result(), **{f"k{i}": i for i in range(2000)}})))
    add("size:many-blocks", "\n".join(fence(dumps(ok_result(f"T{i}"))) for i in range(200)), "T199")
    add("size:many-unterminated-starts", "```json\n{" * 500)
    add("size:many-closers-without-fence", '```json\n{"a":' + "} " * 2000)
    add("size:long-whitespace-run", '```json' + " " * 20_000 + good + " " * 20_000 + "```")
    return cases


def schema_cases() -> list[tuple[str, str, str, bool]]:
    """Valid results for every state and shape, then one targeted violation per schema keyword and property."""
    cases: list[tuple[str, str, str, bool]] = []

    def add(case_id: str, result: Value, task_id: str = "T1") -> None:
        cases.append((case_id, fence(json.dumps(result)), task_id, False))

    for state in SCHEMA_ENUM:
        add(f"schema:state-{state}", ok_result(state=state))
    for state in TaskState:
        if state.value not in SCHEMA_ENUM:
            add(f"schema:state-not-allowed-{state.value}", ok_result(state=state.value))
    for label, bad_state in [("lowercase", "failed"), ("padded", " FAILED"), ("trailing-newline", "FAILED\n"), ("empty", ""), ("null", None),
                         ("true", True), ("false", False), ("zero", 0), ("negative", -3), ("float", 1.5), ("list", ["FAILED"]), ("empty-list", []),
                         ("dict", {"a": 1}), ("quote", "it's"), ("double-quote", 'say "hi"'), ("both-quotes", "it's \"x\""),
                         ("backslash", "a\\b"), ("newline", "a\nb"), ("tab-cr", "a\tb\rc"), ("control", "a\x01\x1f\x7fb"),
                         ("non-ascii", "ÉCHEC"), ("emoji", "🙂"), ("long", "S" * 800), ("bigint", 10**25)]:
        add(f"schema:state-bad-{label}", {"task_id": "T1", "state": bad_state})
    for key in ("task_id", "state"):
        full = ok_result()
        del full[key]
        add(f"schema:missing-{key}", full)
    add("schema:missing-both", {})
    add("schema:missing-both-with-others", {"outputs": 5, "summary_md": 1})
    add("schema:extra-keys-everywhere", ok_result(extra=1, notes={"a": [1, 2]}, outputs=[{"kind": "k", "extra": True}],
                                                  verdicts={"g": {"verdict": "pass", "findings": [], "extra": 1}}, error={"code": "c", "x": 1}))
    wrong_types: dict[str, list[Any]] = {
        "task_id": [5, None, True, ["T1"], {"a": 1}, 1.5],
        "outputs": ["x", 5, None, {"a": 1}, True, 1.5],
        "metrics": ["x", 5, None, [1], True, 2.5],
        "summary_md": [5, None, ["a"], {"a": 1}, True, 2.5],
        "needs": [5, None, True, 1.5],
        "error": ["boom", 5, None, [1], True],
        "verdicts": ["x", 5, None, [1], True],
        "gate": [5, None, ["qa"], {"a": 1}, True, 2.5],
    }
    for key, bad_values in wrong_types.items():
        for index, bad in enumerate(bad_values):
            add(f"schema:wrong-type-{key}-{index}", {**ok_result(), key: bad})
    # every valid shape of each optional field
    add("schema:needs-string", ok_result(needs="human-approval"))
    add("schema:needs-array", ok_result(needs=["a", 1, None, {"x": 1}]))
    add("schema:needs-object", ok_result(needs={"a": [1]}))
    add("schema:needs-empty-string", ok_result(needs=""))
    add("schema:outputs-empty", ok_result(outputs=[]))
    add("schema:outputs-full", ok_result(outputs=[{"kind": "doc", "uri": "file:///a", "version": "1", "digest": "sha256:ab"}, {}]))
    add("schema:outputs-unicode", ok_result(outputs=[{"kind": "文書", "uri": "file:///é"}]))
    for index, item in enumerate(["x", 5, None, [1], True, 1.5]):
        add(f"schema:outputs-item-not-object-{index}", ok_result(outputs=[{"kind": "k"}, item]))
    for prop in ("kind", "uri", "version", "digest"):
        for index, bad in enumerate([5, None, ["a"], {"a": 1}, True, 0.5]):
            add(f"schema:outputs-item-{prop}-wrong-{index}", ok_result(outputs=[{"kind": "k"}, {prop: bad}]))
    add("schema:outputs-two-bad-items", ok_result(outputs=[5, {"kind": 1}, "x", {"uri": None}]))
    add("schema:metrics-empty", ok_result(metrics={}))
    add("schema:metrics-nested", ok_result(metrics={"a": {"b": [1, 2.5, None, True, "x"]}}))
    add("schema:error-empty", ok_result(error={}))
    add("schema:error-full", ok_result(error={"code": "E-X", "message": "m"}))
    for prop in ("code", "message"):
        for index, bad in enumerate([5, None, ["a"], {"a": 1}, True, 0.5]):
            add(f"schema:error-{prop}-wrong-{index}", ok_result(error={prop: bad}))
    add("schema:verdicts-empty", ok_result(verdicts={}))
    add("schema:verdicts-full", ok_result(state="IN_REVIEW", verdicts={"qa": {"verdict": "pass", "findings": [{"severity": "major"}, 1]},
                                                                        "rev": {"verdict": "request_changes", "findings": []}, "sec": {}}))
    for index, bad in enumerate(["pass", 5, None, ["x"], True, 0.5]):
        add(f"schema:verdicts-entry-not-object-{index}", ok_result(verdicts={"qa": {"verdict": "pass"}, "rev": bad}))
    for index, bad in enumerate([5, None, ["x"], {"a": 1}, True, 0.5]):
        add(f"schema:verdicts-verdict-wrong-{index}", ok_result(verdicts={"qa": {"verdict": bad}}))
    for index, bad in enumerate(["x", 5, None, {"a": 1}, True, 0.5]):
        add(f"schema:verdicts-findings-wrong-{index}", ok_result(verdicts={"qa": {"findings": bad}}))
    add("schema:verdicts-many-bad-truncated", ok_result(verdicts={f"gate-{i:03d}": i for i in range(60)}))
    add("schema:verdicts-non-ascii-keys-truncated", ok_result(verdicts={("é" * 40) + str(i): i for i in range(30)}))
    add("schema:verdicts-key-with-slash", ok_result(verdicts={"a/b": 5}))
    add("schema:verdicts-empty-key", ok_result(verdicts={"": 5}))
    add("schema:gate-string", ok_result(gate="qa"))
    add("schema:all-fields", ok_result(state="BLOCKED", outputs=[{"kind": "k"}], metrics={"n": 1}, summary_md="s", needs=["human-approval"],
                                       error={"code": "E", "message": "m"}, verdicts={"qa": {"verdict": "pass", "findings": []}}, gate="qa"))
    add("schema:all-fields-wrong", ok_result(state="NOPE", outputs=5, metrics=[], summary_md=1, needs=2, error=3, verdicts=4, gate=5))
    add("schema:error-order-follows-object-not-schema", {"state": "X", "task_id": 1, "gate": 5, "outputs": "x"})
    add("schema:required-before-properties", {"outputs": 5})
    for pad in range(0, 4):
        add(f"schema:truncation-boundary-{pad}", ok_result(state="FAILED", verdicts={("k" * (400 + pad * 7)): 1, "z": 2}))
    # task_id binding
    for label, rid, assigned in [("equal", "T1", "T1"), ("different", "T1", "T2"), ("case", "t1", "T1"), ("padded", "T1 ", "T1"),
                                 ("empty-result", "", "T1"), ("empty-assigned", "T1", ""), ("both-empty", "", ""),
                                 ("quote-result", "it's", "T1"), ("double-quote-result", 'a"b', "T1"), ("both-quotes-result", "it's \"x\"", "T1"),
                                 ("quote-assigned", "T1", "it's"), ("backslash", "a\\b", "a/b"), ("newline", "a\nb", "T1"),
                                 ("control", "a\x01b\x7f", "T1"), ("long", "L" * 600, "T1"), ("number-like", "1", "01")]:
        add(f"binding:{label}", ok_result(rid), assigned)
    add("binding:mismatch-and-schema-error", ok_result("T2", outputs=5), "T1")
    add("binding:mismatch-with-bad-state", ok_result("T2", state="NOPE"), "T1")
    add("binding:not-reached-when-state-bad-non-ascii", ok_result("T2", state="é"), "T1")
    return cases


# ---------------------------------------------------------------------------------------------------------------------------------------
# Seeded fuzz


TASK_IDS = ["T1", "task-7", "A01.plan", "gate.qa", "t_é", "x" * 40, "id with space", "it's"]
WORDS = ["done", "ok", "fix", "review", "é", "日本", "🙂", "note", "```", "}", "{", '"', "\\", "\n", "\t", "  "]
PROSE = ["", "All done.\n\n", "Here is my result:\n", "```text\nlog line\n```\n", "I changed {a} and {b}.\n", "\r\n", "Result below.\n\n", "````\n"]
TAIL = ["", "\n", "\n\nThanks!\n", "\r\n", "\nEND", "\n```text\nmore\n```\n", " }"]


def _rand_str(rng: random.Random, limit: int = 12) -> str:
    return "".join(rng.choice(WORDS) for _ in range(rng.randint(0, limit)))


def _rand_scalar(rng: random.Random) -> Value:
    return rng.choice([0, 1, -1, 7, 2**40, I64_MAX, I64_MIN, 0.5, -2.25, 1e22, 1e-7, 5e-324, 123456.789, True, False, None,
                       "", "x", _rand_str(rng)])


def _rand_value(rng: random.Random, depth: int = 0) -> Value:
    pick = rng.random()
    if depth >= 3 or pick < 0.5:
        return _rand_scalar(rng)
    if pick < 0.75:
        return [_rand_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return {_rand_str(rng, 3) or "k": _rand_value(rng, depth + 1) for _ in range(rng.randint(0, 3))}


def _rand_result(rng: random.Random, task_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"task_id": task_id, "state": rng.choice(SCHEMA_ENUM)}
    if rng.random() < 0.5:
        result["outputs"] = [{k: _rand_str(rng, 3) for k in rng.sample(["kind", "uri", "version", "digest", "extra"], rng.randint(0, 5))}
                             for _ in range(rng.randint(0, 3))]
    if rng.random() < 0.5:
        result["metrics"] = {_rand_str(rng, 2) or "m": _rand_scalar(rng) for _ in range(rng.randint(0, 4))}
    if rng.random() < 0.6:
        result["summary_md"] = _rand_str(rng, 40)
    if rng.random() < 0.4:
        result["needs"] = rng.choice([_rand_str(rng, 3), [_rand_value(rng) for _ in range(rng.randint(0, 3))], {"k": _rand_value(rng)}])
    if rng.random() < 0.3:
        result["error"] = {k: _rand_str(rng, 4) for k in rng.sample(["code", "message", "other"], rng.randint(0, 3))}
    if rng.random() < 0.4:
        result["verdicts"] = {f"g{i}": {k: (_rand_str(rng, 3) if k == "verdict" else [_rand_value(rng) for _ in range(rng.randint(0, 2))])
                                         for k in rng.sample(["verdict", "findings", "x"], rng.randint(0, 3))} for i in range(rng.randint(0, 3))}
    if rng.random() < 0.3:
        result["gate"] = rng.choice(["qa", "rev", "sec", ""])
    if rng.random() < 0.3:
        result["unknown_extension"] = _rand_value(rng)
    return result


def _serialise(rng: random.Random, value: Value) -> str:
    style = rng.randrange(5)
    if style == 0:
        return json.dumps(value)
    if style == 1:
        return json.dumps(value, indent=2)
    if style == 2:
        return json.dumps(value, ensure_ascii=False)
    if style == 3:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(value, indent="\t", ensure_ascii=False).replace("\n", "\r\n")


def _schema_mutation(rng: random.Random, result: dict[str, Any]) -> dict[str, Any]:
    mutated = dict(result)
    choice = rng.randrange(9)
    if choice == 0:
        mutated.pop(rng.choice(["task_id", "state"]), None)
    elif choice == 1:
        mutated["state"] = rng.choice([s.value for s in TaskState] + ["", "failed", None, 3, 1.5, ["FAILED"], "ÉCHEC", "it's"])
    elif choice == 2:
        mutated[rng.choice(["task_id", "summary_md", "gate"])] = rng.choice([1, None, [1], {"a": 1}, True, 2.5])
    elif choice == 3:
        mutated[rng.choice(["outputs", "metrics", "error", "verdicts"])] = rng.choice(["x", 3, None, [1], True, 0.5, {}])
    elif choice == 4:
        mutated["outputs"] = [rng.choice([5, "x", None, {"kind": 1}, {"uri": []}, {}]) for _ in range(rng.randint(1, 3))]
    elif choice == 5:
        mutated["verdicts"] = {f"g{i}": rng.choice([1, "pass", {"verdict": 5}, {"findings": {}}, {"verdict": "pass", "findings": []}])
                               for i in range(rng.randint(1, 4))}
    elif choice == 6:
        mutated["error"] = {"code": rng.choice([1, None, "E"]), "message": rng.choice([2, [], "m"])}
    elif choice == 7:
        mutated["needs"] = rng.choice([1, None, True, 1.5, "ok", [], {}])
    else:
        mutated.clear()
    return mutated


def _text_mutation(rng: random.Random, body: str) -> str:
    choice = rng.randrange(14)
    cut = max(1, len(body) - 1)
    if choice == 0:
        return body[:cut].rstrip() + ",}"
    if choice == 1:
        return "{ // note\n" + body[1:]
    if choice == 2:
        return body[:cut] + ', "metrics": {"x": NaN}}'
    if choice == 3:
        return body[:cut] + ', "metrics": {"x": Infinity, "y": -Infinity}}'
    if choice == 4:
        return body.replace('"', "'")
    if choice == 5:
        return body.replace('"task_id"', "task_id", 1)
    if choice == 6:
        return body[:cut] + ', "state": "%s"}' % rng.choice(SCHEMA_ENUM + ["NOPE"])
    if choice == 7:
        return body[: rng.randrange(1, max(2, len(body)))]
    if choice == 8:
        return body + " x"
    if choice == 9:
        return body + body
    if choice == 10:
        return body[:cut] + ', "metrics": {"n": 123456789012345678901234567890}}'
    if choice == 11:
        return body[:cut] + ', "summary_md": "\\ud800 lone"}'
    if choice == 12:
        return body[:cut] + ', "summary_md": "a}\\n``` b"}'
    return body[:cut] + ', "metrics": {"f": 1e999}}'


def _opener(rng: random.Random) -> str:
    return rng.choice(["```json\n", "```json\r\n", "```json ", "```json", "```json\n\n", "```json\t", "```json\n  ", "````json\n"])


def _closer(rng: random.Random) -> str:
    return rng.choice(["\n```", "\r\n```", "```", " ```", "\n\n```", "\n  ```", "\n````"])


def fuzz_cases() -> list[tuple[str, str, str, bool]]:
    rng = random.Random(FUZZ_SEED)
    cases: list[tuple[str, str, str, bool]] = []
    for index in range(FUZZ_CASES):
        task_id = rng.choice(TASK_IDS)
        kind = rng.choices(["valid", "multi", "schema", "syntax", "bare", "noise", "mismatch"], weights=[30, 18, 18, 14, 6, 6, 8])[0]
        assigned = task_id
        parts: list[str] = []
        if kind == "valid":
            parts.append(_serialise(rng, _rand_result(rng, task_id)))
        elif kind == "schema":
            parts.append(_serialise(rng, _schema_mutation(rng, _rand_result(rng, task_id))))
        elif kind == "syntax":
            parts.append(_text_mutation(rng, _serialise(rng, _rand_result(rng, task_id))))
        elif kind == "mismatch":
            assigned = rng.choice(TASK_IDS)
            parts.append(_serialise(rng, _rand_result(rng, task_id)))
        elif kind == "multi":
            for _ in range(rng.randint(2, 4)):
                roll = rng.random()
                result = _rand_result(rng, rng.choice([task_id, assigned, rng.choice(TASK_IDS)]))
                if roll < 0.25:
                    parts.append(_text_mutation(rng, _serialise(rng, result)))
                elif roll < 0.5:
                    parts.append(_serialise(rng, _schema_mutation(rng, result)))
                else:
                    parts.append(_serialise(rng, result))
        if kind in ("bare",):
            body = _serialise(rng, _rand_result(rng, task_id))
            body = _text_mutation(rng, body) if rng.random() < 0.4 else body
            text = rng.choice(["", " ", "\n"]) + body + rng.choice(["", "\n", " tail"])
        elif kind == "noise":
            text = "".join(rng.choice(["```json", "```", "{", "}", '"', "\n", " ", "\r\n", "task_id", ":", ",", "1", "[", "]"]) for _ in range(rng.randint(1, 60)))
        else:
            text = rng.choice(PROSE)
            for part in parts:
                text += _opener(rng) + part + _closer(rng) + rng.choice(PROSE + TAIL)
            text += rng.choice(TAIL)
        cases.append((f"fuzz:{index:04d}-{kind}", text, assigned, False))
    return cases


# ---------------------------------------------------------------------------------------------------------------------------------------


def build_cases() -> list[Case]:
    """Run every hand-written, schema and fuzz case against the real Python path."""
    cases = [run_case(case_id, text, task_id, refused) for case_id, text, task_id, refused in hand_cases() + schema_cases() + fuzz_cases()]
    seen: set[str] = set()
    for case in cases:
        assert case["id"] not in seen, f"duplicate case id {case['id']}"
        seen.add(case["id"])
    _check_coverage(cases)
    return cases


def _check_coverage(cases: list[Case]) -> None:
    """Fail the generator when a class of case silently stops occurring (a fuzz seed or oracle change that empties a class)."""
    tally: dict[str, int] = {}
    for case in cases:
        for key in (f"parse.{case['parse']['outcome']}", f"validate.{case['validate']['outcome']}", f"rust.parse.{case['rust']['parse']}",
                    f"rust.validate.{case['rust']['validate']}"):
            tally[key] = tally.get(key, 0) + 1
        if case["id"].startswith("fuzz:"):
            tally["fuzz"] = tally.get("fuzz", 0) + 1
            if case["validate"]["outcome"] == "ok":
                tally["fuzz.accepted"] = tally.get("fuzz.accepted", 0) + 1
            if case["parse"]["outcome"] == "none":
                tally["fuzz.none"] = tally.get("fuzz.none", 0) + 1
            if case["validate"]["outcome"] == "error" and case["parse"]["outcome"] == "value":
                tally["fuzz.invalid-dict"] = tally.get("fuzz.invalid-dict", 0) + 1
            if case["rust"]["parse"] == "not_started":
                tally["fuzz.not_started"] = tally.get("fuzz.not_started", 0) + 1
    floors = {"parse.value": 200, "parse.none": 60, "parse.raises": 2, "validate.ok": 150, "validate.error": 200, "rust.parse.not_started": 20,
              "rust.validate.not_started": 20, "fuzz.accepted": 60, "fuzz.none": 20, "fuzz.invalid-dict": 30, "fuzz.not_started": 5}
    for key, floor in floors.items():
        assert tally.get(key, 0) >= floor, f"coverage floor {key}: {tally.get(key, 0)} < {floor}"


def oracle() -> Case:
    """Facts about the Python oracle the Rust constants must agree with, so a drift of any of them changes the committed file."""
    return {
        "json_block_pattern": results_module.JSON_BLOCK.pattern,
        "json_block_flags": int(results_module.JSON_BLOCK.flags),
        "schema_name": results_module.SCHEMA_NAME,
        "schema_sha256": sha256_file(SCHEMA_PATH),
        "state_enum": SCHEMA_ENUM,
        "depth_cap": DEPTH_CAP,
        "message_cap": 500,
        "space_ranges": space_ranges(),
    }


def render() -> str:
    """The committed file text: oracle facts first, then one case per line (stable and reviewable in a diff)."""
    lines = ",\n".join("  " + json.dumps(case, sort_keys=True) for case in build_cases())
    head = json.dumps({"schema": SCHEMA, "seed": FUZZ_SEED, "oracle": oracle()}, sort_keys=True)
    return head[:-1] + ',\n "cases": [\n' + lines + "\n ]\n}\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = VECTOR_PATH.read_text(encoding="utf-8") if VECTOR_PATH.exists() else ""
        print("up to date" if current == text else "STALE: rerun without --check")
        return 0 if current == text else 1
    VECTOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTOR_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {VECTOR_PATH.relative_to(REPO)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
