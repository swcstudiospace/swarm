"""File-claim table: which in-flight run may touch which repo paths.

A claim is a repo-relative glob (`*`, `?`, `[..]` within a segment, `**` across segments) plus a mode. Two claims
conflict when their patterns can match a common path AND at least one of them is exclusive; shared+shared never
conflicts. A trailing `/` means the whole directory (`src/` == `src/**`); an EXCLUSIVE bare name such as `src` (no glob
character in its last segment) reserves that path and everything beneath it, so it conflicts with `src/a.py` too; a
shared bare name is just that one path.
Overlap is decided conservatively: when in doubt (character classes) two patterns are reported as overlapping.

`make_claim_table()` is the one place the scheduler obtains its table. `SWARM_CLAIMS_ENGINE=rust` opts in to `NativeClaimTable`, the same
interface over `swarm._native.ClaimTable` (crate gsd-sched-core); the default stays this module's Python `ClaimTable`, the oracle.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from ._native_loader import load_native, pure_python_requested

EXCLUSIVE = "exclusive"
SHARED = "shared"
MODES = (EXCLUSIVE, SHARED)
_DRIVE = re.compile(r"^[A-Za-z]:")
_CLASS = re.compile(r"\[(?:[^\]]|(?<=\[)\])+\]")


def normalize_pattern(pattern: str) -> str:
    """Canonical repo-relative form; raises ValueError for empty, absolute or `..` patterns."""
    if not isinstance(pattern, str):
        raise ValueError(f"claim pattern must be a string, got {type(pattern).__name__}")
    p = pattern.strip().replace("\\", "/")
    if not p:
        raise ValueError("claim pattern is empty")
    if p.startswith("/") or p.startswith("~") or _DRIVE.match(p):
        raise ValueError(f"claim pattern must be repo-relative, got {pattern!r}")
    directory = p.endswith("/")
    segments: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ValueError(f"claim pattern must not contain '..': {pattern!r}")
        if seg == "**" and segments and segments[-1] == "**":
            continue
        segments.append(seg)
    if not segments:
        raise ValueError(f"claim pattern is empty: {pattern!r}")
    if directory and segments[-1] != "**":
        segments.append("**")
    return "/".join(segments)


@dataclass(frozen=True)
class Claim:
    """One claimed pattern; `pattern` is normalized on construction."""

    pattern: str
    mode: str = EXCLUSIVE

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"claim mode must be one of {MODES}, got {self.mode!r}")
        object.__setattr__(self, "pattern", normalize_pattern(self.pattern))


@dataclass(frozen=True)
class ClaimConflict:
    """A requested claim that collides with a claim held by another owner."""

    owner: str
    held: Claim
    requested: Claim


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    conflicts: tuple[ClaimConflict, ...] = ()


def parse_claims(notes: Any) -> list[Claim]:
    """Claims from `notes["claims"]`: each item is a pattern string (exclusive) or `{"pattern", "mode"}`.

    Missing notes or claims yield []; a malformed item raises ValueError (fail closed rather than run unclaimed).
    """
    if not isinstance(notes, dict):
        return []
    raw = notes.get("claims")
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("notes.claims must be a list")
    claims: list[Claim] = []
    for item in raw:
        if isinstance(item, Claim):
            claims.append(item)
        elif isinstance(item, str):
            claims.append(Claim(item))
        elif isinstance(item, dict) and "pattern" in item:
            claims.append(Claim(item["pattern"], item.get("mode", EXCLUSIVE)))
        else:
            raise ValueError(f"unsupported claim entry: {item!r}")
    return claims


def _tokens(segment: str) -> tuple[str, ...]:
    """Segment glob -> tokens: '*' (any run), '?' (any one char; also stands in for a [..] class), or a literal char."""
    out: list[str] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "[":
            m = _CLASS.match(segment, i)
            if m:
                out.append("?")
                i = m.end()
                continue
        if ch == "*":
            if not out or out[-1] != "*":
                out.append("*")
        elif ch == "?":
            out.append("?")
        else:
            out.append("L" + ch)
        i += 1
    return tuple(out)


@lru_cache(maxsize=4096)
def _segments_overlap(a: str, b: str) -> bool:
    if a == b:
        return True
    ta, tb = _tokens(a), _tokens(b)

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> bool:
        if i == len(ta) and j == len(tb):
            return True
        if i < len(ta) and ta[i] == "*":
            if go(i + 1, j) or (j < len(tb) and go(i, j + 1)):
                return True
        if j < len(tb) and tb[j] == "*":
            if go(i, j + 1) or (i < len(ta) and go(i + 1, j)):
                return True
        if i < len(ta) and j < len(tb) and ta[i] != "*" and tb[j] != "*":
            x, y = ta[i], tb[j]
            if x == "?" or y == "?" or x == y:
                return go(i + 1, j + 1)
        return False

    return go(0, 0)


@lru_cache(maxsize=4096)
def _overlap_normalized(a: str, b: str) -> bool:
    sa, sb = tuple(a.split("/")), tuple(b.split("/"))

    @lru_cache(maxsize=None)
    def go(i: int, j: int) -> bool:
        if i == len(sa) and j == len(sb):
            return True
        if i < len(sa) and sa[i] == "**":
            if go(i + 1, j) or (j < len(sb) and go(i, j + 1)):
                return True
        if j < len(sb) and sb[j] == "**":
            if go(i, j + 1) or (i < len(sa) and go(i + 1, j)):
                return True
        if i < len(sa) and j < len(sb) and sa[i] != "**" and sb[j] != "**":
            return _segments_overlap(sa[i], sb[j]) and go(i + 1, j + 1)
        return False

    return go(0, 0)


def overlaps(a: str | Claim, b: str | Claim) -> bool:
    """True when two patterns can match a common repo path. Raises ValueError for absolute, `..` or empty patterns."""
    pa = a.pattern if isinstance(a, Claim) else normalize_pattern(a)
    pb = b.pattern if isinstance(b, Claim) else normalize_pattern(b)
    return _overlap_normalized(pa, pb)


_GLOB_CHARS = re.compile(r"[*?\[]")


def _footprint(c: Claim) -> tuple[str, ...]:
    """Patterns an EXCLUSIVE claim really reserves: a bare name (`swarm/tools`, last segment without glob characters) is
    the name AND everything under it, so claiming a directory without the trailing slash cannot silently allow
    overlapping edits. Shared claims and glob-ended patterns stand for themselves."""
    if c.mode == EXCLUSIVE and not _GLOB_CHARS.search(c.pattern.rsplit("/", 1)[-1]):
        return (c.pattern, f"{c.pattern}/**")
    return (c.pattern,)


def _conflicts(x: Claim, y: Claim) -> bool:
    if x.mode != EXCLUSIVE and y.mode != EXCLUSIVE:
        return False
    return any(_overlap_normalized(a, b) for a in _footprint(x) for b in _footprint(y))


class ClaimTable:
    """Thread-safe registry of claims per owner (a run id). `acquire` is all-or-nothing."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._held: dict[str, list[Claim]] = {}

    def acquire(self, owner: str, claims: list[Claim] | tuple[Claim, ...]) -> ClaimResult:
        """Take every claim for `owner`, or none if any conflicts with another owner's claim.

        An owner never conflicts with itself, and re-acquiring a claim it already holds is a no-op.
        """
        requested = list(claims)
        with self._lock:
            conflicts = [
                ClaimConflict(other, held, want)
                for want in requested
                for other, held_claims in self._held.items()
                if other != owner
                for held in held_claims
                if _conflicts(want, held)
            ]
            if conflicts:
                return ClaimResult(False, tuple(conflicts))
            if requested:
                mine = self._held.setdefault(owner, [])
                for want in requested:
                    if want not in mine:
                        mine.append(want)
            return ClaimResult(True, ())

    def release(self, owner: str) -> int:
        """Drop everything `owner` holds; idempotent. Returns how many claims were released."""
        with self._lock:
            return len(self._held.pop(owner, ()))

    def held(self) -> dict[str, tuple[Claim, ...]]:
        """Snapshot of owner -> claims; later table changes do not alter it."""
        with self._lock:
            return {owner: tuple(claims) for owner, claims in self._held.items()}


class ClaimStore(Protocol):
    """What the scheduler needs of a claim table: the Python `ClaimTable` and `NativeClaimTable` both provide it."""

    def acquire(self, owner: str, claims: list[Claim] | tuple[Claim, ...]) -> ClaimResult: ...

    def release(self, owner: str) -> int: ...

    def held(self) -> dict[str, tuple[Claim, ...]]: ...


# Opt-in Rust claim table (`swarm._native.ClaimTable`, crate gsd-sched-core). The Python `ClaimTable` above is the oracle and the default;
# `SWARM_CLAIMS_ENGINE=rust` only swaps who decides conflicts. A configuration typo, a missing or stale extension and a native error all
# end with the Python table (the last one in the middle of a run, with the claims held so far), each reason reported once per process.
_ENGINE_ENV = "SWARM_CLAIMS_ENGINE"
_engine_log = logging.getLogger("swarm.claims")
# Reasons already reported this process. The two extension reasons double as a negative cache (no import retry per table); tests reset it
# with `monkeypatch.setattr(claims, "_ENGINE_WARNED", set())`.
_ENGINE_WARNED: set[str] = set()
_engine_warn_lock = threading.Lock()


def _engine_warn_once(reason: str, message: str) -> None:
    with _engine_warn_lock:
        if reason in _ENGINE_WARNED:
            return
        _ENGINE_WARNED.add(reason)
    _engine_log.warning("%s: %s; using the Python claim table", _ENGINE_ENV, message)


class NativeClaimTable:
    """`ClaimTable` semantics over `swarm._native.ClaimTable`: same results, held view, owner identity, first conflict owner and idempotent
    release, so the scheduler cannot tell them apart.

    Threads. The native class is `frozen` (not `unsendable`): it may be created in one thread and used from any other, and its own mutex
    serialises calls. This adapter adds one re-entrant lock, like the Python table's, because it also keeps a journal of what the native
    table holds (`Claim` objects per owner in acquisition order): the journal is what maps a native conflict back to the modes of the two
    claims, and what the Python table is rebuilt from when the native one fails, so journal and native table must change in one order.

    A native failure (any exception, including an adapter/journal disagreement) is never raised to the caller: the table is rebuilt as a
    Python `ClaimTable` holding the journal's claims, the failed call is answered by it, and every later call goes to it.
    """

    def __init__(self, native_table: Any) -> None:
        self._lock = threading.RLock()
        self._native: Any = native_table
        self._python: ClaimTable | None = None
        self._journal: dict[str, dict[Claim, None]] = {}

    def acquire(self, owner: str, claims: list[Claim] | tuple[Claim, ...]) -> ClaimResult:
        """As `ClaimTable.acquire`; the conflicts name the same holders, held and requested claims in the same order."""
        requested = list(claims)
        pairs = [(claim.pattern, claim.mode) for claim in requested]
        with self._lock:
            if self._python is not None:
                return self._python.acquire(owner, requested)
            try:
                ok, raw = self._native.acquire(owner, pairs)
                if not ok:
                    return ClaimResult(False, self._map_conflicts(requested, raw))
                if requested:
                    self._journal.setdefault(owner, {}).update(dict.fromkeys(requested))
                return ClaimResult(True, ())
            except Exception as exc:  # noqa: BLE001 - any native failure must degrade to the oracle, never propagate
                return self._fall_back(exc).acquire(owner, requested)

    def release(self, owner: str) -> int:
        """As `ClaimTable.release`: drops everything `owner` holds, idempotent, returns how many claims that was."""
        with self._lock:
            if self._python is not None:
                return self._python.release(owner)
            try:
                count = int(self._native.release(owner))
                self._journal.pop(owner, None)
                return count
            except Exception as exc:  # noqa: BLE001 - see acquire
                return self._fall_back(exc).release(owner)

    def held(self) -> dict[str, tuple[Claim, ...]]:
        """As `ClaimTable.held`: a snapshot of owner -> claims in acquisition order."""
        with self._lock:
            if self._python is not None:
                return self._python.held()
            try:
                return {owner: tuple(Claim(pattern, mode) for pattern, mode in claims) for owner, claims in self._native.held().items()}
            except Exception as exc:  # noqa: BLE001 - see acquire
                return self._fall_back(exc).held()

    def _map_conflicts(self, requested: list[Claim], raw: list[tuple[str, str, str]]) -> tuple[ClaimConflict, ...]:
        """The native `(holder, held_pattern, requested_pattern)` triples as `ClaimConflict`s.

        A triple does not say which claims were meant: `a` may be held, or requested, in both modes, and a bare-name exclusive claim reaches
        under its name while a shared one does not, so the modes decide whether two patterns collide. The native table lists one triple
        per conflicting (requested claim, held claim) pair: requested claims in order, then holders, and each holder's claims, in
        acquisition order. So the n-th triple of a key is the n-th conflicting pair of that key in the same order; a key with a single
        candidate pair needs no check (the native table said it conflicts), a key with several keeps those that `_conflicts` accepts.
        """
        wants: dict[str, list[Claim]] = {}
        for want in requested:
            wants.setdefault(want.pattern, []).append(want)
        replay: dict[tuple[str, str, str], Iterator[tuple[Claim, Claim]]] = {}
        conflicts: list[ClaimConflict] = []
        for holder, held_pattern, want_pattern in raw:
            key = (holder, held_pattern, want_pattern)
            pairs = replay.get(key)
            if pairs is None:
                held_claims = self._journal[holder]
                helds = [claim for claim in (Claim(held_pattern, EXCLUSIVE), Claim(held_pattern, SHARED)) if claim in held_claims]
                if len(helds) == 2:  # both modes held: acquisition order decides which comes first
                    helds = [claim for claim in held_claims if claim.pattern == held_pattern]
                candidates = [(want, held) for want in wants[want_pattern] for held in helds]
                pairs = iter(candidates if len(candidates) < 2 else [pair for pair in candidates if _conflicts(*pair)])
                replay[key] = pairs
            want, held = next(pairs)
            conflicts.append(ClaimConflict(holder, held, want))
        return tuple(conflicts)

    def _fall_back(self, exc: Exception) -> ClaimTable:
        """Replaces the native table by a Python `ClaimTable` holding the journal's claims; the caller repeats the failed call on it."""
        _engine_warn_once("native-error", f"swarm._native.ClaimTable failed ({type(exc).__name__})")
        table = ClaimTable()
        table._held = {owner: list(claims) for owner, claims in self._journal.items()}
        self._python, self._native, self._journal = table, None, {}
        return table


def _rust_engine_selected() -> bool:
    """True when `SWARM_CLAIMS_ENGINE=rust` is set and not overridden. Unset, empty or `python` is the default; `SWARM_PURE_PYTHON=1`
    always wins over `rust`; any other value warns once and selects Python."""
    engine = os.environ.get(_ENGINE_ENV, "")
    if engine in ("", "python"):
        return False
    if engine != "rust":
        _engine_warn_once("unknown-engine", f"unknown value {engine!r} (expected 'python' or 'rust')")
        return False
    if pure_python_requested():
        _engine_warn_once("pure-python", "'rust' requested but SWARM_PURE_PYTHON=1 is set")
        return False
    return True


def make_claim_table() -> ClaimStore:
    """A new, empty claim table for one scheduler: the Python `ClaimTable`, or a `NativeClaimTable` when `SWARM_CLAIMS_ENGINE=rust` is
    selected and `swarm._native` provides `ClaimTable`. Reads the environment on every call."""
    if not _rust_engine_selected() or "missing-extension" in _ENGINE_WARNED or "stale-extension" in _ENGINE_WARNED:
        return ClaimTable()
    try:
        native = load_native()
    except ImportError as exc:
        _engine_warn_once("missing-extension", f"swarm._native is unavailable ({exc})")
        return ClaimTable()
    native_class = getattr(native, "ClaimTable", None)
    if native_class is None:
        _engine_warn_once("stale-extension", "swarm._native has no ClaimTable (stale build: reinstall with `pip install .`)")
        return ClaimTable()
    try:
        return NativeClaimTable(native_class())
    except Exception as exc:  # noqa: BLE001 - any native failure must degrade to the oracle, never propagate
        _engine_warn_once("native-error", f"swarm._native.ClaimTable failed ({type(exc).__name__})")
        return ClaimTable()
