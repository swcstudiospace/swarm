"""A30 Web3 Custody & Compliance Engineer tools: pure analyses of custody policy and compliance evidence.

A30 designs policy and produces evidence only. Nothing here reads, generates, stores, logs or transmits key material, signs,
broadcasts or calls a network. Free-text inputs that look like key material are refused (INVALID_INPUT naming the field,
never echoing the value) and the leak scan reports locations and lengths only.

Tools:
  custody_multisig_policy       m-of-n sanity, signer diversity (entities, regions, hot keys) and value-tiered thresholds.
  custody_timelock_check        timelock delay versus value at risk and response time, cancel path, admin role, expiry.
  custody_ceremony_checklist    key-ceremony checklist completeness (items, evidence references, witnesses).
  custody_shard_distribution    t-of-n secret-share distribution: quorum independence by custodian, organization, country.
  custody_signer_policy_lint    signer policy lint: limits, allowlist, cool-off, velocity, approval expiry, change quorum.
  custody_wallet_tier_caps      hot/warm/cold balances against percentage and absolute caps and a cold minimum.
  custody_key_rotation          key age against its maximum: overdue, due soon, ok.
  custody_tx_policy_eval        transaction descriptor versus policy -> allow, hold or deny (never signs).
  custody_sanctions_screening   screening results: list freshness, screening age and the action taken on each match state.
  custody_travel_rule           FATF R.16 originator/beneficiary field completeness above a threshold.
  custody_audit_trail           audit-trail completeness: who, what, when, approvals, separation of duties, SHA-256 hash chain.
  custody_recovery_drill        recovery drill recency, outcome and recovery-time objective per scenario.
  custody_secret_scan           key-material leak scan over supplied file contents: locations and lengths only.

Public references behind the embedded tables: Keccak-256 (Keccak team, original padding 0x01, as used by Ethereum, not the
NIST SHA3 0x06); EIP-55 mixed-case checksum addresses; the 4-byte selector is the first four bytes of keccak256 of the
canonical signature (Solidity ABI spec); Wallet Import Format and BIP32 extended-key serialization (Base58Check, version
bytes below; SLIP-132 version bytes); FATF Recommendation 16 and its interpretive note (INR.16 paras 5-7).
"""
from __future__ import annotations

import bisect
import hashlib
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from swarm.tools.support import respond

SEVERITY_RANK = {"info": 0, "minor": 1, "major": 2, "blocker": 3}
MAX_FINDINGS = 100  # unbounded input (events, files) must not push the result past the registry's output cap
SECONDS_PER_DAY = 86_400
MIN_COOLOFF_S = 86_400  # lint default for a new allowlist entry; an internal norm, not a standard
MAX_APPROVAL_EXPIRY_S = 86_400  # lint default for how long a collected approval stays valid
MAX_REATTEST_DAYS = 365  # lint default for how long an allowlist entry goes without re-verification
GENESIS_HASH = "0" * 64


class _Invalid(Exception):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field, self.message = field, message


def _invalid(field: str, message: str) -> dict[str, Any]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _guarded(payload: dict[str, Any], fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    """Work function for `respond`: runs fn(payload) and turns a raised _Invalid into the INVALID_INPUT shape."""

    def work() -> dict[str, Any]:
        try:
            return fn(payload)
        except _Invalid as exc:
            return _invalid(exc.field, exc.message)

    return work


def _respond(payload: dict[str, Any], fn: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """`support.respond` is untyped, so its result is Any; this restates it as the handler's result type."""
    result: dict[str, Any] = respond(payload, _guarded(payload, fn))
    return result


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _num(value: Decimal, places: int = 4) -> float:
    return float(round(value, places))


def _ts(field: str, value: str) -> datetime:
    """ISO-8601 timestamp with an explicit offset (Z or +hh:mm); naive and malformed values are refused."""
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise _Invalid(field, f"{field} must be an ISO-8601 timestamp with an explicit offset") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _Invalid(field, f"{field} must carry an explicit UTC offset (Z or +hh:mm)")
    return parsed


def _norm(text: str) -> str:
    """Identity form for comparisons: NFKC, case-folded, trimmed. "Bob", "bob " and a fullwidth "ｂob" are one person."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold()).strip()


def _finding(fid: str, severity: str, summary: str) -> dict[str, Any]:
    return {"id": fid, "severity": severity, "summary": summary}


def _ordered(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Worst first, then by id, so the same input always lists the same way."""
    return sorted(findings, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["id"], f["summary"]))


def _verdict(findings: list[dict[str, Any]]) -> str:
    worst = max((SEVERITY_RANK[f["severity"]] for f in findings), default=0)
    return "fail" if worst >= SEVERITY_RANK["major"] else "warn" if worst == SEVERITY_RANK["minor"] else "pass"


def _capped(findings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    ordered = _ordered(findings)
    return ordered[:MAX_FINDINGS], max(0, len(ordered) - MAX_FINDINGS)


def _min_groups(sizes: Iterable[int], target: int) -> int:
    """Fewest groups whose sizes add up to at least `target` (largest first is optimal for a sum)."""
    total = 0
    ordered = sorted(sizes, reverse=True)
    for count, size in enumerate(ordered, 1):
        total += size
        if total >= target:
            return count
    return len(ordered)


# --- Keccak-256 (pure Python), EIP-55 and selectors -------------------------------------------------------------------

_MASK64 = (1 << 64) - 1
_KECCAK_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)


def _rho_offsets() -> list[list[int]]:
    offsets = [[0] * 5 for _ in range(5)]
    x, y = 1, 0
    for t in range(24):
        offsets[x][y] = ((t + 1) * (t + 2) // 2) % 64
        x, y = y, (2 * x + 3 * y) % 5
    return offsets


_KECCAK_ROT = _rho_offsets()


def _rotl64(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (64 - shift))) & _MASK64 if shift else value


def _keccak_f(lanes: list[int]) -> list[int]:
    for rc in _KECCAK_RC:
        parity = [lanes[x] ^ lanes[x + 5] ^ lanes[x + 10] ^ lanes[x + 15] ^ lanes[x + 20] for x in range(5)]
        delta = [parity[(x - 1) % 5] ^ _rotl64(parity[(x + 1) % 5], 1) for x in range(5)]
        lanes = [lanes[i] ^ delta[i % 5] for i in range(25)]
        moved = [0] * 25
        for x in range(5):
            for y in range(5):
                moved[y + 5 * ((2 * x + 3 * y) % 5)] = _rotl64(lanes[x + 5 * y], _KECCAK_ROT[x][y])
        lanes = [moved[x + 5 * y] ^ ((~moved[(x + 1) % 5 + 5 * y]) & moved[(x + 2) % 5 + 5 * y]) for y in range(5) for x in range(5)]
        lanes[0] ^= rc
    return lanes


def _keccak256(data: bytes) -> bytes:
    """Keccak-256 as Ethereum uses it: rate 136 bytes, multi-rate padding 0x01 ... 0x80."""
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
            lanes[i] ^= int.from_bytes(block[8 * i:8 * i + 8], "little")
        lanes = _keccak_f(lanes)
    return b"".join(lanes[i].to_bytes(8, "little") for i in range(4))


_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


def _eip55(lower_hex: str) -> str:
    """EIP-55 mixed-case form of a 40-hex-digit address (given lower case, without 0x)."""
    digest = _keccak256(lower_hex.encode("ascii")).hex()
    return "".join(c.upper() if c.isalpha() and int(digest[i], 16) >= 8 else c for i, c in enumerate(lower_hex))


def _address(field: str, text: str) -> str:
    """Lower-case canonical form of a 0x-prefixed 20-byte address. A mixed-case address must satisfy the EIP-55 checksum;
    all-lower and all-upper spellings carry no checksum and are accepted."""
    if len(text) != 42 or not text.startswith("0x") or any(c not in _HEXDIGITS for c in text[2:]):
        raise _Invalid(field, f"{field} must be a 0x-prefixed 20-byte hex address (42 characters)")
    body = text[2:]
    if body != body.lower() and body != body.upper() and _eip55(body.lower()) != body:
        raise _Invalid(field, f"{field} has mixed case but fails the EIP-55 checksum")
    return "0x" + body.lower()


_RISKY_SIGNATURES = (
    "approve(address,uint256)",  # ERC-20 allowance and ERC-721 single-token approval share this selector
    "increaseAllowance(address,uint256)",
    "permit(address,address,uint256,uint256,uint8,bytes32,bytes32)",  # EIP-2612
    "setApprovalForAll(address,bool)",  # ERC-721 / ERC-1155 operator approval
    "transferOwnership(address)",
    "renounceOwnership()",
    "upgradeTo(address)",  # ERC-1967 / UUPS
    "upgradeToAndCall(address,bytes)",
    "acceptOwnership()",
    "grantRole(bytes32,address)",  # OpenZeppelin AccessControl
    "revokeRole(bytes32,address)",
    "addOwnerWithThreshold(address,uint256)",  # Safe: signer set, threshold, modules, guard and fallback handler
    "removeOwner(address,address,uint256)",
    "swapOwner(address,address,address)",
    "changeThreshold(uint256)",
    "enableModule(address)",
    "disableModule(address,address)",
    "setGuard(address)",
    "setFallbackHandler(address)",
)
_PLAIN_SIGNATURES = ("transfer(address,uint256)", "transferFrom(address,address,uint256)")
_SIGNATURE_BY_SELECTOR = {_keccak256(sig.encode("ascii"))[:4].hex(): sig for sig in _RISKY_SIGNATURES + _PLAIN_SIGNATURES}
_RISKY_SELECTORS = frozenset(_keccak256(sig.encode("ascii"))[:4].hex() for sig in _RISKY_SIGNATURES)


# --- secret-shaped values: refused in free text, found by the scan ----------------------------------------------------

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}
_XPRV_VERSIONS = frozenset(bytes.fromhex(v) for v in (
    "0488ade4", "04358394", "049d7878", "04b2430c", "044a4e28", "045f18bc",  # xprv tprv yprv zprv uprv vprv (SLIP-132)
    "0295b005", "02aa7a99", "024285b5", "02575048",  # Yprv Zprv Uprv Vprv (SLIP-132 multisig)
))
_WIF_VERSIONS = frozenset((0x80, 0xEF))  # Bitcoin mainnet / testnet private-key prefix
_PEM_RE = re.compile(r"-----BEGIN[ \t]+((?:[A-Z0-9]+[ \t]+)*)PRIVATE[ \t]+KEY(?:[ \t]+BLOCK)?[ \t]*-----")
_XPRV_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[xyztuvYZUV]prv[1-9A-HJ-NP-Za-km-z]{100,120}(?![1-9A-HJ-NP-Za-km-z])")
_WIF_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[5KLc9][1-9A-HJ-NP-Za-km-z]{50,51}(?![1-9A-HJ-NP-Za-km-z])")
# a 64-digit hex value assigned to a key-like name; the short names (pk, secret, seed) must stand alone between delimiters
_HEX_KEY_RE = re.compile(
    r"(?i)(?:private[_-]?key|priv[_-]?key|secret[_-]?key|signing[_-]?key|(?<![a-z0-9])(?:pk|pkey|secret|seed)(?![a-z0-9]))"
    r"[\w.-]{0,40}[\"'`]?\s*(?:=>|[:=])\s*[\"'`]?(?:0x)?([0-9a-f]{64})\b"
)
_HEX64_RE = re.compile(r"(?<![0-9A-Fa-f])(?:0[xX])?[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
# a 12/15/18/21/24-word phrase assigned (quoted, backticked, bracketed or bare) to a mnemonic-like name. Its words may be
# split over lines or joined by commas and quotes (JSON, YAML, JS lists); the look-ahead refuses a phrase followed by more
# words on the same line. Bounded joins and a single-class look-ahead keep the scan linear.
_MNEMONIC_RE = re.compile(
    r"(?i)(?:mnemonic|seed[_ -]?phrase|seed[_ -]?words|recovery[_ -]?phrase)[\w.-]{0,40}[\"'`]?\s*[:=][\s\[\"'`]{0,32}"
    r"([a-z]+(?:[ \t\r\n,\"'`]{1,32}[a-z]+){11,23})(?![ \t,\"'`]*[a-z])"
)
_MNEMONIC_WORD_RE = re.compile(r"[a-z]+", re.IGNORECASE)
_MNEMONIC_SHAPE_RE = re.compile(r"[a-z]{3,8}(?: [a-z]{3,8}){11,23}")
_MNEMONIC_WORDS = frozenset((12, 15, 18, 21, 24))
# credentials that are not keys but must never be echoed or stored either (cloud keys, API tokens, URLs with a password)
_TOKEN_RES = (
    ("cloud-access-key-id", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])")),
    ("api-token", re.compile(
        r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}"
        r"|sk_(?:live|test)_[A-Za-z0-9]{16,}|sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35})")),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")),
    ("credentialed-url", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]*:[^\s/@]+@")),
    ("bearer-credential", re.compile(
        r"(?i)\bbearer\s+(?=[A-Za-z._~+/=-]*[0-9])[A-Za-z0-9._~+/=-]{20,}|\bauthorization\s*[:=]\s*(?:basic|bearer|token)\s+\S{8,}")),
)
# A key-name assignment whose value is key-shaped. The name is found wherever it starts (no lookbehind: seed=<a's>secret=<hex>
# reports the second name). The value is captured by a zero-width lookahead, so the run is not consumed and a name inside it is
# still found. _key_shaped then tests each form on its own: a value of 32 or more characters with no placeholder.
_ASSIGNMENT_RE = re.compile(
    r"(?i:(?:[a-z0-9]+[_.-])*(?:private[_-]?key|priv[_-]?key|signing[_-]?key|secret|seed|mnemonic|passw(?:or)?d"
    r"|passwd|token|api[_-]?key|access[_-]?key|pkey|pk))(?![a-zA-Z0-9])[\w.-]{0,40}\s*(?:[\"'`]\s*)?(?:=>|[:=])\s*[\"'`]?(?:0[xX])?"
    r"(?=(?P<value>(?i:[a-z0-9+/_-])+={0,2}))"
)
_HEX_DIGITS_RE = re.compile(r"[0-9a-fA-F]+")


def _has_letter_and_digit(text: str) -> bool:
    return any(c.isalpha() for c in text) and any(c.isdigit() for c in text)


def _mixes_case(text: str) -> bool:
    return any(c.islower() for c in text) and any(c.isupper() for c in text)


def _key_shaped(value: str) -> bool:
    """Key material among the value after a credential label (the trailing '=' padding is not counted):
    - 32 or more characters in all, and not one repeated character (a placeholder, as in _HEX_KEY_RE);
    - hex of 32 or more digits once '-' and '_' are removed, grouped or not;
    - with no '-' or '_': a letter and a digit, or both upper and lower case letters;
    - with '-' or '_': the value with them removed has 32 or more characters and both upper and lower case letters, or its
      first segment (the run before the first separator) is hex of 32 or more digits and not one repeated character, so a
      key followed by a suffix such as -missing or _old is still key material."""
    core = value.rstrip("=")
    if len(core) < 32:
        return False
    bare = core.replace("-", "").replace("_", "")
    if len(set(bare.lower())) <= 1:
        return False
    if _HEX_DIGITS_RE.fullmatch(bare) and len(bare) >= 32:
        return True
    if "-" not in core and "_" not in core:
        return _has_letter_and_digit(bare) or _mixes_case(bare)
    first = re.split(r"[-_]", core, maxsplit=1)[0]
    if len(first) >= 32 and _HEX_DIGITS_RE.fullmatch(first) and len(set(first.lower())) > 1:
        return True
    return len(bare) >= 32 and _mixes_case(bare)


def _assignment_hit(text: str) -> bool:
    """True when `text` holds a credential label whose value is key material (see _key_shaped)."""
    return any(_key_shaped(match.group("value")) for match in _ASSIGNMENT_RE.finditer(text))


def _b58check(text: str) -> bytes | None:
    """Payload of a valid Base58Check string (version byte included), else None."""
    number = 0
    for char in text:
        digit = _B58_INDEX.get(char)
        if digit is None:
            return None
        number = number * 58 + digit
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    raw = b"\x00" * (len(text) - len(text.lstrip("1"))) + raw
    if len(raw) < 5 or hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] != raw[-4:]:
        return None
    return raw[:-4]


def _is_xprv(token: str) -> bool:
    payload = _b58check(token)
    return payload is not None and len(payload) == 78 and payload[:4] in _XPRV_VERSIONS and payload[45] == 0


def _is_wif(token: str) -> bool:
    payload = _b58check(token)
    if payload is None or payload[0] not in _WIF_VERSIONS:
        return False
    return len(payload) == 33 or (len(payload) == 34 and payload[-1] == 0x01)


def _mnemonic_matches(text: str) -> Iterator[tuple[int, int]]:
    """(start, length) of each mnemonic assignment in `text` whose phrase has a valid BIP-39 word count and word shapes
    (3-8 letters). A run that swallowed the words of the next line is cut back to a line break that gives a valid count."""
    for match in _MNEMONIC_RE.finditer(text):
        phrase = match.group(1)
        cuts = [len(phrase)] + [m.start() for m in re.finditer("\n", phrase)][::-1]
        for cut in cuts:
            words = list(_MNEMONIC_WORD_RE.finditer(phrase, 0, cut))
            if len(words) in _MNEMONIC_WORDS and all(3 <= len(w.group()) <= 8 for w in words):
                yield match.start(1), words[-1].end()
                break


def _secret_kind(text: str, strict: bool = True) -> str:
    """Kind of secret-shaped material in a short free-text value, else ''. A key (PEM, BIP32, WIF), a credential (cloud key
    id, API token, JWT, URL with a password, bearer value), a key-name assignment whose value is key-shaped (see _ASSIGNMENT_RE:
    a path component such as secret=<64 hex> is refused whole) or a mnemonic. `strict` (identifiers, which results echo) also
    refuses a bare 64-digit hex value; it is never flagged otherwise because it is also a transaction hash or a digest."""
    if _PEM_RE.search(text):
        return "pem-private-key"
    if any(_is_xprv(m.group()) for m in _XPRV_RE.finditer(text)):
        return "extended-private-key"
    if any(_is_wif(m.group()) for m in _WIF_RE.finditer(text)):
        return "wif-private-key"
    for kind, pattern in _TOKEN_RES:
        if pattern.search(text):
            return kind
    if _assignment_hit(text):
        return "key-assignment"
    squeezed = " ".join(text.lower().split())
    if (_MNEMONIC_SHAPE_RE.fullmatch(squeezed) and len(squeezed.split(" ")) in _MNEMONIC_WORDS) or next(_mnemonic_matches(text), None):
        return "mnemonic-phrase"
    if strict and _HEX64_RE.search(text):
        return "hex-key-shaped"
    return ""


def _label(field: str, value: str, strict: bool = True) -> str:
    """A free-text identifier or reference, stripped; refused (without echoing it) when it is secret-shaped."""
    kind = _secret_kind(value, strict)
    if kind:
        raise _Invalid(field, f"{field} contains a value shaped like key material or a credential ({kind}); supply an identifier, never a secret")
    return value.strip()


def _ident(field: str, value: str) -> str:
    """An identifier that results may echo: secret-shaped values and blank values are refused."""
    text = _label(field, value)
    if not text:
        raise _Invalid(field, f"{field} contains a blank identifier")
    return text


# --- 1. multisig policy ----------------------------------------------------------------------------------------------

HOT_DEVICES = frozenset(("software", "mobile"))


def _multisig_policy(payload: dict[str, Any]) -> dict[str, Any]:
    threshold, signers = payload["threshold"], payload["signers"]
    n = len(signers)
    if threshold > n:
        raise _Invalid("threshold", f"threshold {threshold} exceeds the {n} signer(s)")
    seen: set[str] = set()
    entities: Counter[str] = Counter()
    regions: Counter[str] = Counter()
    hot = 0
    for index, signer in enumerate(signers):
        sid = _norm(_ident("signers", signer["signer_id"]))
        if sid in seen:
            raise _Invalid("signers", f"duplicate signer_id at position {index}")
        seen.add(sid)
        entities[_norm(_ident("signers", signer["entity"]))] += 1
        regions[_norm(_ident("signers", signer["region"]))] += 1
        hot += signer["device"] in HOT_DEVICES
    min_entities = _min_groups(entities.values(), threshold)
    min_regions = _min_groups(regions.values(), threshold)
    findings: list[dict[str, Any]] = []
    if n == 1:
        findings.append(_finding("single-signer", "blocker", "one signer is not a multisig: its key alone moves funds"))
    elif threshold == 1:
        findings.append(_finding("threshold-one", "blocker", f"1-of-{n}: compromising any single signer key moves funds"))
    elif threshold == n:
        findings.append(_finding("no-loss-tolerance", "major", f"{n}-of-{n}: losing one key makes the funds unrecoverable"))
    if n > 1 and 2 * threshold <= n:
        findings.append(_finding("no-majority-quorum", "minor", f"{threshold}-of-{n}: two disjoint signer groups can each reach the threshold"))
    if n > 1 and min_entities == 1:
        findings.append(_finding("entity-controls-quorum", "blocker", "one entity controls enough signers to reach the threshold alone"))
    if len(regions) == 1 and n > 1:
        findings.append(_finding("single-region", "minor", "all signers sit in one region: one disaster or one legal order reaches them all"))
    elif n > 1 and min_regions == 1:
        findings.append(_finding("region-controls-quorum", "minor", "enough signers sit in one region to reach the threshold: one disaster or one legal order there moves funds"))
    if hot >= threshold:
        findings.append(_finding("hot-signers-reach-quorum", "major", f"{hot} software/mobile signer(s) reach the threshold of {threshold} without any hardware key"))
    tiers = payload.get("tiers", [])
    if tiers:
        ordered = sorted(((_dec(t["min_value_usd"]), t["required_signatures"]) for t in tiers), key=lambda t: t[0])
        if len({t[0] for t in ordered}) != len(ordered):
            raise _Invalid("tiers", "two tiers start at the same min_value_usd")
        if ordered[0][0] != 0:
            findings.append(_finding("tier-gap-below-lowest", "major", f"values below {ordered[0][0]} USD fall under no tier"))
        previous = 0
        for index, (_low, required) in enumerate(ordered, 1):
            if required > n:
                findings.append(_finding(f"tier-unsatisfiable:{index}", "blocker", f"tier {index} needs {required} signatures but only {n} signer(s) exist"))
            if required < previous:
                findings.append(_finding(f"tier-not-monotonic:{index}", "major", f"tier {index} needs fewer signatures ({required}) than the tier below ({previous})"))
            if required < threshold:
                findings.append(_finding(f"tier-below-onchain-threshold:{index}", "minor", f"tier {index} needs {required} signature(s), below the on-chain threshold {threshold}, which governs"))
            previous = max(previous, required)
    findings = _ordered(findings)
    return {
        "state": "SUCCESS", "signer_count": n, "threshold": threshold, "loss_tolerance": n - threshold,
        "compromise_tolerance": threshold - 1, "distinct_entities": len(entities), "distinct_regions": len(regions),
        "min_entities_for_quorum": min_entities, "hot_signers": hot, "tiers_checked": len(tiers),
        "verdict": _verdict(findings), "findings": findings,
    }


def multisig_policy(payload: dict[str, Any]) -> dict[str, Any]:
    """m-of-n sanity (1-of-n, n-of-n, minority quorum), signer diversity (the fewest entities, and the fewest regions, that can
    reach the threshold; software keys) and value-tiered signature requirements (gaps, non-monotonic, unsatisfiable). The rules
    are this lane's policy, not a standard; names and regions are compared NFKC-normalised, trimmed and case-folded."""
    return _respond(payload, _multisig_policy)


# --- 2. timelock check -----------------------------------------------------------------------------------------------


def _timelock_check(payload: dict[str, Any]) -> dict[str, Any]:
    delay, value = payload["delay_seconds"], _dec(payload["value_at_risk_usd"])
    response = payload["response_seconds"]
    bands = sorted(((_dec(b["min_value_usd"]), b["min_delay_seconds"]) for b in payload["delay_bands"]), key=lambda b: b[0])
    if len({b[0] for b in bands}) != len(bands):
        raise _Invalid("delay_bands", "two bands start at the same min_value_usd")
    required = 0
    for low, min_delay in bands:
        if value >= low:
            required = max(required, min_delay)
    findings: list[dict[str, Any]] = []
    if bands and bands[0][0] != 0:
        findings.append(_finding("band-gap-below-lowest", "major", f"values below {bands[0][0]} USD fall under no delay band"))
    if delay == 0:
        findings.append(_finding("no-delay", "blocker", "delay is 0: a queued operation can execute immediately, nothing can be cancelled in time"))
    if delay < required:
        findings.append(_finding("delay-below-required", "major", f"delay {delay}s is below the {required}s required for {value} USD at risk"))
    if delay < response:
        findings.append(_finding("delay-shorter-than-response", "major", f"delay {delay}s is shorter than the {response}s needed to detect and cancel"))
    cancellers, guardian = payload["canceller_count"], payload["guardian_can_cancel"]
    if cancellers == 0 and not guardian:
        findings.append(_finding("no-cancel-path", "major", "no canceller and no guardian: a malicious queued operation cannot be stopped"))
    elif cancellers <= 1 and not guardian:
        findings.append(_finding("single-canceller", "minor", "a single canceller and no guardian: one unavailable key removes the cancel path"))
    if not payload["admin_renounced"]:
        findings.append(_finding("admin-not-renounced", "major", "an admin role that is neither renounced nor held by the timelock itself can reconfigure roles outside the delay"))
    if payload["execution_window_seconds"] == 0:
        findings.append(_finding("no-expiry", "minor", "queued operations never expire: a stale operation stays executable indefinitely"))
    findings = _ordered(findings)
    return {
        "state": "SUCCESS", "required_delay_seconds": required, "delay_margin_seconds": delay - max(required, response),
        "delay_hours": _num(Decimal(delay) / 3600, 2), "verdict": _verdict(findings), "findings": findings,
    }


def timelock_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Delay >= the longest band delay among the bands whose min_value_usd <= the value at risk, and >= the response time
    (detect + decide + cancel); needs a cancel path (canceller or guardian), a renounced admin and an expiry window. House
    policy, with these references: OpenZeppelin TimelockController says its optional admin "should be subsequently renounced"
    (an admin acts outside the delay), and has no expiry; Compound's Timelock hard-codes a 14-day GRACE_PERIOD after the eta."""
    return _respond(payload, _timelock_check)


# --- 3. key-ceremony checklist ---------------------------------------------------------------------------------------

_COMMON = ("participants-identified", "procedure-reviewed", "ceremony-record-signed")
_CEREMONY_ITEMS = {
    "key_generation": _COMMON + (
        "hardware-integrity-verified", "offline-environment-attested", "entropy-source-attested",
        "public-key-recorded-out-of-band", "backup-restore-verified", "backup-custody-receipted", "media-sanitized",
    ),
    "key_rotation": _COMMON + (
        "new-key-generated-by-ceremony", "public-key-recorded-out-of-band", "new-key-added-onchain-before-old-removed",
        "old-key-removed-onchain", "old-key-material-retired", "rotation-verified-onchain",
    ),
    "shard_distribution": _COMMON + (
        "shard-threshold-confirmed", "custodian-independence-verified", "shard-integrity-verified",
        "shard-custody-receipted", "storage-locations-recorded",
    ),
    "recovery_restore": _COMMON + (
        "authorization-recorded", "offline-environment-attested", "restore-verified-against-public-key", "media-sanitized",
    ),
}
# steps performed physically in the room: they need the minimum number of independent witnesses
_WITNESSED = frozenset((
    "participants-identified", "hardware-integrity-verified", "offline-environment-attested", "entropy-source-attested",
    "public-key-recorded-out-of-band", "backup-restore-verified", "backup-custody-receipted", "media-sanitized",
    "shard-integrity-verified", "shard-custody-receipted", "restore-verified-against-public-key", "ceremony-record-signed",
    "old-key-material-retired",
))


def _ceremony_checklist(payload: dict[str, Any]) -> dict[str, Any]:
    required = _CEREMONY_ITEMS[payload["ceremony_type"]]
    min_witnesses = payload["min_witnesses"]
    given: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(payload["items"]):
        name = entry["item"]
        if name not in required:
            raise _Invalid("items", f"items[{index}] is not part of the {payload['ceremony_type']} checklist; valid items: {', '.join(required)}")
        if name in given:
            raise _Invalid("items", f"items[{index}] repeats an earlier item")
        _label("items", entry["evidence_ref"], strict=False)
        given[name] = entry
    findings: list[dict[str, Any]] = []
    missing: list[str] = []
    for name in required:
        entry = given.get(name)
        if entry is None or not entry["done"]:
            findings.append(_finding(f"item-open:{name}", "major", f"{name} is not done"))
        elif not entry["evidence_ref"].strip():
            findings.append(_finding(f"missing-evidence:{name}", "major", f"{name} is marked done without an evidence reference"))
        elif name in _WITNESSED and entry["witness_count"] < min_witnesses:
            findings.append(_finding(f"insufficient-witnesses:{name}", "major", f"{name} had {entry['witness_count']} witness(es), {min_witnesses} required"))
        else:
            continue
        missing.append(name)
    findings = _ordered(findings)
    completed = len(required) - len(missing)
    return {
        "state": "SUCCESS", "required_items": len(required), "completed_items": completed,
        "completion_pct": _num(Decimal(completed) * 100 / len(required), 2), "missing_items": missing,
        "verdict": _verdict(findings), "findings": findings,
    }


def ceremony_checklist(payload: dict[str, Any]) -> dict[str, Any]:
    """An item counts as complete when it is done, has a non-blank evidence reference and, for in-room steps, at least
    min_witnesses witnesses. Items outside the ceremony type's catalogue and duplicates are refused."""
    return _respond(payload, _ceremony_checklist)


# --- 4. shard distribution -------------------------------------------------------------------------------------------


def _shard_distribution(payload: dict[str, Any]) -> dict[str, Any]:
    threshold, shards = payload["threshold"], payload["shards"]
    n = len(shards)
    if threshold > n:
        raise _Invalid("threshold", f"threshold {threshold} exceeds the {n} shard(s)")
    ids: set[str] = set()
    custodians: Counter[str] = Counter()
    orgs: Counter[str] = Counter()
    countries: Counter[str] = Counter()
    org_of: dict[str, str] = {}
    for index, shard in enumerate(shards):
        sid = _norm(_ident("shards", shard["shard_id"]))
        if sid in ids:
            raise _Invalid("shards", f"duplicate shard_id at position {index}")
        ids.add(sid)
        custodian, org = _norm(_ident("shards", shard["custodian"])), _norm(_ident("shards", shard["organization"]))
        country = shard["country"]
        if len(country) != 2 or not (country.isascii() and country.isalpha() and country.isupper()):
            raise _Invalid("shards", f"the country of the shard at position {index} must be an ISO 3166-1 alpha-2 code in capitals")
        if org_of.setdefault(custodian, org) != org:
            raise _Invalid("shards", f"the custodian of the shard at position {index} is listed under two organizations")
        custodians[custodian] += 1
        orgs[org] += 1
        countries[country] += 1
    min_custodians = _min_groups(custodians.values(), threshold)
    min_orgs = _min_groups(orgs.values(), threshold)
    min_countries = _min_groups(countries.values(), threshold)
    findings: list[dict[str, Any]] = []
    if threshold == 1:
        findings.append(_finding("threshold-one", "blocker", "threshold 1: any single shard reconstructs the secret"))
    elif threshold == n:
        findings.append(_finding("no-loss-tolerance", "major", f"{n}-of-{n}: losing one shard makes the secret unrecoverable"))
    if min_custodians == 1 and threshold > 1:
        findings.append(_finding("single-custodian-quorum", "blocker", "one custodian holds enough shards to reconstruct the secret alone"))
    elif min_orgs == 1 and threshold > 1:
        findings.append(_finding("single-organization-quorum", "blocker", "one organization holds enough shards to reconstruct the secret"))
    if min_countries == 1 and threshold > 1:
        findings.append(_finding("single-jurisdiction-quorum", "major", "enough shards sit in one country that a single legal order can compel a quorum"))
    for name, count in sorted(custodians.items()):
        if count > 1 and threshold > 1 and count < threshold:
            findings.append(_finding(f"custodian-holds-multiple:{name}", "minor", f"custodian {name} holds {count} shards"))
    findings = _ordered(findings)
    return {
        "state": "SUCCESS", "shard_count": n, "threshold": threshold, "loss_tolerance": n - threshold,
        "compromise_tolerance": threshold - 1, "min_custodians_for_quorum": min_custodians,
        "min_organizations_for_quorum": min_orgs, "min_countries_for_quorum": min_countries,
        "verdict": _verdict(findings), "findings": findings,
    }


def shard_distribution(payload: dict[str, Any]) -> dict[str, Any]:
    """t-of-n shard distribution: loss and compromise tolerance, and the fewest custodians, organizations and countries
    that can together reconstruct the secret (greedy by holdings, which is minimal for a sum)."""
    return _respond(payload, _shard_distribution)


# --- 5. signer policy lint -------------------------------------------------------------------------------------------


def _signer_policy_lint(payload: dict[str, Any]) -> dict[str, Any]:
    per_tx, daily, balance = _dec(payload["per_tx_limit_usd"]), _dec(payload["daily_limit_usd"]), _dec(payload["balance_usd"])
    velocity, enforced = payload["velocity_max_tx_per_hour"], payload["allowlist_enforced"]
    cooloff, reattest = payload["allowlist_cooloff_seconds"], payload["allowlist_reattest_days"]
    expiry = payload["approval_expiry_seconds"]
    # (id, severity, summary, applicable, violated)
    rules = [
        ("no-per-tx-limit", "major", "no per-transaction limit (0): one approved transaction can move the whole balance", True, per_tx == 0),
        ("no-daily-limit", "major", "no rolling daily limit (0)", True, daily == 0),
        ("per-tx-exceeds-daily", "minor", "the per-transaction limit is above the daily limit, so it can never bind", per_tx > 0 and daily > 0, per_tx > daily),
        ("per-tx-limit-not-binding", "minor", "the per-transaction limit is at or above the whole balance", balance > 0 and per_tx > 0, per_tx >= balance),
        ("daily-limit-not-binding", "minor", "the daily limit is at or above the whole balance", balance > 0 and daily > 0, daily >= balance),
        ("no-velocity-limit", "minor", "no transactions-per-hour limit (0)", True, velocity == 0),
        ("allowlist-not-enforced", "major", "destination allowlist is not enforced: funds can go to any address", True, not enforced),
        ("no-cooloff", "major", "new allowlist entries are usable immediately: a compromised approver can add and use an address at once", enforced, cooloff == 0),
        ("short-cooloff", "minor", f"allowlist cool-off is under {MIN_COOLOFF_S}s", enforced and cooloff > 0, cooloff < MIN_COOLOFF_S),
        ("allowlist-never-reattested", "minor", "allowlist entries are never re-verified (0 days)", enforced, reattest == 0),
        ("allowlist-reattest-too-long", "minor", f"allowlist entries are re-verified less often than every {MAX_REATTEST_DAYS} days", enforced and reattest > 0, reattest > MAX_REATTEST_DAYS),
        ("weak-policy-change-quorum", "major", "limits and allowlist can be changed with fewer approvals than a transaction needs", True, payload["policy_change_approvals"] < payload["signing_threshold"]),
        ("approvals-never-expire", "major", "collected approvals never expire (0): they can be replayed later", True, expiry == 0),
        ("long-approval-expiry", "minor", f"approvals stay valid longer than {MAX_APPROVAL_EXPIRY_S}s", expiry > 0, expiry > MAX_APPROVAL_EXPIRY_S),
    ]
    findings = _ordered([_finding(rid, sev, text) for rid, sev, text, applies, bad in rules if applies and bad])
    return {
        "state": "SUCCESS", "rules_evaluated": sum(1 for r in rules if r[3]), "verdict": _verdict(findings), "findings": findings,
    }


def signer_policy_lint(payload: dict[str, Any]) -> dict[str, Any]:
    """Lint a signer policy: missing or non-binding limits, unenforced allowlist, cool-off under 24 h, stale allowlist
    entries (> 365 days), approvals that never expire or outlive 24 h, and a policy-change quorum below the signing threshold.
    A limit of 0 means "none configured". The 24 h, 365 d and 24 h figures are this lane's lint defaults (internal norms,
    not a standard or a regulation)."""
    return _respond(payload, _signer_policy_lint)


# --- 6. wallet-tier caps ---------------------------------------------------------------------------------------------

_TIER_ORDER = ("hot", "warm", "cold")


def _wallet_tier_caps(payload: dict[str, Any]) -> dict[str, Any]:
    seen: set[str] = set()
    for entry in payload["tiers"]:
        if entry["tier"] in seen:
            raise _Invalid("tiers", f"tier {entry['tier']!r} is listed twice")
        seen.add(entry["tier"])
    by_tier = {e["tier"]: e for e in payload["tiers"]}
    total = sum((_dec(e["balance_usd"]) for e in payload["tiers"]), Decimal(0))
    findings: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    sweep = Decimal(0)
    for tier in _TIER_ORDER:
        entry = by_tier.get(tier)
        if entry is None:
            continue
        balance = _dec(entry["balance_usd"])
        cap = total * _dec(entry["max_pct_of_total"]) / 100
        if "max_balance_usd" in entry:
            cap = min(cap, _dec(entry["max_balance_usd"]))
        excess = max(Decimal(0), balance - cap)
        sweep += excess
        if excess > 0:
            findings.append(_finding(f"{tier}-over-cap", "minor" if tier == "cold" else "major",
                                     f"{tier} holds {_num(balance, 2)} USD, {_num(excess, 2)} USD above its cap of {_num(cap, 2)} USD"))
        rows.append({
            "tier": tier, "balance_usd": _num(balance, 2), "pct_of_total": _num(balance * 100 / total, 4) if total else 0.0,
            "cap_usd": _num(cap, 2), "excess_usd": _num(excess, 2), "within_cap": excess == 0,
        })
    pct = {r["tier"]: r["pct_of_total"] for r in rows}
    cold_pct = Decimal(0) if "cold" not in by_tier or not total else _dec(by_tier["cold"]["balance_usd"]) * 100 / total
    if total > 0 and cold_pct < _dec(payload["min_cold_pct"]):
        findings.append(_finding("cold-below-minimum", "major", f"cold holds {_num(cold_pct, 2)}% of the total, below the {payload['min_cold_pct']}% minimum"))
    findings = _ordered(findings)
    return {
        "state": "SUCCESS", "total_usd": _num(total, 2), "hot_pct": pct.get("hot", 0.0), "cold_pct": pct.get("cold", 0.0),
        "sweep_required_usd": _num(sweep, 2), "tiers": rows,
        "verdict": _verdict(findings), "findings": findings,
    }


def wallet_tier_caps(payload: dict[str, Any]) -> dict[str, Any]:
    """Each tier's balance against min(max_pct_of_total x total, max_balance_usd); a balance equal to its cap is within it.
    Cold must hold at least min_cold_pct of the total. Excess is what must be swept out of that tier."""
    return _respond(payload, _wallet_tier_caps)


# --- 7. key rotation -------------------------------------------------------------------------------------------------


def _days(delta: timedelta) -> float:
    """Days to 2 decimals; `+ 0.0` turns a rounded -0.0 (overdue by under 7.2 minutes) into 0.0."""
    return round(delta.total_seconds() / SECONDS_PER_DAY, 2) + 0.0


def _key_rotation(payload: dict[str, Any]) -> dict[str, Any]:
    as_of = _ts("as_of", payload["as_of"])
    warn = timedelta(days=payload["warn_days"])
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for key in payload["keys"]:
        key_id = _ident("keys", key["key_id"])
        if _norm(key_id) in seen:
            raise _Invalid("keys", f"duplicate key_id {key_id!r}")
        seen.add(_norm(key_id))
        created = _ts("keys", key["created_at"])
        reference = created
        if key["last_rotated_at"]:
            reference = _ts("keys", key["last_rotated_at"])
            if reference < created:
                raise _Invalid("keys", f"last_rotated_at precedes created_at for {key_id!r}")
        if reference > as_of:
            raise _Invalid("keys", f"{key_id!r} was created or rotated after as_of")
        maximum = timedelta(days=key["max_age_days"])
        age = as_of - reference
        remaining = maximum - age
        if age > maximum:
            status = "overdue"
            blocker = age > 2 * maximum
            findings.append(_finding(f"rotation-overdue:{key_id}", "blocker" if blocker else "major",
                                     f"{key_id} is {_days(age)} days old, maximum {key['max_age_days']}"))
        elif remaining <= warn:
            status = "due_soon"
            findings.append(_finding(f"rotation-due-soon:{key_id}", "minor",
                                     f"{key_id} reaches its {key['max_age_days']}-day maximum in {_days(remaining)} days"))
        else:
            status = "ok"
        rows.append({"key_id": key_id, "age_days": _days(age), "days_remaining": _days(remaining), "status": status})
    rows.sort(key=lambda r: (r["days_remaining"], r["key_id"]))
    findings = _ordered(findings)
    count = Counter(r["status"] for r in rows)
    return {
        "state": "SUCCESS", "key_count": len(rows), "overdue_count": count["overdue"], "due_soon_count": count["due_soon"],
        "ok_count": count["ok"], "keys": rows, "verdict": _verdict(findings), "findings": findings,
    }


def key_rotation(payload: dict[str, Any]) -> dict[str, Any]:
    """Age (since last rotation, else creation) against max_age_days: overdue when age > maximum, due soon when the time left
    is <= warn_days, else ok. Overdue by more than twice the maximum is a blocker (a house escalation rule; the maximum itself
    is the caller's policy, NIST SP 800-57 only gives cryptoperiod guidance)."""
    return _respond(payload, _key_rotation)


# --- 8. transaction policy evaluation --------------------------------------------------------------------------------

ZERO_ADDRESS = "0x" + "0" * 40


def _tx_policy_eval(payload: dict[str, Any]) -> dict[str, Any]:
    policy = payload["policy"]
    target = _address("to", payload["to"])
    allow = {_address("policy", a) for a in policy["allowlist"]}
    deny = {_address("policy", a) for a in policy["denylist"]}
    overlap = sorted(allow & deny)
    if overlap:
        raise _Invalid("policy", "an address is both allowlisted and denylisted")
    selector = payload["selector"].lower()
    if selector and (len(selector) != 10 or not selector.startswith("0x") or any(c not in _HEXDIGITS for c in selector[2:])):
        raise _Invalid("selector", "selector must be empty or 0x followed by 8 hex digits (4 bytes)")
    selector = selector[2:]
    value, spent = _dec(payload["value_usd"]), _dec(payload["spent_24h_usd"])
    findings: list[dict[str, Any]] = []

    def deny_(rule: str, text: str) -> None:
        findings.append(_finding(rule, "blocker", text))

    def hold_(rule: str, text: str) -> None:
        findings.append(_finding(rule, "major", text))

    if payload["chain_id"] not in policy["allowed_chain_ids"]:
        deny_("chain-not-allowed", f"chain id {payload['chain_id']} is not in the allowed chain list (EIP-155 replay domain)")
    if target == ZERO_ADDRESS:
        deny_("destination-zero-address", "the zero address burns the funds")
    if target in deny:
        deny_("destination-denylisted", "the destination is on the denylist")
    if payload["operation"] == "delegatecall" and not policy["allow_delegatecall"]:
        deny_("delegatecall-blocked", "delegatecall runs foreign code in the wallet's own context and is not allowed by policy")
    if value > _dec(policy["per_tx_limit_usd"]):
        deny_("per-tx-limit-exceeded", f"{value} USD exceeds the per-transaction limit of {policy['per_tx_limit_usd']} USD")
    if spent + value > _dec(policy["daily_limit_usd"]):
        deny_("daily-limit-exceeded", f"{spent + value} USD within 24 h exceeds the daily limit of {policy['daily_limit_usd']} USD")
    listed = target in allow
    if target not in deny and target != ZERO_ADDRESS:
        if not listed:
            hold_("destination-not-allowlisted", "the destination is not on the allowlist")
        elif payload["destination_listed_seconds"] < policy["cooloff_seconds"]:
            hold_("destination-cooloff", f"the destination was allowlisted {payload['destination_listed_seconds']}s ago, cool-off is {policy['cooloff_seconds']}s")
    if value >= _dec(policy["hold_above_usd"]):
        hold_("manual-review-threshold", f"{value} USD is at or above the manual-review threshold of {policy['hold_above_usd']} USD")
    if selector in _RISKY_SELECTORS:
        hold_("risky-selector", f"{_SIGNATURE_BY_SELECTOR[selector]} grants authority or changes ownership/code and needs review")
    if payload["approvals"] < policy["required_approvals"]:
        hold_("insufficient-approvals", f"{payload['approvals']} of {policy['required_approvals']} required approvals")
    if payload["tx_count_1h"] + 1 > policy["max_tx_per_hour"]:
        hold_("velocity-exceeded", f"this would be transaction {payload['tx_count_1h'] + 1} in an hour, the limit is {policy['max_tx_per_hour']}")
    findings = _ordered(findings)
    denied = sum(1 for f in findings if f["severity"] == "blocker")
    held = len(findings) - denied
    return {
        "state": "SUCCESS", "decision": "deny" if denied else "hold" if held else "allow", "deny_count": denied, "hold_count": held,
        "selector_signature": "" if not selector else _SIGNATURE_BY_SELECTOR.get(selector, "unknown"), "findings": findings,
    }


def tx_policy_eval(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a transaction descriptor against policy. deny (blocker findings): chain not allowed, zero or denylisted
    destination, delegatecall, per-tx limit exceeded (>), rolling daily limit exceeded (>). hold (major): destination not
    allowlisted or inside its cool-off, value >= manual-review threshold, risky selector (token approvals and permits,
    ownership, role, upgrade and Safe signer-set/module/guard changes), too few approvals, velocity. Otherwise allow; an
    unlisted selector is reported as "unknown" and is not a finding. The selector table is a fixed list, not an ABI
    decoder: it cannot see calldata. It never signs."""
    return _respond(payload, _tx_policy_eval)


# --- 9. sanctions screening ------------------------------------------------------------------------------------------


def _sanctions_screening(payload: dict[str, Any]) -> dict[str, Any]:
    as_of = _ts("as_of", payload["as_of"])
    max_list = timedelta(hours=payload["max_list_age_hours"])
    max_screen = timedelta(hours=payload["max_screening_age_hours"])
    findings: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    stale_lists = 0
    for entry in payload["screenings"]:
        subject, lst = _ident("screenings", entry["subject_id"]), _ident("screenings", entry["list_name"])
        tag = f"{subject}@{lst}"
        updated, screened = _ts("screenings", entry["list_updated_at"]), _ts("screenings", entry["screened_at"])
        if screened > as_of:
            raise _Invalid("screenings", f"{tag} was screened after as_of")
        if updated > screened:
            raise _Invalid("screenings", f"{tag}: the list version is newer than the screening")
        result, action = entry["result"], entry["action"]
        counts[result] += 1
        if screened - updated > max_list:
            stale_lists += 1
            findings.append(_finding(f"stale-list:{tag}", "major", f"{lst} was {round((screened - updated).total_seconds() / 3600, 2)} h old when used, maximum {payload['max_list_age_hours']} h"))
        if as_of - screened > max_screen and result == "clear":
            findings.append(_finding(f"stale-screening:{tag}", "minor", f"the clear result is {round((as_of - screened).total_seconds() / 3600, 2)} h old, maximum {payload['max_screening_age_hours']} h: rescreen before release"))
        if result == "error":
            if action == "released":
                findings.append(_finding(f"released-on-error:{tag}", "blocker", "released although the screening errored: fail closed"))
            elif action in ("held", "blocked"):
                findings.append(_finding(f"error-retry-pending:{tag}", "minor", "screening errored: retry before releasing"))
            else:
                findings.append(_finding(f"error-unactioned:{tag}", "major", f"screening errored and the action is {action}: hold it and retry the screen"))
        elif result == "potential_match":
            if action == "released":
                findings.append(_finding(f"potential-match-released:{tag}", "blocker", "released while the potential match is unresolved"))
            elif action in ("held", "blocked"):
                findings.append(_finding(f"potential-match-under-review:{tag}", "info", f"potential match, action {action}"))
            else:
                findings.append(_finding(f"potential-match-unactioned:{tag}", "major", f"a potential match with action {action} is not held: hold it for review"))
        elif result == "confirmed_match":
            if action == "blocked":
                findings.append(_finding(f"confirmed-match-report-pending:{tag}", "minor", "blocked but not yet reported to the regulator (OFAC: 31 CFR 501.603, 10 business days)"))
            else:
                findings.append(_finding(f"confirmed-match-not-blocked:{tag}", "blocker", f"confirmed match with action {action}: the funds must be blocked"))
        elif action in ("held", "blocked"):
            findings.append(_finding(f"clear-but-withheld:{tag}", "info", f"result is clear but the action is {action}: record why"))
    findings, omitted = _capped(findings)
    return {
        "state": "SUCCESS", "screened": len(payload["screenings"]), "stale_lists": stale_lists,
        "counts": {k: counts[k] for k in ("clear", "potential_match", "confirmed_match", "error")},
        "omitted_findings": omitted, "verdict": _verdict(findings), "findings": findings,
    }


def sanctions_screening(payload: dict[str, Any]) -> dict[str, Any]:
    """Screening hygiene: list age at screening time (> max_list_age_hours is stale), clear results older than
    max_screening_age_hours, and the action taken per match state (errors and potential matches must not be released; a
    confirmed match must be blocked, then reported). The report finding cites OFAC 31 CFR 501.603 (10 business days from the
    date the property is blocked, US persons); other regimes' deadlines differ and are not modelled."""
    return _respond(payload, _sanctions_screening)


# --- 10. Travel Rule --------------------------------------------------------------------------------------------------

def _travel_rule(payload: dict[str, Any]) -> dict[str, Any]:
    amount, threshold = _dec(payload["amount"]), _dec(payload["threshold"])
    applies = amount >= threshold if payload["comparison"] == "at_or_above" else amount > threshold
    kind, ident = payload["originator_identifier_kind"], payload["originator_identifier"]
    for field in ("originator_name", "originator_account", "originator_identifier", "beneficiary_name", "beneficiary_account"):
        _label(field, payload[field], strict=False)  # never echoed: a digest or transaction hash is a legitimate value here
    if kind == "none" and ident.strip():
        raise _Invalid("originator_identifier_kind", "an identifier is supplied but its kind is none")
    fields = {
        "originator_name": bool(payload["originator_name"].strip()),
        "originator_account": bool(payload["originator_account"].strip()),
        "originator_identifier": kind != "none" and bool(ident.strip()),
        "beneficiary_name": bool(payload["beneficiary_name"].strip()),
        "beneficiary_account": bool(payload["beneficiary_account"].strip()),
    }
    missing = sorted(k for k, present in fields.items() if not present)
    if applies:
        findings = _ordered([_finding(f"missing-{k.replace('_', '-')}", "major", f"{k} is required for a transfer of {amount} (threshold {threshold}) and is missing") for k in missing])
    else:  # FATF INR.16 para 5(a): below a de minimis threshold both names and an account or reference each still accompany the transfer
        findings = _ordered([
            _finding(f"below-threshold-missing-{k.replace('_', '-')}", "info",
                     f"{k} is missing: below the threshold FATF INR.16 para 5(a) still expects both names and an account or reference each")
            for k in missing if k in _MINIMAL_FIELDS
        ])
    status = "not_applicable" if not applies else "incomplete" if missing else "complete"
    return {
        "state": "SUCCESS", "applies": applies, "status": status, "required_fields": len(fields) if applies else 0,
        "present_fields": len(fields) - len(missing), "missing_fields": missing if applies else [], "findings": findings,
    }


_MINIMAL_FIELDS = frozenset(("originator_name", "originator_account", "beneficiary_name", "beneficiary_account"))


def travel_rule(payload: dict[str, Any]) -> dict[str, Any]:
    """FATF R.16 (INR.16 para 6, with para 7 for a unique transaction reference when there is no account): at or above the
    threshold the originator name, account and one identifier (address, national id, customer id, or date and place of
    birth), plus the beneficiary name and account, must accompany the transfer. Presence only, nothing is verified.
    The threshold is the caller's regime, not FATF's: INR.16 para 5 allows a de minimis of at most USD/EUR 1,000 and words
    qualifying transfers as "above any applicable threshold" (equality unspecified); US 31 CFR 1010.410(f) is USD 3,000 or
    more; the EU Regulation 2023/1113 sets none for crypto-asset transfers. `comparison` selects at_or_above (>=) or above
    (>). Below the threshold the full set is not required, but a missing name or account is reported as info (para 5(a))."""
    return _respond(payload, _travel_rule)


# --- 11. audit trail --------------------------------------------------------------------------------------------------

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_CHAIN_RULES = frozenset(("hash-mismatch", "chain-broken", "seq-gap", "duplicate-seq", "genesis-mismatch"))


def _event_hash(event: dict[str, Any]) -> str:
    """sha256 over `seq|at|actor|action|approver1,approver2,...|prev_hash` (approvers sorted, UTF-8)."""
    body = f"{event['seq']}|{event['at']}|{event['actor']}|{event['action']}|{','.join(sorted(event['approvers']))}|{event['prev_hash']}"
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _audit_trail(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload["events"]
    required_actions = {_norm(a) for a in payload["approval_required_actions"]} - {""}
    min_approvals = payload["min_approvals"]
    genesis = payload.get("genesis_hash", GENESIS_HASH)
    if not _HASH_RE.fullmatch(genesis):
        raise _Invalid("genesis_hash", "genesis_hash must be 64 lower-case hex digits")
    for event in events:
        for field in ("prev_hash", "hash"):
            if not _HASH_RE.fullmatch(event[field]):
                raise _Invalid("events", f"event {event['seq']}: {field} must be 64 lower-case hex digits")
        for text in (event["actor"], event["action"], *event["approvers"]):
            if "|" in text or "," in text:
                raise _Invalid("events", f"event {event['seq']}: actor, action and approvers must not contain '|' or ','")
            _label("events", text)
        _ts("events", event["at"])
    ordered = sorted(events, key=lambda e: e["seq"])
    findings: list[dict[str, Any]] = []
    breaks: list[int] = []

    def flag(rule: str, severity: str, seq: int, text: str) -> None:
        findings.append(_finding(f"{rule}:{seq}", severity, text))
        if rule in _CHAIN_RULES:
            breaks.append(seq)

    previous = None
    for event in ordered:
        seq = event["seq"]
        if previous is None:
            if event["prev_hash"] != genesis:
                flag("genesis-mismatch", "blocker", seq, "the first event does not link to the genesis hash")
        elif seq == previous["seq"]:
            flag("duplicate-seq", "blocker", seq, "two events share this sequence number")
        else:
            if seq != previous["seq"] + 1:
                flag("seq-gap", "blocker", seq, f"events {previous['seq'] + 1}..{seq - 1} are missing")
            if event["prev_hash"] != previous["hash"]:
                flag("chain-broken", "blocker", seq, "prev_hash does not match the hash of the preceding event")
            if _ts("events", event["at"]) < _ts("events", previous["at"]):
                flag("time-regression", "major", seq, "this event is dated earlier than the preceding one")
        if event["hash"] != _event_hash(event):
            flag("hash-mismatch", "blocker", seq, "the stored hash does not match the event content")
        if not event["actor"].strip():
            flag("missing-actor", "major", seq, "no actor recorded")
        if not event["action"].strip():
            flag("missing-action", "major", seq, "no action recorded")
        if _norm(event["action"]) in required_actions:
            actor = _norm(event["actor"])
            approvers = {_norm(a) for a in event["approvers"]} - {""}
            if actor and actor in approvers:
                flag("self-approval", "major", seq, "the actor is among the approvers")
            if len(approvers - {actor}) < min_approvals:
                flag("insufficient-approvals", "major", seq, f"{event['action'].strip()} needs {min_approvals} independent approval(s)")
        previous = event
    findings, omitted = _capped(findings)
    return {
        "state": "SUCCESS", "event_count": len(events), "chain_valid": not breaks, "first_break_seq": min(breaks) if breaks else -1,
        "omitted_findings": omitted, "verdict": _verdict(findings), "findings": findings,
    }


def audit_trail(payload: dict[str, Any]) -> dict[str, Any]:
    """Verify an audit trail: contiguous sequence, SHA-256 hash chain from the genesis hash (see _event_hash), non-decreasing
    timestamps, actor and action present, and independent approvals (excluding the actor) for the listed actions."""
    return _respond(payload, _audit_trail)


# --- 12. recovery drill -----------------------------------------------------------------------------------------------


def _recovery_drill(payload: dict[str, Any]) -> dict[str, Any]:
    as_of = _ts("as_of", payload["as_of"])
    maximum = timedelta(days=payload["max_age_days"])
    target = _dec(payload["target_rto_minutes"])
    by_scenario: dict[str, dict[str, Any]] = {}
    for drill in payload["drills"]:
        if drill["scenario"] in by_scenario:
            raise _Invalid("drills", f"scenario {drill['scenario']!r} appears twice")
        when = _ts("drills", drill["last_drill_at"])
        if when > as_of:
            raise _Invalid("drills", f"the {drill['scenario']} drill is dated after as_of")
        by_scenario[drill["scenario"]] = {**drill, "when": when}
    rows: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    for scenario in sorted(set(payload["required_scenarios"])):
        drill = by_scenario.get(scenario)
        age: float | None = None  # absent for a scenario never drilled: there is no age to report
        if drill is None:
            status = "never"
            findings.append(_finding(f"never-drilled:{scenario}", "major", f"no {scenario} drill on record"))
        else:
            age_td = as_of - drill["when"]
            age = round(age_td.total_seconds() / SECONDS_PER_DAY, 2)
            if not drill["passed"]:
                status = "failed"
                findings.append(_finding(f"drill-failed:{scenario}", "blocker", f"the last {scenario} drill failed: recovery is unproven"))
            elif _dec(drill["measured_rto_minutes"]) > target:
                status = "rto_exceeded"
                findings.append(_finding(f"rto-exceeded:{scenario}", "major", f"recovery took {drill['measured_rto_minutes']} min, target {payload['target_rto_minutes']} min"))
            elif age_td > maximum:
                status = "overdue"
                findings.append(_finding(f"drill-overdue:{scenario}", "major", f"last drill {age} days ago, maximum {payload['max_age_days']}"))
            else:
                status = "current"
        row = {"scenario": scenario, "status": status}
        if age is not None:
            row["age_days"] = age
        rows.append(row)
    findings = _ordered(findings)
    count = Counter(r["status"] for r in rows)
    return {
        "state": "SUCCESS", "required_count": len(rows),
        "counts": {k: count[k] for k in ("current", "overdue", "failed", "rto_exceeded", "never")},
        "scenarios": rows, "verdict": _verdict(findings), "findings": findings,
    }


def recovery_drill(payload: dict[str, Any]) -> dict[str, Any]:
    """For each required scenario: never drilled, failed, over the RTO target (>), overdue (age > max_age_days) or current,
    in that order of precedence."""
    return _respond(payload, _recovery_drill)


# --- 13. key-material leak scan ---------------------------------------------------------------------------------------


def _line_of(newlines: list[int], index: int) -> int:
    """1-based line of a character offset, from the sorted offsets of every newline (the count of those before it, plus one)."""
    return bisect.bisect_left(newlines, index) + 1


def _scan_content(path: str, content: str) -> list[dict[str, Any]]:
    """Findings for one file. Only the location, the kind and the length of the matched text are reported."""
    hits: list[tuple[str, str, int, int]] = []  # (kind, severity, start offset, length)
    ends: list[int] | None = None
    for match in _PEM_RE.finditer(content):
        if ends is None:
            ends = [m.start() for m in re.finditer("-----END", content)]
        slot = bisect.bisect_left(ends, match.end())
        end = ends[slot] if slot < len(ends) else -1
        close = content.find("-----", end + 8) if end != -1 else -1
        stop = close + 5 if close != -1 else match.end()
        encrypted = "ENCRYPTED" in match.group(1)
        hits.append(("pem-private-key", "major" if encrypted else "blocker", match.start(), stop - match.start()))
    for match in _XPRV_RE.finditer(content):
        if _is_xprv(match.group()):
            hits.append(("extended-private-key", "blocker", match.start(), len(match.group())))
    for match in _WIF_RE.finditer(content):
        if _is_wif(match.group()):
            hits.append(("wif-private-key", "blocker", match.start(), len(match.group())))
    for match in _HEX_KEY_RE.finditer(content):
        digits = match.group(1)
        if len(set(digits.lower())) > 1:  # 000..0 and fff..f are placeholders, not keys
            hits.append(("hex-private-key", "blocker", match.start(1), len(digits)))
    for start, length in _mnemonic_matches(content):
        hits.append(("mnemonic-phrase", "blocker", start, length))
    if re.search(r'"ciphertext"\s*:', content) and re.search(r'"kdf"\s*:', content):
        start = content.index('"ciphertext"')
        hits.append(("encrypted-keystore", "major", start, len(content) - start))
    if not hits:
        return []
    newlines = [m.start() for m in re.finditer("\n", content)]
    out = []
    for kind, severity, start, length in hits:
        line = _line_of(newlines, start)
        out.append({
            "id": f"{kind}:{path}:{line}", "severity": severity, "kind": kind, "location": f"{path}:{line}", "length": length,
            "summary": f"{kind} found at {path} line {line} ({length} characters); the value is not reproduced",
        })
    return out


def _path_label(path: str, ordinal: int) -> str:
    """The path as findings and reasons show it. A path is user data too: when any part of it is secret-shaped (a cloud key id,
    token, credentialed URL, key, phrase, or a key-name assignment with a key-shaped value such as secret=<64 hex>), the whole
    path is replaced by its 1-based position label. A bare 64-digit hex file name is a content-addressed artifact and stays."""
    return f"<redacted-path-{ordinal}>" if _secret_kind(path, strict=False) else path


def _secret_scan(payload: dict[str, Any]) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(payload["files"]):
        if entry["path"] in seen:
            raise _Invalid("files", f"files[{index}] repeats an earlier path")
        seen.add(entry["path"])
        shown = _path_label(entry["path"], index + 1)
        findings.extend(_scan_content(shown, entry["content"]))
    findings.sort(key=lambda f: (-SEVERITY_RANK[f["severity"]], f["location"], f["kind"]))
    return {
        "state": "SUCCESS", "files_scanned": len(payload["files"]), "finding_count": len(findings),
        "omitted_findings": max(0, len(findings) - MAX_FINDINGS), "verdict": _verdict(findings), "findings": findings[:MAX_FINDINGS],
    }


def secret_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Find private-key material in supplied file contents: PEM private-key blocks, BIP32 extended private keys and WIF keys
    (both verified by their Base58Check checksum), 64-digit hex values assigned to private/secret/signing-key names,
    12/15/18/21/24-word mnemonics assigned to mnemonic/seed-phrase names, and encrypted keystore JSON. Reports location
    (path:line), kind and length only, never the value."""
    return _respond(payload, _secret_scan)
