"""Durable join ledger: the Python oracle of the U12.2-4 route.

Each correlation owns one file, ``<swarm_dir>/joins/<correlation>.jsonl``. Every line is one UTF-8 JSON object,
newline-terminated::

    {"key": {"task_id": str, "stage": int, "join_seq": int, "attempt": int},
     "log": [{"key": {"stage": int, "depth": int, "creation": int, "task_id": str},
              "outcome": {...},
              "provenance": {"producer": str, "attempt": int,
                             "verdict": {"verdict": str, "code": str|null, "detail": str|null}}}, ...]}

``emit`` appends a join at most once per ledger key: a replay after failover returns ``"deduplicated"`` and writes
nothing. The read-check-append runs under an exclusive ``flock`` so concurrent writers cannot both collect the same key.
A malformed line is never skipped: reading or emitting into such a file raises ``ValueError`` and leaves it untouched.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any, NoReturn, TypeGuard

LEDGER_KEY_FIELDS = ("task_id", "stage", "join_seq", "attempt")
RECORD_KEY_FIELDS = ("stage", "depth", "creation", "task_id")
OUTCOME_KINDS = ("completed", "rate_limited", "failed")

_CORRELATION = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")


def ledger_path_for(swarm_dir: Path, correlation: str) -> Path:
    """Return ``swarm_dir/joins/<correlation>.jsonl`` after validating the correlation id."""
    if not isinstance(correlation, str) or _CORRELATION.fullmatch(correlation) is None:
        raise ValueError(
            "invalid correlation: must be 1-128 characters of [A-Za-z0-9._-] and must not start with a dot"
        )
    return Path(swarm_dir) / "joins" / f"{correlation}.jsonl"


def emit(ledger_path: Path, key: dict[Any, Any], log: list[dict[Any, Any]]) -> str:
    """Append one join line unless its key is already present. Returns ``"collected"`` or ``"deduplicated"``."""
    path = Path(ledger_path)
    _check_ledger_key(key, "key")
    _check_log(log, "log")
    ordered = sorted(log, key=_record_sort_key)
    line = json.dumps(
        {"key": {field: key[field] for field in LEDGER_KEY_FIELDS}, "log": ordered},
        separators=(",", ":"),
    )
    data = (line + "\n").encode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            for existing_key, _ in _parse(fh.read(), str(path)):
                if all(existing_key[field] == key[field] for field in LEDGER_KEY_FIELDS):
                    return "deduplicated"
            created = os.fstat(fh.fileno()).st_size == 0
            fh.seek(0, os.SEEK_END)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            if created:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return "collected"


def read_ledger(ledger_path: Path) -> list[tuple[dict[Any, Any], list[dict[Any, Any]]]]:
    """Return the ``(key, log)`` pairs in file order; an absent file reads as an empty ledger."""
    path = Path(ledger_path)
    try:
        fh = open(path, "rb")
    except FileNotFoundError:
        return []
    with fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        try:
            data = fh.read()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return _parse(data, str(path))


def _parse(data: bytes, where: str) -> list[tuple[dict[Any, Any], list[dict[Any, Any]]]]:
    if not data:
        return []
    if not data.endswith(b"\n"):
        _fail(where, "final line is not newline-terminated")
    entries: list[tuple[dict[Any, Any], list[dict[Any, Any]]]] = []
    for lineno, raw in enumerate(data.split(b"\n")[:-1], start=1):
        line_where = f"{where}:{lineno}"
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail(line_where, f"not valid UTF-8 JSON ({exc})")
        _exact(obj, ("key", "log"), line_where)
        _check_ledger_key(obj["key"], f"{line_where}.key")
        _check_log(obj["log"], f"{line_where}.log")
        entries.append((obj["key"], obj["log"]))
    return entries


def _record_sort_key(record: dict[Any, Any]) -> tuple[Any, ...]:
    k = record["key"]
    return (k["stage"], k["depth"], k["creation"], k["task_id"])


def _fail(where: str, message: str) -> NoReturn:
    raise ValueError(f"{where}: {message}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check(value: object, kind: str, where: str) -> None:
    ok = {
        "int": _is_int(value),
        "str": isinstance(value, str),
        "int|null": value is None or _is_int(value),
        "str|null": value is None or isinstance(value, str),
    }[kind]
    if not ok:
        _fail(where, f"expected {kind.replace('|', ' or ')}")


def _exact(obj: object, names: tuple[str, ...], where: str) -> TypeGuard[dict[Any, Any]]:
    _fail(where, "expected an object")
    if set(obj) != set(names):
        _fail(where, f"expected fields {sorted(names)}, got {sorted(obj)}")
    return True


def _check_ledger_key(key: dict[Any, Any], where: str) -> None:
    _exact(key, LEDGER_KEY_FIELDS, where)
    _check(key["task_id"], "str", f"{where}.task_id")
    for name in ("stage", "join_seq", "attempt"):
        _check(key[name], "int", f"{where}.{name}")


def _check_record_key(key: dict[Any, Any], where: str) -> None:
    _exact(key, RECORD_KEY_FIELDS, where)
    _check(key["task_id"], "str", f"{where}.task_id")
    for name in ("stage", "depth", "creation"):
        _check(key[name], "int", f"{where}.{name}")


def _check_outcome(outcome: dict[Any, Any], where: str) -> None:
    if not isinstance(outcome, dict):
        _fail(where, "expected an object")
    kind = outcome.get("kind")
    if kind == "completed":
        _exact(outcome, ("kind",), where)
    elif kind == "rate_limited":
        _exact(outcome, ("kind", "retry_after_ns"), where)
        _check(outcome["retry_after_ns"], "int|null", f"{where}.retry_after_ns")
    elif kind == "failed":
        _exact(outcome, ("kind", "code", "message"), where)
        _check(outcome["code"], "str", f"{where}.code")
        _check(outcome["message"], "str", f"{where}.message")
    else:
        _fail(where, f"unknown outcome kind {kind!r}; expected one of {list(OUTCOME_KINDS)}")


def _check_record(record: dict[Any, Any], where: str) -> None:
    _exact(record, ("key", "outcome", "provenance"), where)
    _check_record_key(record["key"], f"{where}.key")
    _check_outcome(record["outcome"], f"{where}.outcome")
    provenance = record["provenance"]
    _exact(provenance, ("producer", "attempt", "verdict"), f"{where}.provenance")
    _check(provenance["producer"], "str", f"{where}.provenance.producer")
    _check(provenance["attempt"], "int", f"{where}.provenance.attempt")
    verdict = provenance["verdict"]
    _exact(verdict, ("verdict", "code", "detail"), f"{where}.provenance.verdict")
    _check(verdict["verdict"], "str", f"{where}.provenance.verdict.verdict")
    _check(verdict["code"], "str|null", f"{where}.provenance.verdict.code")
    _check(verdict["detail"], "str|null", f"{where}.provenance.verdict.detail")


def _check_log(log: object, where: str) -> None:
    if not isinstance(log, list) or not log:
        _fail(where, "expected a non-empty list of records")
    for index, record in enumerate(log):
        _check_record(record, f"{where}[{index}]")
