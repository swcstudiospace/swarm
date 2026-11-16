#!/usr/bin/env python3
"""Fail when a deny.toml advisory ignore has no id, no reason, no expiry, or an expiry outside the allowed window.

cargo-deny has no expiry key, so every ``[advisories].ignore`` entry carries ``expires YYYY-MM-DD`` in its reason.
Exit status: 0 all entries valid, 1 at least one violation, 2 deny.toml unreadable or unparsable.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

ID_PATTERN = re.compile(r"^RUSTSEC-[0-9]{4}-[0-9]{4}$")
EXPIRY_PATTERN = re.compile(r"expires (\d{4}-\d{2}-\d{2})")
MAX_DAYS_AHEAD = 120


def check_entry(entry: object, index: int, today: date) -> tuple[list[str], date | None]:
    """Return the violations of one ignore entry and its expiry date when it has a valid one."""
    label = f"ignore[{index}]"
    if isinstance(entry, str):
        return [f"{label} {entry!r}: bare string entry has no reason or expiry"], None
    if not isinstance(entry, dict):
        return [f"{label}: entry must be a string or a table, got {type(entry).__name__}"], None
    ident = entry.get("id")
    label = f"ignore[{index}] {ident!r}"
    problems: list[str] = []
    if not isinstance(ident, str) or not ID_PATTERN.fullmatch(ident):
        problems.append(f"{label}: id must match RUSTSEC-YYYY-NNNN")
    reason = entry.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return [*problems, f"{label}: reason is missing or empty"], None
    found = EXPIRY_PATTERN.search(reason)
    if found is None:
        return [*problems, f"{label}: reason must contain 'expires YYYY-MM-DD'"], None
    try:
        expiry = date.fromisoformat(found.group(1))
    except ValueError:
        return [*problems, f"{label}: expiry {found.group(1)} is not a calendar date"], None
    if expiry < today:
        problems.append(f"{label}: expired on {expiry} (today is {today})")
    elif expiry > today + timedelta(days=MAX_DAYS_AHEAD):
        problems.append(f"{label}: expiry {expiry} is more than {MAX_DAYS_AHEAD} days after {today}")
    return problems, None if problems else expiry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deny-toml", type=Path, default=Path(__file__).resolve().parent.parent / "deny.toml")
    parser.add_argument("--today", type=date.fromisoformat, default=None, help="override today's UTC date (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    today: date = args.today or datetime.now(UTC).date()
    try:
        with args.deny_toml.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        print(f"check_deny_ignores: cannot read {args.deny_toml}: {error}", file=sys.stderr)
        return 2
    advisories = document.get("advisories", {})
    entries = advisories.get("ignore", []) if isinstance(advisories, dict) else []
    if not isinstance(entries, list):
        print(f"check_deny_ignores: {args.deny_toml}: [advisories].ignore must be an array", file=sys.stderr)
        return 2
    violations: list[str] = []
    expiries: list[date] = []
    for index, entry in enumerate(entries):
        problems, expiry = check_entry(entry, index, today)
        violations.extend(problems)
        if expiry is not None:
            expiries.append(expiry)
    if violations:
        for line in violations:
            print(line)
        return 1
    nearest = min(expiries).isoformat() if expiries else "none"
    print(f"check_deny_ignores: {len(entries)} ignores, nearest expiry {nearest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
