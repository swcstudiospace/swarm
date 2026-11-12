#!/usr/bin/env python3
"""Generates `crates/gsd-route/tests/vectors/pydumps.json`: Python's JSON output for the Rust write routes (Phase 14.5, U3.1b).

The Rust crate `gsd-route` renders the JSON the write routes store and print (`pydumps.rs`, `to_python_json`, `SeamError`). The
oracle is Python itself, so every expected string below is produced by the real code at generation time:

* `dumps`:  `json.dumps(value)` (the storage form: `json.dumps(notes)`, `json.dumps(envelope)` in `swarm/taskstore.py`), `json.dumps(value,
  sort_keys=True)` (the hashed form: `plan_sha` in `scripts/orch_plan.py`), and the reply form, captured from the real
  `swarm.script_base.AgentScript._print` (`json.dumps(obj, indent=2, default=str)` plus a newline).
* `parse`:  JSON text, the class of refusal the Rust reader owes for it (`bigint`, `surrogate`, `nonfinite`, `invalid`, or none) and,
  when none, the two dump forms of `json.loads(text)`. The class is computed from Python's own reader and a hand-labelled case that
  disagrees with it stops the generation.
* `errors`: `SwarmError(code, message, **details).to_dict()` inside the error reply `AgentScript.main` prints, again through `_print`.

Values cross into Rust as tagged trees so the int/float split, non-finite floats and key order survive: `["null"]`, `["bool", b]`,
`["int", "<decimal>"]`, `["float", "<16 hex digits of the big-endian IEEE 754 bits>"]`, `["str", s]`, `["arr", [..]]` and
`["obj", [[key, value], ..]]`. The random cases use a SplitMix64 generator written here (no `random` module), so the file is
byte-identical on every Python 3 version. Nothing in it is secret: it holds no key and no signature.

usage: python3 tests/parity/gen_pydumps_vectors.py [--out PATH]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import struct
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from swarm.errors import ErrorCode, SwarmError  # noqa: E402
from swarm.script_base import AgentScript  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "pydumps.json"
SEED = 0x14_5_01_B
I64_MIN, I64_MAX = -(2**63), 2**63 - 1
MASK64 = (1 << 64) - 1

Tagged = list[Any]


class SplitMix64:
    """The reference SplitMix64 generator: the same sequence on every platform and Python version."""

    def __init__(self, seed: int) -> None:
        self.state = seed & MASK64

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & MASK64
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        return z ^ (z >> 31)

    def below(self, bound: int) -> int:
        return self.next() % bound

    def pick(self, items: list[Any] | tuple[Any, ...] | str) -> Any:
        return items[self.below(len(items))]


def tag(value: Any) -> Tagged:
    """The tagged-tree form of a JSON-able Python value (see the module docstring)."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        if not I64_MIN <= value <= I64_MAX:
            raise ValueError(f"int {value} does not fit i64; it belongs in the parse section as a refusal")
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", struct.pack(">d", value).hex()]
    if isinstance(value, str):
        value.encode("utf-8")  # a lone surrogate cannot be a Rust String: raises
        return ["str", value]
    if isinstance(value, list):
        return ["arr", [tag(item) for item in value]]
    if isinstance(value, dict):
        return ["obj", [[key, tag(item)] for key, item in value.items()]]
    raise TypeError(f"untaggable {type(value).__name__}")


def printed(obj: Any) -> str:
    """What the real `AgentScript._print(obj, as_json=True)` writes to stdout."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        AgentScript._print(obj, True)
    return buffer.getvalue()


def float_from_bits(bits: int) -> float:
    return struct.unpack(">d", bits.to_bytes(8, "big"))[0]


def dumps_case(name: str, value: Any) -> dict[str, Any]:
    return {
        "name": name,
        "input": tag(value),
        "dumps": json.dumps(value),
        "sorted": json.dumps(value, sort_keys=True),
        "reply": printed(value),
    }


# ---- hand-written cases -------------------------------------------------------------------------------------------------------

CONTROL_CHARS = "".join(chr(code) for code in range(0x20))
FLOAT_EDGES: list[tuple[str, float]] = [
    ("zero", 0.0),
    ("negative-zero", -0.0),
    ("one", 1.0),
    ("minus-one", -1.0),
    ("tenth", 0.1),
    ("third-sum", 0.1 + 0.2),
    ("half", 0.5),
    ("timestamp", 1781234567.5),
    ("timestamp-micro", 1781234567.123456),
    ("e15", 1e15),
    ("just-below-e16", math.nextafter(1e16, 0.0)),
    ("e16", 1e16),
    ("just-above-e16", math.nextafter(1e16, math.inf)),
    ("e16-plus-two", 1e16 + 2.0),
    ("e17", 1e17),
    ("e21", 1e21),
    ("e22", 1e22),
    ("big-integral", 123456789012345680.0),
    ("max", sys.float_info.max),
    ("min-normal", sys.float_info.min),
    ("min-subnormal", 5e-324),
    ("e-4", 1e-4),
    ("just-below-e-4", math.nextafter(1e-4, 0.0)),
    ("just-above-e-4", math.nextafter(1e-4, 1.0)),
    ("e-5", 1e-5),
    ("one-and-a-half-e-5", 1.5e-5),
    ("e-7", 2.5e-7),
    ("e-100", 1.23e-100),
    ("e300", 1.5e300),
    ("negative-e16", -1e16),
    ("negative-e-5", -1e-5),
    ("two-thirds", 2 / 3),
    ("halfway-tie-negative", -749382848194079.25),
    ("halfway-tie-positive", 749382848194079.25),
    ("halfway-tie-even-side", 749382848194079.75),
    ("exact-binary-fraction", 1.125),
    ("pi", math.pi),
    ("four-thirty-five", 4.35),
    ("nan", math.nan),
    ("nan-with-payload", float_from_bits(0x7FF8000000000001)),
    ("negative-nan", float_from_bits(0xFFF8000000000000)),
    ("infinity", math.inf),
    ("negative-infinity", -math.inf),
]
INT_EDGES = [0, 1, -1, 9, 10, 255, 256, 65535, 2**31 - 1, 2**31, -(2**31), 2**32, 2**53 - 1, 2**53, 2**53 + 1, I64_MAX - 1, I64_MAX, I64_MIN + 1, I64_MIN]
STRING_EDGES: list[tuple[str, str]] = [
    ("empty", ""),
    ("space", " "),
    ("ascii-printable", "".join(chr(code) for code in range(0x20, 0x7F))),
    ("quote-and-backslash", 'say "hi" \\ done'),
    ("slash-is-not-escaped", "a/b</script>"),
    ("named-escapes", "\n\r\t\b\f"),
    ("all-control-characters", CONTROL_CHARS),
    ("nul", "a\x00b"),
    ("delete", "\x7f"),
    ("first-non-ascii", "\x80"),
    ("latin-1", "\xe9\xff\xa0"),
    ("line-separators", "\u2028\u2029"),
    ("bom-and-replacement", "\ufeff\ufffd"),
    ("bmp-edges", "\u07ff\u0800\ud7ff\ue000\uffff"),
    ("cjk", "\u65e5\u672c\u8a9e"),
    ("astral-first", "\U00010000"),
    ("astral-emoji", "\U0001f600"),
    ("astral-last", "\U0010ffff"),
    ("astral-between-ascii", "a\U0001f4a9b"),
    ("combining", "e\u0301"),
    ("escape-look-alike", "\\u00e9 \\n \\\\"),
    ("long", "x" * 300 + "\u00e9" + "y" * 300),
]
KEY_SETS: list[tuple[str, list[str]]] = [
    ("insertion-order", ["z", "a", "m", "B", "b", "A"]),
    ("numeric-looking", ["10", "9", "1", "100", "02"]),
    ("empty-key", ["b", "", "a"]),
    ("prefix-keys", ["ab", "a", "abc", ""]),
    ("code-point-order", ["\U00010000", "\ue000", "\uffff", "\ud7ff", "z", "\x7f", "\x80"]),
    ("escaped-keys", ['q"uote', "back\\slash", "new\nline", "\u00e9", "\U0001f600"]),
]


def hand_values() -> list[tuple[str, Any]]:
    cases: list[tuple[str, Any]] = [
        ("null", None),
        ("true", True),
        ("false", False),
        ("empty-array", []),
        ("empty-object", {}),
        ("empty-in-array", [[], {}, [[]], [{}]]),
        ("empty-in-object", {"a": [], "b": {}, "c": {"d": []}}),
        ("bool-is-not-int", [True, 1, False, 0]),
        ("int-and-float-differ", [1, 1.0, -0.0, 0, 0.0]),
        ("null-members", {"a": None, "b": [None, None]}),
        ("array-of-objects", [{"a": 1}, {"b": 2, "a": 3}, {}]),
        ("nested-20", _nest(20)),
        ("nested-mixed", {"a": [1, {"b": [2, {"c": [3, {"d": []}]}]}], "e": {"f": {"g": {"h": None}}}}),
        (
            "task-notes",
            {
                "gates": ["review", "quality"],
                "verdicts_since": 1781234567.5,
                "ingest_keys": ["k1", "k2"],
                "feedback": [{"from": "A09", "text": "m\u00e9ssage \U0001f600"}],
            },
        ),
        (
            "envelope-like",
            {
                "v": 1,
                "source": "A09",
                "payload": {"task_id": "T-1", "gate": "review", "verdict": "pass", "findings": [], "expires_s": 3600, "dry_run": False},
                "issued_at": "2026-10-28T10:00:00Z",
                "sig": "hmac-sha256:" + "ab" * 32,
            },
        ),
        ("budget-default", {"max_tokens": 200000, "max_wall_s": 1800, "max_cost_usd": 5}),
        ("budget-float", {"max_tokens": 200000, "max_wall_s": 1800.0, "max_cost_usd": 0.05}),
    ]
    for name, number in FLOAT_EDGES:
        cases.append((f"float-{name}", number))
        cases.append((f"float-{name}-in-object", {"k": number, "after": 1}))
    cases.append(("floats-all-edges", [number for _, number in FLOAT_EDGES]))
    for number in INT_EDGES:
        cases.append((f"int-{number}", number))
    cases.append(("ints-all-edges", list(INT_EDGES)))
    for name, text in STRING_EDGES:
        cases.append((f"string-{name}", text))
        cases.append((f"string-{name}-as-key", {text: text}))
    cases.append(("strings-all-edges", [text for _, text in STRING_EDGES]))
    for name, keys in KEY_SETS:
        cases.append((f"keys-{name}", {key: index for index, key in enumerate(keys)}))
        cases.append((f"keys-{name}-nested", {"outer": {key: [index] for index, key in enumerate(keys)}, "a": 0}))
    return cases


def _nest(depth: int) -> Any:
    value: Any = []
    for level in range(depth):
        value = {"k": value, "n": level} if level % 2 else [value, level]
    return value


# ---- seeded random cases ------------------------------------------------------------------------------------------------------

ALPHABET = (
    [chr(code) for code in range(0x20, 0x7F)] * 3
    + [chr(code) for code in range(0x20)]
    + ["\x7f", "\x80", "\xe9", "\xff", "\u0100", "\u07ff", "\u0800", "\u2028", "\ud7ff", "\ue000", "\ufffd", "\uffff"]
    + ["\U00010000", "\U0001f600", "\U0001f4a9", "\U0010ffff"]
)


def random_text(rng: SplitMix64, limit: int) -> str:
    return "".join(rng.pick(ALPHABET) for _ in range(rng.below(limit + 1)))


def random_float(rng: SplitMix64) -> float:
    kind = rng.below(6)
    if kind == 0:
        return float_from_bits(rng.next())
    if kind == 1:
        return float(f"{rng.below(10**17)}e{rng.below(60) - 30}") * (1 if rng.below(2) else -1)
    if kind == 2:
        return float(rng.below(10**18) * (1 if rng.below(2) else -1)) * (10.0 ** rng.below(8))
    if kind == 3:
        return round(rng.below(10**9) / 100.0, 2)
    if kind == 4:
        return 1.7e9 + rng.below(10**9) / 1e6
    return rng.below(10**6) * 10.0 ** (rng.below(41) - 20)


def random_int(rng: SplitMix64) -> int:
    magnitude = rng.next() >> rng.below(64)
    value = magnitude if rng.below(2) else -magnitude
    return max(I64_MIN, min(I64_MAX, value))


def random_value(rng: SplitMix64, depth: int) -> Any:
    kind = rng.below(7 if depth < 4 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return bool(rng.below(2))
    if kind == 2:
        return random_int(rng)
    if kind == 3:
        return random_float(rng)
    if kind == 4:
        return random_text(rng, 12)
    if kind == 5:
        return [random_value(rng, depth + 1) for _ in range(rng.below(5))]
    return {random_text(rng, 6): random_value(rng, depth + 1) for _ in range(rng.below(5))}


def random_dumps_cases(rng: SplitMix64) -> list[tuple[str, Any]]:
    cases: list[tuple[str, Any]] = []
    for index in range(80):
        cases.append((f"random-value-{index}", random_value(rng, 0)))
    for index in range(40):
        cases.append((f"random-floats-{index}", [random_float(rng) for _ in range(24)]))
    for index in range(10):
        cases.append((f"random-float-bits-{index}", [float_from_bits(rng.next()) for _ in range(24)]))
    for index in range(10):
        cases.append((f"random-ints-{index}", [random_int(rng) for _ in range(24)]))
    for index in range(20):
        cases.append((f"random-strings-{index}", [random_text(rng, 40) for _ in range(6)]))
    for exponent in range(-12, 0):
        for index in range(3):
            cases.append((f"binary-fractions-2e{exponent}-{index}", [math.ldexp(rng.below(2**52) + 2**52, exponent) for _ in range(16)]))
    return cases


def is_halfway(number: float) -> bool:
    """True when `number` lies exactly halfway between two decimals of the length of its `repr`: Rust rounds that tie up, Python to even."""
    exact = "".join(map(str, Decimal(number).as_tuple().digits)).rstrip("0")
    shortest = repr(abs(number)).lower().split("e")[0].replace(".", "").lstrip("0")
    if "." not in repr(abs(number)):
        shortest = shortest.rstrip("0")
    return len(exact) == len(shortest) + 1 and exact.endswith("5") and exact.startswith(shortest[:-1])


def floats_in(value: Any) -> list[float]:
    if isinstance(value, float):
        return [value]
    if isinstance(value, list):
        return [number for item in value for number in floats_in(item)]
    if isinstance(value, dict):
        return [number for item in value.values() for number in floats_in(item)]
    return []


# ---- parse cases --------------------------------------------------------------------------------------------------------------


def has_lone_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    if isinstance(value, list):
        return any(has_lone_surrogate(item) for item in value)
    if isinstance(value, dict):
        return any(has_lone_surrogate(key) or has_lone_surrogate(item) for key, item in value.items())
    return False


def classify(text: str) -> tuple[str | None, Any]:
    """The refusal class Python's own reader implies for `text`, and the value it reads (None for `invalid`)."""
    problems: set[str] = set()

    def parse_int(digits: str) -> int:
        number = int(digits)
        if not I64_MIN <= number <= I64_MAX:
            problems.add("bigint")
        return number

    def parse_constant(name: str) -> float:
        problems.add("nonfinite")
        return float(name.replace("Infinity", "inf").replace("NaN", "nan"))

    try:
        value = json.loads(text, parse_int=parse_int, parse_constant=parse_constant)
    except ValueError:
        return "invalid", None
    if has_lone_surrogate(value):
        problems.add("surrogate")
    if len(problems) > 1:
        raise ValueError(f"case {text!r} has several refusal classes {sorted(problems)}; split it")
    return (next(iter(problems)) if problems else None), value


# (name, text, class the case is labelled with; None for a case Rust must read)
PARSE_TEXTS: list[tuple[str, str, str | None]] = [
    ("compact-object", '{"a":1,"b":[true,false,null]}', None),
    ("indented-object", '{\n  "a": 1,\n\t"b": [ 1 , 2 ]\n}', None),
    ("surrounding-whitespace", ' \t\r\n[1]\n ', None),
    ("empty-containers", '{"a": [], "b": {}, "c": [[], {}]}', None),
    ("duplicate-key-last-wins-first-position", '{"a": 1, "b": 2, "a": 3}', None),
    ("duplicate-key-nested", '[{"k": 1, "k": {"x": 1, "x": 2}}]', None),
    ("minus-zero-int", "-0", None),
    ("minus-zero-float", "-0.0", None),
    ("minus-zero-exp", "[-0e0, 0e0, 0E0]", None),
    ("exponent-forms", "[1E2, 1e2, 1.0E+2, 0.1e1, 1e+2, 1e-2, 12e1]", None),
    ("float-overflows-to-infinity", "[1e400, -1e400]", None),
    ("float-underflows-to-zero", "[1e-400, -1e-400, 5e-324, 2e-324]", None),
    ("float-shortest-repr", "[0.30000000000000004, 0.1, 100000000000000000000.0, 1e16, 9999999999999998.0, 123456789.123456789]", None),
    ("int-i64-max", "9223372036854775807", None),
    ("int-i64-min", "-9223372036854775808", None),
    ("ints-around-zero", "[0, -1, 1, 10, -10]", None),
    ("float-integral-large", "12345678901234567890.0", None),
    ("float-with-many-digits", "1.2345678901234567890123", None),
    ("string-escapes", '"\\" \\\\ \\/ \\b \\f \\n \\r \\t \\u0041 \\u00e9 \\u00E9 \\u0000"', None),
    ("string-surrogate-pair", '"\\ud83d\\ude00 \\uD83D\\uDE00 \\ud800\\udc00 \\udbff\\udfff"', None),
    ("string-raw-unicode", '"\u00e9\u65e5\U0001f600\u2028"', None),
    ("string-pair-then-text", '"x\\ud83d\\ude00y"', None),
    ("key-escapes", '{"\\u00e9": 1, "\\ud83d\\ude00": 2, "a\\nb": 3}', None),
    ("digits-in-string", '["99999999999999999999", "-99999999999999999999", "NaN", "Infinity"]', None),
    ("digits-after-escaped-quote", '["a\\"99999999999999999999"]', None),
    ("deep-but-allowed", "[" * 100 + "]" * 100, None),
    ("bigint-positive", "9223372036854775808", "bigint"),
    ("bigint-negative", "-9223372036854775809", "bigint"),
    ("bigint-in-array", "[1, 2, 99999999999999999999]", "bigint"),
    ("bigint-in-object", '{"a": {"b": [123456789012345678901234567890]}}', "bigint"),
    ("bigint-after-string-with-quote", '["a\\"", 18446744073709551616]', "bigint"),
    ("bigint-u64-max", "18446744073709551615", "bigint"),
    ("nan", "NaN", "nonfinite"),
    ("infinity", "Infinity", "nonfinite"),
    ("negative-infinity", "-Infinity", "nonfinite"),
    ("nan-in-array", "[1, NaN, 2]", "nonfinite"),
    ("infinity-in-object", '{"x": Infinity}', "nonfinite"),
    ("lone-high-surrogate", '"\\ud800"', "surrogate"),
    ("lone-low-surrogate", '"\\udc00"', "surrogate"),
    ("high-then-ascii", '"\\ud83dx"', "surrogate"),
    ("high-then-bmp-escape", '"\\ud83d\\u0041"', "surrogate"),
    ("high-then-high", '"\\ud800\\ud800"', "surrogate"),
    ("low-then-high", '"\\ude00\\ud83d"', "surrogate"),
    ("high-at-end-of-string", '"abc\\ud83d"', "surrogate"),
    ("lone-surrogate-key", '{"\\ud800": 1}', "surrogate"),
    ("lone-surrogate-in-array", '["ok", ["\\udfff"]]', "surrogate"),
    ("empty-text", "", "invalid"),
    ("whitespace-only", " \n", "invalid"),
    ("unterminated-object", "{", "invalid"),
    ("unterminated-string", '"abc', "invalid"),
    ("trailing-comma-array", "[1,]", "invalid"),
    ("trailing-comma-object", '{"a": 1,}', "invalid"),
    ("single-quotes", "{'a': 1}", "invalid"),
    ("leading-zero", "01", "invalid"),
    ("bare-minus", "-", "invalid"),
    ("trailing-dot", "1.", "invalid"),
    ("leading-dot", ".5", "invalid"),
    ("leading-plus", "+1", "invalid"),
    ("missing-comma", "[1 2]", "invalid"),
    ("raw-tab-in-string", '"a\tb"', "invalid"),
    ("raw-newline-in-string", '"a\nb"', "invalid"),
    ("bad-escape", '"\\x41"', "invalid"),
    ("short-unicode-escape", '"\\u00e"', "invalid"),
    ("non-hex-unicode-escape", '"\\u00zz"', "invalid"),
    ("trailing-garbage", '{"a": 1} x', "invalid"),
    ("two-values", "1 2", "invalid"),
    ("truncated-true", "tru", "invalid"),
    ("capital-true", "True", "invalid"),
    ("python-none", "None", "invalid"),
    ("unquoted-key", "{a: 1}", "invalid"),
    ("byte-order-mark", "\ufeff[1]", "invalid"),
    ("missing-colon", '{"a" 1}', "invalid"),
    ("comment", "[1] // c", "invalid"),
]


def parse_case(name: str, text: str, label: str | None) -> dict[str, Any]:
    klass, value = classify(text)
    if klass != label:
        raise ValueError(f"parse case {name}: labelled {label!r} but Python's reader implies {klass!r}")
    case: dict[str, Any] = {"name": name, "text": text, "refuse": klass}
    if klass is None:
        case["dumps"] = json.dumps(value)
        case["sorted"] = json.dumps(value, sort_keys=True)
    return case


def is_finite_tree(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        return False
    return True


def random_parse_cases(rng: SplitMix64) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    indents: list[Any] = [None, 0, 1, 2, 4, "\t"]
    for index in range(60):
        value = random_value(rng, 0)
        while not is_finite_tree(value):  # a NaN or infinity prints as a literal: that class has its own hand cases
            value = random_value(rng, 0)
        separators = rng.pick([None, (",", ":"), (" ,", " : ")])
        text = json.dumps(
            value,
            indent=rng.pick(indents),
            separators=separators,
            ensure_ascii=bool(rng.below(2)),
            sort_keys=bool(rng.below(2)),
        )
        cases.append(parse_case(f"random-text-{index}", text, None))
    return cases


# ---- error replies ------------------------------------------------------------------------------------------------------------

ERROR_CASES: list[tuple[str, ErrorCode, str, dict[str, Any]]] = [
    ("no-details", ErrorCode.E_INPUT, "unknown task nope", {}),
    ("task-id", ErrorCode.E_CONTRACT, "concurrent transition", {"task_id": "T-1"}),
    ("not-started", ErrorCode.E_DEP, "bigint refused", {"write_state": "not_started"}),
    ("task-id-then-state", ErrorCode.E_DEP, "refused", {"task_id": "T-9", "write_state": "not_started"}),
    ("state-then-task-id", ErrorCode.E_DEP, "refused", {"write_state": "not_started", "task_id": "T-9"}),
    ("transition", ErrorCode.E_POLICY, "illegal transition", {"task_id": "T-2", "from": "DONE", "to": "PLANNED", "attempt": 3}),
    ("float-and-null", ErrorCode.E_TIMEOUT, "lease expired", {"waited_s": 30.5, "limit": 1e-05, "holder": None, "retry": False}),
    ("nested", ErrorCode.E_CAPACITY, "wip cap", {"task_id": "T-3", "caps": {"A05": 2, "B": [1, 2.5, {"c": []}]}, "empty": {}}),
    ("unicode", ErrorCode.E_INTERNAL, "caf\u00e9 \U0001f600", {"task_id": "T-\u00e9", "note": 'q"uote \\ \x00 \u2028'}),
    ("every-code-input", ErrorCode.E_INPUT, "x", {"task_id": "T-4"}),
    ("every-code-timeout", ErrorCode.E_TIMEOUT, "x", {"task_id": "T-4"}),
    ("every-code-dep", ErrorCode.E_DEP, "x", {"task_id": "T-4"}),
    ("every-code-capacity", ErrorCode.E_CAPACITY, "x", {"task_id": "T-4"}),
    ("every-code-contract", ErrorCode.E_CONTRACT, "x", {"task_id": "T-4"}),
    ("every-code-policy", ErrorCode.E_POLICY, "x", {"task_id": "T-4"}),
    ("every-code-internal", ErrorCode.E_INTERNAL, "x", {"task_id": "T-4"}),
    ("empty-message", ErrorCode.E_INPUT, "", {"k": ""}),
]


def error_case(name: str, code: ErrorCode, message: str, details: dict[str, Any]) -> dict[str, Any]:
    error = SwarmError(code, message, **details)
    reply = {"agent": "A01", "script": "orch_status", "status": "error", "error": error.to_dict()}
    return {"name": name, "code": code.value, "message": message, "details": tag(details), "reply": printed(reply)}


# ---- assembly -----------------------------------------------------------------------------------------------------------------


def build_vectors() -> dict[str, Any]:
    rng = SplitMix64(SEED)
    dumps = [dumps_case(name, value) for name, value in hand_values()]
    dumps += [dumps_case(name, value) for name, value in random_dumps_cases(rng)]
    parse = [parse_case(name, text, label) for name, text, label in PARSE_TEXTS] + random_parse_cases(rng)
    errors = [error_case(*case) for case in ERROR_CASES]
    ties = sum(is_halfway(number) for _, value in hand_values() for number in floats_in(value) if math.isfinite(number))
    ties += sum(is_halfway(number) for _, value in random_dumps_cases(SplitMix64(SEED)) for number in floats_in(value) if math.isfinite(number))
    if ties < 50:
        raise ValueError(f"only {ties} halfway floats: the vectors would not pin the round-half-even rule")
    names = [case["name"] for section in (dumps, parse, errors) for case in section]
    if len(names) != len(set(names)):
        raise ValueError("duplicate case names")
    return {
        "generator": "tests/parity/gen_pydumps_vectors.py",
        "note": "expected strings are Python's own output; see the generator docstring for the encoding",
        "dumps": dumps,
        "parse": parse,
        "errors": errors,
    }


def render(vectors: dict[str, Any]) -> str:
    """The file text: ASCII only, one case per line, keys sorted."""
    lines = ["{"]
    sections = [key for key in sorted(vectors) if isinstance(vectors[key], list)]
    entries = [f" {json.dumps(key)}: {json.dumps(vectors[key], sort_keys=True)}" for key in sorted(vectors) if key not in sections]
    for section in sections:
        cases = ",\n".join("  " + json.dumps(case, sort_keys=True) for case in vectors[section])
        entries.append(f' {json.dumps(section)}: [\n{cases}\n ]')
    lines.append(",\n".join(entries))
    lines.append("}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", type=Path, default=VECTOR_PATH)
    args = parser.parse_args()
    text = render(build_vectors())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="ascii")
    print(f"wrote {args.out} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
