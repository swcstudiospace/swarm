"""A29 dApp & Wallet Integration Engineer tools: pure checks over wallet/chain integration data supplied by the caller.

Nothing here holds a key, signs, or touches a network: every tool judges a payload, configuration or history that the
agent already has (EVM-first, EIP/ERC numbered).

Tools: dapp_typed_data_check (EIP-712 payload: domain, chain id, type closure, values, deadline, nonce),
dapp_permit_check (ERC-2612 / Permit2 permit: nonce, deadline, spender, amount), dapp_siwe_check (EIP-4361 message),
dapp_chain_config_check (EIP-3085 chain parameters), dapp_approval_risk (token approvals: unlimited, unknown spender),
dapp_tx_state_machine (transaction lifecycle history), dapp_userop_check (ERC-4337 UserOperation coherence),
dapp_provider_error_map (EIP-1193/JSON-RPC error to UX class, revert decoding), dapp_rpc_fallback_check
(client RPC provider fallback), dapp_wallet_matrix (EIP-1193 / EIP-6963 / WalletConnect capability matrix),
dapp_ens_name_check (ENS name hygiene, namehash, reverse-record verification), dapp_signing_request_scan
(phishing indicators in signing requests and links), dapp_gas_buffer_check (gas limit headroom and balance).

Severity vocabulary (shared with scripts/dapp_wallet_check.py): info < minor < major < blocker. Verdicts: `block` when any
blocker exists, `review` when any major exists, else `pass`.

Reference tables and their public sources (kept small, pinned by tests):
- Chain ids and native symbols: ethereum-lists/chains (chainid.network, chainlist.org).
- Keccak-256 (original Keccak padding 0x01, NOT NIST SHA3-256): the Keccak team reference; EIP-55 checksums; EIP-137 namehash.
- Provider error codes: EIP-1193 (4001, 4100, 4200, 4900, 4901), EIP-1474 (-32000..-32006), JSON-RPC 2.0 (-32700..-32603), geth's code 3
  for execution reverted. 4902 is MetaMask's "Unrecognized chain ID" answer to wallet_switchEthereumChain; EIP-3326 defines no error code.
- Solidity revert payloads: Error(string) 0x08c379a0 and Panic(uint256) 0x4e487b71 (Solidity ABI spec, panic code table).
- ERC-4337 EntryPoint v0.6 / v0.7 required-prefund formula (eth-infinitism account-abstraction `_getRequiredPrefund`).
- Permit2 AllowanceTransfer (Uniswap permit2 repository): exact-nonce rule, expiration 0 means the current block.
- Chain table rows: chainid.network (ethereum-lists/chains). 1337 and 31337 are local-devnet conventions, not registry rows (the registry
  lists 31337 as a different chain). MAX_SAFE_CHAIN_ID and the 90% block-gas share are MetaMask limits (MetaMask/core).
- ERC-4337 AA94: both EntryPoints reject any gas or fee value at or above 2**120 ("gas values overflow").

House policy, NOT standards (defaults chosen by this lane; summaries say so where it matters): an allowance at or above 2**255 (half of
uint256; half of uint160 for Permit2) counts as unlimited; a signature must live 60..3600 s; SIWE Issued At may be at most 600 s old with 60 s
of clock skew; a contract call needs 10% gas headroom (20% recommended, over 100% excessive) and a 60 s old estimate is stale; a Permit2
allowance over 30 days is long-lived (Uniswap's frontend uses 30 days); a transaction pending 600 s is stuck; failover timeouts run 1..30 s
with at most 3 retries per provider.
"""
from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, NoReturn, TypeGuard

from swarm.tools import support
from swarm.tools.support import respond

# ---- shared constants ------------------------------------------------------------------------------------------------

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
MAX_FINDINGS = 100
MAX_SUMMARY_CHARS = 400
MAX_UINT256 = 2**256 - 1
MAX_DIGITS = 100  # longest decimal string parsed as a number; int() of thousands of digits raises in CPython
MAX_TYPE_CHARS = 128  # longest EIP-712 type expression inspected (struct name plus array suffixes)
UNLIMITED_THRESHOLD = 2**255  # house policy, not a standard: an allowance at or above half of uint256 is treated as unlimited
ZERO_ADDRESS = "0x" + "0" * 40
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"  # canonical Permit2 deployment (same address on every supported chain)
MAX_UINT160 = 2**160 - 1
PERMIT2_UNLIMITED_THRESHOLD = (MAX_UINT160 + 1) // 2  # house policy: a Permit2 amount (uint160) at or above half of uint160 counts as unlimited
MAX_UINT48 = 2**48 - 1
MS_EPOCH_HEURISTIC = 10**11  # a "seconds" timestamp above this (year 5138) is almost certainly milliseconds
MS_EPOCH_CEILING = 10**14  # largest "seconds" value that can still be a millisecond clock (year 5138 in ms); above it a value is neither
MIN_DEADLINE_RUNWAY_S = 60  # a signature expiring sooner than this cannot be mined in time
DEFAULT_MAX_TTL_S = 3600
MAX_CLOCK_SKEW_S = 60
LONG_ALLOWANCE_S = 30 * 86400  # Permit2 allowance expiration beyond 30 days is flagged as long-lived (house policy; Uniswap's frontend uses 30 days)
MAX_SAFE_CHAIN_ID = 4503599627370476  # largest chain id MetaMask accepts in wallet_addEthereumChain
MAX_DEPTH = 32

_HEXCHARS = frozenset("0123456789abcdefABCDEF")


def _invalid(field: str, message: str) -> dict[str, str]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr)."""
    return Decimal(repr(value))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _f(fid: str, severity: str, summary: str) -> dict[str, str]:
    """One finding. Summaries may quote caller-supplied names, so they are clipped to keep every result small."""
    return {"id": fid, "severity": severity, "summary": _clip(summary, MAX_SUMMARY_CHARS)}


def _finish(findings: list[dict[str, str]]) -> tuple[list[dict[str, str]], int, int, int, str]:
    """(sorted findings capped at MAX_FINDINGS, blocker count, major count, minor count, verdict). Counts are pre-cap."""
    ordered = sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))
    blockers = sum(1 for f in ordered if f["severity"] == "blocker")
    majors = sum(1 for f in ordered if f["severity"] == "major")
    minors = sum(1 for f in ordered if f["severity"] == "minor")
    if len(ordered) > MAX_FINDINGS:
        ordered = ordered[:MAX_FINDINGS - 1] + [_f("TRUNCATED", "info", f"{len(findings) - MAX_FINDINGS + 1} further findings omitted")]
    verdict = "block" if blockers else "review" if majors else "pass"
    return ordered, blockers, majors, minors, verdict


# ---- keccak-256 and addresses ----------------------------------------------------------------------------------------

_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_KECCAK_ROT = ((0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61), (28, 55, 25, 21, 56), (27, 20, 39, 8, 14))
_MASK64 = (1 << 64) - 1


def _rol(value: int, shift: int) -> int:
    shift %= 64
    return ((value << shift) | (value >> (64 - shift))) & _MASK64 if shift else value


def _keccak_f(lanes: list[int]) -> list[int]:
    for rc in _KECCAK_RC:
        c = [lanes[x] ^ lanes[x + 5] ^ lanes[x + 10] ^ lanes[x + 15] ^ lanes[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        lanes = [lanes[i] ^ d[i % 5] for i in range(25)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(lanes[x + 5 * y], _KECCAK_ROT[x][y])
        lanes = [b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y]) for y in range(5) for x in range(5)]
        lanes[0] ^= rc
    return lanes


def _keccak256(data: bytes) -> bytes:
    """Keccak-256 as used by Ethereum (pad10*1 with the 0x01 domain byte), rate 136 bytes."""
    rate = 136
    padded = bytearray(data) + b"\x01"
    padded += b"\x00" * (-len(padded) % rate)
    padded[-1] |= 0x80
    lanes = [0] * 25
    for offset in range(0, len(padded), rate):
        block = padded[offset:offset + rate]
        for i in range(rate // 8):
            lanes[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        lanes = _keccak_f(lanes)
    return b"".join(lane.to_bytes(8, "little") for lane in lanes[:4])


def _selector(signature: str) -> str:
    """4-byte function selector (hex, no 0x) of a canonical signature such as `transfer(address,uint256)`."""
    return _keccak256(signature.encode("ascii"))[:4].hex()


def _namehash(name: str) -> str:
    """EIP-137 namehash of an already-normalized name, as 0x-prefixed hex."""
    node = b"\x00" * 32
    if name:
        for label in reversed(name.split(".")):
            node = _keccak256(node + _keccak256(label.encode("utf-8")))
    return "0x" + node.hex()


def _is_hex(text: str) -> bool:
    return all(c in _HEXCHARS for c in text)


def _is_address(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and len(value) == 42 and value.startswith("0x") and _is_hex(value[2:])


def _checksum_state(address: str) -> str:
    """`none` when the hex letters are all one case (no checksum asserted), `valid` / `invalid` for mixed case (EIP-55)."""
    body = address[2:]
    if body == body.lower() or body == body.upper():
        return "none"
    digest = _keccak256(body.lower().encode("ascii")).hex()
    expected = "".join(c.upper() if int(digest[i], 16) >= 8 else c for i, c in enumerate(body.lower()))
    return "valid" if body == expected else "invalid"


def _same_address(left: str, right: str) -> bool:
    return left.lower() == right.lower()


def _address_findings(prefix: str, label: str, address: object, findings: list[dict[str, str]]) -> bool:
    """Shape and EIP-55 findings for one address-valued field. Returns True when it is a well-formed address."""
    if not _is_address(address):
        findings.append(_f(f"{prefix}-ADDRESS-MALFORMED", "blocker", f"{label} is not a 0x-prefixed 20-byte hex address"))
        return False
    if _checksum_state(address) == "invalid":
        findings.append(_f(f"{prefix}-ADDRESS-CHECKSUM", "major", f"{label} has mixed case that fails its EIP-55 checksum (typo or spoofed address)"))
    return True


# ---- time ------------------------------------------------------------------------------------------------------------

_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def _parse_iso(value: str) -> datetime | None:
    """Aware datetime of an ISO-8601 string with an explicit offset, else None."""
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp() // 1)


def _digits(value: object) -> int | None:
    """Non-negative integer of a decimal-digit string of at most MAX_DIGITS characters (uint256 needs 78), else None."""
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= MAX_DIGITS:
        return int(value)
    return None


def _clamp(value: int, bound: int = 10**12) -> int:
    return max(-bound, min(bound, value))


def _strip_userinfo(authority: str) -> str:
    """`user:pass@host:port` -> `host:port`: an authority is echoed only without its userinfo, which can be a credential."""
    return authority.rsplit("@", 1)[-1]


def _plain_token(text: str, limit: int = 16) -> str:
    """`text` when it is a short version-like token (digits and dots), else only its length: free text can be a secret."""
    return text if re.fullmatch(r"[0-9.]{1,%d}" % limit, text) else f"a {len(text)}-character value"


def _pct_text(value: Decimal, rounding: str) -> str:
    """A percentage with at most four decimals, rounded in the stated direction so a failing value is never shown as the threshold."""
    return format(value.quantize(Decimal("0.0001"), rounding=rounding).normalize(), "f")


def _deadline_findings(prefix: str, label: str, deadline: int, now_epoch: int, max_ttl: int, findings: list[dict[str, str]]) -> bool:
    """Expiry findings for a unix-seconds deadline. Returns True when the remaining time is meaningful (not never/ms)."""
    if deadline >= MAX_UINT256:
        findings.append(_f(f"{prefix}-DEADLINE-NEVER", "major", f"{label} is max uint256: the signature never expires"))
        return False
    if deadline > MS_EPOCH_CEILING:
        findings.append(_f(f"{prefix}-DEADLINE-NEVER", "major", f"{label} is above 10^14: beyond any date in seconds or in milliseconds, the signature never expires in practice"))
        return False
    if deadline > MS_EPOCH_HEURISTIC:
        findings.append(_f(f"{prefix}-DEADLINE-MILLISECONDS", "major", f"{label} {deadline} looks like milliseconds; contracts compare unix seconds"))
        return False
    remaining = deadline - now_epoch
    if remaining < 0:
        findings.append(_f(f"{prefix}-DEADLINE-EXPIRED", "blocker", f"{label} is {-remaining}s in the past: the signature is already expired"))
    elif remaining < MIN_DEADLINE_RUNWAY_S:
        findings.append(_f(f"{prefix}-DEADLINE-TOO-SHORT", "major", f"{label} leaves {remaining}s, under the {MIN_DEADLINE_RUNWAY_S}s needed to get mined"))
    elif remaining > max_ttl:
        findings.append(_f(f"{prefix}-DEADLINE-TOO-LONG", "major", f"{label} leaves {remaining}s, above the {max_ttl}s maximum lifetime"))
    return True


# ---- 1. EIP-712 typed data -------------------------------------------------------------------------------------------

DOMAIN_MEMBERS = ("name", "version", "chainId", "verifyingContract", "salt")
DOMAIN_TYPES = {"name": "string", "version": "string", "chainId": "uint256", "verifyingContract": "address", "salt": "bytes32"}
DEADLINE_NAMES = ("deadline", "sigDeadline", "validBefore", "validUntil", "expiry", "expiration", "expires")
NONCE_NAMES = ("nonce",)
_ARRAY_SUFFIX = re.compile(r"^(.*)\[(\d*)\]$")
ARRAY_ZERO = re.compile(r"\[0\]$")


class _TooDeep(Exception):
    pass


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"{name} is not valid JSON")


def _int_of(value: object) -> int | None:
    """Integer of a JSON number, decimal string or 0x-hex string; None for anything else (bool included)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        sign = -1 if text.startswith("-") else 1
        body = text[1:] if text.startswith("-") else text
        if body.startswith(("0x", "0X")) and len(body) > 2 and _is_hex(body[2:]):
            return sign * int(body[2:], 16)
        if body.isascii() and body.isdigit():
            return sign * int(body) if len(body) <= MAX_DIGITS else sign * 2**512
    return None


def _atomic_kind(type_name: str) -> str:
    """`ok` for a valid EIP-712 atomic type, `alias` for bare uint/int, `` when it is not an atomic type."""
    if type_name in ("bool", "address", "string", "bytes"):
        return "ok"
    for prefix in ("bytes", "uint", "int"):
        if type_name.startswith(prefix):
            rest = type_name[len(prefix):]
            if rest == "" and prefix != "bytes":
                return "alias"
            if rest.isascii() and rest.isdigit() and len(rest) <= 3 and str(int(rest)) == rest:
                size = int(rest)
                if prefix == "bytes":
                    return "ok" if 1 <= size <= 32 else ""
                return "ok" if 8 <= size <= 256 and size % 8 == 0 else ""
    return ""


def _base_type(type_name: str) -> str:
    while True:
        match = _ARRAY_SUFFIX.match(type_name)
        if not match:
            return type_name
        type_name = match.group(1)


def _walk_value(types: dict[str, Any], type_name: str, value: object, path: str, findings: list[dict[str, str]], depth: int) -> None:
    if depth > MAX_DEPTH:
        raise _TooDeep()
    if len(type_name) > MAX_TYPE_CHARS:
        return
    array = _ARRAY_SUFFIX.match(type_name)
    if array:
        inner, size = array.group(1), array.group(2)
        if not isinstance(value, list):
            findings.append(_f("TD-VALUE-TYPE", "major", f"{path}: type {type_name} needs a JSON array"))
            return
        if size and (len(size) > 9 or len(value) != int(size)):
            findings.append(_f("TD-ARRAY-LENGTH", "major", f"{path}: fixed array {type_name} has {len(value)} elements"))
        for index, element in enumerate(value):
            _walk_value(types, inner, element, f"{path}[{index}]", findings, depth + 1)
        return
    if type_name in types:
        if not isinstance(value, dict):
            findings.append(_f("TD-VALUE-TYPE", "major", f"{path}: struct {type_name} needs a JSON object"))
            return
        declared = {field["name"]: field["type"] for field in types[type_name]}
        for name, field_type in declared.items():
            if name not in value:
                findings.append(_f("TD-FIELD-MISSING", "major", f"{path}.{name} is declared in {type_name} but absent from the message"))
            else:
                _walk_value(types, field_type, value[name], f"{path}.{name}", findings, depth + 1)
        for name in sorted(k for k in value if k not in declared):
            findings.append(_f("TD-FIELD-EXTRA", "minor", f"{path}.{name} is not declared in {type_name}: signers ignore it, so it is not covered by the signature"))
        return
    if not _atomic_kind(type_name):
        return
    if type_name == "bool":
        if not isinstance(value, bool):
            findings.append(_f("TD-VALUE-TYPE", "major", f"{path}: bool needs true or false"))
    elif type_name == "address":
        _address_findings("TD", path, value, findings)
    elif type_name == "string":
        if not isinstance(value, str):
            findings.append(_f("TD-VALUE-TYPE", "major", f"{path}: string needs a JSON string"))
    elif type_name.startswith("bytes"):
        size = type_name[5:]
        if not isinstance(value, str) or not value.startswith("0x") or len(value) % 2 or not _is_hex(value[2:]):
            findings.append(_f("TD-VALUE-TYPE", "major", f"{path}: {type_name} needs an even-length 0x-hex string"))
        elif size and (len(value) - 2) // 2 != int(size):
            findings.append(_f("TD-VALUE-TYPE", "major", f"{path}: {type_name} needs exactly {size} bytes, got {(len(value) - 2) // 2}"))
    elif type_name.startswith(("uint", "int")):
        signed = type_name.startswith("int")
        bits = int(type_name[3:] or 256) if signed else int(type_name[4:] or 256)
        number = _int_of(value)
        low, high = (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if signed else (0, 2**bits - 1)
        if number is None:
            findings.append(_f("TD-VALUE-TYPE", "major", f"{path}: {type_name} needs an integer, decimal string or 0x-hex string"))
        elif not low <= number <= high:
            findings.append(_f("TD-VALUE-RANGE", "blocker", f"{path}: value does not fit {type_name}"))


def _find_member(message: dict[str, Any], names: tuple[str, ...]) -> tuple[str, Any] | None:
    """First (path, value) of any of `names` in `message`, shallowest first, names in the given order. Descends into nested structs and
    into arrays of structs (Permit2 PermitBatch keeps one nonce per `details[]` entry)."""

    def children(prefix: str, value: object) -> Iterator[tuple[str, dict[str, Any]]]:
        stack = [(prefix, value, 0)]
        while stack:  # iterative and depth-capped: array nesting inside a 64 KiB document can exceed the recursion limit
            path, node, depth = stack.pop()
            if isinstance(node, dict):
                yield path, node
            elif isinstance(node, list) and depth < MAX_DEPTH:
                stack.extend((f"{path}[{index}]", element, depth + 1) for index, element in reversed(list(enumerate(node))))

    level = [("", message)]
    for _ in range(MAX_DEPTH):
        for name in names:
            for prefix, node in level:
                if name in node:
                    return (f"{prefix}.{name}" if prefix else name), node[name]
        level = [pair for prefix, node in level for k, v in sorted(node.items()) for pair in children(f"{prefix}.{k}" if prefix else k, v)]
        if not level:
            break
    return None


def typed_data_check(payload: dict[str, Any]) -> dict[str, Any]:
    """EIP-712 eth_signTypedData_v4 payload: type closure, domain, chain id, value types, deadline and nonce."""

    def work() -> dict[str, Any]:
        now = _parse_iso(payload["now"])
        if now is None:
            return _invalid("now", "now must be an ISO-8601 timestamp with an explicit offset")
        max_ttl = payload.get("max_ttl_seconds", DEFAULT_MAX_TTL_S)
        try:
            doc = json.loads(payload["typed_data_json"], parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return _invalid("typed_data_json", "typed_data_json is not valid JSON")
        if not isinstance(doc, dict):
            return _invalid("typed_data_json", "typed data must be a JSON object")
        types, primary, message = doc.get("types"), doc.get("primaryType"), doc.get("message")
        domain = doc.get("domain", {})
        if not isinstance(types, dict) or not isinstance(primary, str) or not isinstance(message, dict) or not isinstance(domain, dict):
            return _invalid("typed_data_json", "typed data needs object `types`, string `primaryType`, object `message` and object `domain`")
        for name, fields in types.items():
            if not isinstance(fields, list) or not all(isinstance(f, dict) and isinstance(f.get("name"), str) and isinstance(f.get("type"), str) for f in fields):
                return _invalid("typed_data_json", f"types.{name} must be a list of {{name, type}} string pairs")
        findings: list[dict[str, str]] = []

        # type closure
        for name in sorted(types):
            seen = set()
            for field in types[name]:
                if field["name"] in seen:
                    findings.append(_f("TD-FIELD-DUPLICATE", "major", f"type {name} declares field {field['name']} twice"))
                seen.add(field["name"])
                if len(field["type"]) > MAX_TYPE_CHARS:
                    findings.append(_f("TD-TYPE-UNDEFINED", "blocker", f"{name}.{field['name']} has a type name longer than {MAX_TYPE_CHARS} characters"))
                    continue
                base = _base_type(field["type"])
                kind = _atomic_kind(base)
                if base not in types and not kind:
                    findings.append(_f("TD-TYPE-UNDEFINED", "blocker", f"{name}.{field['name']} uses type {field['type']} which is neither atomic nor defined in types"))
                elif base not in types and kind == "alias":
                    findings.append(_f("TD-TYPE-ALIAS", "minor", f"{name}.{field['name']} uses the alias {base}; write {base}256 (signers differ on aliases)"))
                if field["type"] != base and ARRAY_ZERO.search(field["type"]):
                    findings.append(_f("TD-TYPE-UNDEFINED", "blocker", f"{name}.{field['name']} declares a zero-length fixed array"))
        if primary not in types:
            findings.append(_f("TD-PRIMARY-UNDEFINED", "blocker", f"primaryType {primary} is not defined in types"))
        else:
            reachable, stack = set(), [primary]
            while stack:
                current = stack.pop()
                if current in reachable or current not in types:
                    continue
                reachable.add(current)
                stack.extend(_base_type(field["type"]) for field in types[current] if len(field["type"]) <= MAX_TYPE_CHARS)
            unused = sorted(name for name in types if name not in reachable and name != "EIP712Domain")
            if unused:
                findings.append(_f("TD-TYPE-UNREACHABLE", "minor", f"types not reachable from {primary}: {', '.join(unused)}"))

        # domain
        unknown_members = sorted(k for k in domain if k not in DOMAIN_MEMBERS)
        if unknown_members:
            findings.append(_f("TD-DOMAIN-UNKNOWN-MEMBER", "major", f"domain members {', '.join(unknown_members)} are not EIP-712 domain fields: signers drop them from the domain hash"))
        present = [m for m in DOMAIN_MEMBERS if m in domain]
        if "EIP712Domain" in types:
            declared = {f["name"]: f["type"] for f in types["EIP712Domain"]}
            if set(declared) != set(present):
                findings.append(_f("TD-DOMAIN-TYPE-MISMATCH", "major", "types.EIP712Domain fields differ from the domain object members (the domain hash would not match what is shown)"))
            for member in present:
                if member in declared and declared[member] != DOMAIN_TYPES[member]:
                    findings.append(_f("TD-DOMAIN-TYPE-MISMATCH", "major", f"types.EIP712Domain declares {member} as {declared[member]}, EIP-712 defines {DOMAIN_TYPES[member]}"))
        else:
            findings.append(_f("TD-DOMAIN-TYPE-OMITTED", "info", "types.EIP712Domain is omitted; the eth_signTypedData_v4 JSON-RPC payload normally includes it"))
        chain_id = -1
        if "chainId" not in domain:
            findings.append(_f("TD-CHAIN-MISSING", "major", "domain has no chainId: the signature is replayable on every chain where the verifying contract exists"))
        else:
            parsed = _int_of(domain["chainId"])
            if parsed is None or parsed < 0:
                findings.append(_f("TD-CHAIN-MALFORMED", "blocker", "domain.chainId is not a non-negative integer"))
            else:
                chain_id = parsed
                if parsed != payload["expected_chain_id"]:
                    findings.append(_f("TD-CHAIN-MISMATCH", "blocker", f"domain.chainId {parsed} differs from the expected chain {payload['expected_chain_id']}"))
        if "verifyingContract" not in domain:
            findings.append(_f("TD-VERIFYING-MISSING", "major", "domain has no verifyingContract: the signature is not bound to one contract and replays on any contract with the same domain name and chain"))
        elif _address_findings("TD-VERIFYING", "domain.verifyingContract", domain["verifyingContract"], findings):
            contract = domain["verifyingContract"]
            if _same_address(contract, ZERO_ADDRESS):
                findings.append(_f("TD-VERIFYING-ZERO", "major", "domain.verifyingContract is the zero address"))
            allowed = payload.get("expected_verifying_contracts")
            if allowed is not None and not any(_same_address(contract, a) for a in allowed):
                findings.append(_f("TD-VERIFYING-UNEXPECTED", "major", f"domain.verifyingContract {contract} is not in the expected contract list"))
        if not isinstance(domain.get("name"), str) or not isinstance(domain.get("version"), str):
            findings.append(_f("TD-DOMAIN-NAME-VERSION", "minor", "domain lacks a string name and version: wallets show less context and versions cannot be separated"))
        if "salt" in domain:
            salt = domain["salt"]
            if not (isinstance(salt, str) and len(salt) == 66 and salt.startswith("0x") and _is_hex(salt[2:])):
                findings.append(_f("TD-DOMAIN-SALT", "major", "domain.salt is not a 32-byte 0x-hex string (EIP-712 bytes32)"))

        # message values
        if primary in types:
            try:
                _walk_value(types, primary, message, "message", findings, 0)
            except _TooDeep:
                return _invalid("typed_data_json", f"message nests deeper than {MAX_DEPTH} levels")

        # deadline and nonce
        deadline_name = payload.get("deadline_field")
        deadline_hit = _find_member(message, (deadline_name,) if deadline_name else DEADLINE_NAMES)
        nonce_hit = _find_member(message, NONCE_NAMES)
        deadline_checked, remaining = False, 0
        if deadline_hit is not None:
            deadline = _int_of(deadline_hit[1])
            if deadline is None or deadline < 0:
                findings.append(_f("TD-DEADLINE-MALFORMED", "major", f"message.{deadline_hit[0]} is not a non-negative integer"))
            else:
                deadline_checked = _deadline_findings("TD", f"message.{deadline_hit[0]}", deadline, _epoch(now), max_ttl, findings)
                remaining = _clamp(deadline - _epoch(now)) if deadline_checked else 0
        if deadline_hit is None and nonce_hit is None:
            findings.append(_f("TD-REPLAY-UNBOUNDED", "blocker", "message has neither a deadline nor a nonce: the signature can be replayed forever"))
        elif deadline_hit is None:
            findings.append(_f("TD-DEADLINE-MISSING", "major", "message has no deadline/expiry field: the signature never expires"))
        elif nonce_hit is None:
            findings.append(_f("TD-NONCE-MISSING", "major", "message has no nonce: the signature can be replayed until the deadline"))
        if nonce_hit is not None and _int_of(nonce_hit[1]) is None:
            findings.append(_f("TD-NONCE-MALFORMED", "major", f"message.{nonce_hit[0]} is not an integer or 0x-hex value"))

        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {
            "state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
            "primary_type": _clip(primary, 255), "type_count": len(types), "domain_chain_id": chain_id,
            "deadline_checked": deadline_checked, "deadline_remaining_seconds": remaining,
        }

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 2. permit (ERC-2612 / Permit2) ----------------------------------------------------------------------------------


def permit_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Pre-sign check of an ERC-2612 permit or Permit2 AllowanceTransfer permit: nonce equality, deadline, spender, amount.

    `permit2` means AllowanceTransfer (PermitSingle / PermitBatch: sequential uint48 nonce, uint160 amount, uint48 allowance expiration).
    SignatureTransfer permits (PermitTransferFrom) use unordered bitmap nonces, so the exact-nonce rule does not apply to them and this
    tool must not be used to judge their nonce.
    """

    def work() -> dict[str, Any]:
        now = _parse_iso(payload["now"])
        if now is None:
            return _invalid("now", "now must be an ISO-8601 timestamp with an explicit offset")
        kind = payload["kind"]
        values = {}
        for field in ("value", "nonce", "current_nonce", "deadline"):
            number = _digits(payload[field])
            if number is None:
                return _invalid(field, f"{field} must be a decimal digit string")
            if number > MAX_UINT256:
                return _invalid(field, f"{field} exceeds uint256")
            values[field] = number
        for field in ("owner", "spender", "token", "connected_account", "domain_verifying_contract"):
            if not _is_address(payload[field]):
                return _invalid(field, f"{field} must be a 0x-prefixed 20-byte hex address")
        for index, entry in enumerate(payload["known_spenders"]):
            if not _is_address(entry):
                return _invalid("known_spenders", f"known_spenders[{index}] is not a 0x-prefixed 20-byte hex address")
        requested = None
        if "requested_amount" in payload:
            requested = _digits(payload["requested_amount"])
            if requested is None or requested > MAX_UINT256:
                return _invalid("requested_amount", "requested_amount must be a decimal digit string within uint256")
        expiration = None
        if "expiration" in payload:
            expiration = _digits(payload["expiration"])
            if expiration is None:
                return _invalid("expiration", "expiration must be a decimal digit string")
            if kind != "permit2":
                return _invalid("expiration", "expiration only exists for kind permit2")
            if expiration > MAX_UINT48:
                return _invalid("expiration", "expiration exceeds uint48")
        if kind == "permit2" and values["value"] > MAX_UINT160:
            return _invalid("value", "a Permit2 amount is a uint160")
        if kind == "permit2":
            for field in ("nonce", "current_nonce"):
                if values[field] > MAX_UINT48:
                    return _invalid(field, "a Permit2 nonce is a uint48")
        max_ttl = payload.get("max_ttl_seconds", DEFAULT_MAX_TTL_S)
        owner, spender = payload["owner"], payload["spender"]
        findings: list[dict[str, str]] = []
        for label, address in (("owner", owner), ("spender", spender), ("token", payload["token"])):
            if _checksum_state(address) == "invalid":
                findings.append(_f("PERMIT-ADDRESS-CHECKSUM", "major", f"{label} has mixed case that fails its EIP-55 checksum"))
        if not _same_address(owner, payload["connected_account"]):
            findings.append(_f("PERMIT-OWNER-MISMATCH", "blocker", "permit owner is not the connected account"))
        if _same_address(spender, ZERO_ADDRESS):
            findings.append(_f("PERMIT-SPENDER-ZERO", "blocker", "spender is the zero address"))
        elif not any(_same_address(spender, known) for known in payload["known_spenders"]):
            findings.append(_f("PERMIT-SPENDER-UNKNOWN", "blocker", "spender is not in the known spender list: unknown spenders with a permit are the standard token-drain pattern"))
        if _same_address(spender, payload["token"]):
            findings.append(_f("PERMIT-SPENDER-IS-TOKEN", "major", "spender equals the token contract"))
        top = MAX_UINT160 if kind == "permit2" else MAX_UINT256
        unlimited = values["value"] >= (top + 1) // 2
        if unlimited:
            findings.append(_f("PERMIT-UNLIMITED", "major", "permit amount is effectively unlimited (at least half of the maximum for its type); sign the exact amount"))
        elif requested is not None and values["value"] > requested:
            findings.append(_f("PERMIT-EXCESS", "minor", "permit amount exceeds the amount the action needs"))
        if requested is not None and values["value"] < requested:
            findings.append(_f("PERMIT-INSUFFICIENT", "blocker", "permit amount is below the amount the action needs: the follow-up call would revert"))
        nonce_matches = values["nonce"] == values["current_nonce"]
        if values["nonce"] < values["current_nonce"]:
            findings.append(_f("PERMIT-NONCE-STALE", "blocker", f"nonce {values['nonce']} is below the on-chain nonce {values['current_nonce']}: the permit is already consumed"))
        elif values["nonce"] > values["current_nonce"]:
            findings.append(_f("PERMIT-NONCE-AHEAD", "blocker", f"nonce {values['nonce']} is above the on-chain nonce {values['current_nonce']}: the contract requires exact equality"))
        remaining = 0
        if _deadline_findings("PERMIT", "deadline", values["deadline"], _epoch(now), max_ttl, findings):
            remaining = _clamp(values["deadline"] - _epoch(now))
        if expiration is not None and expiration > 0 and expiration - _epoch(now) > LONG_ALLOWANCE_S:
            findings.append(_f("PERMIT-ALLOWANCE-LONG", "minor", f"allowance expiration is more than {LONG_ALLOWANCE_S // 86400} days away"))
        if expiration is not None and 0 < expiration < _epoch(now):
            findings.append(_f("PERMIT-ALLOWANCE-EXPIRED", "blocker", "allowance expiration is already in the past"))
        if payload["domain_chain_id"] != payload["expected_chain_id"]:
            findings.append(_f("PERMIT-CHAIN-MISMATCH", "blocker", f"domain chainId {payload['domain_chain_id']} differs from the expected chain {payload['expected_chain_id']}"))
        expected_contract = PERMIT2_ADDRESS if kind == "permit2" else payload["token"]
        if not _same_address(payload["domain_verifying_contract"], expected_contract):
            findings.append(_f("PERMIT-DOMAIN-CONTRACT", "blocker",
                               "domain verifyingContract is not the " + ("canonical Permit2 contract" if kind == "permit2" else "token contract")))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "unlimited": unlimited, "nonce_matches": nonce_matches, "deadline_remaining_seconds": remaining}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 3. SIWE (EIP-4361) ----------------------------------------------------------------------------------------------

_SIWE_HEADER = re.compile(r"^(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://)?(?P<domain>[^\s/?#]+) wants you to sign in with your Ethereum account:$")
_NONCE = re.compile(r"[A-Za-z0-9]{8,}")
_URI_WITHOUT_HOST = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:[^\s/][^\s]*")
_STATEMENT_WORDS = re.compile(r"approve|transfer|permit|allowance|unlimited|spend", re.IGNORECASE)
_LOOPBACK = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
DEFAULT_SIWE_MAX_AGE_S = 600


def _host_only(authority: str) -> str:
    host = authority.lower()
    if host.startswith("["):
        return host.split("]")[0] + "]"
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        if port.isdigit():
            return name
    return host


def siwe_check(payload: dict[str, Any]) -> dict[str, Any]:
    """EIP-4361 Sign-In with Ethereum message: field grammar, domain binding, address, chain, nonce and validity window."""

    def work() -> dict[str, Any]:
        now = _parse_iso(payload["now"])
        if now is None:
            return _invalid("now", "now must be an ISO-8601 timestamp with an explicit offset")
        if "expected_address" in payload and not _is_address(payload["expected_address"]):
            return _invalid("expected_address", "expected_address must be a 0x-prefixed 20-byte hex address")
        max_age = payload.get("max_age_seconds", DEFAULT_SIWE_MAX_AGE_S)
        findings: list[dict[str, str]] = []
        text = payload["message"]
        if "\r" in text:
            findings.append(_f("SIWE-LINE-ENDINGS", "major", "message contains carriage returns; EIP-4361 separates lines with LF only"))
            text = text.replace("\r", "")
        lines = text.split("\n")
        header = _SIWE_HEADER.match(lines[0])
        if header is None:
            return _invalid("message", "first line must be '[scheme://]domain wants you to sign in with your Ethereum account:'")
        if len(lines) < 5 or lines[2] != "":
            return _invalid("message", "line 2 must be the address followed by an empty line (address LF LF)")
        scheme, domain, address = header.group("scheme"), header.group("domain"), lines[1]
        bare_domain = _strip_userinfo(domain)
        if lines[3] == "":
            statement, index = "", 4
        else:
            statement, index = lines[3], 5
            if len(lines) <= 5 or lines[4] != "":
                return _invalid("message", "a statement must be followed by an empty line")
        fields: dict[str, str] = {}
        for label, required in (("URI", True), ("Version", True), ("Chain ID", True), ("Nonce", True), ("Issued At", True),
                                ("Expiration Time", False), ("Not Before", False), ("Request ID", False)):
            prefix = f"{label}: "
            if index < len(lines) and lines[index].startswith(prefix):
                fields[label] = lines[index][len(prefix):]
                index += 1
            elif required:
                return _invalid("message", f"expected the line '{label}: ...' at line {index + 1}")
        resources: list[str] = []
        if index < len(lines) and lines[index] == "Resources:":
            index += 1
            while index < len(lines) and lines[index].startswith("- "):
                resources.append(lines[index][2:])
                index += 1
        if index != len(lines):
            return _invalid("message", f"unexpected content at line {index + 1} (a trailing newline also fails EIP-4361)")

        if domain.lower() != payload["expected_domain"].lower():
            findings.append(_f("SIWE-DOMAIN-MISMATCH", "blocker", f"message domain {bare_domain} is not the expected domain {_strip_userinfo(payload['expected_domain'])}: a phishing page can replay the signature"))
        if "@" in domain:
            findings.append(_f("SIWE-DOMAIN-USERINFO", "major", "message domain carries userinfo before an @: EIP-4361 binds the sign-in to the host, so a credential or look-alike prefix has no place there"))
        if scheme is not None and scheme.lower() != "https" and _host_only(bare_domain) not in _LOOPBACK:
            findings.append(_f("SIWE-SCHEME-INSECURE", "major", f"message scheme {scheme} is not https"))
        checksum = "invalid"
        if not _is_address(address):
            findings.append(_f("SIWE-ADDRESS-MALFORMED", "blocker", "address line is not a 0x-prefixed 20-byte hex address"))
        else:
            checksum = _checksum_state(address)
            if checksum == "invalid":
                findings.append(_f("SIWE-ADDRESS-CHECKSUM", "major", "address mixed case fails its EIP-55 checksum"))
            elif checksum == "none":
                findings.append(_f("SIWE-ADDRESS-NO-CHECKSUM", "minor", "address is not EIP-55 checksummed, which EIP-4361 requires where applicable"))
            if "expected_address" in payload and not _same_address(address, payload["expected_address"]):
                findings.append(_f("SIWE-ADDRESS-MISMATCH", "blocker", "message address is not the connected account"))
        if statement and _STATEMENT_WORDS.search(statement):
            findings.append(_f("SIWE-STATEMENT-ASSETS", "major", "login statement talks about approvals or transfers; a SIWE login must not describe asset permissions"))
        uri_info = support.script("dapp_wallet_check").rpc_url_inspect(fields["URI"])
        if not uri_info["valid"] and _URI_WITHOUT_HOST.fullmatch(fields["URI"]):
            findings.append(_f("SIWE-URI-NO-HOST", "minor", "URI has a scheme but no host, so it cannot be bound to the message domain"))
        elif not uri_info["valid"]:
            findings.append(_f("SIWE-URI-MALFORMED", "major", "URI is not an absolute RFC 3986 URI (scheme ':' ...)"))
        elif uri_info["host"] != _host_only(bare_domain).strip("[]"):
            findings.append(_f("SIWE-URI-HOST", "major", f"URI host {uri_info['host']} differs from the message domain"))
        if fields["Version"] != "1":
            findings.append(_f("SIWE-VERSION", "major", f"Version is {_plain_token(fields['Version'])}; EIP-4361 defines version 1"))
        chain = _digits(fields["Chain ID"])
        if chain is None or chain == 0:
            findings.append(_f("SIWE-CHAIN-MALFORMED", "blocker", "Chain ID is not a positive decimal integer"))
        else:
            if fields["Chain ID"].startswith("0"):
                findings.append(_f("SIWE-CHAIN-LEADING-ZERO", "minor", "Chain ID has leading zeros: the ABNF (1*DIGIT) allows it, but the canonical spelling has none and string comparisons against the stored chain fail"))
            if chain != payload["expected_chain_id"]:
                findings.append(_f("SIWE-CHAIN-MISMATCH", "major", f"Chain ID {chain} differs from the expected chain {payload['expected_chain_id']}"))
        if not _NONCE.fullmatch(fields["Nonce"]):
            findings.append(_f("SIWE-NONCE-WEAK", "major", "Nonce must be at least 8 alphanumeric characters"))
        if "expected_nonce" in payload and fields["Nonce"] != payload["expected_nonce"]:
            findings.append(_f("SIWE-NONCE-MISMATCH", "blocker", "Nonce differs from the server-issued nonce: replay or forged login"))
        issued_age = 0
        issued = _parse_iso(fields["Issued At"]) if _RFC3339.fullmatch(fields["Issued At"]) else None
        if issued is None:
            findings.append(_f("SIWE-ISSUED-MALFORMED", "major", "Issued At is not an RFC 3339 date-time with an offset"))
        else:
            issued_age = _epoch(now) - _epoch(issued)
            if issued_age < -MAX_CLOCK_SKEW_S:
                findings.append(_f("SIWE-ISSUED-FUTURE", "major", f"Issued At is {-issued_age}s in the future (more than {MAX_CLOCK_SKEW_S}s of skew)"))
            elif issued_age > max_age:
                findings.append(_f("SIWE-ISSUED-STALE", "major", f"Issued At is {issued_age}s old, above the {max_age}s limit"))
        expires_in = 0
        expiration = None
        if "Expiration Time" in fields:
            expiration = _parse_iso(fields["Expiration Time"]) if _RFC3339.fullmatch(fields["Expiration Time"]) else None
            if expiration is None:
                findings.append(_f("SIWE-EXPIRATION-MALFORMED", "major", "Expiration Time is not an RFC 3339 date-time with an offset"))
            else:
                expires_in = _epoch(expiration) - _epoch(now)
                if expires_in <= 0:
                    findings.append(_f("SIWE-EXPIRED", "blocker", "Expiration Time has passed: the message is no longer valid"))
                if issued is not None and expiration <= issued:
                    findings.append(_f("SIWE-EXPIRATION-ORDER", "major", "Expiration Time is not after Issued At"))
        else:
            findings.append(_f("SIWE-NO-EXPIRATION", "minor", "no Expiration Time: the signed message stays valid until the server forgets the nonce"))
        if "Not Before" in fields:
            not_before = _parse_iso(fields["Not Before"]) if _RFC3339.fullmatch(fields["Not Before"]) else None
            if not_before is None:
                findings.append(_f("SIWE-NOT-BEFORE-MALFORMED", "major", "Not Before is not an RFC 3339 date-time with an offset"))
            else:
                if _epoch(not_before) > _epoch(now):
                    findings.append(_f("SIWE-NOT-YET-VALID", "blocker", "Not Before is in the future: the message is not valid yet"))
                if expiration is not None and not_before >= expiration:
                    findings.append(_f("SIWE-NOT-BEFORE-ORDER", "major", "Not Before is not before Expiration Time"))
        for position, resource in enumerate(resources, start=1):
            if ":" not in resource or resource.startswith(":") or " " in resource:
                findings.append(_f("SIWE-RESOURCE-MALFORMED", "major", f"resource {position} is not a URI"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "domain": _clip(bare_domain, 255), "address_checksum": checksum, "resource_count": len(resources), "has_expiration": "Expiration Time" in fields,
                "issued_at_valid": issued is not None, "issued_age_seconds": issued_age, "expires_in_seconds": expires_in}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 4. chain configuration (EIP-3085) -------------------------------------------------------------------------------

# chain id -> (network class, accepted native symbols). Source: ethereum-lists/chains (chainid.network).
KNOWN_CHAINS = {
    1: ("mainnet", ("ETH",)), 10: ("mainnet", ("ETH",)), 56: ("mainnet", ("BNB",)), 137: ("mainnet", ("POL", "MATIC")),
    324: ("mainnet", ("ETH",)), 8453: ("mainnet", ("ETH",)), 42161: ("mainnet", ("ETH",)), 43114: ("mainnet", ("AVAX",)),
    59144: ("mainnet", ("ETH",)), 534352: ("mainnet", ("ETH",)),
    97: ("testnet", ("tBNB",)), 43113: ("testnet", ("AVAX",)), 80002: ("testnet", ("POL", "MATIC")), 84532: ("testnet", ("ETH",)),
    421614: ("testnet", ("ETH",)), 11155111: ("testnet", ("ETH",)), 11155420: ("testnet", ("ETH",)), 17000: ("testnet", ("ETH",)),
    1337: ("devnet", ("ETH",)), 31337: ("devnet", ("ETH",)),
}
# normalized chain names that users and wallets take for one specific well-known network -> its chain id (chainid.network names plus common short forms)
WELL_KNOWN_NAMES = {
    "ethereum": 1, "ethereum mainnet": 1, "eth mainnet": 1,
    "optimism": 10, "op mainnet": 10, "optimism mainnet": 10,
    "bnb smart chain": 56, "bnb smart chain mainnet": 56, "binance smart chain": 56, "bsc": 56, "bsc mainnet": 56,
    "polygon": 137, "polygon mainnet": 137, "polygon pos": 137,
    "zksync": 324, "zksync era": 324, "zksync mainnet": 324,
    "base": 8453, "base mainnet": 8453,
    "arbitrum": 42161, "arbitrum one": 42161,
    "avalanche": 43114, "avalanche c-chain": 43114, "avalanche mainnet": 43114,
    "linea": 59144, "linea mainnet": 59144,
    "scroll": 534352, "scroll mainnet": 534352,
}
_TESTNET_WORDS = re.compile(r"\b(?:test\w*|sepolia|goerli|holesky|hoodi|amoy|devnet|fuji)\b", re.IGNORECASE)


def chain_config_check(payload: dict[str, Any]) -> dict[str, Any]:
    """EIP-3085 wallet_addEthereumChain parameters: chain id form, https RPC and explorer URLs without credentials, native currency."""

    def work() -> dict[str, Any]:
        raw = payload["chain_id"]
        findings: list[dict[str, str]] = []
        if raw.startswith("0x") and len(raw) > 2 and _is_hex(raw[2:]):
            chain_id = int(raw[2:], 16)
            if len(raw) > 3 and raw[2] == "0":
                findings.append(_f("CHAIN-ID-LEADING-ZERO", "major", f"chainId {raw} has leading zeros; eth_chainId quantities have none (use {hex(chain_id)})"))
            if raw[2:] != raw[2:].lower():
                findings.append(_f("CHAIN-ID-CASE", "minor", "chainId hex digits should be lowercase"))
        elif raw.isascii() and raw.isdigit():
            chain_id = int(raw)
            findings.append(_f("CHAIN-ID-NOT-HEX", "blocker", f"chainId {raw} is decimal; EIP-3085 requires a 0x-prefixed hexadecimal string (use {hex(chain_id)})"))
        else:
            return _invalid("chain_id", "chain_id must be a 0x-prefixed hexadecimal string")
        if chain_id == 0:
            return _invalid("chain_id", "chain id 0 is not valid")
        if chain_id > MAX_SAFE_CHAIN_ID:
            findings.append(_f("CHAIN-ID-TOO-LARGE", "major", f"chain id exceeds {MAX_SAFE_CHAIN_ID}, which wallets reject"))
        if "expected_chain_id" in payload and payload["expected_chain_id"] != chain_id:
            findings.append(_f("CHAIN-ID-MISMATCH", "blocker", f"chain id {chain_id} differs from the expected chain {payload['expected_chain_id']}"))
        known = KNOWN_CHAINS.get(chain_id)
        network_class = known[0] if known else "unknown"
        name_looks_test = bool(_TESTNET_WORDS.search(payload["chain_name"]))
        if known is None:
            findings.append(_f("CHAIN-UNKNOWN", "info", f"chain id {chain_id} is not in the built-in reference table: verify it against chainlist; until it is listed it is treated as a mainnet and needs L3 approval"))
        else:
            if payload["native_symbol"] not in known[1]:
                findings.append(_f("CHAIN-SYMBOL-MISMATCH", "major", f"native symbol {payload['native_symbol']!r} differs from the registry symbol {'/'.join(known[1])} for chain {chain_id}"))
            if known[0] == "mainnet" and name_looks_test:
                findings.append(_f("CHAIN-NAME-SPOOF", "blocker", f"chain {chain_id} is a mainnet but the name {payload['chain_name']!r} says test network"))
            if known[0] == "testnet" and not name_looks_test:
                findings.append(_f("CHAIN-NAME-NOT-TEST", "minor", f"chain {chain_id} is a test network but the name does not say so"))
        claimed = WELL_KNOWN_NAMES.get(" ".join(payload["chain_name"].lower().split()))
        if claimed is not None and claimed != chain_id:
            findings.append(_f("CHAIN-NAME-IMPERSONATION", "blocker", f"the name claims well-known chain {claimed} but the chain id is {chain_id}: a fake network that borrows a trusted name"))
        mainnet = network_class not in ("testnet", "devnet")  # fail closed: a chain outside the reference table may be a mainnet
        if known is not None and mainnet:
            findings.append(_f("CHAIN-MAINNET", "info", "mainnet chain: enabling it in a client needs L3 approval"))
        if not 1 <= len(payload["native_symbol"]) <= 6:
            findings.append(_f("CHAIN-SYMBOL-LENGTH", "major", "native currency symbol must be 1-6 characters (MetaMask's wallet_addEthereumChain rule)"))
        if payload["native_decimals"] != 18:
            findings.append(_f("CHAIN-DECIMALS", "major", f"native currency has {payload['native_decimals']} decimals; MetaMask rejects wallet_addEthereumChain unless nativeCurrency.decimals is 18"))
        inspect = support.script("dapp_wallet_check").rpc_url_inspect
        rpc_urls = payload["rpc_urls"]
        if not rpc_urls:
            findings.append(_f("CHAIN-RPC-NONE", "blocker", "no RPC URL: the wallet cannot talk to the chain"))
        if len(set(rpc_urls)) != len(rpc_urls):
            findings.append(_f("CHAIN-RPC-DUPLICATE", "minor", "rpc_urls lists the same URL twice"))
        for position, url in enumerate(rpc_urls, start=1):
            info = inspect(url)
            _url_findings(f"CHAIN-RPC-{position}", f"rpc_urls[{position}]", info, chain_id in (1337, 31337), findings)
            if info["valid"] and info["scheme"] in ("ws", "wss"):
                findings.append(_f(f"CHAIN-RPC-{position}-WEBSOCKET", "minor", f"rpc_urls[{position}]: wallet_addEthereumChain expects http(s) RPC URLs"))
        explorers = payload["explorer_urls"]
        if not explorers:
            findings.append(_f("CHAIN-EXPLORER-NONE", "minor", "no block explorer URL: transaction links cannot be shown"))
        for position, url in enumerate(explorers, start=1):
            if _scheme_of(url) in DANGEROUS_SCHEMES:
                findings.append(_f(f"CHAIN-EXPLORER-{position}-DANGEROUS-SCHEME", "blocker", f"explorer_urls[{position}] uses a script or embedded-content scheme; explorer links are shown to users as clickable links"))
                continue
            info = inspect(url)
            for issue in info["issues"]:
                if issue["code"] in ("insecure-scheme", "credentials", "malformed", "scheme"):
                    findings.append(_f(f"CHAIN-EXPLORER-{position}", "major", f"explorer_urls[{position}]: {issue['detail']}"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "chain_id": chain_id, "chain_id_hex": hex(chain_id), "network_class": network_class, "mainnet_enable_requires_approval": mainnet,
                "rpc_count": len(rpc_urls)}

    result: dict[str, Any] = respond(payload, work)
    return result


def _url_findings(prefix: str, label: str, info: dict[str, Any], allow_local: bool, findings: list[dict[str, str]]) -> None:
    """Findings for one inspected RPC URL (see scripts/dapp_wallet_check.py rpc_url_inspect)."""
    for issue in info["issues"]:
        code, detail = issue["code"], issue["detail"]
        if code == "malformed":
            findings.append(_f(f"{prefix}-MALFORMED", "blocker", f"{label}: {detail}"))
        elif code == "scheme":
            findings.append(_f(f"{prefix}-SCHEME", "blocker", f"{label}: {detail}"))
        elif code == "insecure-scheme":
            findings.append(_f(f"{prefix}-INSECURE", "major", f"{label}: {detail}"))
        elif code == "local-endpoint" and not allow_local:
            findings.append(_f(f"{prefix}-LOCAL", "major", f"{label}: {detail}; not reachable by users"))
        elif code == "credentials":
            findings.append(_f(f"{prefix}-CREDENTIALS", "blocker", f"{label}: {detail}"))
        elif code in ("path-key", "query-key"):
            findings.append(_f(f"{prefix}-KEY", "major", f"{label}: {detail}; every user can read it, proxy the endpoint server-side"))


# ---- 5. token approval risk ------------------------------------------------------------------------------------------


def approval_risk(payload: dict[str, Any]) -> dict[str, Any]:
    """Risk of the token approvals a flow asks the user to grant: unlimited allowances, blanket NFT approvals, unknown spenders."""

    def work() -> dict[str, Any]:
        known = []
        for index, entry in enumerate(payload["known_spenders"]):
            if not _is_address(entry):
                return _invalid("known_spenders", f"known_spenders[{index}] is not a 0x-prefixed 20-byte hex address")
            known.append(entry.lower())
        findings: list[dict[str, str]] = []
        unlimited = unknown = blanket = revokes = 0
        for index, item in enumerate(payload["approvals"]):
            kind = item["kind"]
            if not _is_address(item["token"]):
                return _invalid("approvals", f"approvals[{index}].token is not a 0x-prefixed 20-byte hex address")
            if not _is_address(item["spender"]):
                return _invalid("approvals", f"approvals[{index}].spender is not a 0x-prefixed 20-byte hex address")
            amount = None
            if kind in ("erc20_approve", "erc20_increase_allowance", "erc20_permit"):
                amount = _digits(item.get("amount"))
                if amount is None or amount > MAX_UINT256:
                    return _invalid("approvals", f"approvals[{index}].amount must be a decimal digit string within uint256 for {kind}")
            requested = None
            if "requested_amount" in item:
                requested = _digits(item["requested_amount"])
                if requested is None or requested > MAX_UINT256:
                    return _invalid("approvals", f"approvals[{index}].requested_amount must be a decimal digit string within uint256")
            tag = f"approvals[{index}]"
            spender, token = item["spender"], item["token"]
            for label, address in (("token", token), ("spender", spender)):
                if _checksum_state(address) == "invalid":
                    findings.append(_f("APPROVAL-ADDRESS-CHECKSUM", "major", f"{tag}.{label} has mixed case that fails its EIP-55 checksum"))
            if _same_address(token, ZERO_ADDRESS):
                findings.append(_f("APPROVAL-TOKEN-ZERO", "major", f"{tag}.token is the zero address"))
            is_revoke = amount == 0 if amount is not None else (item.get("approved", True) is False or (kind == "erc721_approve" and _same_address(spender, ZERO_ADDRESS)))
            if is_revoke:
                revokes += 1
                findings.append(_f("APPROVAL-REVOKE", "info", f"{tag} revokes an approval"))
                continue
            if _same_address(spender, ZERO_ADDRESS):
                findings.append(_f("APPROVAL-SPENDER-ZERO", "blocker", f"{tag}.spender is the zero address"))
                continue
            is_known = spender.lower() in known
            if not is_known:
                unknown += 1
            if amount is None:
                blanket += kind == "nft_set_approval_for_all"
                findings.append(_f("APPROVAL-NFT-BLANKET" if kind == "nft_set_approval_for_all" else "APPROVAL-NFT-SINGLE",
                                   "blocker" if not is_known else "major" if kind == "nft_set_approval_for_all" else "info",
                                   f"{tag} {'approves every token of the collection' if kind == 'nft_set_approval_for_all' else 'approves one NFT'} for "
                                   f"{'an unknown' if not is_known else 'a known'} spender"))
                continue
            if amount >= UNLIMITED_THRESHOLD:
                unlimited += 1
                findings.append(_f("APPROVAL-UNLIMITED", "blocker" if not is_known else "major",
                                   f"{tag} grants an effectively unlimited allowance to {'an unknown' if not is_known else 'a known'} spender; approve the exact amount"))
            else:
                if not is_known:
                    findings.append(_f("APPROVAL-SPENDER-UNKNOWN", "blocker", f"{tag} grants an allowance to a spender that is not in the known spender list"))
                if requested is not None and amount > requested:
                    findings.append(_f("APPROVAL-EXCESS", "minor", f"{tag} allowance exceeds the {requested} the action needs"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "approval_count": len(payload["approvals"]), "unlimited_count": unlimited, "unknown_spender_count": unknown,
                "nft_blanket_count": blanket, "revoke_count": revokes}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 6. transaction state machine ------------------------------------------------------------------------------------

TX_TRANSITIONS = {
    "draft": ("simulating",),
    "simulating": ("simulated", "simulation_failed"),
    "simulated": ("awaiting_confirmation", "draft"),
    "awaiting_confirmation": ("user_confirmed", "rejected", "draft"),
    "user_confirmed": ("awaiting_signature",),
    "awaiting_signature": ("submitted", "rejected"),
    "submitted": ("confirmed", "reverted", "replaced", "dropped"),
    "confirmed": ("finalized", "reorged"),
    "reorged": ("submitted", "confirmed", "dropped", "replaced"),
    "simulation_failed": (), "rejected": (), "reverted": (), "replaced": (), "dropped": (), "finalized": (),
}
TX_STATES = tuple(sorted(TX_TRANSITIONS))
TX_TERMINAL = frozenset(state for state, nexts in TX_TRANSITIONS.items() if not nexts)
TX_NEEDS_HASH = frozenset({"submitted", "confirmed", "reverted", "replaced", "dropped", "reorged", "finalized"})
TX_SIGNING = frozenset({"awaiting_signature", "submitted", "confirmed", "reverted", "replaced", "dropped", "reorged", "finalized"})
DEFAULT_STUCK_AFTER_S = 600
DEFAULT_REQUIRED_CONFIRMATIONS = 1
_TX_HASH = re.compile(r"0x[0-9a-fA-F]{64}")


def tx_state_machine(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a transaction lifecycle history against the allowed transitions, the simulate-then-confirm policy and stuck handling."""

    def work() -> dict[str, Any]:
        now = _parse_iso(payload["now"])
        if now is None:
            return _invalid("now", "now must be an ISO-8601 timestamp with an explicit offset")
        stuck_after = payload.get("stuck_after_seconds", DEFAULT_STUCK_AFTER_S)
        required = payload.get("required_confirmations", DEFAULT_REQUIRED_CONFIRMATIONS)
        events = payload["events"]
        stamps = []
        for index, event in enumerate(events):
            stamp = _parse_iso(event["at"])
            if stamp is None:
                return _invalid("events", f"events[{index}].at must be an ISO-8601 timestamp with an explicit offset")
            stamps.append(stamp)
        if _epoch(now) < max(_epoch(s) for s in stamps):
            return _invalid("now", "now precedes the latest event timestamp")
        findings: list[dict[str, str]] = []
        invalid_transitions = 0
        seen_simulated = seen_confirmed = False
        policy_breach = False
        first_hash = ""
        if events[0]["state"] != "draft":
            findings.append(_f("TX-HISTORY-INCOMPLETE", "major", f"history starts at {events[0]['state']}, not draft: earlier steps are not evidenced"))
        for index, event in enumerate(events):
            state = event["state"]
            if index:
                before = events[index - 1]["state"]
                if state not in TX_TRANSITIONS[before]:
                    invalid_transitions += 1
                    findings.append(_f("TX-TRANSITION-INVALID", "blocker",
                                       f"events[{index}]: {before} -> {state} is not allowed" + (" (after a terminal state)" if before in TX_TERMINAL else "")))
                if _epoch(stamps[index]) < _epoch(stamps[index - 1]):
                    findings.append(_f("TX-TIME-ORDER", "major", f"events[{index}] is dated before events[{index - 1}]"))
            if state == "draft":
                seen_simulated = seen_confirmed = False
            elif state == "simulated":
                seen_simulated = True
            elif state == "user_confirmed":
                seen_confirmed = True
            if state in TX_SIGNING and not (seen_simulated and seen_confirmed) and not policy_breach:
                policy_breach = True
                findings.append(_f("TX-POLICY-NO-SIMULATION-CONFIRMATION", "blocker",
                                   f"events[{index}] ({state}) is reached without both a simulation and an explicit user confirmation"))
            tx_hash = event.get("tx_hash", "")
            if state in TX_NEEDS_HASH:
                if not tx_hash:
                    findings.append(_f("TX-HASH-MISSING", "major", f"events[{index}] ({state}) carries no tx_hash"))
                elif not _TX_HASH.fullmatch(tx_hash):
                    findings.append(_f("TX-HASH-MALFORMED", "major", f"events[{index}].tx_hash is not a 0x-prefixed 32-byte hash"))
                elif not first_hash:
                    first_hash = tx_hash.lower()
                elif tx_hash.lower() != first_hash:
                    findings.append(_f("TX-HASH-CHANGED", "major", f"events[{index}].tx_hash differs from the submitted hash without a replacement"))
            if state == "replaced":
                replacement = event.get("replaced_by", "")
                if not _TX_HASH.fullmatch(replacement):
                    findings.append(_f("TX-REPLACEMENT-MISSING", "major", f"events[{index}] is replaced without a valid replaced_by hash to track"))
                elif replacement.lower() == tx_hash.lower():
                    findings.append(_f("TX-REPLACEMENT-SAME", "major", f"events[{index}].replaced_by equals the replaced hash"))
            if state in ("confirmed", "finalized", "reorged"):
                confirmations = event.get("confirmations")
                if state == "finalized" and confirmations is None:
                    findings.append(_f("TX-CONFIRMATIONS-MISSING", "minor", f"events[{index}] is finalized without a confirmation count"))
                elif state == "finalized" and confirmations < required:
                    findings.append(_f("TX-FINALIZED-EARLY", "major", f"events[{index}] is finalized with {confirmations} confirmations, below the required {required}"))
        last = events[-1]["state"]
        pending_seconds = -1
        if last in ("submitted", "reorged"):
            pending_seconds = _epoch(now) - _epoch(stamps[-1])
            if pending_seconds >= stuck_after:
                findings.append(_f("TX-STUCK", "major",
                                   f"{last} for {pending_seconds}s (limit {stuck_after}s): offer speed-up or cancel with the same nonce and fees raised at least 10% "
                                   "(maxFeePerGas and maxPriorityFeePerGas), the default replacement rule of geth-style mempools"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "final_state": last, "terminal": last in TX_TERMINAL, "valid": blockers == 0, "transition_count": len(events) - 1,
                "invalid_transition_count": invalid_transitions, "pending_seconds": pending_seconds, "allowed_next": sorted(TX_TRANSITIONS[last])}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 7. ERC-4337 UserOperation ---------------------------------------------------------------------------------------

GAS_VALUE_LIMIT = 2**120  # EntryPoint v0.6.0 and v0.7.0: maxGasValues <= type(uint120).max else AA94 "gas values overflow"


def _hex_quantity(text: str) -> int | None:
    if text.startswith("0x") and len(text) > 2 and _is_hex(text[2:]):
        return int(text[2:], 16)
    return None


def _hex_bytes(text: str) -> bytes | None:
    if text.startswith("0x") and len(text) % 2 == 0 and _is_hex(text[2:]):
        return bytes.fromhex(text[2:])
    return None


def userop_check(payload: dict[str, Any]) -> dict[str, Any]:
    """ERC-4337 UserOperation coherence for EntryPoint v0.6/v0.7: fee ordering, gas limits, calldata floor, init code, paymaster, prefund."""

    def work() -> dict[str, Any]:
        version = payload["entry_point_version"]
        if not _is_address(payload["sender"]):
            return _invalid("sender", "sender must be a 0x-prefixed 20-byte hex address")
        quantities = {}
        for field in ("nonce", "call_gas_limit", "verification_gas_limit", "pre_verification_gas", "max_fee_per_gas", "max_priority_fee_per_gas"):
            number = _hex_quantity(payload[field])
            if number is None or number > MAX_UINT256:
                return _invalid(field, f"{field} must be a 0x-prefixed hex quantity within uint256")
            quantities[field] = number
        blobs = {}
        for field in ("init_code", "call_data", "paymaster_and_data", "signature"):
            data = _hex_bytes(payload[field])
            if data is None:
                return _invalid(field, f"{field} must be an even-length 0x-prefixed hex string")
            blobs[field] = data
        available = None
        if "prefund_available_wei" in payload:
            available = _digits(payload["prefund_available_wei"])
            if available is None:
                return _invalid("prefund_available_wei", "prefund_available_wei must be a decimal digit string")
        findings: list[dict[str, str]] = []
        if _same_address(payload["sender"], ZERO_ADDRESS):
            findings.append(_f("UOP-SENDER-ZERO", "blocker", "sender is the zero address"))
        if quantities["max_priority_fee_per_gas"] > quantities["max_fee_per_gas"]:
            findings.append(_f("UOP-FEE-ORDER", "major", "maxPriorityFeePerGas exceeds maxFeePerGas: bundlers reject the operation"))
        if quantities["max_fee_per_gas"] == 0:
            findings.append(_f("UOP-FEE-ZERO", "major", "maxFeePerGas is zero: no bundler will include the operation"))
        if quantities["verification_gas_limit"] == 0:
            findings.append(_f("UOP-VERIFICATION-GAS-ZERO", "blocker", "verificationGasLimit is zero: account validation cannot run"))
        if quantities["call_gas_limit"] == 0 and blobs["call_data"]:
            findings.append(_f("UOP-CALL-GAS-ZERO", "major", "callGasLimit is zero although callData is set: the execution call runs out of gas"))
        if not blobs["call_data"] and not blobs["init_code"]:
            findings.append(_f("UOP-EMPTY", "minor", "no callData and no initCode: the operation does nothing"))
        if not blobs["signature"]:
            findings.append(_f("UOP-SIGNATURE-EMPTY", "major", "signature is empty (acceptable only for gas estimation with a dummy signature)"))
        deploys = bool(blobs["init_code"])
        if deploys:
            if len(blobs["init_code"]) < 20:
                findings.append(_f("UOP-INIT-CODE-SHORT", "blocker", "initCode is shorter than the 20-byte factory address"))
            if quantities["nonce"] & (2**64 - 1):
                findings.append(_f("UOP-INIT-NONCE", "major", "initCode is set but the nonce sequence (low 64 bits) is not zero: the account cannot be deployed and the EntryPoint reverts with AA25"))
        paymaster_len = len(blobs["paymaster_and_data"])
        paymaster = paymaster_len > 0
        min_paymaster = 20 if version == "0.6" else 52
        pm_verification = pm_post_op = 0
        if paymaster:
            if paymaster_len < min_paymaster:
                findings.append(_f("UOP-PAYMASTER-SHORT", "blocker",
                                   f"paymasterAndData is {paymaster_len} bytes; v{version} needs the 20-byte paymaster address" + (" plus two 16-byte gas limits (52 bytes)" if version == "0.7" else "")))
            else:
                if blobs["paymaster_and_data"][:20] == b"\x00" * 20:
                    findings.append(_f("UOP-PAYMASTER-ZERO", "blocker", "paymaster address is the zero address"))
                if version == "0.7":
                    pm_verification = int.from_bytes(blobs["paymaster_and_data"][20:36], "big")
                    pm_post_op = int.from_bytes(blobs["paymaster_and_data"][36:52], "big")
                    if pm_verification == 0:
                        findings.append(_f("UOP-PAYMASTER-VERIFICATION-GAS-ZERO", "major", "paymasterVerificationGasLimit is zero: validatePaymasterUserOp cannot run"))
        gas_values = {name: value for name, value in quantities.items() if name != "nonce"}
        if version == "0.7":
            gas_values.update(paymaster_verification_gas_limit=pm_verification, paymaster_post_op_gas_limit=pm_post_op)
        for field in sorted(gas_values):
            if gas_values[field] >= GAS_VALUE_LIMIT:
                findings.append(_f("UOP-GAS-OVERFLOW", "blocker", f"{field} is at or above 2^120: EntryPoint v{version} rejects the operation with AA94 (gas values overflow)"))
        floor = sum(4 if byte == 0 else 16 for key in ("init_code", "call_data", "paymaster_and_data", "signature") for byte in blobs[key])
        if quantities["pre_verification_gas"] < floor:
            findings.append(_f("UOP-PRE-VERIFICATION-GAS", "blocker",
                               f"preVerificationGas {quantities['pre_verification_gas']} is below the {floor} gas of the operation's own calldata bytes (4 per zero byte, 16 per non-zero byte)"))
        if version == "0.6":
            multiplier = 3 if paymaster else 1
            required_gas = quantities["call_gas_limit"] + quantities["verification_gas_limit"] * multiplier + quantities["pre_verification_gas"]
        else:
            required_gas = (quantities["verification_gas_limit"] + quantities["call_gas_limit"] + pm_verification + pm_post_op
                            + quantities["pre_verification_gas"])
        prefund = required_gas * quantities["max_fee_per_gas"]
        if available is not None and available < prefund:
            payer = "paymaster deposit" if paymaster else "account deposit plus balance"
            findings.append(_f("UOP-PREFUND-SHORT", "blocker", f"required prefund {prefund} wei exceeds the {available} wei available in the {payer}"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "required_prefund_wei": str(prefund), "calldata_gas_floor": floor, "paymaster_present": paymaster, "deploys_account": deploys}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 8. provider error mapping ---------------------------------------------------------------------------------------

# code -> (category, standard, message class, retryable, suggested action)
ERROR_CODES = {
    4001: ("user_rejected", "EIP-1193", "info", False, "reset_to_confirm_step"),
    4100: ("unauthorized", "EIP-1193", "warning", True, "request_accounts_then_retry"),
    4200: ("unsupported_method", "EIP-1193", "error", False, "hide_or_fallback_feature"),
    4900: ("disconnected", "EIP-1193", "error", True, "reconnect_wallet"),
    4901: ("chain_disconnected", "EIP-1193", "error", True, "reconnect_or_switch_chain"),
    4902: ("chain_not_added", "MetaMask", "warning", True, "add_chain_then_switch"),
    -32700: ("parse_error", "JSON-RPC 2.0", "error", False, "report_bug"),
    -32600: ("invalid_request", "JSON-RPC 2.0", "error", False, "report_bug"),
    -32601: ("method_not_found", "JSON-RPC 2.0", "error", False, "hide_or_fallback_feature"),
    -32602: ("invalid_params", "JSON-RPC 2.0", "error", False, "report_bug"),
    -32603: ("internal_error", "JSON-RPC 2.0", "error", True, "retry_with_backoff_or_fallback_rpc"),
    -32000: ("server_error", "EIP-1474", "error", False, "show_message"),
    -32001: ("resource_not_found", "EIP-1474", "error", False, "check_block_or_hash"),
    -32002: ("resource_unavailable", "EIP-1474", "info", False, "open_wallet_pending_request"),
    -32003: ("transaction_rejected", "EIP-1474", "error", False, "show_message"),
    -32004: ("method_not_supported", "EIP-1474", "error", False, "hide_or_fallback_feature"),
    -32005: ("rate_limited", "EIP-1474", "warning", True, "retry_with_backoff_or_fallback_rpc"),
    -32006: ("json_rpc_version_not_supported", "EIP-1474", "error", False, "report_bug"),
    3: ("execution_reverted", "geth", "error", False, "show_revert_reason"),
}
GENERIC_CODES = frozenset({-32000, -32003, -32602, -32603, 3})
# ordered (first match wins) message fragments -> (category, message class, retryable, action)
MESSAGE_PATTERNS = (
    ("execution reverted", ("execution_reverted", "error", False, "show_revert_reason")),  # first: a revert reason is contract text, never a node error
    ("user rejected", ("user_rejected", "info", False, "reset_to_confirm_step")),
    ("user denied", ("user_rejected", "info", False, "reset_to_confirm_step")),
    ("rejected the request", ("user_rejected", "info", False, "reset_to_confirm_step")),
    ("insufficient funds", ("insufficient_funds", "error", False, "reduce_value_or_fund_account")),
    ("nonce too low", ("nonce_too_low", "error", True, "refresh_nonce_and_resubmit")),
    ("nonce too high", ("nonce_gap", "error", True, "fill_nonce_gap")),
    ("replacement transaction underpriced", ("replacement_underpriced", "warning", True, "raise_fees_by_10_percent")),
    ("already known", ("already_known", "info", False, "track_existing_transaction")),
    ("known transaction", ("already_known", "info", False, "track_existing_transaction")),
    ("max fee per gas less than block base fee", ("fee_too_low", "warning", True, "refresh_fees_and_resubmit")),
    ("fee cap less than block base fee", ("fee_too_low", "warning", True, "refresh_fees_and_resubmit")),
    ("transaction underpriced", ("fee_too_low", "warning", True, "refresh_fees_and_resubmit")),
    ("intrinsic gas too low", ("gas_too_low", "error", True, "re_estimate_gas")),
    ("exceeds block gas limit", ("gas_limit_exceeded", "error", False, "reduce_gas_limit")),
    ("gas required exceeds allowance", ("gas_too_low", "error", True, "re_estimate_gas")),
    ("out of gas", ("gas_too_low", "error", True, "re_estimate_gas")),
    ("rate limit", ("rate_limited", "warning", True, "retry_with_backoff_or_fallback_rpc")),
    ("too many requests", ("rate_limited", "warning", True, "retry_with_backoff_or_fallback_rpc")),
)
PANIC_CODES = {
    0x00: "generic compiler panic", 0x01: "assert failed", 0x11: "arithmetic overflow or underflow", 0x12: "division or modulo by zero",
    0x21: "invalid enum conversion", 0x22: "corrupted storage byte array", 0x31: "pop on an empty array", 0x32: "array index out of bounds",
    0x41: "out of memory", 0x51: "call to a zero-initialized function pointer",
}
MAX_REASON_CHARS = 256


def _printable(text: str) -> str:
    cleaned = "".join(c if c.isprintable() else "?" for c in text)
    return cleaned[:MAX_REASON_CHARS]


def _decode_revert(data: bytes) -> tuple[str, str]:
    """(kind, reason) of a revert payload: empty, error_string, panic, custom or malformed. Reasons come from chain data: sanitized."""
    if not data:
        return "empty", "execution reverted with no data"
    selector = data[:4].hex()
    if selector == "08c379a0":
        body = data[4:]
        if len(body) >= 64 and int.from_bytes(body[:32], "big") == 32:
            length = int.from_bytes(body[32:64], "big")
            if length <= len(body) - 64:
                try:
                    return "error_string", _printable(body[64:64 + length].decode("utf-8"))
                except UnicodeDecodeError:
                    pass
        return "malformed", "Error(string) payload does not decode"
    if selector == "4e487b71":
        body = data[4:]
        if len(body) == 32:
            code = int.from_bytes(body, "big")
            return "panic", f"Panic(0x{code:02x}): {PANIC_CODES.get(code, 'unknown panic code')}"
        return "malformed", "Panic(uint256) payload is not 32 bytes"
    if len(data) >= 4:
        return "custom", f"custom error 0x{selector}"
    return "malformed", f"revert data of {len(data)} bytes has no selector"


def _has_fragment(text: str, fragment: str) -> bool:
    """True when fragment starts at a word boundary in text, so 'known transaction' does not match inside 'unknown transaction'."""
    return re.search(r"(?<![a-z0-9])" + re.escape(fragment), text) is not None


def provider_error_map(payload: dict[str, Any]) -> dict[str, Any]:
    """Map an EIP-1193 / JSON-RPC provider error to a UX category, message class and action; decode Solidity revert data."""

    def work() -> dict[str, Any]:
        code = payload["code"]
        message = payload.get("message", "")
        revert_kind, revert_reason = "", ""
        if "revert_data" in payload:
            data = _hex_bytes(payload["revert_data"])
            if data is None:
                return _invalid("revert_data", "revert_data must be an even-length 0x-prefixed hex string")
            revert_kind, revert_reason = _decode_revert(data)
        row = ERROR_CODES.get(code)
        if row is None and -32099 <= code <= -32000:
            row = ("server_error", "EIP-1474", "error", False, "show_message")
        category, standard, klass, retryable, action = row if row else ("unknown", "none", "error", False, "show_message")
        matched = "code" if row else "none"
        if (row is None or code in GENERIC_CODES or row[0] == "server_error") and message:
            lowered = message.lower()
            for fragment, (cat, cls, retry, act) in MESSAGE_PATTERNS:
                if _has_fragment(lowered, fragment):
                    if cat != category:
                        matched = "message"
                    category, klass, retryable, action = cat, cls, retry, act
                    break
        return {"state": "SUCCESS", "category": category, "standard": standard, "message_class": klass, "retryable": retryable,
                "suggested_action": action, "show_error_toast": klass in ("warning", "error"), "matched_by": matched,
                "revert_kind": revert_kind, "revert_reason": revert_reason}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 9. RPC fallback configuration -----------------------------------------------------------------------------------

MIN_TIMEOUT_MS = 1000
MAX_TIMEOUT_MS = 30000
MAX_RETRIES = 3


def rpc_fallback_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Client-side RPC fallback configuration: failure-domain diversity, quorum, timeouts, retries and worst-case time to failure."""

    def work() -> dict[str, Any]:
        providers = payload["providers"]
        quorum, backoff, budget = payload["quorum"], payload["backoff_ms"], payload["ui_budget_ms"]
        inspect = support.script("dapp_wallet_check").rpc_url_inspect
        findings: list[dict[str, str]] = []
        hosts: list[str] = []
        total_weight = 0
        worst = 0
        for position, provider in enumerate(providers, start=1):
            info = inspect(provider["url"])
            if not info["valid"]:
                return _invalid("providers", f"providers[{position - 1}].url is not an absolute URL with a host")
            hosts.append(info["host"])
            total_weight += provider.get("weight", 1)
            worst += provider["timeout_ms"] * (provider["retries"] + 1) + backoff * provider["retries"]
            _url_findings(f"RPC-{position}", f"providers[{position - 1}].url", info, False, findings)
            if provider["timeout_ms"] < MIN_TIMEOUT_MS:
                findings.append(_f("RPC-TIMEOUT-SHORT", "minor", f"providers[{position - 1}] timeout {provider['timeout_ms']}ms is under {MIN_TIMEOUT_MS}ms: healthy but slow responses (eth_getLogs) will fail over"))
            if provider["timeout_ms"] > MAX_TIMEOUT_MS:
                findings.append(_f("RPC-TIMEOUT-LONG", "major", f"providers[{position - 1}] timeout {provider['timeout_ms']}ms is over {MAX_TIMEOUT_MS}ms: the UI hangs on a dead provider"))
            if provider["retries"] > MAX_RETRIES:
                findings.append(_f("RPC-RETRIES-HIGH", "minor", f"providers[{position - 1}] retries {provider['retries']} (more than {MAX_RETRIES}): fail over instead of hammering one provider"))
        distinct = len(set(hosts))
        if len(providers) == 1:
            findings.append(_f("RPC-NO-FALLBACK", "major", "a single provider is a single point of failure"))
        elif distinct < 2:
            findings.append(_f("RPC-SAME-HOST", "major", "every provider URL has the same host: one outage takes them all down"))
        elif distinct < len(providers):
            findings.append(_f("RPC-DUPLICATE-HOST", "minor", f"{len(providers)} providers share only {distinct} distinct hosts"))
        if distinct >= 2 and len({_registrable(host) for host in hosts}) < 2:
            findings.append(_f("RPC-SAME-DOMAIN", "major", "every provider URL is under one registrable domain: one operator or DNS outage takes them all down"))
        majority = quorum * 2 > total_weight
        if quorum > total_weight:
            findings.append(_f("RPC-QUORUM-UNREACHABLE", "blocker", f"quorum {quorum} exceeds the total provider weight {total_weight}: no read can ever succeed"))
        elif not majority:
            findings.append(_f("RPC-QUORUM-NO-MAJORITY", "minor", f"quorum {quorum} of weight {total_weight} is not a majority: two disagreeing groups can both reach quorum"))
        if worst > budget:
            findings.append(_f("RPC-WORST-CASE", "major", f"sequential failover can take {worst}ms, above the {budget}ms UI budget"))
        stall = payload.get("stall_timeout_ms")
        if stall is not None and stall >= min(p["timeout_ms"] for p in providers):
            findings.append(_f("RPC-STALL", "minor", f"stall timeout {stall}ms is not below the shortest provider timeout: hedged requests never start in time"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "provider_count": len(providers), "distinct_hosts": distinct, "total_weight": total_weight, "worst_case_ms": worst, "majority_quorum": majority}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 10. wallet capability matrix ------------------------------------------------------------------------------------

_UUID_V4 = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}")
_RDNS = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)+")
DISCOURAGED_METHODS = {"eth_sign": ("blocker", "signs an arbitrary hash blind and is the basis of wallet-drain phishing"),
                       "eth_signTypedData": ("minor", "is the legacy v1 typed-data format"),
                       "eth_signTypedData_v3": ("minor", "lacks array and recursive-struct support of v4")}


def wallet_matrix(payload: dict[str, Any]) -> dict[str, Any]:
    """Capability matrix of candidate wallets against required RPC methods and chains, with EIP-6963 announcement validation."""

    def work() -> dict[str, Any]:
        wallets = payload["wallets"]
        required_methods, required_chains = payload["required_methods"], payload["required_chain_ids"]
        findings: list[dict[str, str]] = []
        for method in sorted(set(required_methods)):
            if method in DISCOURAGED_METHODS:
                severity, why = DISCOURAGED_METHODS[method]
                findings.append(_f("WALLET-METHOD-DISCOURAGED", severity, f"required method {method} {why}"))
        names = [w["name"] for w in wallets]
        for name in sorted({n for n in names if names.count(n) > 1}):
            findings.append(_f("WALLET-NAME-DUPLICATE", "minor", f"wallet name {name!r} appears more than once"))
        uuids = [w["uuid"] for w in wallets if w.get("uuid")]
        rdns = [w["rdns"] for w in wallets if w.get("rdns")]
        for value in sorted({u for u in uuids if uuids.count(u) > 1}):
            shown = value if _UUID_V4.fullmatch(value) else f"of {len(value)} characters"  # only a well-formed uuid is echoed: free text can be a secret
            findings.append(_f("WALLET-6963-UUID-DUPLICATE", "major", f"EIP-6963 uuid {shown} is announced by more than one wallet"))
        for value in sorted({r for r in rdns if rdns.count(r) > 1}):
            shown = value if _RDNS.fullmatch(value) else f"of {len(value)} characters"
            findings.append(_f("WALLET-6963-RDNS-DUPLICATE", "minor", f"EIP-6963 rdns {shown} is announced by more than one wallet"))
        compatible: list[str] = []
        method_count = {m: 0 for m in sorted(set(required_methods))}
        chain_count = {c: 0 for c in sorted(set(required_chains))}
        for wallet in sorted(wallets, key=lambda w: w["name"]):
            name = wallet["name"]
            wc = wallet["walletconnect"]
            reachable = wallet["eip1193"] or wc != "none"
            usable = wallet["eip1193"] or wc == "v2"
            if not reachable:
                findings.append(_f("WALLET-UNREACHABLE", "blocker", f"{name} exposes neither an EIP-1193 provider nor WalletConnect"))
            if wc == "v1":
                if wallet["eip1193"]:
                    findings.append(_f("WALLET-WALLETCONNECT-V1", "minor", f"{name} also lists WalletConnect v1, whose relay has been shut down: do not offer that path"))
                else:
                    findings.append(_f("WALLET-WALLETCONNECT-V1", "major", f"{name} only offers WalletConnect v1, whose relay has been shut down"))
            if wallet["eip1193"] and not wallet["announced_6963"]:
                findings.append(_f("WALLET-LEGACY-INJECTION", "minor", f"{name} is not announced through EIP-6963: it can only be reached via window.ethereum, which wallets overwrite each other on"))
            if wallet["announced_6963"]:
                if not wallet.get("uuid") or not _UUID_V4.fullmatch(wallet["uuid"]):
                    findings.append(_f("WALLET-6963-UUID", "major", f"{name}: EIP-6963 uuid must be a UUIDv4"))
                if not wallet.get("rdns") or not _RDNS.fullmatch(wallet["rdns"]):
                    findings.append(_f("WALLET-6963-RDNS", "major", f"{name}: EIP-6963 rdns must be a reverse-DNS name with at least two labels, such as io.metamask"))
                if not wallet.get("icon", "").startswith("data:image/"):
                    findings.append(_f("WALLET-6963-ICON", "major", f"{name}: EIP-6963 icon must be an RFC 2397 image data URI"))
            supported = set(wallet["methods"])
            missing = sorted(m for m in method_count if m not in supported)
            for method in method_count:
                method_count[method] += method in supported
            missing_chains = sorted(c for c in chain_count if c not in set(wallet["chain_ids"]))
            for chain in chain_count:
                chain_count[chain] += chain in set(wallet["chain_ids"])
            if missing:
                findings.append(_f("WALLET-METHODS-MISSING", "major", f"{name} lacks required methods: {', '.join(missing)}"))
            if missing_chains:
                findings.append(_f("WALLET-CHAINS-MISSING", "major", f"{name} lacks required chains: {', '.join(str(c) for c in missing_chains)}"))
            if usable and not missing and not missing_chains:
                compatible.append(name)
        if wallets and not compatible:
            findings.append(_f("WALLET-NONE-COMPATIBLE", "major", "no listed wallet satisfies every required method and chain"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "wallet_count": len(wallets), "compatible_count": len(compatible), "compatible_wallets": compatible,
                "method_coverage": [{"method": m, "wallet_count": n} for m, n in method_count.items()],
                "chain_coverage": [{"chain_id": c, "wallet_count": n} for c, n in chain_count.items()]}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 11. ENS names ---------------------------------------------------------------------------------------------------

_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Cs", "Co", "Cn", "Zs", "Zl", "Zp"})
_ASCII_LABEL = re.compile(r"[a-z0-9_-]+")
MIN_ETH_LABEL = 3


_SCRIPT_ALIASES = {"CJK": "HAN", "IDEOGRAPHIC": "HAN"}
# UTS #39 section 5.1 augmented script sets: Han with Hiragana and Katakana (Japanese), Han with Hangul (Korean), Han with Bopomofo (Chinese)
_SCRIPT_GROUPS = (frozenset({"HAN", "HIRAGANA", "KATAKANA"}), frozenset({"HAN", "HANGUL"}), frozenset({"HAN", "BOPOMOFO"}))


def _scripts_of(label: str) -> set[str]:
    found = set()
    for ch in label:
        if ch.isalpha():
            words = unicodedata.name(ch, "").split(" ")
            first = words[1] if words[0] in ("FULLWIDTH", "HALFWIDTH") and len(words) > 1 else words[0]
            found.add(_SCRIPT_ALIASES.get(first, first or "UNKNOWN"))
    return found


def _mixed_scripts(scripts: set[str]) -> bool:
    return len(scripts) > 1 and not any(scripts <= group for group in _SCRIPT_GROUPS)


def _joins_emoji(label: str, index: int) -> bool:
    """True for a zero-width joiner between two emoji (ENSIP-15 allows U+200D only inside emoji sequences; approximated by Unicode category)."""
    if label[index] != "\u200d" or index == 0 or index == len(label) - 1:
        return False
    return unicodedata.category(label[index - 1]) in ("So", "Sk", "Mn") and unicodedata.category(label[index + 1]) == "So"


def ens_name_check(payload: dict[str, Any]) -> dict[str, Any]:
    """ENS name hygiene: normalization form, homograph and invisible-character checks, EIP-137 namehash, reverse-record forward verification."""

    def work() -> dict[str, Any]:
        name = payload["name"]
        address = payload.get("address")
        forward = payload.get("forward_address")
        for field, value in (("address", address), ("forward_address", forward)):
            if value is not None and not _is_address(value):
                return _invalid(field, f"{field} must be a 0x-prefixed 20-byte hex address")
        findings: list[dict[str, str]] = []
        labels = name.split(".")
        ascii_only = name.isascii()
        if any(label == "" for label in labels):
            findings.append(_f("ENS-EMPTY-LABEL", "blocker", "name has an empty label (leading, trailing or doubled dot)"))
        normalized = name.lower() if ascii_only else unicodedata.normalize("NFC", name)
        if ascii_only:
            if name != name.lower():
                findings.append(_f("ENS-NOT-NORMALIZED", "major", f"name has uppercase letters; normalize to {name.lower()!r} before hashing, or it resolves a different node"))
            for label in labels:
                lowered = label.lower()
                if lowered and not _ASCII_LABEL.fullmatch(lowered):
                    findings.append(_f("ENS-FORBIDDEN-CHARACTER", "blocker", f"label {lowered!r} contains characters outside a-z, 0-9, '-' and '_'"))
                if lowered.lstrip("_") != lowered.replace("_", ""):
                    findings.append(_f("ENS-UNDERSCORE", "major", f"label {lowered!r} has an underscore after its first character; ENSIP-15 allows underscores only at the start"))
                if lowered.startswith("xn--"):
                    findings.append(_f("ENS-PUNYCODE", "major", f"label {lowered!r} is punycode; ENS names are normalized Unicode, not punycode"))
                elif len(lowered) >= 4 and lowered[2:4] == "--":
                    findings.append(_f("ENS-LABEL-EXTENSION", "major", f"label {lowered!r} has hyphens at positions 3 and 4 (reserved, ENSIP-15)"))
            if len(labels) >= 2 and labels[-1].lower() == "eth" and labels[-2] and len(labels[-2]) < MIN_ETH_LABEL:
                findings.append(_f("ENS-ETH-SHORT-LABEL", "major", f".eth names need at least {MIN_ETH_LABEL} characters in the second-level label"))
            normalization = "ascii-verified"
        else:
            normalization = "needs-library"
            findings.append(_f("ENS-NON-ASCII", "major", "name contains non-ASCII characters: normalize and confusable-check with an ENSIP-15 library (for example @adraffy/ens-normalize) before resolving; this tool only verifies ASCII names"))
            if normalized != name:
                findings.append(_f("ENS-NOT-NFC", "major", "name is not in Unicode NFC form"))
            for label in labels:
                for index, ch in enumerate(label):
                    if unicodedata.category(ch) in _INVISIBLE_CATEGORIES and not _joins_emoji(label, index):
                        findings.append(_f("ENS-INVISIBLE-CHARACTER", "blocker", f"label contains invisible or control character U+{ord(ch):04X} at index {index}"))
                scripts = _scripts_of(label)
                if _mixed_scripts(scripts):
                    findings.append(_f("ENS-MIXED-SCRIPT", "blocker", f"label mixes scripts {', '.join(sorted(scripts))}: classic homograph spoofing"))
        node = _namehash(normalized) if ascii_only and not any(label == "" for label in labels) else ""
        reverse_name = reverse_node = ""
        verified = False
        if address is not None:
            reverse_name = f"{address[2:].lower()}.addr.reverse"
            reverse_node = _namehash(reverse_name)
            if _checksum_state(address) == "invalid":
                findings.append(_f("ENS-ADDRESS-CHECKSUM", "major", "address mixed case fails its EIP-55 checksum"))
            if forward is None:
                findings.append(_f("ENS-REVERSE-UNVERIFIED", "major", "forward resolution of the name was not supplied: a reverse record is self-asserted and must resolve forward to the same address before the UI shows the name"))
            elif _same_address(forward, ZERO_ADDRESS):
                findings.append(_f("ENS-FORWARD-UNSET", "blocker", "the name does not resolve to any address: show the raw address instead"))
            elif not _same_address(forward, address):
                findings.append(_f("ENS-REVERSE-SPOOF", "blocker", "the name resolves to a different address than the one claiming it: spoofed reverse record"))
            else:
                verified = True
        elif forward is not None:
            findings.append(_f("ENS-FORWARD-WITHOUT-ADDRESS", "info", "forward_address given without address: no reverse verification was possible"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "normalized": normalized, "normalization": normalization, "label_count": len(labels), "namehash": node,
                "reverse_name": reverse_name, "reverse_node": reverse_node, "reverse_verified": verified}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 12. signing request and link scan -------------------------------------------------------------------------------

CALL_SIGNATURES = {
    "approve(address,uint256)": "approval",
    "increaseAllowance(address,uint256)": "approval",
    "setApprovalForAll(address,bool)": "approval_for_all",
    "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)": "permit",
    "approve(address,address,uint160,uint48)": "permit2_approval",  # Permit2 AllowanceTransfer: the amount is a uint160
    "permit(address,address,uint256,uint256,bool,uint8,bytes32,bytes32)": "dai_permit",  # DAI-style permit: `allowed` is an unlimited allowance
    "transfer(address,uint256)": "transfer",
    "transferFrom(address,address,uint256)": "transfer_from",
    "safeTransferFrom(address,address,uint256)": "transfer_from",
    "safeTransferFrom(address,address,uint256,bytes)": "transfer_from",
    "safeBatchTransferFrom(address,address,uint256[],uint256[],bytes)": "transfer_from",
}
CALL_TABLE = {_selector(sig): (sig, kind) for sig, kind in CALL_SIGNATURES.items()}
BLIND_METHODS = {"eth_sign": ("blocker", "signs an arbitrary hash blind"), "eth_signTransaction": ("major", "returns a signed transaction without broadcasting it, a drainer pattern"),
                 "eth_signTypedData": ("minor", "is the legacy v1 typed-data format"), "eth_signTypedData_v3": ("minor", "lacks array and recursive-struct support of v4")}


_MULTI_PART_SUFFIXES = frozenset({"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.jp", "co.nz", "co.in", "co.za", "com.br",
                                  "com.cn", "com.mx", "com.sg", "com.tr", "com.hk", "com.tw", "co.kr", "com.ar"})
DANGEROUS_SCHEMES = frozenset({"javascript", "data", "vbscript", "file", "blob"})


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _registrable(host: str) -> str:
    """Registrable domain approximated without the Public Suffix List: the last two labels, three under a common two-part suffix; an IP is itself."""
    if _is_ip(host):
        return host
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _scheme_of(url: str) -> str:
    scheme: str = support.script("dapp_wallet_check").url_scheme_userinfo(url)[0]
    return scheme


_TAB_OR_NEWLINE = re.compile(r"[\t\r\n]")
_C0_OR_SPACE = "".join(chr(code) for code in range(0x21))  # U+0000 to U+0020


def _whatwg_strip(text: str) -> tuple[str, bool]:
    """A link or origin as the WHATWG URL parser reads it (spec-based; not run in a browser): leading and trailing C0 controls and spaces are
    removed, then every ASCII tab and newline. Returns the stripped text and whether a tab or newline lies inside it, which is a look-alike
    signal in itself; surrounding controls and spaces are not."""
    trimmed = text.strip(_C0_OR_SPACE)
    return _TAB_OR_NEWLINE.sub("", trimmed), _TAB_OR_NEWLINE.search(trimmed) is not None  # only an inner tab or newline is a look-alike signal


def _userinfo_findings(prefix: str, tag: str, url: str, findings: list[dict[str, str]]) -> None:
    """Userinfo in a displayed link or origin. Never echoes it: `app.example.com@evil.com` is the classic look-alike, `user:pw@host` a credential."""
    kind = support.script("dapp_wallet_check").url_scheme_userinfo(url)[1]
    if kind == "host-like":
        findings.append(_f(f"{prefix}-USERINFO-SPOOF", "blocker", f"{tag} puts a host-like name before the @, so the browser opens the host after it: a look-alike link"))
    elif kind == "plain":
        findings.append(_f(f"{prefix}-USERINFO", "major", f"{tag} embeds credentials (userinfo) in the URL"))


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (a != b)))
        previous = current
    return previous[-1]


def _squash(name: str) -> str:
    """A host with its dots and hyphens removed: `app-example.com` and `appexample.com` both read as `appexamplecom`, as `app.example.com` does."""
    return name.replace(".", "").replace("-", "")


def _domain_findings(label: str, host: str, expected: list[str], findings: list[dict[str, str]], *, kind: str) -> None:
    if any(host == e or host.endswith("." + e) for e in expected):
        return
    if host.startswith("xn--") or ".xn--" in host:
        findings.append(_f(f"{kind}-PUNYCODE", "major", f"{label} host {host} uses punycode (IDN homograph risk)"))
    if not host.isascii():
        findings.append(_f(f"{kind}-NON-ASCII", "major", f"{label} host {host} contains non-ASCII characters"))
    for e in expected:
        if host.startswith(e + ".") or ("." + e + ".") in host:
            findings.append(_f(f"{kind}-SPOOF", "blocker", f"{label} host {host} embeds the trusted domain {e} inside a different registrable domain"))
            return
    reg = _registrable(host)
    for e in expected:
        target = _registrable(e)
        limit = 2 if len(target) >= 8 else 1
        if reg != target and len(reg) <= 64 and abs(len(reg) - len(target)) <= limit and _edit_distance(reg, target) <= limit:
            findings.append(_f(f"{kind}-LOOKALIKE", "blocker", f"{label} host {host} is within {limit} edit(s) of the trusted domain {e}"))
            return
        if _squash(host) == _squash(e) or re.search(r"(?:^|\.)" + re.escape(e) + "-", host):
            findings.append(_f(f"{kind}-LOOKALIKE", "blocker", f"{label} host {host} merges or hyphenates the trusted domain {e}"))
            return
    findings.append(_f(f"{kind}-UNEXPECTED", "major" if kind == "ORIGIN" else "minor", f"{label} host {host} is not one of the expected domains"))


def signing_request_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Phishing indicators in pending signing/transaction requests, hard-coded addresses and displayed links."""

    def work() -> dict[str, Any]:
        known = []
        for index, entry in enumerate(payload["known_contracts"]):
            if not _is_address(entry):
                return _invalid("known_contracts", f"known_contracts[{index}] is not a 0x-prefixed 20-byte hex address")
            known.append(entry.lower())
        for index, entry in enumerate(payload["hardcoded_addresses"]):
            if not _is_address(entry):
                return _invalid("hardcoded_addresses", f"hardcoded_addresses[{index}] is not a 0x-prefixed 20-byte hex address")
        expected = [e.lower().strip(".") for e in payload["expected_domains"]]
        if any(not e or " " in e or "/" in e for e in expected):
            return _invalid("expected_domains", "expected_domains entries must be bare host names such as app.example.com")
        inspect = support.script("dapp_wallet_check").rpc_url_inspect
        findings: list[dict[str, str]] = []
        decoded: list[dict[str, Any]] = []
        for index, request in enumerate(payload["requests"]):
            method, tag = request["method"], f"requests[{index}]"
            if method in BLIND_METHODS:
                severity, why = BLIND_METHODS[method]
                findings.append(_f("SIGN-METHOD", severity, f"{tag}: {method} {why}"))
            target = request.get("to")
            if target is not None:
                if not _is_address(target):
                    return _invalid("requests", f"{tag}.to is not a 0x-prefixed 20-byte hex address")
                if target.lower() not in known:
                    findings.append(_f("SIGN-TARGET-UNKNOWN", "major", f"{tag}: transaction target is not a known contract of this app"))
            verifying = request.get("typed_domain_verifying_contract")
            if verifying is not None:
                if not _is_address(verifying):
                    return _invalid("requests", f"{tag}.typed_domain_verifying_contract is not a 0x-prefixed 20-byte hex address")
                if verifying.lower() not in known:
                    findings.append(_f("SIGN-VERIFYING-UNKNOWN", "major", f"{tag}: typed data is bound to a contract that is not a known contract of this app"))
            origin = request.get("origin")
            if origin:
                origin, control = _whatwg_strip(origin)
                if control:
                    findings.append(_f("ORIGIN-CONTROL-CHARACTER", "major", f"{tag} origin contains a tab or newline, which the browser removes before it reads the host: a look-alike construction"))
                origin_url = origin if "://" in origin else f"https://{origin}"
                _userinfo_findings("ORIGIN", f"{tag} origin", origin_url, findings)
                host = inspect(origin_url)
                if not host["valid"]:
                    findings.append(_f("SIGN-ORIGIN-MALFORMED", "major", f"{tag}: origin is not a valid URL or host"))
                elif expected:
                    _domain_findings(f"{tag} origin", host["host"], expected, findings, kind="ORIGIN")
            data = request.get("data", "")
            raw = _hex_bytes(data) if data else b""
            if raw is None:
                return _invalid("requests", f"{tag}.data must be an even-length 0x-prefixed hex string")
            if len(raw) >= 4:
                entry = CALL_TABLE.get(raw[:4].hex())
                if entry is None:
                    continue
                signature, kind = entry
                decoded.append({"index": index, "selector": "0x" + raw[:4].hex(), "signature": signature})
                words = [raw[4 + 32 * i:36 + 32 * i] for i in range((len(raw) - 4) // 32)]
                need = {"approval": 2, "approval_for_all": 2, "permit": 7, "permit2_approval": 4, "dai_permit": 8, "transfer": 2, "transfer_from": 3}[kind]
                if len(words) < need:
                    findings.append(_f("SIGN-CALLDATA-TRUNCATED", "major", f"{tag}: {signature} calldata has {len(words)} argument words, needs at least {need}"))
                    continue
                if kind in ("approval", "approval_for_all", "permit", "permit2_approval", "dai_permit"):
                    spender = "0x" + words[0 if kind in ("approval", "approval_for_all") else 1][12:].hex()
                    unknown = spender not in known
                    if kind == "approval_for_all":
                        flag = int.from_bytes(words[1], "big")
                        if flag > 1:
                            findings.append(_f("SIGN-CALLDATA-MALFORMED", "major", f"{tag}: setApprovalForAll flag word is {flag}, not a canonical bool: Solidity 0.8+ reverts on it"))
                        elif flag == 1:
                            findings.append(_f("SIGN-APPROVAL-FOR-ALL", "blocker" if unknown else "major",
                                               f"{tag}: setApprovalForAll(true) hands every token of the collection to {'an unknown' if unknown else 'a known'} operator"))
                    elif kind == "dai_permit":  # the allowance is a bool: true is an unlimited allowance, and a non-canonical bool reverts
                        flag = int.from_bytes(words[4], "big")
                        if flag > 1:
                            findings.append(_f("SIGN-CALLDATA-MALFORMED", "major", f"{tag}: {signature} allowed flag word is {flag}, not a canonical bool: Solidity 0.8+ reverts on it"))
                        elif flag == 1:
                            if unknown:
                                findings.append(_f("SIGN-SPENDER-UNKNOWN", "blocker", f"{tag}: {signature} grants an allowance to a spender that is not a known contract of this app"))
                            findings.append(_f("SIGN-UNLIMITED", "blocker" if unknown else "major", f"{tag}: {signature} grants an effectively unlimited allowance"))
                    else:
                        amount = int.from_bytes(words[2 if kind in ("permit", "permit2_approval") else 1], "big")
                        threshold = PERMIT2_UNLIMITED_THRESHOLD if kind == "permit2_approval" else UNLIMITED_THRESHOLD  # a Permit2 amount is a uint160
                        if amount:  # a zero amount (or setApprovalForAll(false)) is a revoke: decoded_calls shows it, nothing to warn about
                            if unknown:
                                findings.append(_f("SIGN-SPENDER-UNKNOWN", "blocker", f"{tag}: {signature} grants an allowance to a spender that is not a known contract of this app"))
                            if amount >= threshold:
                                findings.append(_f("SIGN-UNLIMITED", "blocker" if unknown else "major", f"{tag}: {signature} grants an effectively unlimited allowance"))
                elif kind in ("transfer", "transfer_from"):
                    findings.append(_f("SIGN-ASSET-MOVEMENT", "info", f"{tag}: {signature} moves assets out of the connected account"))
        for index, link in enumerate(payload["link_urls"]):
            tag = f"link_urls[{index}]"
            link, control = _whatwg_strip(link)
            if control:
                findings.append(_f("LINK-CONTROL-CHARACTER", "major", f"{tag} contains a tab or newline, which the browser removes before it reads the host: a look-alike construction"))
            scheme = _scheme_of(link)
            if scheme in DANGEROUS_SCHEMES:
                findings.append(_f("LINK-DANGEROUS-SCHEME", "blocker", f"{tag} uses the {scheme}: scheme, which runs or embeds content instead of opening a page"))
                continue
            info = inspect(link)
            if not info["valid"]:
                findings.append(_f("LINK-MALFORMED", "minor", f"{tag} is not a valid URL"))
                continue
            if info["scheme"] not in ("http", "https"):
                findings.append(_f("LINK-SCHEME", "major", f"{tag} uses a {len(info['scheme'])}-char scheme that is not http or https; a displayed link must be http or https"))
            elif info["scheme"] == "http":
                findings.append(_f("LINK-INSECURE", "minor", f"{tag} uses http"))
            _userinfo_findings("LINK", tag, link, findings)
            if expected:
                _domain_findings(tag, info["host"], expected, findings, kind="LINK")
        for index, address in enumerate(payload["hardcoded_addresses"]):
            if _checksum_state(address) == "invalid":
                findings.append(_f("ADDRESS-CHECKSUM", "major", f"hardcoded_addresses[{index}] fails its EIP-55 checksum (typo or spoofed literal)"))
            if address.lower() not in known:
                findings.append(_f("ADDRESS-NOT-IN-REGISTRY", "minor", f"hardcoded_addresses[{index}] {address} is not in the deployment registry: source the address from a verified deployment list"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "request_count": len(payload["requests"]), "link_count": len(payload["link_urls"]), "decoded_calls": decoded}

    result: dict[str, Any] = respond(payload, work)
    return result


# ---- 13. gas estimate buffer -----------------------------------------------------------------------------------------

MIN_BUFFER_PCT = Decimal(10)  # headroom a contract call needs over eth_estimateGas (state moves, EIP-150 63/64 rule); policy value
MAX_BUFFER_PCT = Decimal(100)
RECOMMENDED_BUFFER_PCT = Decimal(20)
BLOCK_SHARE_PCT = Decimal(90)  # keep gas limits under 90% of the block gas limit (MetaMask applies the same cap)
STALE_ESTIMATE_S = 60
INTRINSIC_GAS = 21000


def gas_buffer_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Gas limit headroom over eth_estimateGas, block and protocol caps, and whether the balance covers value plus gas_limit * maxFeePerGas."""

    def work() -> dict[str, Any]:
        estimate, limit, block = payload["estimated_gas"], payload["gas_limit"], payload["block_gas_limit"]
        cap = payload.get("tx_gas_cap")
        contract = payload["contract_call"]
        buffer_pct = (_dec(limit) - _dec(estimate)) / _dec(estimate) * 100
        findings: list[dict[str, str]] = []
        if limit < INTRINSIC_GAS:
            findings.append(_f("GAS-BELOW-INTRINSIC", "blocker", f"gas limit {limit} is below the {INTRINSIC_GAS} intrinsic gas of any transaction"))
        if limit < estimate:
            findings.append(_f("GAS-BELOW-ESTIMATE", "blocker", f"gas limit {limit} is below the estimate {estimate}: the transaction runs out of gas"))
        elif contract and buffer_pct < MIN_BUFFER_PCT:
            findings.append(_f("GAS-NO-HEADROOM", "major", f"contract call has {_pct_text(buffer_pct, ROUND_FLOOR)}% headroom over its estimate, under {MIN_BUFFER_PCT}%: state changes before inclusion can make it fail"))
        elif buffer_pct > MAX_BUFFER_PCT:
            findings.append(_f("GAS-BUFFER-EXCESSIVE", "minor", f"gas limit is {_pct_text(buffer_pct, ROUND_CEILING)}% above the estimate (over {MAX_BUFFER_PCT}%): it inflates the balance the wallet reserves"))
        if limit > block:
            findings.append(_f("GAS-ABOVE-BLOCK-LIMIT", "blocker", f"gas limit {limit} exceeds the block gas limit {block}"))
        elif _dec(limit) * 100 > _dec(block) * BLOCK_SHARE_PCT:
            findings.append(_f("GAS-NEAR-BLOCK-LIMIT", "major", f"gas limit uses more than {BLOCK_SHARE_PCT}% of the block gas limit and may never be included"))
        if cap is not None and limit > cap:
            findings.append(_f("GAS-ABOVE-TX-CAP", "blocker", f"gas limit {limit} exceeds the protocol per-transaction gas cap {cap}"))
        max_cost = limit * payload["max_fee_per_gas_wei"] + payload["value_wei"]
        if payload["balance_wei"] < max_cost:
            findings.append(_f("GAS-INSUFFICIENT-BALANCE", "blocker", f"balance {payload['balance_wei']} wei is below value + gas_limit * maxFeePerGas = {max_cost} wei: nodes reject the transaction"))
        if payload["estimate_age_seconds"] > STALE_ESTIMATE_S:
            findings.append(_f("GAS-ESTIMATE-STALE", "minor", f"estimate is {payload['estimate_age_seconds']}s old (over {STALE_ESTIMATE_S}s): re-estimate before signing"))
        ceiling = int(_dec(block) * BLOCK_SHARE_PCT / 100)
        if cap is not None:
            ceiling = min(ceiling, cap)
        recommended = estimate
        if contract:
            recommended = max(estimate, min(-((-estimate * (100 + int(RECOMMENDED_BUFFER_PCT))) // 100), ceiling))
        if estimate > ceiling:
            findings.append(_f("GAS-RECOMMEND-OVER-CEILING", "minor", f"the estimate {estimate} is above the {ceiling} gas ceiling (90% of the block gas limit or the protocol cap): no limit under the ceiling covers it, so the recommendation is the estimate and the transaction may never be included"))
        ordered, blockers, majors, minors, verdict = _finish(findings)
        return {"state": "SUCCESS", "verdict": verdict, "findings": ordered, "blocker_count": blockers, "major_count": majors, "minor_count": minors,
                "buffer_percent": round(float(buffer_pct), 4), "max_cost_wei": str(max_cost), "recommended_gas_limit": recommended}

    result: dict[str, Any] = respond(payload, work)
    return result
