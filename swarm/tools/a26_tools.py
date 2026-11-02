"""A26 Smart Contract Engineer tools: pure checks over supplied ABIs, layouts, sources and plans (EVM, simulation only).

Tools: sol_selector_check (pure-Python keccak-256 selectors, topics, ERC-165 id, collisions), sol_abi_diff (breaking
ABI changes), sol_storage_layout (upgrade-compatibility of two solc storage layouts), sol_erc_conformance (ERC-20, 721,
1155, 4626, 2612 and ERC-165 ids), sol_upgrade_safety (initializer, constructor, selfdestruct, delegatecall, gap, UUPS
authorization), sol_access_matrix (state-changing functions by guard), sol_event_coverage (state changes that emit
nothing), sol_natspec_coverage (NatSpec completeness), sol_gas_regression (per-function gas budget),
sol_deploy_plan_check (chain-id guard, constructor args, verification, simulation, owner, signer, RPC URL),
sol_pragma_policy (pinned pragma, minimum version, known compiler bugs), sol_test_gate (coverage, fuzz and invariant
floors), sol_eip712_typehash (EIP-712 type hash and domain separator).

All tools are pure: nothing reads a key, signs, or calls a network; a deployment plan is judged, never executed.
The Solidity source reader, guard/event analysis and pragma rules are shared with scripts/sol_contract_lint.py.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable
from decimal import Decimal
from types import ModuleType
from typing import Any

from swarm.scan_status import incomplete_findings
from swarm.tools.support import respond, script

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
MAX_FINDINGS = 200
MAX_BASE_WALK_STEPS = 10_000  # steps per _inherits call (one per base-contract visit): a cyclic base graph fails as INVALID_INPUT

# --------------------------------------------------------------------------------------------------------------------
# Keccak-256 (the pre-NIST padding Ethereum uses: 0x01 domain byte, not SHA3's 0x06). Verified in the tests against
# keccak256("") = c5d24601..., keccak256("abc") = 4e03657a... and transfer(address,uint256) = a9059cbb.
# --------------------------------------------------------------------------------------------------------------------

_MASK64 = (1 << 64) - 1


def _round_constants() -> list[int]:
    constants, r = [], 1
    for _ in range(24):
        value = 0
        for j in range(7):
            if r & 1:
                value ^= 1 << ((1 << j) - 1)
            r <<= 1
            if r & 0x100:
                r ^= 0x171
        constants.append(value)
    return constants


def _rotation_offsets() -> list[list[int]]:
    rot = [[0] * 5 for _ in range(5)]
    x, y = 1, 0
    for t in range(24):
        rot[x][y] = ((t + 1) * (t + 2) // 2) % 64
        x, y = y, (2 * x + 3 * y) % 5
    return rot


_RC = _round_constants()
_ROT = _rotation_offsets()


def _rol(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK64 if shift else value


def _keccak_f(lanes: list[int]) -> None:
    for rc in _RC:
        c = [lanes[x] ^ lanes[x + 5] ^ lanes[x + 10] ^ lanes[x + 15] ^ lanes[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(lanes[x + 5 * y] ^ d[x], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                lanes[x + 5 * y] = b[x + 5 * y] ^ (~b[(x + 1) % 5 + 5 * y] & b[(x + 2) % 5 + 5 * y])
        lanes[0] ^= rc


def _keccak256(data: bytes) -> bytes:
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate:
        padded.append(0)
    padded[-1] |= 0x80
    lanes = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            lanes[i] ^= int.from_bytes(block[i * 8:i * 8 + 8], "little")
        _keccak_f(lanes)
    return b"".join(lanes[i].to_bytes(8, "little") for i in range(4))


def _keccak_hex(text: str) -> str:
    return _keccak256(text.encode("utf-8")).hex()


# --------------------------------------------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------------------------------------------


# Secret-shaped substrings (cloud and VCS tokens, JWTs, 40+ hex digits such as a private key or address, user:password in a
# URL, long mixed alphanumeric runs, a quoted 12+ word phrase) are never echoed: a tool reports a location and a length.
_SECRET_SHAPES = re.compile(
    r"(?:AKIA|ASIA)[0-9A-Z]{12,}|\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_\w{16,}|\bxox[abprs]-[\w-]{8,}|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bAIza[\w-]{20,}|\beyJ[\w-]{8,}\.[\w-]{8,}|(?:0x)?[0-9a-fA-F]{40,}|://[^\s/?#]*@"
    r"|(?<![A-Za-z0-9+/=_-])(?=[A-Za-z0-9+/=_-]*\d)[A-Za-z0-9+/=_-]{32,}|(['\"])(?:[a-z]{3,8} ){11,}[a-z]{3,8}\1")


def _text(text: str) -> str:
    """User-visible message text: secret-shaped substrings redacted, any single token over 80 characters clipped, total capped,
    so a hostile or oversized identifier can neither leak a credential nor inflate the result."""
    text = _SECRET_SHAPES.sub(lambda m: f"<redacted {len(m.group(0))} chars>", text)
    text = re.sub(r"\S{81,}", lambda m: m.group(0)[:72] + "...", text)
    return text if len(text) <= 360 else text[:357] + "..."


def _invalid(field: str, message: str) -> dict[str, Any]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": _text(message)}


def _short(text: str, limit: int = 120) -> str:
    """An identifier-like label clipped to `limit` characters (source-derived names can be as long as the source)."""
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _finding(rule: str, severity: str, summary: str) -> dict[str, Any]:
    return {"id": rule, "severity": severity, "summary": _text(summary)}


def _incomplete_source(source: str, analysed: int) -> list[dict[str, Any]]:
    """A major scan/incomplete finding when the Solidity source does not parse (truncated, or a declaration outside every contract
    body): the handler then reads only part of it, so the verdict fails. Only labels are reported, never the source text."""
    problems = _lint().source_problems(source)
    return [_finding(item["id"], item["severity"], item["summary"]) for item in incomplete_findings(problems, analysed)] if problems else []


def _call_depth_bounded(work: Callable[[], dict[str, Any]], stopped: dict[str, Any]) -> Callable[[], dict[str, Any]]:
    """`work`, with the interpreter's recursion limit turned into a stop: a call walk past the limit returns `stopped` (its verdict
    fails) with a scan-incomplete major finding naming the limit, as a19's $ref walk stops, never as an exception."""

    def bounded() -> dict[str, Any]:
        try:
            return work()
        except RecursionError:
            reason = (f"call depth exceeds the call-depth limit (the interpreter's recursion limit, {sys.getrecursionlimit()}): "
                      "the call walk stopped and the source is not fully analysed")
            findings = [_finding(item["id"], item["severity"], item["summary"]) for item in incomplete_findings([reason], 0)]
            return {**stopped, "state": "SUCCESS", "findings": findings, "findings_total": len(findings)}

    return bounded


def _finish(findings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Findings sorted (worst first, then id, then text) and capped at MAX_FINDINGS; the second value is the uncapped count."""
    ordered = sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))
    return ordered[:MAX_FINDINGS], len(ordered)


def _worst(findings: list[dict[str, Any]]) -> int:
    return max((SEVERITY_RANK[f["severity"]] for f in findings), default=0)


def _lint() -> ModuleType:
    """scripts/sol_contract_lint.py: Solidity reader, guard/event analysis and pragma rules shared with the lint script."""
    return script("sol_contract_lint")


def _split_top(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts if text else []


_ELEMENTARY = re.compile(r"(?:address|bool|string|bytes|bytes(?:[1-9]|[12]\d|3[0-2])|u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256))\Z")
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*\Z")
_TYPE_ALIASES = {"uint": "uint256", "int": "int256", "byte": "bytes1"}


def _type_error(text: str) -> str:
    """"" when `text` is a canonical ABI type (elementary, tuple, arrays), else why not."""
    t = text
    while True:
        m = re.search(r"\[(\d*)\]\Z", t)
        if not m:
            break
        if m.group(1) and (m.group(1).startswith("0") or int(m.group(1)) > 1 << 24):
            return f"bad array length in {text!r}"
        t = t[:m.start()]
    if t.startswith("(") and t.endswith(")"):
        inner = t[1:-1]
        if not inner:
            return f"empty tuple {text!r}"
        for part in _split_top(inner):
            err = _type_error(part)
            if err:
                return err
        return ""
    if t in _TYPE_ALIASES:
        return f"{t!r} is not canonical, write {_TYPE_ALIASES[t]!r} (selectors hash canonical types)"
    if _ELEMENTARY.match(t):
        return ""
    return f"{t!r} is not a canonical ABI type (no spaces, no aliases, no user-defined names)"


def _sig_error(signature: str) -> str:
    """"" when `signature` is `name(type,type)` with canonical types, else the reason."""
    m = re.fullmatch(r"([A-Za-z_$][A-Za-z0-9_$]*)\((.*)\)", signature, re.S)
    if not m:
        return "expected name(type,type) with no spaces"
    inner = m.group(2)
    if inner == "":
        return ""
    if inner.startswith(",") or inner.endswith(",") or ",," in inner:
        return "empty parameter type"
    for part in _split_top(inner):
        err = _type_error(part)
        if err:
            return err
    return ""


def _sig_name(signature: str) -> str:
    return signature[:signature.index("(")]


def _types_error(text: str) -> str:
    """Comma-separated canonical types ('' = none)."""
    if text == "":
        return ""
    for part in _split_top(text):
        err = _type_error(part)
        if err:
            return err
    return ""


def _is_hex(text: str, nbytes: int | None = None) -> bool:
    if not re.fullmatch(r"0x[0-9a-fA-F]*", text):
        return False
    return nbytes is None or len(text) == 2 + 2 * nbytes


def _checksum_ok(address: str) -> bool:
    """EIP-55: all-lowercase and all-uppercase hex are unchecked and accepted; mixed case must carry the checksum."""
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return True
    digest = _keccak_hex(body.lower())
    return all(c.isupper() == (int(digest[i], 16) >= 8) for i, c in enumerate(body) if c.isalpha())


def _entries(field: str, items: list[dict[str, Any]]) -> dict[Any, Any]:
    """Validated ABI entries keyed by (kind, signature); an INVALID_INPUT dict on the first problem."""
    keyed: dict[tuple[str, str], dict[str, Any]] = {}
    for i, item in enumerate(items):
        kind, signature = item["kind"], item["signature"]
        err = _sig_error(signature)
        if err:
            return _invalid(field, f"{field}[{i}] {signature!r}: {err}")
        entry = {"kind": kind, "signature": signature, "mutability": item.get("mutability", "nonpayable"),
                 "returns": item.get("returns"), "indexed": item.get("indexed")}
        if entry["returns"] is not None and _types_error(entry["returns"]):
            return _invalid(field, f"{field}[{i}] returns: {_types_error(entry['returns'])}")
        if entry["indexed"] is not None:
            count = len(_split_top(signature[signature.index("(") + 1:-1]))
            if kind != "event" or len(entry["indexed"]) != count:
                return _invalid(field, f"{field}[{i}] indexed must list one flag per event parameter ({count}) and only for events")
        if (kind, signature) in keyed:
            return _invalid(field, f"{field}[{i}] duplicates {kind} {signature!r}")
        keyed[(kind, signature)] = entry
    return keyed


# --------------------------------------------------------------------------------------------------------------------
# 1. selectors
# --------------------------------------------------------------------------------------------------------------------


def selector_check(payload: dict[str, Any]) -> dict[str, Any]:
    """4-byte selectors (keccak256 of the canonical signature), event topic0, the ERC-165 interface id (XOR of the
    selectors) and clashes: two signatures with one selector, a proxy function that shadows or clashes with an
    implementation function, a declared selector constant that does not match its signature."""

    def work() -> dict[str, Any]:
        functions = payload["functions"]
        events = payload.get("events", [])
        others = payload.get("other_functions", [])
        expected = payload.get("expected_selectors", [])
        for field, items in (("functions", functions), ("events", events), ("other_functions", others)):
            for i, signature in enumerate(items):
                err = _sig_error(signature)
                if err:
                    return _invalid(field, f"{field}[{i}] {signature!r}: {err}")
        for i, item in enumerate(expected):
            err = _sig_error(item["signature"])
            if err:
                return _invalid("expected_selectors", f"expected_selectors[{i}] {item['signature']!r}: {err}")
            if not _is_hex(item["selector"], 4):
                return _invalid("expected_selectors", f"expected_selectors[{i}].selector must be 0x followed by 8 hex digits")
        findings: list[dict[str, Any]] = []
        unique = sorted(set(functions))
        for signature in sorted({s for s in functions if functions.count(s) > 1}):
            findings.append(_finding("SEL-DUPLICATE", "minor", f"{signature} is listed {functions.count(signature)} times"))
        selector_of = {s: _keccak256(s.encode())[:4].hex() for s in unique}
        by_selector: dict[str, list[str]] = {}
        for signature, sel in selector_of.items():
            by_selector.setdefault(sel, []).append(signature)
        for sel in sorted(by_selector):
            if len(by_selector[sel]) > 1:
                findings.append(_finding("SEL-COLLISION", "blocker",
                                         f"0x{sel} is the selector of {' and '.join(sorted(by_selector[sel]))}: the dispatcher cannot tell them apart"))
        for signature in sorted(set(others)):
            sel = _keccak256(signature.encode())[:4].hex()
            for clash in by_selector.get(sel, []):
                if clash == signature:
                    findings.append(_finding("SEL-PROXY-SHADOW", "major",
                                             f"{signature} exists on both the proxy and the implementation: the proxy answers and the implementation is unreachable"))
                else:
                    findings.append(_finding("SEL-PROXY-CLASH", "blocker",
                                             f"proxy function {signature} and implementation function {clash} share selector 0x{sel}"))
        for item in expected:
            actual = _keccak256(item["signature"].encode())[:4].hex()
            if actual != item["selector"][2:].lower():
                findings.append(_finding("SEL-CONSTANT-MISMATCH", "blocker",
                                         f"declared selector {item['selector'].lower()} for {item['signature']} is wrong, the real selector is 0x{actual}"))
        interface_id = 0
        for sel in selector_of.values():  # one term per unique signature: two signatures that share a selector both count
            interface_id ^= int(sel, 16)
        unique_events = sorted(set(events))
        listed, total = _finish(findings)
        return {
            "state": "SUCCESS",
            "function_count": len(unique),
            "event_count": len(unique_events),
            "selectors": [{"signature": s, "selector": "0x" + selector_of[s]} for s in unique],
            "topics": [{"signature": s, "topic0": "0x" + _keccak_hex(s)} for s in unique_events],
            "interface_id": f"0x{interface_id:08x}",
            "verdict": "conflict" if _worst(findings) >= SEVERITY_RANK["major"] else "clean",
            "findings": listed,
            "findings_total": total,
        }

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 2. ABI diff
# --------------------------------------------------------------------------------------------------------------------


def _mutability_change(old: str, new: str) -> str | None:
    """Severity of a stateMutability change, None when equal. Callers break when a read-only function starts writing
    (STATICCALL reverts) or a payable function stops accepting value; tightening or accepting value is compatible."""
    if old == new:
        return None
    if new in ("nonpayable", "payable") and old in ("pure", "view"):
        return "major"
    if old == "payable":
        return "major"
    return "minor"


def abi_diff(payload: dict[str, Any]) -> dict[str, Any]:
    """Breaking changes between two ABIs: removed or re-typed functions (the selector changes), stateMutability
    changes, return-type changes, removed or re-typed events, flipped `indexed` flags (the topic layout changes)."""

    def work() -> dict[str, Any]:
        old = _entries("old_abi", payload["old_abi"])
        if "state" in old:
            return old
        new = _entries("new_abi", payload["new_abi"])
        if "state" in new:
            return new
        findings: list[dict[str, Any]] = []
        added = removed = changed = 0
        labels = {"function": ("FN", "function", "blocker"), "event": ("EVENT", "event", "major"), "error": ("ERROR", "error", "minor")}
        for kind, (prefix, noun, gone_severity) in labels.items():
            old_sigs = sorted(s for k, s in old if k == kind)
            new_sigs = sorted(s for k, s in new if k == kind)
            gone = [s for s in old_sigs if s not in new_sigs]
            fresh = [s for s in new_sigs if s not in old_sigs]
            rule_changed = {"function": "FN-SELECTOR-CHANGED", "event": "EVENT-SIGNATURE-CHANGED", "error": "ERROR-SIGNATURE-CHANGED"}[kind]
            for name in sorted({_sig_name(s) for s in gone} & {_sig_name(s) for s in fresh}):
                pairs = list(zip([s for s in gone if _sig_name(s) == name], [s for s in fresh if _sig_name(s) == name]))
                for before, after in pairs:
                    gone.remove(before)
                    fresh.remove(after)
                    changed += 1
                    if kind == "event":
                        detail = f"topic0 0x{_keccak_hex(before)[:8]}.. -> 0x{_keccak_hex(after)[:8]}.."
                    else:
                        detail = f"selector 0x{_keccak256(before.encode())[:4].hex()} -> 0x{_keccak256(after.encode())[:4].hex()}"
                    findings.append(_finding(rule_changed, gone_severity,
                                             f"{noun} {before} became {after} ({detail}): existing {'callers' if kind == 'function' else 'consumers'} no longer match"))
            for s in gone:
                removed += 1
                findings.append(_finding(f"{prefix}-REMOVED", gone_severity, f"{noun} {s} was removed"))
            for s in fresh:
                added += 1
                findings.append(_finding(f"{prefix}-ADDED", "info", f"{noun} {s} was added"))
        for (kind, signature), before in sorted(old.items()):
            after = new.get((kind, signature))
            if after is None:
                continue
            if kind == "function":
                severity = _mutability_change(before["mutability"], after["mutability"])
                if severity:
                    changed += 1
                    findings.append(_finding("FN-MUTABILITY-CHANGED", severity,
                                             f"{signature} changed stateMutability {before['mutability']} -> {after['mutability']}"))
                if before["returns"] is not None and after["returns"] is not None and before["returns"] != after["returns"]:
                    changed += 1
                    findings.append(_finding("FN-RETURNS-CHANGED", "major",
                                             f"{signature} returns ({after['returns']}) instead of ({before['returns']}): callers decode the wrong data"))
            elif kind == "event" and before["indexed"] is not None and after["indexed"] is not None and before["indexed"] != after["indexed"]:
                changed += 1
                findings.append(_finding("EVENT-INDEXED-CHANGED", "major",
                                         f"event {signature} changed which parameters are indexed: the log topic layout differs and indexers misdecode"))
        listed, total = _finish(findings)
        breaking = sum(1 for f in findings if SEVERITY_RANK[f["severity"]] >= SEVERITY_RANK["major"])
        return {"state": "SUCCESS", "added": added, "removed": removed, "changed": changed, "breaking": breaking,
                "verdict": "breaking" if breaking else "compatible", "findings": listed, "findings_total": total}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 3. storage layout
# --------------------------------------------------------------------------------------------------------------------

_AST_ID = re.compile(r"(t_(?:struct|contract|enum|userDefinedValueType)\([^()]*\))\d+")
_CONTRACT_TYPE = re.compile(r"t_contract\([^()]*\)")
# A storage gap is `__gap` / `_gap` / `gap` (optionally `_suffix`), never an unrelated name that merely starts with "gap".
_GAP = re.compile(r"_*gap(?:_\w*)?\Z", re.IGNORECASE)


def _comparable_type(type_id: str) -> str:
    """Type identifier without AST ids; a contract type is stored exactly like `address` (OpenZeppelin Upgrades compares
    address and contract types as equal), so t_contract(X) becomes t_address."""
    return _CONTRACT_TYPE.sub("t_address", _AST_ID.sub(r"\1", type_id))


# One identifier-like token (variable label, solc type id, Contract.fn): no spaces or free text, so a pasted phrase or credential
# cannot ride along into a finding.
_TOKEN = re.compile(r"[\w$.:()<>,\[\]/#@~+-]*\Z")


def _layout(field: str, items: list[dict[str, Any]]) -> list[dict[str, Any]] | dict[str, Any]:
    """Validated variables of a solc storageLayout (label, slot, offset, type, numberOfBytes) or an INVALID_INPUT dict."""
    variables, seen = [], set()
    for i, item in enumerate(items):
        if not all(_TOKEN.match(item.get(part, "x")) for part in ("label", "type", "contract")):
            return _invalid(field, f"{field}[{i}] label, type and contract must be identifier-like tokens without spaces")
        slot, offset, size = item["slot"], item["offset"], item["bytes"]
        key = (item.get("contract", ""), item["label"])
        if key in seen:
            return _invalid(field, f"{field}[{i}] repeats variable {item['label']!r}")
        seen.add(key)
        if size <= 32 and offset + size > 32:
            return _invalid(field, f"{field}[{i}] {item['label']!r}: offset {offset} + {size} bytes crosses the end of its slot")
        if size > 32 and (offset != 0 or size % 32):
            return _invalid(field, f"{field}[{i}] {item['label']!r}: a multi-slot variable starts at offset 0 and spans whole slots")
        start = slot * 32 + offset
        variables.append({"key": key, "label": item["label"], "slot": slot, "offset": offset, "start": start, "end": start + size,
                          "bytes": size, "type": _comparable_type(item["type"]),
                          "gap": bool(_GAP.match(item["label"])) and item["type"].startswith("t_array")})
    ordered = sorted(variables, key=lambda v: (v["start"], v["end"]))
    for a, b in zip(ordered, ordered[1:]):
        if b["start"] < a["end"]:
            return _invalid(field, f"{field}: {a['label']!r} and {b['label']!r} overlap in slot {b['slot']}")
    return ordered


def _where(v: dict[str, Any]) -> str:
    return f"slot {v['slot']} offset {v['offset']}"


def storage_layout(payload: dict[str, Any]) -> dict[str, Any]:
    """Upgrade compatibility of two solc storage layouts (`solc --storage-layout`): a variable must keep its slot,
    offset, type and size; deleting, moving, retyping or inserting before existing variables corrupts proxy state; new
    variables go after the old end or into a `__gap` (an array labelled `__gap`/`__gap_*`; its end may not grow). Struct,
    contract and enum AST ids are ignored and a contract type equals `address` (as in OpenZeppelin Upgrades). Heuristic over
    top-level variables: struct members are not compared."""

    def work() -> dict[str, Any]:
        old = _layout("old_layout", payload["old_layout"])
        if isinstance(old, dict):
            return old
        new = _layout("new_layout", payload["new_layout"])
        if isinstance(new, dict):
            return new
        findings: list[dict[str, Any]] = []
        new_by_key = {v["key"]: v for v in new}
        new_by_start = {v["start"]: v for v in new}
        old_keys = {v["key"] for v in old}
        old_end = max(v["end"] for v in old)
        matched: set[tuple[str, str]] = set()
        for o in old:
            if o["gap"]:
                continue
            n = new_by_key.get(o["key"])
            if n is not None:
                matched.add(n["key"])
                if n["start"] != o["start"]:
                    findings.append(_finding("STORAGE-MOVED", "blocker",
                                             f"{o['label']} moved from {_where(o)} to {_where(n)}: every later read returns another variable's data"))
                elif n["type"] != o["type"] or n["bytes"] != o["bytes"]:
                    same_size = n["bytes"] == o["bytes"]
                    change = f"changed type {o['type']} -> {n['type']}" if n["type"] != o["type"] else "changed size"
                    findings.append(_finding("STORAGE-TYPE-CHANGED", "major" if same_size else "blocker",
                                             f"{o['label']} {change} ({o['bytes']} -> {n['bytes']} bytes): "
                                             + ("stored bits are reinterpreted" if same_size else "the variable no longer fits its old footprint")))
                continue
            at = new_by_start.get(o["start"])
            if at is not None and at["key"] not in old_keys:
                matched.add(at["key"])
                if at["type"] == o["type"] and at["bytes"] == o["bytes"]:
                    findings.append(_finding("STORAGE-RENAMED", "minor", f"{o['label']} at {_where(o)} is now called {at['label']} with the same type"))
                else:
                    detail = at["type"] if at["type"] != o["type"] else f"same type, {o['bytes']} -> {at['bytes']} bytes"
                    findings.append(_finding("STORAGE-REPLACED", "blocker",
                                             f"{o['label']} ({o['type']}) at {_where(o)} was replaced by {at['label']} ({detail})"))
                continue
            hit_vars = [v for v in new if not v["gap"] and v["start"] < o["end"] and o["start"] < v["end"]]
            if hit_vars:
                matched.update(v["key"] for v in hit_vars)  # reported once, here, not again as new variables
                findings.append(_finding("STORAGE-COLLISION", "blocker",
                                         f"{o['label']} at {_where(o)} was dropped and {', '.join(sorted(v['label'] for v in hit_vars))} now occupies its bytes"))
            else:
                findings.append(_finding("STORAGE-REMOVED", "major", f"{o['label']} at {_where(o)} was deleted: its data stays in storage and the slot may be reused later"))
        appended = 0
        old_gaps = [v for v in old if v["gap"]]
        for g in old_gaps:
            ng = new_by_key.get(g["key"]) or next((v for v in new if v["gap"]), None)
            if ng is None:
                findings.append(_finding("STORAGE-GAP-REMOVED", "major", f"{g['label']} ({g['bytes'] // 32} slots) was removed: no headroom is left for later upgrades"))
            elif ng["end"] > g["end"]:
                findings.append(_finding("STORAGE-GAP-GREW", "blocker", f"{g['label']} now ends at byte {ng['end']} instead of {g['end']}: inheriting contracts shift"))
            elif ng["end"] < g["end"]:
                findings.append(_finding("STORAGE-GAP-SHRUNK", "blocker", f"{g['label']} now ends {(g['end'] - ng['end'])} bytes earlier than before: inheriting contracts shift"))
            elif ng["start"] > g["start"]:
                findings.append(_finding("STORAGE-GAP-CONSUMED", "info", f"{(ng['start'] - g['start'])} bytes of {g['label']} were used by new variables, its end is unchanged"))
        for n in new:
            if n["key"] in matched or n["key"] in old_keys or n["gap"]:
                continue
            if n["start"] >= old_end:
                appended += 1
                # a gap only protects the contract that declares it: a variable of another declaring contract (when both name theirs) is no shift
                if any(g["end"] == old_end and not (g["key"][0] and n["key"][0] and g["key"][0] != n["key"][0]) for g in old_gaps):
                    findings.append(_finding("STORAGE-APPENDED-AFTER-GAP", "major",
                                             f"{n['label']} was appended at {_where(n)} after the trailing gap: inheriting contracts shift, consume the gap instead"))
                else:
                    findings.append(_finding("STORAGE-APPENDED", "info", f"{n['label']} was appended at {_where(n)} after the old layout"))
                continue
            if any(g["start"] <= n["start"] and n["end"] <= g["end"] for g in old_gaps):
                continue
            hits = sorted(o["label"] for o in old if not o["gap"] and o["start"] < n["end"] and n["start"] < o["end"])
            if hits:
                findings.append(_finding("STORAGE-COLLISION", "blocker", f"{n['label']} at {_where(n)} overlaps existing variable(s) {', '.join(hits)}"))
            elif n["end"] > old_end:  # it begins inside the old layout and ends past its end: inheriting contracts shift
                findings.append(_finding("STORAGE-PAST-OLD-END", "blocker", f"{n['label']} at {_where(n)} runs past the old layout end (byte {old_end}): inheriting contracts shift"))
            else:
                findings.append(_finding("STORAGE-FILLED-FREE-SPACE", "minor",
                                         f"{n['label']} was placed in unused space inside the old layout at {_where(n)}: contracts inheriting this one may already use it"))
        listed, total = _finish(findings)
        breaking = sum(1 for f in findings if SEVERITY_RANK[f["severity"]] >= SEVERITY_RANK["major"])
        return {"state": "SUCCESS", "old_variables": len(old), "new_variables": len(new), "appended": appended, "breaking": breaking,
                "verdict": "incompatible" if breaking else "compatible", "findings": listed, "findings_total": total}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 4. ERC conformance. Public sources: eips.ethereum.org EIP-20, EIP-165, EIP-721, EIP-1155, EIP-2612, EIP-4626.
# Rows: (signature, mutability class, returns, counted in the interface id, required). Mutability classes: view (view or
# pure), write (nonpayable), write_pay (nonpayable or payable, as EIP-721 declares transfers payable).
# --------------------------------------------------------------------------------------------------------------------

_ERC20_FUNCTIONS = (
    ("totalSupply()", "view", "uint256", True, True),
    ("balanceOf(address)", "view", "uint256", True, True),
    ("transfer(address,uint256)", "write", "bool", True, True),
    ("transferFrom(address,address,uint256)", "write", "bool", True, True),
    ("approve(address,uint256)", "write", "bool", True, True),
    ("allowance(address,address)", "view", "uint256", True, True),
    ("name()", "view", "string", False, False),
    ("symbol()", "view", "string", False, False),
    ("decimals()", "view", "uint8", False, False),
)
_ERC20_EVENTS = (
    ("Transfer(address,address,uint256)", (True, True, False), True),
    ("Approval(address,address,uint256)", (True, True, False), True),
)
# ERC-4626 "MUST implement EIP-20's optional metadata extensions" (name, symbol, decimals): required there, optional in ERC-20.
_ERC4626_BASE = tuple(row[:4] + (True,) if row[0] in ("name()", "symbol()", "decimals()") else row for row in _ERC20_FUNCTIONS)
_ERC_TABLE = {
    "erc20": (_ERC20_FUNCTIONS, _ERC20_EVENTS),
    "erc721": (
        (
            ("balanceOf(address)", "view", "uint256", True, True),
            ("ownerOf(uint256)", "view", "address", True, True),
            ("safeTransferFrom(address,address,uint256,bytes)", "write_pay", "", True, True),
            ("safeTransferFrom(address,address,uint256)", "write_pay", "", True, True),
            ("transferFrom(address,address,uint256)", "write_pay", "", True, True),
            ("approve(address,uint256)", "write_pay", "", True, True),
            ("setApprovalForAll(address,bool)", "write", "", True, True),
            ("getApproved(uint256)", "view", "address", True, True),
            ("isApprovedForAll(address,address)", "view", "bool", True, True),
            ("supportsInterface(bytes4)", "view", "bool", False, True),
        ),
        (
            ("Transfer(address,address,uint256)", (True, True, True), True),
            ("Approval(address,address,uint256)", (True, True, True), True),
            ("ApprovalForAll(address,address,bool)", (True, True, False), True),
        ),
    ),
    "erc1155": (
        (
            ("safeTransferFrom(address,address,uint256,uint256,bytes)", "write", "", True, True),
            ("safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)", "write", "", True, True),
            ("balanceOf(address,uint256)", "view", "uint256", True, True),
            ("balanceOfBatch(address[],uint256[])", "view", "uint256[]", True, True),
            ("setApprovalForAll(address,bool)", "write", "", True, True),
            ("isApprovedForAll(address,address)", "view", "bool", True, True),
            ("supportsInterface(bytes4)", "view", "bool", False, True),
        ),
        (
            ("TransferSingle(address,address,address,uint256,uint256)", (True, True, True, False, False), True),
            ("TransferBatch(address,address,address,uint256[],uint256[])", (True, True, True, False, False), True),
            ("ApprovalForAll(address,address,bool)", (True, True, False), True),
            ("URI(string,uint256)", (False, True), False),
        ),
    ),
    "erc4626": (
        _ERC4626_BASE + (
            ("asset()", "view", "address", True, True),
            ("totalAssets()", "view", "uint256", True, True),
            ("convertToShares(uint256)", "view", "uint256", True, True),
            ("convertToAssets(uint256)", "view", "uint256", True, True),
            ("maxDeposit(address)", "view", "uint256", True, True),
            ("previewDeposit(uint256)", "view", "uint256", True, True),
            ("deposit(uint256,address)", "write", "uint256", True, True),
            ("maxMint(address)", "view", "uint256", True, True),
            ("previewMint(uint256)", "view", "uint256", True, True),
            ("mint(uint256,address)", "write", "uint256", True, True),
            ("maxWithdraw(address)", "view", "uint256", True, True),
            ("previewWithdraw(uint256)", "view", "uint256", True, True),
            ("withdraw(uint256,address,address)", "write", "uint256", True, True),
            ("maxRedeem(address)", "view", "uint256", True, True),
            ("previewRedeem(uint256)", "view", "uint256", True, True),
            ("redeem(uint256,address,address)", "write", "uint256", True, True),
        ),
        _ERC20_EVENTS + (
            ("Deposit(address,address,uint256,uint256)", (True, True, False, False), True),
            ("Withdraw(address,address,address,uint256,uint256)", (True, True, True, False, False), True),
        ),
    ),
    "erc2612": (
        (
            ("permit(address,address,uint256,uint256,uint8,bytes32,bytes32)", "write", "", True, True),
            ("nonces(address)", "view", "uint256", True, True),
            ("DOMAIN_SEPARATOR()", "view", "bytes32", True, True),
        ),
        (),
    ),
}
# ERC-165 ids and the functions an id promises. 0x80ac58cd / 0xd9b67a26 are the ERC-721 / ERC-1155 core ids.
_ERC165_REQUIRED = {"erc721": ("0x80ac58cd",), "erc1155": ("0xd9b67a26",)}
_ERC165_EXTENSIONS = {
    "0x5b5e139f": ("ERC-721 Metadata", (("name()", "string"), ("symbol()", "string"), ("tokenURI(uint256)", "string"))),
    "0x780e9d63": ("ERC-721 Enumerable", (("totalSupply()", "uint256"), ("tokenOfOwnerByIndex(address,uint256)", "uint256"), ("tokenByIndex(uint256)", "uint256"))),
    "0x0e89341c": ("ERC-1155 Metadata URI", (("uri(uint256)", "string"),)),
}
_MUTABILITY_OK = {"view": ("view", "pure"), "write": ("nonpayable",), "write_pay": ("nonpayable", "payable")}


def erc_conformance(payload: dict[str, Any]) -> dict[str, Any]:
    """Required functions (mutability, return types), events (indexed flags) and ERC-165 ids of an ERC-20, 721, 1155,
    4626 (includes ERC-20 and, as EIP-4626 requires, its name/symbol/decimals) or 2612 implementation. Return types and
    `indexed` are compared only when supplied. `interface_id` is the XOR of the selectors of the standard's own functions: the
    published ERC-165 id for 721 and 1155 only (ERC-4626 and ERC-2612 publish none; the value is a convenience, not a standard)."""

    def work() -> dict[str, Any]:
        standard = payload["standard"]
        abi = _entries("abi", payload["abi"])
        if "state" in abi:
            return abi
        ids = payload.get("erc165_ids")
        if ids is not None:
            for i, value in enumerate(ids):
                if not _is_hex(value, 4):
                    return _invalid("erc165_ids", f"erc165_ids[{i}] must be 0x followed by 8 hex digits")
        functions, events = _ERC_TABLE[standard]
        findings: list[dict[str, Any]] = []
        missing = 0
        interface_id = 0
        for row in functions:
            if row[3] and (standard != "erc4626" or row not in _ERC20_FUNCTIONS):
                interface_id ^= int.from_bytes(_keccak256(row[0].encode())[:4], "big")
        for signature, mutability_class, returns, in_id, required in functions:
            entry = abi.get(("function", signature))
            if entry is None:
                if required:
                    missing += 1
                    findings.append(_finding("ERC-MISSING-FN", "blocker", f"{standard.upper()} requires function {signature}"))
                else:
                    findings.append(_finding("ERC-OPTIONAL-FN-MISSING", "info", f"optional function {signature} is not implemented (wallets and explorers read it)"))
                continue
            if entry["mutability"] not in _MUTABILITY_OK[mutability_class]:
                findings.append(_finding("ERC-MUTABILITY", "major",
                                         f"{signature} is {entry['mutability']}, the standard declares it {' or '.join(_MUTABILITY_OK[mutability_class])}"))
            if entry["returns"] is not None and entry["returns"] != returns:
                findings.append(_finding("ERC-RETURNS", "major", f"{signature} returns ({entry['returns']}), the standard returns ({returns})"))
        for signature, indexed, required in events:
            entry = abi.get(("event", signature))
            if entry is None:
                if required:
                    missing += 1
                    findings.append(_finding("ERC-MISSING-EVENT", "blocker", f"{standard.upper()} requires event {signature}"))
                else:
                    findings.append(_finding("ERC-OPTIONAL-EVENT-MISSING", "info", f"optional event {signature} is not declared"))
            elif entry["indexed"] is not None and tuple(entry["indexed"]) != indexed:
                findings.append(_finding("ERC-EVENT-INDEXED", "major",
                                         f"event {signature} must index parameters {[i + 1 for i, flag in enumerate(indexed) if flag]} "
                                         f"but indexes {[i + 1 for i, flag in enumerate(entry['indexed']) if flag]}"))
        required_ids = _ERC165_REQUIRED.get(standard)
        if required_ids:
            if ids is None:
                findings.append(_finding("ERC165-UNCHECKED", "info", "erc165_ids was not supplied: the supportsInterface answers were not verified"))
            else:
                claimed = {value.lower() for value in ids}
                for needed in ("0x01ffc9a7",) + required_ids:
                    if needed not in claimed:
                        findings.append(_finding("ERC165-ID-MISSING", "major", f"supportsInterface must return true for {needed}"))
                if "0xffffffff" in claimed:
                    findings.append(_finding("ERC165-INVALID-ID", "blocker", "supportsInterface(0xffffffff) must return false (EIP-165)"))
        for claimed_id in sorted({value.lower() for value in (ids or [])} & set(_ERC165_EXTENSIONS)):
            label, promised = _ERC165_EXTENSIONS[claimed_id]
            for signature, returns in promised:
                entry = abi.get(("function", signature))
                if entry is None or (entry["returns"] is not None and entry["returns"] != returns):
                    findings.append(_finding("ERC165-EXTENSION-INCOMPLETE", "major", f"{claimed_id} ({label}) is claimed but {signature} returning ({returns}) is not exposed"))
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "standard": standard,
                "required_functions": sum(1 for row in functions if row[4]), "required_events": sum(1 for row in events if row[2]),
                "missing": missing, "interface_id": f"0x{interface_id:08x}",
                "verdict": "non_conformant" if _worst(findings) >= SEVERITY_RANK["major"] else "conformant",
                "findings": listed, "findings_total": total}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 5-8. Solidity source tools (reader shared with scripts/sol_contract_lint.py)
# --------------------------------------------------------------------------------------------------------------------


def _selected_contracts(payload: dict[str, Any], field_name: str = "contract") -> tuple[Any, list[dict[str, Any]]] | dict[str, Any]:
    parsed = _lint().parse_source(payload["source"])
    concrete = [c for c in parsed if c["kind"] == "contract"]
    if not concrete:
        return _invalid("source", "source declares no contract (interfaces and libraries are not analysed)")
    focus = payload.get(field_name, "")
    if focus:
        chosen = [c for c in concrete if c["name"] == focus]
        if not chosen:
            return _invalid(field_name, f"contract {focus!r} is not declared in source")
        return parsed, chosen
    return parsed, concrete


class _BaseWalkTooLarge(Exception):
    """The base-contract walk took more than MAX_BASE_WALK_STEPS steps: a cyclic or very large inheritance graph."""


def _inherits(contract: dict[str, Any], by_name: dict[str, dict[str, Any]], marker: str) -> tuple[bool, bool]:
    """(inherits a base whose name contains marker, some base could not be resolved in this source)."""
    found, unresolved, _ = _walk_bases(contract, by_name, marker, frozenset(), {}, [MAX_BASE_WALK_STEPS])
    return found, unresolved


def _walk_bases(contract: dict[str, Any], by_name: dict[str, dict[str, Any]], marker: str, seen: frozenset[str],
                done: dict[str, bool], steps: list[int]) -> tuple[bool, bool, bool]:
    """`_inherits` with its walk state: (found, unresolved, cut), where cut is True when a base already on the walk path (`seen`)
    was named. A base walked without a cut has no cycle below it, so its result does not depend on the path that reached it and
    is kept in `done`: a diamond of inheritance is walked once, not once per path."""
    steps[0] -= 1
    if steps[0] < 0:
        raise _BaseWalkTooLarge()
    unresolved = cut = False
    for base in contract["bases"]:
        if marker in base:
            return True, False, False
        if base in by_name and base not in seen:
            if base in done:
                unresolved = unresolved or done[base]
                continue
            found, unknown, inner_cut = _walk_bases(by_name[base], by_name, marker, seen | {contract["name"]}, done, steps)
            if found:
                return True, False, False
            unresolved = unresolved or unknown
            cut = cut or inner_cut
            if not inner_cut:
                done[base] = unknown
        else:
            unresolved = True
            cut = cut or base in seen
    return False, unresolved, cut


def upgrade_safety(payload: dict[str, Any]) -> dict[str, Any]:
    """Proxy-implementation hazards in Solidity source, following the OpenZeppelin Upgrades plugin rules: constructor
    state, missing _disableInitializers(), unguarded initialize*, selfdestruct, delegatecall, inline state
    initializers, immutables, a missing __gap in abstract bases, and UUPS: UUPSUpgradeable inheritance and an
    unprotected _authorizeUpgrade. Policy beyond the plugin: a constructor that only calls _disableInitializers() is accepted
    (the plugin also wants `@custom:oz-upgrades-unsafe-allow constructor` on it; run the plugin for that check) and the
    __gap rules are advice (ERC-7201 namespaced storage replaces a gap). Heuristic: reads text, resolves no imports."""

    def work() -> dict[str, Any]:
        picked = _selected_contracts(payload)
        if isinstance(picked, dict):
            return picked
        parsed, chosen = picked
        L = _lint()
        pattern = payload["pattern"]
        by_name = {c["name"]: c for c in parsed}
        immutable_allowed = bool(re.search(r"@custom:oz-upgrades-unsafe-allow[^\n]*state-variable-immutable", payload["source"]))
        findings: list[dict[str, Any]] = []
        for contract in chosen:
            index = L.visible_index(contract, parsed)
            name = contract["name"]
            fns = contract["functions"]
            state_vars = [v for v in contract["state_vars"] if not v["constant"] and not v["immutable"]]
            immutables = {v["name"] for v in contract["state_vars"] if v["immutable"]}
            concrete = not contract["abstract"]
            for ctor in (f for f in fns if f["kind"] == "constructor"):
                statements = [s.strip() for s in ctor["body"].split(";") if s.strip()]
                rest = [s for s in statements if re.sub(r"\s+", "", s) != "_disableInitializers()"
                        and re.split(r"[^\w$]", s.strip(), maxsplit=1)[0] not in immutables]
                if rest:
                    findings.append(_finding("UPG-CONSTRUCTOR-STATE", "major",
                                             f"{name}: the constructor runs only for the implementation, state it sets is invisible to the proxy; move it to an initializer"))
            if concrete and not any("_disableInitializers" in f["body"] for f in fns if f["kind"] == "constructor"):
                findings.append(_finding("UPG-NO-DISABLE-INITIALIZERS", "major" if pattern == "uups" else "minor",
                                         f"{name}: no constructor calls _disableInitializers(): anyone can initialize the bare implementation"
                                         + (" and, through UUPS, upgrade it to a contract that selfdestructs (before EIP-6780 that bricks every proxy)" if pattern == "uups" else "")))
            initializer_mods = ("initializer", "reinitializer", "onlyInitializing")
            has_initializer = any(m in initializer_mods for f in fns for m, _ in f["modifiers"]) or any(re.match(r"initialize\w*$", f["name"]) for f in fns)
            if concrete and not has_initializer and (state_vars or any(b.endswith("Upgradeable") for b in contract["bases"])):
                findings.append(_finding("UPG-NO-INITIALIZER", "minor", f"{name}: has state or upgradeable bases but no initializer"))
            for fn in fns:
                if fn["kind"] != "function":
                    continue
                if re.match(r"(initialize\w*|init)$", fn["name"]) and fn["visibility"] in ("public", "external") and fn["has_body"]:
                    if not any(m in initializer_mods for m, _ in fn["modifiers"]) and not L.guards(fn, index):
                        findings.append(_finding("UPG-INIT-UNGUARDED", "blocker", f"{name}.{fn['signature']} has no initializer modifier and no access control: it can be re-run by anyone"))
                if re.match(r"__\w+_init(_unchained)?$", fn["name"]) and fn["visibility"] == "internal" and not any(m == "onlyInitializing" for m, _ in fn["modifiers"]):
                    findings.append(_finding("UPG-INIT-HELPER-UNGUARDED", "minor", f"{name}.{fn['signature']} is an init helper without onlyInitializing"))
                if re.search(r"\b(selfdestruct|suicide)\s*\(", fn["body"]):
                    findings.append(_finding("UPG-SELFDESTRUCT", "blocker", f"{name}.{fn['signature']} can selfdestruct: before EIP-6780 (Cancun) a destroyed implementation bricks every proxy, since Cancun it only deletes code created in the same transaction; the OpenZeppelin Upgrades plugin still rejects it"))
                if re.search(r"(?<![\w$])delegatecall\s*\(|\bfunctionDelegateCall\s*\(", fn["body"]):
                    findings.append(_finding("UPG-DELEGATECALL", "major", f"{name}.{fn['signature']} delegatecalls: a malicious target can selfdestruct or overwrite proxy storage"))
            for var in contract["state_vars"]:
                if var["initial_value"] and not var["constant"] and not var["immutable"]:
                    findings.append(_finding("UPG-INLINE-INIT", "major", f"{name}.{var['name']} has an initial value that the proxy never sees; assign it in the initializer"))
                if var["immutable"]:
                    findings.append(_finding("UPG-IMMUTABLE", "info" if immutable_allowed else "major",
                                             f"{name}.{var['name']} is immutable: its value lives in the implementation code" + ("" if immutable_allowed else " (annotate with @custom:oz-upgrades-unsafe-allow state-variable-immutable once reviewed)")))
            gaps = [v for v in contract["state_vars"] if _GAP.match(v["name"])]
            if contract["abstract"] and state_vars and not gaps:
                findings.append(_finding("UPG-NO-GAP", "minor", f"{name}: abstract base with state but no __gap: adding a variable later shifts every inheriting contract"))
            for gap in gaps:
                if not re.search(r"\[\d+\]$", gap["type"]):
                    findings.append(_finding("UPG-GAP-TYPE", "minor", f"{name}.{gap['name']} should be a fixed-size array such as uint256[50]"))
            try:
                inherits_uups, unresolved = _inherits(contract, by_name, "UUPS")
            except _BaseWalkTooLarge:
                return _invalid("source", "contract inheritance is cyclic or too large to walk")
            if pattern == "uups" and concrete:
                if not inherits_uups and not any(f["name"] in ("upgradeTo", "upgradeToAndCall") for f in fns):
                    findings.append(_finding("UPG-UUPS-INFO" if unresolved else "UPG-UUPS-NOT-INHERITED", "info" if unresolved else "major",
                                             f"{name}: " + ("no base named UUPS* is visible in this source (bases defined elsewhere), upgrade logic could not be verified"
                                                            if unresolved else "pattern is uups but the implementation has no upgrade function: the proxy can never be upgraded")))
            if pattern != "uups" and inherits_uups:
                findings.append(_finding("UPG-UUPS-IN-NON-UUPS", "minor", f"{name}: inherits UUPS upgrade logic although the pattern is {pattern}; remove it or the upgrade path is duplicated"))
            for fn in (f for f in fns if f["name"] == "_authorizeUpgrade"):
                if not L.guards(fn, index):
                    findings.append(_finding("UPG-UUPS-UNPROTECTED", "blocker", f"{name}._authorizeUpgrade has no access control: anyone can replace the implementation"))
        findings += _incomplete_source(payload["source"], len(chosen))
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "pattern": pattern, "contracts_checked": len(chosen),
                "verdict": "unsafe" if _worst(findings) >= SEVERITY_RANK["major"] else "safe", "findings": listed, "findings_total": total}

    return respond(payload, _call_depth_bounded(work, {"pattern": payload.get("pattern", ""), "contracts_checked": 0,
                                                       "verdict": "unsafe"}))


# privileged operations: a run of whole words of the function name equals one of these terms (`setSkillLevel` is not `kill`)
_PRIVILEGED_TERMS = frozenset({"mint", "upgrade", "selfdestruct", "destroy", "kill", "pause", "unpause", "transferownership",
                               "renounceownership", "grantrole", "revokerole", "sweep", "rescue", "drain", "setowner", "setadmin",
                               "authorize", "setimplementation", "setproxy", "emergency"})
_ADMIN_VERBS = frozenset({"set", "update", "add", "remove", "change", "enable", "disable", "configure", "withdraw", "execute",
                          "migrate", "register", "revoke", "whitelist", "blacklist", "freeze", "seize"})
# a fallback or receive that writes an owner, admin or role variable hands the contract to any caller
_ROLE_WRITE = re.compile(r"(?<![\w$.])\w*(?:owner|admin|role|governor|guardian|operator|minter|manager|keeper|implementation)\w*"
                         r"(?:\s*\[[^\]\n]{0,120}\])*\s*[-+*/%&|^]?=(?!=)", re.IGNORECASE)


def _name_words(name: str) -> list[str]:
    """Lower-case words of a camelCase, PascalCase or snake_case identifier."""
    return [word.lower() for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name)]


def _is_privileged(name: str) -> bool:
    words = _name_words(name)
    return any("".join(words[i:j]) in _PRIVILEGED_TERMS for i in range(len(words)) for j in range(i + 1, min(len(words), i + 3) + 1))


def _is_admin_verb(name: str) -> bool:
    words = _name_words(name)
    return bool(words) and words[0] in _ADMIN_VERBS


def access_matrix(payload: dict[str, Any]) -> dict[str, Any]:
    """State-changing public/external functions by access guard. Guards: access modifiers (onlyOwner, onlyRole(X), auth,
    restricted; a modifier defined in the source counts only when its body checks the caller before its `_;`), inline checks
    (_checkOwner, _checkRole, hasRole, `require(msg.sender == X)`, `if (msg.sender != X) revert`, role-named allow-list lookups)
    and the checks of internal callees reached by an unconditional call. A check counts only on the path that runs it before the
    write it guards: a check after a write guards nothing for that write, and a check inside an `if` branch guards nothing
    outside that branch. Applied modifiers run in the order listed, before the body: `onlyOwner nonReentrant` guards the body,
    while `nonReentrant onlyOwner` does not, because nonReentrant writes its lock before the owner check runs. Code after a
    modifier's `_;` runs after the body, so a check placed there guards nothing. Not access control: tx.origin, a comparison with
    address(0), `!=` inside require, an `if` that only branches, nonReentrant, whenNotPaused and initializer. An unguarded writer
    is a blocker when its name is privileged (mint, upgrade, pause, ownership, role, sweep ...), major for admin verbs (set,
    update, withdraw ...), minor otherwise; `public_by_design` names are skipped. A fallback or receive is an entry point too: one
    that writes an owner, admin or role variable without access control is a blocker. An unqualified call that runs before the
    first check and names no function this source defines is an unverified call (ACCESS-UNVERIFIED-CALL-BEFORE-GUARD, major).
    Applied modifiers are judged in the order they are applied: once an earlier one checks the sender, the later modifiers and the
    body run after that guard and are not judged. Not judged: a call on msg.sender as a receiver, upper-case names (taken as type
    conversions or constructors), declared struct and contract names (constructors), elementary types and builtins, calls inside
    assembly, external calls with a receiver, and a call that is itself the recognised check. A call that takes msg.sender as an
    argument is exempt only when it is a named guard helper whose account is the sender: its last argument is msg.sender or
    _msgSender(), or it takes no argument, or it is a recognised guard call with one argument, _checkRole(ROLE), which is exempt on
    the OpenZeppelin convention since that helper checks msg.sender itself. hasRole(msg.sender, ROLE) is judged, since the sender
    is not its last argument. A guard
    helper with no argument (isOwner(), hasRole()) is exempt from the unverified finding but earns no guard credit. Credit is given
    by name and form. _checkOwner(), _checkRole(ROLE) and _checkRole(ROLE, msg.sender) are credited as statements. hasRole(ROLE,
    msg.sender) and isOwner(msg.sender) are credited inside require(...) or if (!...) revert(), and so is an undefined name that
    contains a role word (owner, admin, operator, minter, authorized, whitelist, allow, role, governor, guardian, manager, keeper)
    and takes msg.sender as its last argument, in the same forms; that undefined call is still reported as unverified, so the
    verdict still fails. _checkRole(ROLE, user) with another account earns none. An entry point with
    no check at all judges every such call as before the check.
    Heuristic: reads text, resolves no imports."""

    def work() -> dict[str, Any]:
        picked = _selected_contracts(payload)
        if isinstance(picked, dict):
            return picked
        parsed, chosen = picked
        L = _lint()
        by_design = set(payload.get("public_by_design", []))
        findings: list[dict[str, Any]] = []
        roles: dict[str, list[str]] = {}
        state_changing = guarded = unprotected = 0
        for contract in chosen:
            index = L.visible_index(contract, parsed)
            for fn in contract["functions"]:
                if not (fn["kind"] in L.ENTRY_KINDS and fn["visibility"] in ("public", "external") and fn["mutability"] in ("nonpayable", "payable") and fn["has_body"]):
                    continue
                label = _short(f"{contract['name']}.{fn['signature']}")
                state_changing += 1
                found = L.guards(fn, index)
                if fn["name"] not in by_design:
                    unverified = L.unverified_calls(fn, index)
                    if unverified:
                        callees = ", ".join(_short(name) for name in unverified)
                        findings.append(_finding("ACCESS-UNVERIFIED-CALL-BEFORE-GUARD", "major",
                                                 f"{label} calls {callees} before its access guard, and no function of that name is defined in this source: unverified call before guard"))
                if found:
                    guarded += 1
                    for role in found:
                        roles.setdefault(_short(role), []).append(label)
                    continue
                if fn["name"] in by_design:
                    continue
                unprotected += 1
                if any(m in ("initializer", "reinitializer") for m, _ in fn["modifiers"]):
                    findings.append(_finding("ACCESS-INITIALIZER-OPEN", "minor", f"{label} can be called by anyone once (initializer): deploy and initialize atomically"))
                elif fn["kind"] in ("fallback", "receive") and _ROLE_WRITE.search(fn["body"]):
                    findings.append(_finding("ACCESS-UNPROTECTED-FALLBACK", "blocker", f"{label} writes an owner or role variable with no access control: any caller can take over the contract"))
                elif _is_privileged(fn["name"]):
                    findings.append(_finding("ACCESS-UNPROTECTED-PRIVILEGED", "blocker", f"{label} is a privileged operation with no access control"))
                elif _is_admin_verb(fn["name"]):
                    findings.append(_finding("ACCESS-UNPROTECTED-ADMIN", "major", f"{label} looks like an admin operation and has no access control"))
                else:
                    findings.append(_finding("ACCESS-UNPROTECTED-WRITER", "minor", f"{label} changes state and is callable by anyone; list it in public_by_design if intended"))
        findings += _incomplete_source(payload["source"], len(chosen))
        listed, total = _finish(findings)
        matrix, budget = [], 300
        for role in sorted(roles):
            members = sorted(roles[role])[:max(budget, 0)]
            budget -= len(members)
            if members:
                matrix.append({"role": role, "functions": members})
        return {"state": "SUCCESS", "contracts_checked": len(chosen), "state_changing": state_changing, "guarded": guarded,
                "unprotected": unprotected, "roles": matrix,
                "verdict": "fail" if _worst(findings) >= SEVERITY_RANK["major"] else "pass", "findings": listed, "findings_total": total}

    return respond(payload, _call_depth_bounded(work, {"contracts_checked": 0, "state_changing": 0, "guarded": 0, "unprotected": 0,
                                                       "roles": [], "verdict": "fail"}))


def event_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """State-changing public/external functions that emit no event. A function counts as emitting when its body, an
    initializer modifier or a same-source internal callee (followed transitively) has `emit`; a call to an undefined
    underscore function or super is reported as delegated, not as a gap. Coverage = emitting / (emitting + missing);
    `exempt` names are skipped. Fails below `min_coverage_pct` (equal passes)."""

    def work() -> dict[str, Any]:
        picked = _selected_contracts(payload)
        if isinstance(picked, dict):
            return picked
        parsed, chosen = picked
        status = _lint().emission_status(parsed)
        exempt = set(payload.get("exempt", []))
        findings: list[dict[str, Any]] = []
        emits = delegated = missing = 0
        for contract in chosen:
            for fn in contract["functions"]:
                outcome = status.get(id(fn))
                if outcome is None or fn["name"] in exempt:
                    continue
                if outcome == "emits":
                    emits += 1
                elif outcome == "delegated":
                    delegated += 1
                else:
                    missing += 1
                    findings.append(_finding("EVENT-MISSING", "minor", f"{contract['name']}.{fn['signature']} changes state and emits no event: off-chain indexers cannot observe it"))
        judged = emits + missing
        coverage = Decimal(100) if judged == 0 else Decimal(emits) * 100 / Decimal(judged)
        below = coverage < _dec(payload["min_coverage_pct"])
        if below:
            findings.append(_finding("EVENT-COVERAGE-BELOW-MIN", "major", f"event coverage {round(float(coverage), 2)}% is below the required {payload['min_coverage_pct']}%"))
        findings += _incomplete_source(payload["source"], len(chosen))
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "functions_checked": emits + delegated + missing, "emitting": emits, "delegated": delegated, "missing": missing,
                "coverage_pct": round(float(coverage), 2), "verdict": "fail" if below or _worst(findings) >= SEVERITY_RANK["major"] else "pass",
                "findings": listed, "findings_total": total}

    return respond(payload, work)


def natspec_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """NatSpec completeness of public/external functions, events and errors: `@notice` (or `@inheritdoc`), one `@param` per
    named parameter, one `@return` per return value. `@param` names that do not exist and surplus `@return` tags are major
    (solc rejects them). Coverage = fully documented items / items; fails below `min_coverage_pct` (equal passes) or on a
    major finding."""

    def work() -> dict[str, Any]:
        parsed = _lint().parse_source(payload["source"])
        total_items = documented = 0
        findings: list[dict[str, Any]] = []
        for contract in parsed:
            items = [("function", f) for f in contract["functions"] if f["kind"] == "function" and f["visibility"] in ("public", "external")]
            items += [("event", e) for e in contract["events"]] + [("error", e) for e in contract["errors"]]
            for kind, item in items:
                total_items += 1
                label = _short(f"{contract['name']}.{item['signature']}")
                doc = item["doc"]
                problems = 0
                if doc is None or not (doc["notice"] or doc["inheritdoc"]):
                    findings.append(_finding("NATSPEC-NOTICE-MISSING", "minor", f"{kind} {label} has no @notice"))
                    problems += 1
                if doc is not None and not doc["inheritdoc"]:
                    named = [name for _, name in item["params"] if name]
                    for name in named:
                        if name not in doc["params"]:
                            findings.append(_finding("NATSPEC-PARAM-MISSING", "minor", f"{kind} {label} lacks @param {name}"))
                            problems += 1
                    for name in doc["params"]:
                        if name not in named:
                            findings.append(_finding("NATSPEC-PARAM-UNKNOWN", "major", f"{kind} {label} documents @param {name or '?'} which is not a parameter"))
                            problems += 1
                    if kind == "function":
                        want = len(item["returns"])
                        if doc["returns"] < want:
                            findings.append(_finding("NATSPEC-RETURN-MISSING", "minor", f"{kind} {label} documents {doc['returns']} of {want} return value(s)"))
                            problems += 1
                        elif doc["returns"] > want:
                            findings.append(_finding("NATSPEC-RETURN-EXTRA", "major", f"{kind} {label} documents {doc['returns']} @return tags for {want} return value(s)"))
                            problems += 1
                if problems == 0:
                    documented += 1
        if total_items == 0:
            return _invalid("source", "source declares no public/external function, event or error")
        coverage = Decimal(documented) * 100 / Decimal(total_items)
        below = coverage < _dec(payload["min_coverage_pct"])
        if below:
            findings.append(_finding("NATSPEC-COVERAGE-BELOW-MIN", "major", f"NatSpec coverage {round(float(coverage), 2)}% is below the required {payload['min_coverage_pct']}%"))
        findings += _incomplete_source(payload["source"], len(parsed))
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "items_total": total_items, "items_documented": documented, "coverage_pct": round(float(coverage), 2),
                "verdict": "fail" if below or _worst(findings) >= SEVERITY_RANK["major"] else "pass", "findings": listed, "findings_total": total}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 7. gas budget
# --------------------------------------------------------------------------------------------------------------------


def _gas_map(field: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, int] = {}
    for i, item in enumerate(items):
        if not _TOKEN.match(item["name"]):
            return _invalid(field, f"{field}[{i}].name must be one identifier-like token without spaces, such as Vault.deposit")
        if item["name"] in out:
            return _invalid(field, f"{field}[{i}] repeats function {item['name']!r}")
        out[item["name"]] = item["gas"]
    return out


def gas_regression(payload: dict[str, Any]) -> dict[str, Any]:
    """Per-function gas against a baseline (for example two `forge test --gas-report` runs). A function regresses when
    the increase exceeds `min_delta_gas` AND its percentage increase is strictly above `max_regression_pct` (equal is
    within budget); `max_function_gas` is an absolute ceiling (strictly above fails). New functions are info, functions
    missing from the current run are minor."""

    def work() -> dict[str, Any]:
        baseline = _gas_map("baseline", payload["baseline"])
        if "state" in baseline:
            return baseline
        current = _gas_map("current", payload["current"])
        if "state" in current:
            return current
        budget = _dec(payload["max_regression_pct"])
        min_delta = payload.get("min_delta_gas", 0)
        ceiling = payload.get("max_function_gas")
        findings: list[dict[str, Any]] = []
        compared = sorted(set(baseline) & set(current))
        regressions = improved = 0
        worst_name, worst_pct = "", Decimal(0)
        for name in compared:
            before, after = baseline[name], current[name]
            delta = after - before
            pct = Decimal(delta) * 100 / Decimal(before)
            if delta > 0 and pct > worst_pct:
                worst_name, worst_pct = name, pct
            if delta > min_delta and pct > budget:
                regressions += 1
                findings.append(_finding("GAS-REGRESSION", "major", f"{name}: {before} -> {after} gas (+{round(float(pct), 2)}%), budget is +{payload['max_regression_pct']}%"))
            elif delta < 0:
                improved += 1
        for name in sorted(set(current) - set(baseline)):
            findings.append(_finding("GAS-NEW-FUNCTION", "info", f"{name} has no baseline ({current[name]} gas)"))
        for name in sorted(set(baseline) - set(current)):
            findings.append(_finding("GAS-MISSING-FUNCTION", "minor", f"{name} is in the baseline but not in the current run: its gas could not be compared"))
        if ceiling is not None:
            for name in sorted(current):
                if current[name] > ceiling:
                    findings.append(_finding("GAS-OVER-CEILING", "major", f"{name} uses {current[name]} gas, above the ceiling {ceiling}"))
        total_before = sum(baseline[n] for n in compared)
        total_after = sum(current[n] for n in compared)
        change = Decimal(0) if not compared else Decimal(total_after - total_before) * 100 / Decimal(total_before)
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "compared": len(compared), "regressions": regressions, "improved": improved,
                "total_baseline_gas": total_before, "total_current_gas": total_after, "total_change_pct": round(float(change), 4),
                "worst_function": worst_name, "worst_increase_pct": round(float(worst_pct), 4),
                "verdict": "fail" if _worst(findings) >= SEVERITY_RANK["major"] else "pass", "findings": listed, "findings_total": total}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 10. deployment plan. Chain ids and names: https://chainid.network (chains.json, checked); the class (mainnet, testnet) is this
# table's own classification, and 31337 (Hardhat/Anvil default) / 1337 are local-development conventions that chainid.network
# lists as unrelated testnets: a plan that targets them is `local` only when its `network_kind` says so. Any id missing here
# is reported as unknown (major on mainnet), never guessed.
# --------------------------------------------------------------------------------------------------------------------

_CHAINS = {
    1: ("Ethereum Mainnet", "mainnet"), 10: ("OP Mainnet", "mainnet"), 56: ("BNB Smart Chain", "mainnet"), 100: ("Gnosis", "mainnet"),
    137: ("Polygon PoS", "mainnet"), 324: ("zkSync Era", "mainnet"), 5000: ("Mantle", "mainnet"), 8453: ("Base", "mainnet"),
    42161: ("Arbitrum One", "mainnet"), 42170: ("Arbitrum Nova", "mainnet"), 42220: ("Celo", "mainnet"), 43114: ("Avalanche C-Chain", "mainnet"),
    59144: ("Linea", "mainnet"), 81457: ("Blast", "mainnet"), 534352: ("Scroll", "mainnet"), 7777777: ("Zora", "mainnet"),
    97: ("BNB Smart Chain Testnet", "testnet"), 300: ("zkSync Sepolia", "testnet"), 10200: ("Gnosis Chiado", "testnet"),
    43113: ("Avalanche Fuji", "testnet"), 59141: ("Linea Sepolia", "testnet"), 534351: ("Scroll Sepolia", "testnet"),
    11155111: ("Sepolia", "testnet"), 17000: ("Holesky", "testnet"), 560048: ("Hoodi", "testnet"), 84532: ("Base Sepolia", "testnet"),
    80002: ("Polygon Amoy", "testnet"), 421614: ("Arbitrum Sepolia", "testnet"), 11155420: ("OP Sepolia", "testnet"),
    168587773: ("Blast Sepolia", "testnet"),
    1337: ("Local development chain", "local"), 31337: ("Hardhat/Anvil", "local"),
}
_SECRET_WORDS = frozenset({"key", "keys", "token", "secret", "auth", "sig", "signature", "password", "passwd", "pwd", "apikey"})


def _is_secret_name(name: str) -> bool:
    """A query parameter name that carries a credential: one of its words (split at separators and camel case) is a secret
    word, so `monkey` or `keyword` is not one."""
    return any(word.lower() in _SECRET_WORDS for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", name))


_ZERO_ADDRESS = "0x" + "0" * 40


def _arg_error(type_name: str, value: str) -> str:
    """"" when `value` is a valid text form of a scalar constructor argument of `type_name`, else the reason."""
    if type_name == "address":
        if not _is_hex(value, 20):
            return "must be 0x followed by 40 hex digits"
        return "" if _checksum_ok(value) else "mixed-case address fails its EIP-55 checksum"
    if type_name == "bool":
        return "" if value in ("true", "false") else "must be true or false"
    if type_name == "string":
        return ""
    if type_name == "bytes":
        return "" if _is_hex(value) and len(value) % 2 == 0 else "must be 0x followed by an even number of hex digits"
    m = re.fullmatch(r"bytes(\d+)", type_name)
    if m:
        return "" if _is_hex(value, int(m.group(1))) else f"must be 0x followed by {2 * int(m.group(1))} hex digits"
    m = re.fullmatch(r"(u?)int(\d+)", type_name)
    if not m:
        return f"unsupported constructor argument type {type_name!r}"
    bits, signed = int(m.group(2)), m.group(1) == ""
    try:
        number = int(value, 16) if re.fullmatch(r"0x[0-9a-fA-F]+", value) else int(value) if re.fullmatch(r"-?\d+", value) else None
    except ValueError:
        number = None
    if number is None:
        return "must be a decimal or 0x hex integer"
    low, high = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    return "" if low <= number <= high else f"outside the {type_name} range"


def deploy_plan_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Pre-flight of a deployment plan (judged, never executed): chain id equals the guard value and matches a known
    network class (chainid.network), the script asserts block.chainid, a fork simulation passed before broadcasting,
    constructor arguments match their types (EIP-55 checked, no zero address), the source is verified, the owner is a
    multisig or timelock on mainnet (never an EOA), the signer is a hardware wallet / keystore / remote signer (never a raw
    private key), the RPC URL carries no credential (only the location is reported, never a value), and a mainnet
    deployment or live upgrade has a recorded human approval (L4 / L3)."""

    def work() -> dict[str, Any]:
        chain_id, expected = payload["chain_id"], payload["expected_chain_id"]
        kind = payload["network_kind"]
        types, args = payload["constructor_types"], payload["constructor_args"]
        for i, t in enumerate(types):
            if _type_error(t) or t.endswith("]") or t.startswith("("):
                return _invalid("constructor_types", f"constructor_types[{i}] {t!r} must be a scalar canonical type (arrays and tuples are not supported)")
        owner = payload.get("owner_address", "")
        if owner and not _is_hex(owner, 20):
            return _invalid("owner_address", "owner_address must be 0x followed by 40 hex digits")
        threshold, signers = payload.get("multisig_threshold", 0), payload.get("multisig_signers", 0)
        if payload["owner_kind"] == "multisig" and not 1 <= threshold <= signers:
            return _invalid("multisig_threshold", "a multisig owner needs 1 <= multisig_threshold <= multisig_signers")
        findings: list[dict[str, Any]] = []
        local, mainnet = kind == "local", kind == "mainnet"
        known = _CHAINS.get(chain_id)
        if chain_id != expected:
            findings.append(_finding("DEPLOY-CHAIN-MISMATCH", "blocker", f"plan targets chain id {chain_id} but the guard expects {expected}"))
        if known is None:
            findings.append(_finding("DEPLOY-CHAIN-UNKNOWN", "major" if mainnet else "info", f"chain id {chain_id} is not in the known-chain table; classification {kind!r} is unverified"))
        elif known[1] != kind:
            findings.append(_finding("DEPLOY-NETWORK-KIND-MISMATCH", "blocker", f"chain id {chain_id} is {known[0]} ({known[1]}) but the plan calls it {kind}"))
        if not payload["script_asserts_chain_id"]:
            findings.append(_finding("DEPLOY-NO-CHAIN-GUARD", "minor" if local else "major", "the deploy script does not assert block.chainid before broadcasting"))
        if not payload["simulated"]:
            if mainnet and payload["broadcast"]:
                findings.append(_finding("DEPLOY-NOT-SIMULATED", "blocker", "mainnet broadcast without a passing fork simulation"))
            elif not local:
                findings.append(_finding("DEPLOY-NOT-SIMULATED", "major" if mainnet else "minor", "no passing simulation (forge script without --broadcast, or a fork test) before deployment"))
        if not payload["verify_source"] and not local:
            findings.append(_finding("DEPLOY-NOT-VERIFIED", "major" if mainnet else "minor", "source verification on the block explorer is not part of the plan"))
        args_ok = True
        if len(types) != len(args):
            args_ok = False
            findings.append(_finding("DEPLOY-CTOR-ARITY", "blocker", f"constructor takes {len(types)} argument(s) but the plan supplies {len(args)}"))
        else:
            for i, (t, value) in enumerate(zip(types, args)):
                err = _arg_error(t, value)
                if err:
                    args_ok = False
                    findings.append(_finding("DEPLOY-CTOR-ARG", "blocker", f"constructor argument {i} ({t}): {err}"))
                elif t == "address" and value.lower() == _ZERO_ADDRESS:
                    findings.append(_finding("DEPLOY-CTOR-ZERO-ADDRESS", "major", f"constructor argument {i} is the zero address"))
        if owner and owner.lower() == _ZERO_ADDRESS:
            findings.append(_finding("DEPLOY-OWNER-ZERO", "blocker", "the owner is the zero address: administration is permanently lost"))
        elif owner and not _checksum_ok(owner):
            findings.append(_finding("DEPLOY-OWNER-CHECKSUM", "blocker", "owner_address fails its EIP-55 checksum"))
        if payload["owner_kind"] == "eoa" and not local:
            findings.append(_finding("DEPLOY-OWNER-EOA", "blocker" if mainnet else "minor", "the owner is a single externally owned account, not a multisig or timelock"))
        if payload["owner_kind"] == "multisig" and mainnet and threshold < 2:
            findings.append(_finding("DEPLOY-MULTISIG-THRESHOLD", "major", f"a {threshold}-of-{signers} multisig owner is a single point of failure on mainnet"))
        if payload["signer_kind"] == "raw_private_key":
            findings.append(_finding("DEPLOY-RAW-KEY", "info" if local else "blocker", "the plan signs with a raw private key: use a hardware wallet, keystore or remote signer"))
        rpc = payload.get("rpc_url", "")
        if rpc:
            m = re.fullmatch(r"(https?)://([^?#]*)(?:\?([^#]*))?(?:#.*)?", rpc)
            if not m:
                return _invalid("rpc_url", "rpc_url must be an http(s) URL")
            scheme, target, query = m.group(1), m.group(2), m.group(3) or ""
            rest = rpc.split("://", 1)[1]
            # a password may hold an unencoded '/', '?' or '#', so the '@' that ends the userinfo can follow any of them: an '@'
            # anywhere after the scheme is taken as that delimiter, and the value is never echoed
            slash = target.find("/")
            path = target[slash:] if slash >= 0 else ""
            host = re.split(r"[/?#]", rest.rsplit("@", 1)[-1], 1)[0].lower()
            host_only = re.sub(r":\d+\Z", "", host)
            if "@" in rest:
                findings.append(_finding("DEPLOY-RPC-CREDENTIAL", "blocker", "rpc_url embeds user:password in its authority"))
            for position, pair in enumerate(query.split("&") if query else [], 1):
                key, equals, value = pair.partition("=")
                # the parameter name is quoted only when it is a plain short identifier; otherwise the report gives its position
                where = f"query parameter {key!r}" if equals and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,31}", key) else f"query parameter #{position}"
                carried = value if equals else pair  # `?v=<key>` carries the key in its value, `?<key>` is the key
                if _is_secret_name(key):
                    findings.append(_finding("DEPLOY-RPC-CREDENTIAL", "blocker", f"rpc_url {where} looks like a credential"))
                elif len(carried) >= 24 and re.fullmatch(r"[A-Za-z0-9_.~%-]+", carried) and re.search(r"\d", carried):
                    findings.append(_finding("DEPLOY-RPC-PATH-KEY", "major", f"rpc_url {where} carries {len(carried)} characters that look like an API key: use a keyless endpoint or inject it at runtime"))
            for segment in path.split("/"):
                if len(segment) >= 24 and re.fullmatch(r"[A-Za-z0-9_-]+", segment) and re.search(r"\d", segment):
                    findings.append(_finding("DEPLOY-RPC-PATH-KEY", "major", f"rpc_url path segment of {len(segment)} characters looks like an API key: use a keyless endpoint or inject it at runtime"))
            if scheme != "https" and host_only not in ("localhost", "127.0.0.1", "[::1]"):
                findings.append(_finding("DEPLOY-RPC-INSECURE", "major", "rpc_url is not https and not a loopback address"))
        if mainnet and not payload["human_approval"]:
            what = "a live proxy upgrade" if payload["is_proxy_upgrade"] else "a mainnet deployment"
            findings.append(_finding("DEPLOY-NO-APPROVAL", "blocker", f"{what} needs a recorded human approval ({'L3' if payload['is_proxy_upgrade'] else 'L4'}); this agent only simulates"))
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "network_name": known[0] if known else "", "chain_known": known is not None, "constructor_args_ok": args_ok,
                "blocking": sum(1 for f in findings if SEVERITY_RANK[f["severity"]] >= SEVERITY_RANK["major"]),
                "verdict": "fail" if _worst(findings) >= SEVERITY_RANK["major"] else "pass", "findings": listed, "findings_total": total}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 11. pragma policy
# --------------------------------------------------------------------------------------------------------------------


def pragma_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """Compiler pragma review. A pragma is pinned when it admits exactly one version; ranges follow Solidity semver
    (^0.8.20 = >=0.8.20 <0.9.0, ~0.8.5 = >=0.8.5 <0.9.0, as liblangutil/SemVerHandler.cpp reads them). Findings: not pinned
    (major when `require_pinned`, else info), admits versions below `min_version` (major), admits or pins a version hit by a
    medium-or-higher compiler bug that applies to the declared build (bugs.json of the Solidity project; only 0.8.x releases are
    known): pinned = major, range = minor. IR-only bugs apply only with `via_ir`, optimizer bugs only with `optimizer_enabled`;
    the `layout at` bug (SOL-2026-3) needs the source, so only the lint script (which reads it) reports it."""

    def work() -> dict[str, Any]:
        L = _lint()
        try:
            minimum = L.parse_version(payload["min_version"])
        except ValueError:
            return _invalid("min_version", "min_version must be X.Y.Z, for example 0.8.20")
        findings: list[dict[str, Any]] = []
        pinned = floating = buggy = 0
        lowest: list[Any] = []
        for i, expr in enumerate(payload["pragmas"]):
            try:
                review = L.pragma_findings(expr, min_version=minimum, floating_severity="major" if payload["require_pinned"] else "info",
                                           via_ir=payload["via_ir"], optimizer=payload["optimizer_enabled"])
            except ValueError as exc:
                return _invalid("pragmas", f"pragmas[{i}]: {exc}")
            pinned += review["pinned"]
            floating += review["floating"]
            buggy += bool(review["buggy"])
            if review["lowest"]:
                lowest.append(L.parse_version(review["lowest"]))
            findings.extend(review["findings"])
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "pragmas_checked": len(payload["pragmas"]), "pinned": pinned, "floating": floating, "buggy": buggy,
                "lowest_admitted": "" if not lowest else "{}.{}.{}".format(*min(lowest)),
                "verdict": "fail" if _worst(findings) >= SEVERITY_RANK["major"] else "pass", "findings": listed, "findings_total": total}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 12. test gate
# --------------------------------------------------------------------------------------------------------------------

DEFAULT_MIN_FUZZ_RUNS = 256  # Foundry's default [fuzz] runs


def _pct(hit: int, found: int) -> Decimal:
    return Decimal(100) if found == 0 else Decimal(hit) * 100 / Decimal(found)


def test_gate(payload: dict[str, Any]) -> dict[str, Any]:
    """Release gate for contract tests from lcov-style numbers (LF/LH, BRF/BRH, FNF/FNH per file) and test counts. Overall line and
    branch coverage fail strictly below their minimum (equal passes); a file below `min_file_line_pct` is minor. Failing
    tests and zero tests are blockers, skipped tests minor; no fuzz test, fewer than `min_fuzz_runs` runs per fuzz test
    (default 256) or, when required, no invariant test are major. Zero branches is vacuously 100%. Fuzz adequacy
    beyond these floors (depth, seeds, corpus) belongs to the auditor."""

    def work() -> dict[str, Any]:
        files = payload["files"]
        paths: set[str] = set()
        for i, f in enumerate(files):
            if f["path"] in paths:
                return _invalid("files", f"files[{i}] repeats {f['path']!r}")
            paths.add(f["path"])
            for kind in ("lines", "branches", "functions"):
                if f[f"{kind}_hit"] > f[f"{kind}_found"]:
                    return _invalid("files", f"files[{i}] {f['path']!r}: {kind}_hit exceeds {kind}_found")
        total, failed, skipped = payload["tests_total"], payload["tests_failed"], payload["tests_skipped"]
        if failed + skipped > total:
            return _invalid("tests_failed", "tests_failed + tests_skipped exceeds tests_total")
        for field in ("fuzz_tests", "invariant_tests"):
            if payload[field] > total:
                return _invalid(field, f"{field} exceeds tests_total")
        sums = {kind: (sum(f[f"{kind}_hit"] for f in files), sum(f[f"{kind}_found"] for f in files)) for kind in ("lines", "branches", "functions")}
        line, branch, function = (_pct(*sums[k]) for k in ("lines", "branches", "functions"))
        findings: list[dict[str, Any]] = []
        if sums["lines"][1] == 0:
            findings.append(_finding("TEST-NO-COVERAGE", "blocker", "no instrumented lines: coverage was not measured"))
        elif sums["lines"][0] * 100 < _dec(payload["min_line_pct"]) * sums["lines"][1]:
            findings.append(_finding("TEST-LINE-COVERAGE", "major", f"line coverage {round(float(line), 2)}% is below {payload['min_line_pct']}%"))
        if sums["branches"][1] == 0:
            if _dec(payload["min_branch_pct"]) > 0:
                findings.append(_finding("TEST-NO-BRANCHES", "info", "no branches are instrumented: branch coverage is vacuous"))
        elif sums["branches"][0] * 100 < _dec(payload["min_branch_pct"]) * sums["branches"][1]:
            findings.append(_finding("TEST-BRANCH-COVERAGE", "major", f"branch coverage {round(float(branch), 2)}% is below {payload['min_branch_pct']}%"))
        below = 0
        floor = payload.get("min_file_line_pct")
        if floor is not None:
            for f in sorted(files, key=lambda x: x["path"]):
                if f["lines_found"] and f["lines_hit"] * 100 < _dec(floor) * f["lines_found"]:
                    below += 1
                    findings.append(_finding("TEST-FILE-LINES", "minor", f"{f['path']}: line coverage {round(float(_pct(f['lines_hit'], f['lines_found'])), 2)}% is below {floor}%"))
        if total == 0:
            findings.append(_finding("TEST-NONE", "blocker", "the suite contains no tests"))
        if failed:
            findings.append(_finding("TEST-FAILING", "blocker", f"{failed} test(s) fail"))
        if skipped:
            findings.append(_finding("TEST-SKIPPED", "minor", f"{skipped} test(s) are skipped"))
        if payload["fuzz_tests"] == 0:
            findings.append(_finding("TEST-NO-FUZZ", "major", "no fuzz test: arithmetic and input-space bugs go unexplored"))
        else:
            minimum = payload.get("min_fuzz_runs", DEFAULT_MIN_FUZZ_RUNS)
            if payload["fuzz_runs"] < minimum:
                findings.append(_finding("TEST-FUZZ-RUNS", "major", f"fuzz tests run {payload['fuzz_runs']} time(s), the minimum is {minimum}"))
        if payload["require_invariants"] and payload["invariant_tests"] == 0:
            findings.append(_finding("TEST-NO-INVARIANT", "major", "invariants are required and no invariant test exists"))
        listed, count = _finish(findings)
        return {"state": "SUCCESS", "files_checked": len(files), "line_pct": round(float(line), 2), "branch_pct": round(float(branch), 2),
                "function_pct": round(float(function), 2), "files_below_floor": below,
                "verdict": "fail" if _worst(findings) >= SEVERITY_RANK["major"] else "pass", "findings": listed, "findings_total": count}

    return respond(payload, work)


# --------------------------------------------------------------------------------------------------------------------
# 13. EIP-712
# --------------------------------------------------------------------------------------------------------------------

_EIP712_ATOMIC = re.compile(r"(?:address|bool|string|bytes|bytes(?:[1-9]|[12]\d|3[0-2])|u?int(?:8|16|24|32|40|48|56|64|72|80|88|96|104|112|120|128|136|144|152|160|168|176|184|192|200|208|216|224|232|240|248|256))\Z")
_STRUCT_DEF = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\(([^()]*)\)")


def _parse_encode_type(text: str) -> list[tuple[str, list[tuple[str, str]]]] | str:
    """[(name, [(type, field)])] in written order, or an error string."""
    defs, pos = [], 0
    while pos < len(text):
        m = _STRUCT_DEF.match(text, pos)
        if not m:
            return f"cannot parse a struct definition at offset {pos}"
        fields = []
        if m.group(2).strip():
            for part in m.group(2).split(","):
                tokens = part.split()
                if len(tokens) != 2 or not _IDENT.match(tokens[1]):
                    return f"field {part.strip()!r} of {m.group(1)} must be 'type name'"
                fields.append((tokens[0], tokens[1]))
        defs.append((m.group(1), fields))
        pos = m.end()
    if not defs:
        return "encode_type is empty"
    return defs


def _abi_word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def eip712_typehash(payload: dict[str, Any]) -> dict[str, Any]:
    """EIP-712 constants as a contract would hold them: typeHash = keccak256(encodeType) and the domain separator =
    keccak256(abi.encode(domainTypeHash, keccak256(name), keccak256(version), chainId, verifyingContract, salt)) over
    the domain fields supplied, in the fixed order name, version, chainId, verifyingContract, salt. encodeType must be the
    primary struct followed by every referenced struct sorted by name, canonical spacing, no unreferenced or undefined
    types. A declared hash that differs is a blocker (neither declared value is echoed). EIP-712 makes every domain field optional:
    requiring chainId and verifyingContract (major when missing, replay across chains or contracts) is this agent's policy."""

    def work() -> dict[str, Any]:
        text = payload["encode_type"]
        parsed = _parse_encode_type(text)
        if isinstance(parsed, str):
            return _invalid("encode_type", parsed)
        names = [name for name, _ in parsed]
        if len(set(names)) != len(names):
            return _invalid("encode_type", "a struct type is defined twice")
        defs = dict(parsed)
        findings: list[dict[str, Any]] = []
        for name, fields in parsed:
            field_names = [f for _, f in fields]
            if len(set(field_names)) != len(field_names):
                findings.append(_finding("EIP712-DUPLICATE-FIELD", "major", f"{name} declares a field name twice"))
        primary = names[0]
        reachable, queue = {primary}, [primary]
        while queue:
            for type_name, _ in defs.get(queue.pop(), []):
                base = re.sub(r"(\[\d*\])+\Z", "", type_name)
                if base in defs and base not in reachable:
                    reachable.add(base)
                    queue.append(base)
        for name, fields in parsed:
            for type_name, _ in fields:
                base = re.sub(r"(\[\d*\])+\Z", "", type_name)
                if base not in defs and not _EIP712_ATOMIC.match(base):
                    findings.append(_finding("EIP712-UNDEFINED-TYPE", "blocker", f"{name} uses type {base!r} which is neither atomic nor defined in encode_type"))

        def render(order: list[str]) -> str:
            return "".join(f"{n}({','.join(f'{t} {f}' for t, f in defs[n])})" for n in order)

        for name in names:
            if name not in reachable:
                findings.append(_finding("EIP712-UNREFERENCED-TYPE", "blocker", f"struct {name} is defined but not referenced from {primary}: it must not appear in encodeType"))
        canonical_order = [primary] + sorted(n for n in reachable if n != primary)
        canonical = render(canonical_order)
        written_normalized = render(names)
        if names[0] != primary or names[1:] != sorted(names[1:]):
            findings.append(_finding("EIP712-TYPE-ORDER", "blocker", f"referenced structs must follow {primary} in alphabetical order: expected {canonical}"))
        elif written_normalized != text:
            findings.append(_finding("EIP712-NON-CANONICAL-WHITESPACE", "major", "encode_type has extra whitespace: use 'type name' with single spaces and commas without spaces"))
        type_hash = _keccak_hex(text)
        canonical_hash = _keccak_hex(canonical)
        declared = payload.get("declared_type_hash")
        if declared is None:
            type_status = "not_declared"
        else:
            if not _is_hex(declared, 32):
                return _invalid("declared_type_hash", "declared_type_hash must be 0x followed by 64 hex digits")
            type_status = "match" if declared.lower() == "0x" + type_hash else "mismatch"
            if type_status == "mismatch":
                findings.append(_finding("EIP712-TYPEHASH-MISMATCH", "blocker", "declared_type_hash is not keccak256 of encode_type exactly as written (the recomputed value is type_hash)"))
        domain_parts: list[tuple[str, str, bytes]] = []
        for field in ("domain_name", "domain_version"):
            try:
                payload.get(field, "").encode("utf-8")
            except UnicodeEncodeError:
                return _invalid(field, f"{field} must be valid Unicode text (EIP-712 hashes its UTF-8 bytes)")
        if "domain_name" in payload:
            domain_parts.append(("string name", "name", _keccak256(payload["domain_name"].encode())))
        if "domain_version" in payload:
            domain_parts.append(("string version", "version", _keccak256(payload["domain_version"].encode())))
        if "domain_chain_id" in payload:
            domain_parts.append(("uint256 chainId", "chain", _abi_word(payload["domain_chain_id"])))
        contract = payload.get("verifying_contract")
        if contract is not None:
            if not _is_hex(contract, 20) or not _checksum_ok(contract):
                return _invalid("verifying_contract", "verifying_contract must be a 20-byte hex address with a valid EIP-55 checksum when mixed-case")
            domain_parts.append(("address verifyingContract", "contract", bytes(12) + bytes.fromhex(contract[2:])))
        salt = payload.get("domain_salt")
        if salt is not None:
            if not _is_hex(salt, 32):
                return _invalid("domain_salt", "domain_salt must be 0x followed by 64 hex digits")
            domain_parts.append(("bytes32 salt", "salt", bytes.fromhex(salt[2:])))
        declared_separator = payload.get("declared_domain_separator")
        if declared_separator is not None and not _is_hex(declared_separator, 32):
            return _invalid("declared_domain_separator", "declared_domain_separator must be 0x followed by 64 hex digits")
        if not domain_parts:
            if declared_separator is not None:
                return _invalid("declared_domain_separator", "a declared domain separator needs at least one domain field to be recomputed")
            domain_type, separator, separator_status = "", "", "not_declared"
        else:
            domain_type = f"EIP712Domain({','.join(part[0] for part in domain_parts)})"
            separator = _keccak256(_keccak256(domain_type.encode()) + b"".join(part[2] for part in domain_parts)).hex()
            separator_status = "not_declared" if declared_separator is None else "match" if declared_separator.lower() == "0x" + separator else "mismatch"
            if separator_status == "mismatch":
                findings.append(_finding("EIP712-DOMAIN-MISMATCH", "blocker", "declared_domain_separator differs from the recomputed domain_separator"))
        # the binding policy holds with or without domain fields: a domain with neither chain nor contract replays everywhere
        kinds = {part[1] for part in domain_parts}
        if "chain" not in kinds:
            findings.append(_finding("EIP712-DOMAIN-NO-CHAIN-ID", "major", "the domain has no chainId: signatures replay across chains"))
        if "contract" not in kinds:
            findings.append(_finding("EIP712-DOMAIN-NO-CONTRACT", "major", "the domain has no verifyingContract: signatures replay across contracts"))
        elif contract is not None and contract.lower() == _ZERO_ADDRESS:
            findings.append(_finding("EIP712-DOMAIN-ZERO-CONTRACT", "major", "verifyingContract is the zero address"))
        listed, total = _finish(findings)
        return {"state": "SUCCESS", "primary_type": primary, "canonical_encode_type": canonical, "type_hash": "0x" + type_hash,
                "canonical_type_hash": "0x" + canonical_hash, "type_hash_status": type_status, "domain_type_string": domain_type,
                "domain_separator": "0x" + separator if separator else "", "domain_separator_status": separator_status,
                "verdict": "fail" if _worst(findings) >= SEVERITY_RANK["major"] else "pass", "findings": listed, "findings_total": total}

    return respond(payload, work)
