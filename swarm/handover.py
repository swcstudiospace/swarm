"""Versioned handover protocol between agents (v2.2 DESIGN 4.7, schema `swarm.handover.v1`).

A handover is the document one agent leaves for the next: goal, what is done and what remains, the constraints, the
artifacts with digests, open questions and blockers, and how far the work was verified. It must be enough to continue
from the document alone.

Storage: `<swarm_dir>/handovers/<correlation_id>/<task_id>.<seq>.json`, one canonical-JSON file per handover. `seq` is
monotonic per task, race-safe across processes and gap-free: the complete file is written to a temp file and published
with `os.link` (exclusive, atomic), so a writer that dies never leaves a visible reservation. Identity in documents is
the AGENT ID (`A05`), never a slug.

Integrity: `digest` is sha256 over the canonical JSON (sorted keys, compact separators) of the document without its
`digest` field. Write and read both validate against `schemas/handover.v1.json` plus the semantic rules of
`validate_handover`, and read recomputes the digest, so a modified file is rejected.

Everything here reads the Task Store through a read-only connection; the only writes are under `<swarm_dir>`.
"""
from __future__ import annotations

import contextlib
import copy
import errno
import functools
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, NamedTuple, cast

from .clock import SYSTEM_CLOCK, Clock
from .errors import ErrorCode, SwarmError
from .manifest import AGENTS_FILE
from .paths import swarm_dir
from .redact import redact, redact_text
from .schema import load_schema, validate

log = logging.getLogger(__name__)

SCHEMA_VERSION = "swarm.handover.v1"
RECEIPT_SCHEMA = "swarm.handover.receipt.v1"
DEFAULT_MAX_BYTES = 16384
DEFAULT_STALE_S = 21600.0
STATUSES = ("ok", "missing", "partial", "stale", "malformed")
# sections `summarize_handover` must never shorten or drop
MANDATORY = ("goal", "remaining", "blockers", "open_questions", "constraints")
STRING_LISTS = ("completed", "remaining", "blockers", "open_questions", "constraints")
CLIP_ITEM = 120  # characters an entry of completed/decisions keeps once the document had to be shortened
MAX_FILE_BYTES = 1 << 20  # a file larger than this is never parsed, whatever the cap says
MAX_REASON = 400
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIGEST_PLACEHOLDER = "sha256:" + "0" * 64
_PENDING_ID = "9" * 6  # provisional seq width: the real id is never longer, so a size that fits now still fits later
_GENERATED = ("handover_id", "created_wall", "digest", "bytes")


class HandoverInvalid(SwarmError):
    """A handover (or a request to write or acknowledge one) that is refused. `problems` name every field at fault."""

    def __init__(self, problems: list[str] | str, *, code: ErrorCode = ErrorCode.E_INPUT):
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        first = self.problems[0] if self.problems else "invalid handover"
        more = f" (+{len(self.problems) - 1} more)" if len(self.problems) > 1 else ""
        super().__init__(code, f"{first}{more}", problems=self.problems)

    @property
    def field(self) -> str:
        """Top-level field of the first problem (`/artifacts/0/digest: ...` -> `artifacts`), else `document`."""
        match = re.match(r"^/([^/:\s]+)", self.problems[0]) if self.problems else None
        return match.group(1) if match else "document"


# ---- configuration --------------------------------------------------------------------------------------------
def _env_number(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and value > 0 else default


def max_bytes_default() -> int:
    """Size cap of one stored handover: `SWARM_HANDOVER_MAX_BYTES`, default 16384, never above `MAX_FILE_BYTES` (a larger
    file is refused by every read)."""
    return min(int(_env_number("SWARM_HANDOVER_MAX_BYTES", DEFAULT_MAX_BYTES)), MAX_FILE_BYTES)


def stale_after_default() -> float:
    """Age in seconds after which a handover is stale: `SWARM_HANDOVER_STALE_S`, default 21600."""
    return _env_number("SWARM_HANDOVER_STALE_S", DEFAULT_STALE_S)


# ---- agent identity -------------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=8)
def _agents_at(path: str, mtime_ns: int) -> tuple[frozenset[str], dict[str, str]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    agents = data["agents"] if isinstance(data, dict) else data
    ids = frozenset(str(a["id"]) for a in agents if isinstance(a, dict) and a.get("id"))
    slugs = {str(a["slug"]): str(a["id"]) for a in agents if isinstance(a, dict) and a.get("id") and a.get("slug")}
    return ids, slugs


def _agents() -> tuple[frozenset[str], dict[str, str]]:
    try:
        path = Path(os.environ.get("SWARM_AGENTS_FILE", AGENTS_FILE))
        return _agents_at(str(path), path.stat().st_mtime_ns)
    except (OSError, ValueError, KeyError, TypeError):
        return frozenset(), {}


def known_agent_ids() -> frozenset[str]:
    """Agent ids of agents.json (empty when the registry cannot be read)."""
    return _agents()[0]


def agent_id_of(value: object) -> str | None:
    """The agent id (`A05`) for an id or a slug (`a05-backend`); an unknown name comes back unchanged; None when empty."""
    if not isinstance(value, str) or not value.strip():
        return None
    ids, slugs = _agents()
    if value in ids:
        return value
    return slugs.get(value, value)


# ---- canonical form, digest, size -----------------------------------------------------------------------------
def canonical(doc: object) -> bytes:
    """Canonical JSON: sorted keys, compact separators, UTF-8."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def compute_digest(doc: dict[str, Any]) -> str:
    """`sha256:<hex>` over the canonical JSON of `doc` without its `digest` field."""
    body = {k: v for k, v in doc.items() if k != "digest"}
    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


def _plain(doc: dict[str, Any]) -> dict[str, Any]:
    """A deep copy without `digest`."""
    return {k: copy.deepcopy(v) for k, v in doc.items() if k != "digest"}


def _sized(doc: dict[str, Any]) -> dict[str, Any]:
    """A copy whose `bytes` is the exact size of the stored file (canonical JSON including the digest field).

    `bytes` is part of the file, so its digits count toward its own value: pick the width d with len(str(base + d)) == d.
    """
    out = _plain(doc)
    out["bytes"] = 0
    base = len(canonical({**out, "digest": _DIGEST_PLACEHOLDER})) - 1
    for width in range(1, 12):
        if len(str(base + width)) == width:
            out["bytes"] = base + width
            return out
    raise SwarmError(ErrorCode.E_INTERNAL, "cannot size handover")  # unreachable: a width always exists


def stored_size(doc: dict[str, Any]) -> int:
    """Bytes the stored file of `doc` takes (the digest field included)."""
    size: int = _sized(doc)["bytes"]
    return size


# ---- validation -----------------------------------------------------------------------------------------------
@functools.lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return load_schema("handover.v1")


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def validate_handover(doc: object, *, max_bytes: int | None = None) -> list[str]:
    """Every problem of `doc` as `<pointer>: <message>`; an empty list means valid.

    Schema first (`handover.v1.json`), then the rules a schema cannot express: non-blank goal, lists of non-empty
    strings, artifact digests `sha256:<64 hex>`, finite numbers, confidence inside 0..1, safe task and correlation ids,
    and agent ids (never slugs) from agents.json. `digest` may be present and is format-checked, not recomputed here.
    `max_bytes` additionally enforces the stored-size cap.
    """
    if not isinstance(doc, dict):
        return ["/: handover must be an object"]
    errors = validate({k: v for k, v in doc.items() if k != "digest"}, _schema())
    flagged = {e.split(":", 1)[0] for e in errors}

    def add(path: str, message: str) -> None:
        if path not in flagged:
            flagged.add(path)
            errors.append(f"{path}: {message}")

    if "digest" in doc and (not isinstance(doc["digest"], str) or not _DIGEST_RE.match(doc["digest"])):
        add("/digest", "must be sha256:<64 lowercase hex>")
    for key in ("task_id", "correlation_id"):
        value = doc.get(key)
        if isinstance(value, str) and value and not _ID_RE.match(value):
            add(f"/{key}", "must start with a letter or digit and use only letters, digits, '.', '_' and '-' (max 128)")
    if isinstance(doc.get("goal"), str) and not doc["goal"].strip():
        add("/goal", "must not be blank: state what the receiver is to achieve")
    for key in STRING_LISTS:
        items = doc.get(key)
        if isinstance(items, list):
            for index, item in enumerate(items):
                if _blank(item):
                    add(f"/{key}/{index}", "must be a non-empty string")
    created = doc.get("created_wall")
    if isinstance(created, (int, float)) and not isinstance(created, bool) and not math.isfinite(created):
        add("/created_wall", "must be a finite number")
    decisions = doc.get("decisions")
    if isinstance(decisions, list):
        for index, item in enumerate(decisions):
            if isinstance(item, dict) and _blank(item.get("decision")):
                add(f"/decisions/{index}/decision", "must be a non-empty string")
    ids = known_agent_ids()
    slugs = _agents()[1]

    def check_agent(path: str, value: object) -> None:
        if not isinstance(value, str) or not value:
            return
        if ids and value not in ids:
            hint = f"; use the agent id {slugs[value]}, not the slug" if value in slugs else " (an agent id such as A05)"
            add(path, f"unknown agent {value!r}{hint}")

    check_agent("/from_agent", doc.get("from_agent"))
    check_agent("/to_agent", doc.get("to_agent"))
    artifacts = doc.get("artifacts")
    if isinstance(artifacts, list):
        for index, item in enumerate(artifacts):
            if not isinstance(item, dict):
                continue
            where = f"/artifacts/{index}"
            for key in ("kind", "uri"):
                if isinstance(item.get(key), str) and not item[key].strip():
                    add(f"{where}/{key}", "must be a non-empty string")
            if isinstance(item.get("digest"), str) and not _DIGEST_RE.match(item["digest"]):
                add(f"{where}/digest", "must be sha256:<64 lowercase hex>")
            if isinstance(item.get("producer"), str):
                if not item["producer"].strip():
                    add(f"{where}/producer", "must be an agent id")
                else:
                    check_agent(f"{where}/producer", item["producer"])
    verification = doc.get("verification")
    if isinstance(verification, dict):
        confidence = verification.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and not 0 <= confidence <= 1:
            add("/verification/confidence", f"{confidence!r} must be a finite number between 0 and 1")
    if not errors:
        try:
            canonical({k: v for k, v in doc.items() if k != "digest"})
        except (ValueError, TypeError) as exc:
            errors.append(f"/: not serializable as JSON ({exc})")
    if max_bytes is not None and not errors:
        size = stored_size(doc)
        if size > max_bytes:
            errors.append(f"/bytes: document takes {size} bytes, over the {max_bytes} byte cap")
    return errors


# ---- summarization --------------------------------------------------------------------------------------------
def _clip(text: str, limit: int = CLIP_ITEM) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _first_fit(count: int, fits: Callable[[int], bool]) -> int | None:
    """Smallest k in 0..count with fits(k) true (fits is monotone: more dropped never grows the size); None if even
    dropping everything does not fit."""
    if not fits(count):
        return None
    lo, hi = 0, count
    while lo < hi:
        mid = (lo + hi) // 2
        if fits(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def summarize_handover(doc: dict[str, Any], max_bytes: int | None = None, *, best_effort: bool = False) -> dict[str, Any]:
    """The same handover within `max_bytes` (default `SWARM_HANDOVER_MAX_BYTES`), deterministically and without a model.

    A document that fits is returned unchanged apart from `bytes`. Otherwise `summarized` becomes true and, in this
    order, until it fits: long entries of `completed` and `decisions` are clipped, then the oldest `completed`, then the
    oldest `decisions`, then the oldest `artifacts`, then the oldest `verification.checks` are dropped. The first entry
    of `completed` then records what was omitted. `goal`, `remaining`, `blockers`, `open_questions` and `constraints` are
    never touched; when they alone exceed the cap the document is refused (HandoverInvalid) instead of cut, unless
    `best_effort` is set: a READ path then gets the mandatory-sections-only document, over the cap, rather than an error.
    """
    cap = max_bytes if max_bytes is not None else max_bytes_default()
    base = _plain(doc)
    if stored_size(base) <= cap:
        return _sized(base)
    completed = list(base.get("completed") or [])
    decisions = list(base.get("decisions") or [])
    artifacts = list(base.get("artifacts") or [])
    raw_verification = base.get("verification")
    verification: dict[str, Any] = raw_verification if isinstance(raw_verification, dict) else {}
    checks = list(verification.get("checks") or [])

    def build(kc: int, kd: int, ka: int, kk: int) -> dict[str, Any]:
        out = copy.deepcopy(base)
        keep_c = [_clip(str(x)) for x in completed[kc:]]
        keep_d = [{"decision": _clip(str(d.get("decision", ""))), "rationale": _clip(str(d.get("rationale", "")))}
                  for d in decisions[kd:]]
        omitted = [f"{n} {label}" for n, label in ((kc, "completed"), (kd, "decisions"), (ka, "artifacts"),
                                                   (kk, "checks")) if n]
        out["completed"] = ["[summarized: " + (f"omitted {', '.join(omitted)}; " if omitted else "")
                            + f"entries clipped to {CLIP_ITEM} characters]"] + keep_c
        out["decisions"] = keep_d
        out["artifacts"] = artifacts[ka:]
        out["verification"] = {**verification, "checks": checks[kk:]}
        out["summarized"] = True
        return out

    def fits(*ks: int) -> bool:
        return stored_size(build(*ks)) <= cap

    stages: tuple[tuple[int, Callable[[int], tuple[int, int, int, int]]], ...] = (
        (len(completed), lambda k: (k, 0, 0, 0)),
        (len(decisions), lambda k: (len(completed), k, 0, 0)),
        (len(artifacts), lambda k: (len(completed), len(decisions), k, 0)),
        (len(checks), lambda k: (len(completed), len(decisions), len(artifacts), k)),
    )
    if fits(0, 0, 0, 0):
        return _sized(build(0, 0, 0, 0))
    for count, ks in stages:
        found = _first_fit(count, lambda k: fits(*ks(k)))
        if found is not None:
            return _sized(build(*ks(found)))
    mandatory = stored_size(build(len(completed), len(decisions), len(artifacts), len(checks)))
    if best_effort:
        return _sized(build(len(completed), len(decisions), len(artifacts), len(checks)))
    raise HandoverInvalid(
        [f"/goal: goal, remaining, blockers, open_questions and constraints alone take {mandatory} bytes, over the "
         f"{cap} byte cap; they are never cut, so shorten them"])


# ---- storage layout -------------------------------------------------------------------------------------------
def handovers_dir(root: str | Path | None = None) -> Path:
    """`<swarm_dir>/handovers` (not created)."""
    return swarm_dir(root) / "handovers"


class Candidate(NamedTuple):
    correlation_id: str
    seq: int
    path: Path
    mtime_ns: int


def _safe_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise HandoverInvalid([f"/{what}: {value!r} is not a valid id (letters, digits, '.', '_', '-')"])
    return value


def list_handovers(task_id: str, *, root: str | Path | None = None, correlation_id: str | None = None) -> list[Candidate]:
    """Stored handovers of a task, oldest first. Temp files (`.<name>.<pid>.<random>.tmp`) never match and empty files
    (left only by older writers or the O_EXCL fallback) are skipped.

    Order is by `seq` within a correlation, never by file mtime (a clock that stepped back, writers finishing out of
    order or a restored copy invert mtimes). Across correlations the task's own plan (Task Store row) comes last, then
    the correlation whose newest file is newest, so the last entry is the handover the task is currently on."""
    _safe_id(task_id, "task_id")
    base = handovers_dir(root)
    if not base.is_dir():
        return []
    pattern = re.compile(re.escape(task_id) + r"\.(\d+)\.json\Z")
    dirs = [base / _safe_id(correlation_id, "correlation_id")] if correlation_id else sorted(p for p in base.iterdir() if p.is_dir())
    found: list[Candidate] = []
    for folder in dirs:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            match = pattern.match(name)
            if not match:
                continue
            try:
                info = (folder / name).stat()
            except OSError:
                continue
            if info.st_size > 0:
                found.append(Candidate(folder.name, int(match.group(1)), folder / name, info.st_mtime_ns))
    newest: dict[str, int] = {}
    for cand in found:
        newest[cand.correlation_id] = max(newest.get(cand.correlation_id, 0), cand.mtime_ns)
    planned = ""
    if len(newest) > 1:
        with StoreReader(root) as store:
            row = store.task(task_id)
        planned = str((row or {}).get("correlation_id") or "")
    found.sort(key=lambda c: (c.correlation_id == planned, newest[c.correlation_id], c.correlation_id, c.seq))
    return found


_PUBLISH_ATTEMPTS = 1000
_NO_HARDLINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}


def _publish_exclusive(folder: Path, task_id: str, build: Callable[[int], bytes]) -> tuple[int, Path, bytes]:
    """Publish one handover at the lowest free seq, atomically and exclusively; returns `(seq, path, data)`.

    `build(seq)` returns the complete file bytes for that candidate seq (the document embeds `seq`/`handover_id`, so it
    is rebuilt on every retry). The bytes are written to a unique temp file next to the target
    (`.<name>.<pid>.<random>.tmp`, which no reader lists or scans) and published with `os.link(tmp, final)`: the final
    name appears complete or not at all and fails with FileExistsError when another writer took the seq, then the next
    seq is tried. A writer that dies at any point therefore never leaves a visible reservation, so seqs have no gaps;
    its stray temp file is ignored by every reader and is deliberately not reclaimed (age is not ownership).

    Only where hard links are unsupported (EPERM/ENOTSUP/...) the file is created with O_CREAT|O_EXCL, written and
    fsynced in place. That fallback has a crash window that can leave a partial or empty file at its seq; readers treat
    a malformed newest file as malformed (never skipped) and an empty file is not listed, and the seq is never reused."""
    seq = _next_seq(folder, task_id)
    use_links = True
    for _ in range(_PUBLISH_ATTEMPTS):
        data = build(seq)
        path = folder / f"{task_id}.{seq}.json"
        if use_links:
            tmp = folder / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
            try:
                with open(tmp, "xb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                try:
                    os.link(tmp, path)
                    return seq, path, data
                except FileExistsError:  # another writer published this seq first
                    seq = max(seq + 1, _next_seq(folder, task_id))
                    continue
                except OSError as exc:
                    if exc.errno not in _NO_HARDLINKS:
                        raise
                    use_links = False  # same seq again through the O_EXCL fallback
                    continue
            finally:
                with contextlib.suppress(OSError):
                    tmp.unlink()
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            seq = max(seq + 1, _next_seq(folder, task_id))
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        return seq, path, data
    raise SwarmError(ErrorCode.E_INTERNAL, f"no free handover seq for {task_id} after {_PUBLISH_ATTEMPTS} attempts")


def _next_seq(folder: Path, task_id: str) -> int:
    """One above the highest published seq of `task_id` in `folder`. Temp files never match; a stray empty or partial
    file (older writers, the O_EXCL fallback) still counts, so its seq is never reused or removed."""
    pattern = re.compile(re.escape(task_id) + r"\.(\d+)\.json\Z")
    try:
        seqs = [int(m.group(1)) for name in os.listdir(folder) if (m := pattern.match(name))]
    except OSError:
        seqs = []
    return max(seqs, default=0) + 1


def _content_key(doc: dict[str, Any]) -> bytes:
    return canonical({k: v for k, v in doc.items() if k not in _GENERATED})


# ---- Task Store (read-only) -----------------------------------------------------------------------------------
def _loads(text: object, default: object) -> object:
    try:
        value = json.loads(text) if isinstance(text, str) and text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


class StoreReader:
    """Read-only view of `<swarm_dir>/tasks.db`. `available` is False when there is no Task Store yet; nothing is
    created and nothing is written."""

    def __init__(self, root: str | Path | None = None):
        path = swarm_dir(root) / "tasks.db"
        self.conn: sqlite3.Connection | None = None
        if path.is_file():
            self.conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA query_only=ON")

    def __enter__(self) -> StoreReader:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    @property
    def available(self) -> bool:
        return self.conn is not None

    def task(self, task_id: str) -> dict[str, Any] | None:
        """The task row with JSON columns decoded and `notes_json` (a dict), else None."""
        if self.conn is None:
            return None
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        task = dict(row)
        for col, default in (("inputs", []), ("outputs", []), ("acceptance", []), ("budget", {}), ("depends_on", [])):
            task[col] = _loads(task.get(col), default)
        task["notes_json"] = _loads(task.get("notes"), {})
        return task

    def transitions(self, task_id: str) -> list[dict[str, Any]]:
        if self.conn is None:
            return []
        return [dict(r) for r in self.conn.execute("SELECT * FROM transitions WHERE task_id=? ORDER BY id", (task_id,))]

    def artifacts(self, task_id: str) -> list[dict[str, Any]]:
        if self.conn is None:
            return []
        return [dict(r) for r in self.conn.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY id", (task_id,))]

    def verdicts(self, task_id: str, task: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Latest verdict row per gate, findings decoded, each flagged `stale`.

        `stale` is true for a verdict issued before the task's last rework loop (`notes.verdicts_since`): the verdicts
        that CAUSED a rework are always older than that cut-off, so they are kept (a rework receiver needs them) and a
        newer verdict of the same gate replaces them."""
        if self.conn is None:
            return []
        task = task or self.task(task_id)
        try:
            since = float(((task or {}).get("notes_json") or {}).get("verdicts_since") or 0)
        except (TypeError, ValueError):
            since = 0.0
        latest: dict[str, dict[str, Any]] = {}
        for row in self.conn.execute("SELECT * FROM verdicts WHERE task_id=? ORDER BY id", (task_id,)):
            item = dict(row)
            item["findings"] = _loads(item.get("findings"), [])
            ts = item.get("ts")
            item["stale"] = bool(isinstance(ts, (int, float)) and ts < since)
            latest[str(item.get("gate"))] = item
        return list(latest.values())


def read_task(task_id: str, *, root: str | Path | None = None) -> dict[str, Any] | None:
    """One Task Store row (read-only) or None when the task or the store does not exist."""
    with StoreReader(root) as store:
        return store.task(task_id)


# ---- read ------------------------------------------------------------------------------------------------------
def _parse_ref(ref: str | Path, root: str | Path | None) -> Path:
    base = handovers_dir(root).resolve()
    text = str(ref)
    if text.endswith(".json") or os.path.isabs(text):
        path = Path(text)
        path = (path if path.is_absolute() else base / path).resolve()
    elif "/" in text:  # <correlation_id>/<task_id>.<seq>
        corr, _, name = text.partition("/")
        path = (base / _safe_id(corr, "correlation_id") / f"{name}.json").resolve()
    else:  # <task_id>.<seq>: the newest across correlations
        task, _, seq = text.rpartition(".")
        if not task or not seq.isdigit():
            raise HandoverInvalid([f"/ref: {text!r} is not <task_id>.<seq>, <correlation_id>/<task_id>.<seq> or a path"])
        match = [c for c in list_handovers(task, root=root) if c.seq == int(seq)]
        if not match:
            raise HandoverInvalid([f"/ref: no handover {text!r}"])
        path = match[-1].path.resolve()
    if base != path and base not in path.parents:
        raise HandoverInvalid([f"/ref: {text!r} is outside the handover directory"])
    return path


def read_handover(ref: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """Load, validate and integrity-check one stored handover.

    `ref` is `<task_id>.<seq>`, `<correlation_id>/<task_id>.<seq>` or a path under the handover directory. Raises
    HandoverInvalid naming every problem when the file is not JSON, fails `validate_handover`, sits under the wrong
    correlation or file name, or its digest does not match its content (modified after write).
    """
    path = _parse_ref(ref, root)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        raise HandoverInvalid([f"/ref: no handover at {path.parent.name}/{path.name}"]) from None
    if size == 0:
        raise HandoverInvalid([f"/ref: {path.name} is still being written"])
    if size > MAX_FILE_BYTES:
        raise HandoverInvalid([f"/bytes: {path.name} is {size} bytes, over the {MAX_FILE_BYTES} byte read limit"])
    try:
        doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HandoverInvalid([f"/: {path.name} is not valid JSON ({exc})"]) from None
    problems = validate_handover(doc)
    if not problems:
        digest = doc.get("digest")
        if not isinstance(digest, str):
            problems.append("/digest: required")
        elif digest != compute_digest(doc):
            problems.append("/digest: does not match the content; the document was modified after it was written")
        match = re.match(r"(?P<task>.+)\.(?P<seq>\d+)\.json\Z", path.name)
        if match and (doc["task_id"] != match["task"] or doc["handover_id"] != f"{match['task']}.{match['seq']}"):
            problems.append(f"/handover_id: {doc['handover_id']!r} does not belong to file {path.name}")
        if doc["correlation_id"] != path.parent.name:
            problems.append(f"/correlation_id: {doc['correlation_id']!r} does not match directory {path.parent.name!r}")
    if problems:
        raise HandoverInvalid(problems, code=ErrorCode.E_CONTRACT)
    return doc


def latest_handover(task_id: str, *, root: str | Path | None = None, to_agent: str | None = None,
                    correlation_id: str | None = None) -> dict[str, Any] | None:
    """The newest handover of a task (optionally only one addressed to `to_agent`), None when there is none.
    A malformed newest file raises HandoverInvalid: it is never skipped to an older one."""
    for cand in reversed(list_handovers(task_id, root=root, correlation_id=correlation_id)):
        doc = read_handover(cand.path, root=root)
        if to_agent is None or doc["to_agent"] == to_agent:
            return doc
    return None


# ---- status ----------------------------------------------------------------------------------------------------
def inspect_handover(task_id: str, *, root: str | Path | None = None, clock: Clock = SYSTEM_CLOCK,
                     correlation_id: str | None = None, seq: int | None = None,
                     to_agent: str | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """`(status, document)`: `handover_status` plus the document when it is readable (ok, partial or stale). Without
    `seq` the newest handover is judged, with it that sequence number.

    A task can carry several handovers to different receivers (`A05` hands to `A15` and to `A14`). With `to_agent` (and
    no `seq`) the newest handover ADDRESSED to that agent is judged; when none is addressed to it, the newest handover
    is judged as context and the status carries `context_only: true` (a receiver writes no receipt for it). An
    unreadable handover met on the way is never skipped: its addressee is unknown, so the status is `malformed`."""
    cands = list_handovers(task_id, root=root, correlation_id=correlation_id)
    if seq is not None:
        cands = [c for c in cands if c.seq == seq]
    if not cands:
        what = f"handover {seq} of task {task_id}" if seq is not None else f"handover for task {task_id}"
        return {"status": "missing", "reasons": [f"no {what} recorded"]}, None
    with StoreReader(root) as store:
        row = store.task(task_id)
    expected = correlation_id or (row["correlation_id"] if row else None)
    scoped = [c for c in cands if c.correlation_id == expected] if expected else cands
    if not scoped:
        other = sorted({c.correlation_id for c in cands})
        return {"status": "stale", "reasons": [
            f"correlation mismatch: the task's plan is {expected!r} but its handovers belong to {', '.join(other)}"],
            "correlation_id": other[-1]}, None
    latest = scoped[-1]
    info = {"seq": latest.seq, "correlation_id": latest.correlation_id}
    try:
        doc = read_handover(latest.path, root=root)
        if to_agent is not None and seq is None and doc["to_agent"] != to_agent:
            for cand in reversed(scoped[:-1]):
                info = {"seq": cand.seq, "correlation_id": cand.correlation_id}  # a malformed file is reported under its own seq
                older = read_handover(cand.path, root=root)
                if older["to_agent"] == to_agent:
                    doc = older
                    break
            else:
                info = {"seq": latest.seq, "correlation_id": latest.correlation_id}
    except HandoverInvalid as exc:
        return {"status": "malformed", "reasons": [p[:MAX_REASON] for p in exc.problems], **info}, None
    if to_agent is not None and doc["to_agent"] != to_agent:
        info["context_only"] = True
    age = max(0.0, clock.wall() - float(doc["created_wall"]))
    info.update(handover_id=doc["handover_id"], digest=doc["digest"], from_agent=doc["from_agent"],
                to_agent=doc["to_agent"], age_s=round(age, 3))
    reasons: list[str] = []
    limit = stale_after_default()
    if age > limit:
        reasons.append(f"stale: written {age:.0f}s ago, older than the {limit:.0f}s limit (SWARM_HANDOVER_STALE_S)")
    if reasons:
        return {"status": "stale", "reasons": reasons, **info}, doc
    verification = doc["verification"]
    if not doc["remaining"] and not doc["artifacts"] and verification["status"] == "unverified":
        return {"status": "partial", "reasons": [
            "no remaining work, no artifacts and unverified: the document does not say what to continue or what exists"],
            **info}, doc
    return {"status": "ok", "reasons": [], **info}, doc


def handover_status(task_id: str, *, root: str | Path | None = None, clock: Clock = SYSTEM_CLOCK,
                    correlation_id: str | None = None, seq: int | None = None) -> dict[str, Any]:
    """`{status: ok|missing|partial|stale|malformed, reasons: [...]}` for the newest handover of `task_id`.

    missing: no file. malformed: unreadable, fails validation or the digest check. stale: older than
    `SWARM_HANDOVER_STALE_S` (default 21600) or written under another correlation than the task's plan in the Task
    Store. partial: valid schema but empty `remaining`, no artifacts and verification unverified. Otherwise ok. Known
    details (handover_id, seq, digest, age_s, from_agent, to_agent, correlation_id) are included.
    """
    return inspect_handover(task_id, root=root, clock=clock, correlation_id=correlation_id, seq=seq)[0]


def clarification_request(reason: str, missing: list[str], *, task_id: str | None = None) -> dict[str, Any]:
    """A ready BLOCKED `task.result` for a receiver that cannot continue from the handover it has: `needs` names the
    reason and what is missing so A01 can route the question. `task_id` is added when known (the schema requires it)."""
    if not isinstance(reason, str) or not reason.strip():
        raise HandoverInvalid(["/reason: must be a non-empty string"])
    if not isinstance(missing, list) or not missing or not all(isinstance(m, str) and m.strip() for m in missing):
        raise HandoverInvalid(["/missing: must be a non-empty list of non-empty strings"])
    reason = _clip(redact_text(reason.strip()), 240)
    items = [_clip(redact_text(m.strip()), 160) for m in missing]
    result: dict[str, Any] = {
        "state": "BLOCKED",
        "needs": {"kind": "handover_clarification", "reason": reason, "missing": items},
        "summary_md": f"Blocked: {reason}\n\nMissing:\n" + "\n".join(f"- {m}" for m in items),
    }
    if task_id is not None:
        result = {"task_id": _safe_id(task_id, "task_id"), **result}
    return result


_IDENTITY = ("schema_version", "handover_id", "task_id", "correlation_id", "from_agent", "to_agent")


def _restore_identity(clean: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    """`clean` with the identity fields of `original` put back: ids, agent ids and digests are validated identifiers,
    not free text, and a token-shaped id (`sk-onboarding-flow-v2`) must not be rewritten to `[REDACTED]` (which then
    fails validation after the fact). Only free text is redacted."""
    for key in _IDENTITY:
        if key in original:
            clean[key] = original[key]
    cleaned, source = clean.get("artifacts"), original.get("artifacts")
    if isinstance(cleaned, list) and isinstance(source, list) and len(cleaned) == len(source):
        for new, old in zip(cleaned, source):
            if isinstance(new, dict) and isinstance(old, dict):
                for key in ("producer", "digest"):
                    if key in old:
                        new[key] = old[key]
    return clean


# ---- write -----------------------------------------------------------------------------------------------------
def _prepare(doc: object, clock: Clock) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise HandoverInvalid(["/: handover must be an object"])
    out = _plain(doc)
    out.setdefault("schema_version", SCHEMA_VERSION)
    out.setdefault("handover_id", f"pending.{_PENDING_ID}")
    out.setdefault("created_wall", clock.wall())
    out.setdefault("summarized", False)
    out.setdefault("bytes", 0)
    return out


def _emit(doc: dict[str, Any], row: dict[str, Any] | None, root: str | Path | None, clock: Clock) -> bool:
    """`swarm.run.handover` through telemetry (chain of custody in events.jsonl and the live snapshot). Fail-open."""
    try:
        from . import telemetry  # lazy: telemetry imports the run log

        attempt = max(int((row or {}).get("attempt") or 0), 1)
        run_id = f"{doc['task_id']}.a{attempt}" if row else doc["task_id"]
        tel = telemetry.Telemetry(root=root, correlation_id=doc["correlation_id"], clock=clock, live=False)
        tel.emit("swarm.run.handover", {"run_id": run_id, "from_agent": doc["from_agent"], "to_agent": doc["to_agent"],
                                         "handover_id": doc["handover_id"], "digest": doc["digest"],
                                         "bytes": doc["bytes"], "summarized": doc["summarized"]}, task_id=doc["task_id"])
        if row:
            telemetry.refresh_live(root, correlation_id=doc["correlation_id"])
        return True
    except Exception:  # noqa: BLE001 - the document is already durable; telemetry is a projection
        log.warning("swarm.run.handover event failed", exc_info=True)
        return False


def write_handover(doc: dict[str, Any], *, root: str | Path | None = None, clock: Clock = SYSTEM_CLOCK,
                   max_bytes: int | None = None, emit: bool = True) -> dict[str, Any]:
    """Validate, redact, size-cap and persist a handover; returns where and what was written.

    `doc` carries the content fields; `schema_version`, `handover_id`, `created_wall`, `summarized` and `bytes` are
    filled in. Invalid documents raise HandoverInvalid naming every field. Secrets in text are replaced by
    `[REDACTED]` before the digest is computed. A document over `SWARM_HANDOVER_MAX_BYTES` is summarized
    (`summarize_handover`). A document identical in content to the task's newest handover is not written again
    (`duplicate: true`, the existing one is returned). Otherwise the complete document is published at the next free seq
    with a temp file + `os.link` (exclusive and atomic: no visible reservation, no gaps) and `swarm.run.handover` is emitted.

    Returns `{handover_id, seq, ref, path, digest, bytes, summarized, duplicate, redacted, emitted, document}`.
    """
    cap = min(max_bytes if max_bytes is not None else max_bytes_default(), MAX_FILE_BYTES)
    prepared = _prepare(doc, clock)
    problems = validate_handover(prepared)
    if problems:
        raise HandoverInvalid(problems)
    # Size and summarize with the id shape the stored file will have (`<task_id>.<seq>`, a task id up to 128 characters):
    # a shorter provisional id would let a document near the cap pass this check and overflow once the real id is set.
    prepared["handover_id"] = f"{prepared['task_id']}.{_PENDING_ID}"
    clean = _restore_identity(cast(dict[str, Any], redact(prepared)), prepared)  # redact() of a dict returns a dict
    redacted = clean != prepared
    sized = _sized(clean)
    body = sized if sized["bytes"] <= cap else summarize_handover(clean, cap)
    task_id, corr = body["task_id"], body["correlation_id"]
    folder = swarm_dir(root, create=True) / "handovers" / corr
    folder.mkdir(parents=True, exist_ok=True)
    existing = list_handovers(task_id, root=root, correlation_id=corr)
    if existing:
        with contextlib.suppress(HandoverInvalid):  # an unreadable newest file never blocks a fresh write
            previous = read_handover(existing[-1].path, root=root)
            if _content_key(previous) == _content_key(body):
                return {"handover_id": previous["handover_id"], "seq": existing[-1].seq,
                        "ref": f"{corr}/{previous['handover_id']}", "path": existing[-1].path,
                        "digest": previous["digest"], "bytes": previous["bytes"], "summarized": previous["summarized"],
                        "duplicate": True, "redacted": redacted, "emitted": False, "document": previous}
    built: dict[str, dict[str, Any]] = {}

    def build(seq: int) -> bytes:  # the document embeds its seq: rebuilt for every candidate seq
        candidate = dict(body)
        candidate["handover_id"] = f"{task_id}.{seq}"
        final = _sized(candidate)
        final["digest"] = compute_digest(final)
        data = canonical(final)
        if len(data) != final["bytes"] or len(data) > cap:
            raise SwarmError(ErrorCode.E_INTERNAL, f"handover sized {final['bytes']} but serialized {len(data)} bytes (cap {cap})")
        built["final"] = final
        return data

    seq, path, _data = _publish_exclusive(folder, task_id, build)
    final = built["final"]
    emitted = False
    if emit:
        with StoreReader(root) as store:
            row = store.task(task_id)
        emitted = _emit(final, row, root, clock)
    return {"handover_id": final["handover_id"], "seq": seq, "ref": f"{corr}/{final['handover_id']}", "path": path,
            "digest": final["digest"], "bytes": final["bytes"], "summarized": final["summarized"], "duplicate": False,
            "redacted": redacted, "emitted": emitted, "document": final}


# ---- receipts --------------------------------------------------------------------------------------------------
def _receipt_list(name: str, value: object, limit: int = 50) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise HandoverInvalid([f"/{name}: must be a list of non-empty strings"])
    if len(value) > limit:
        raise HandoverInvalid([f"/{name}: at most {limit} entries"])
    return [_clip(redact_text(v.strip()), 500) for v in value]


def write_receipt(task_id: str, *, by: str, digest: str, understood: list[str] | None = None,
                  questions: list[str] | None = None, accepted: bool = True, root: str | Path | None = None,
                  clock: Clock = SYSTEM_CLOCK, correlation_id: str | None = None) -> tuple[dict[str, Any], bool]:
    """Record what the receiver `by` ascertained from the handover with `digest`; returns `(receipt, duplicate)`.

    Idempotent: the key is task_id + to_agent + digest, the first receipt wins and a repeat returns it unchanged. The
    handover must exist, be valid and be addressed to `by`.
    """
    _safe_id(task_id, "task_id")
    if not isinstance(digest, str) or not _DIGEST_RE.match(digest):
        raise HandoverInvalid(["/digest: must be sha256:<64 lowercase hex>"])
    understood = _receipt_list("understood", understood or [])
    questions = _receipt_list("questions", questions or [])
    doc = None
    for cand in reversed(list_handovers(task_id, root=root, correlation_id=correlation_id)):
        with contextlib.suppress(HandoverInvalid):
            candidate = read_handover(cand.path, root=root)
            if candidate["digest"] == digest:
                doc = candidate
                break
    if doc is None:
        raise HandoverInvalid([f"/digest: no valid handover of task {task_id} has digest {digest}"])
    if doc["to_agent"] != by:
        raise HandoverInvalid([f"/task_id: handover {doc['handover_id']} is addressed to {doc['to_agent']}, not to {by}"])
    receipt = {"schema": RECEIPT_SCHEMA, "task_id": task_id, "correlation_id": doc["correlation_id"],
               "handover_id": doc["handover_id"], "digest": digest, "from_agent": doc["from_agent"], "to_agent": by,
               "accepted": bool(accepted), "understood": understood, "questions": questions, "ack_wall": clock.wall()}
    folder = swarm_dir(root, create=True) / "handovers" / doc["correlation_id"]
    path = folder / f"{task_id}.receipt.{by}.{digest.split(':', 1)[1][:16]}.json"
    return _create_receipt(path, receipt)


RECEIPT_RECLAIM_S = 5.0  # an empty receipt file older than this was left by a writer that died before it wrote


def _create_receipt(path: Path, receipt: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Create `path` with the receipt in one atomic step (temp file, then `os.link`): the key is never visible empty,
    so a writer that dies leaves nothing behind. First write wins; a repeat returns the stored receipt unchanged.
    An empty file from an older crashed writer is reclaimed once it is `RECEIPT_RECLAIM_S` old."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(canonical(receipt))
            fh.flush()
            os.fsync(fh.fileno())
        for _attempt in range(2):
            try:
                os.link(tmp, path)
                return receipt, False
            except FileExistsError:
                try:
                    info = path.stat()
                    if info.st_size == 0 and time.time() - info.st_mtime > RECEIPT_RECLAIM_S:
                        path.unlink()  # a crashed legacy writer's husk: take the key over
                        continue
                    return json.loads(path.read_text(encoding="utf-8")), True
                except FileNotFoundError:
                    continue  # removed between the link and the read: try to create it again
                except (OSError, ValueError):  # an empty file of a writer still at work: report the key as taken
                    return receipt, True
        return receipt, True
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def receipt_ref(receipt: dict[str, Any]) -> str:
    """`<correlation_id>/<file name>` of a receipt, relative to the handover directory."""
    return f"{receipt['correlation_id']}/{receipt['task_id']}.receipt.{receipt['to_agent']}.{receipt['digest'].split(':', 1)[1][:16]}.json"


__all__ = [
    "Candidate", "HandoverInvalid", "StoreReader", "agent_id_of", "canonical", "clarification_request", "compute_digest",
    "handover_status", "inspect_handover", "known_agent_ids", "latest_handover", "list_handovers", "read_handover",
    "read_task", "receipt_ref", "stored_size", "summarize_handover", "validate_handover", "write_handover",
    "write_receipt", "max_bytes_default", "stale_after_default",
]
