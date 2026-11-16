"""Fail-closed completeness policy for the A16-A30 scanner wrappers.

A scanner that did not analyse every in-scope input must not report ``status: ok``: a CI gate would pass over input
nobody read. A wrapper collects one reason per input it could not analyse (a missing optional tool, an unparsable,
unreadable, binary or oversized file, a file or entry limit, or a file skipped as a symlink), then calls
``incomplete_findings`` before deriving the status with ``scan_status``. Zero analysed inputs is incomplete too.

Reasons carry labels and paths only, never the value that failed to parse: a mistyped field can hold a secret.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

BLOCKING_SEVERITIES = ("blocker", "major")
MAX_REASONS_SHOWN = 5


def incomplete_findings(reasons: Iterable[object], analysed: int) -> list[dict[str, str]]:
    """One major ``scan-incomplete`` finding when any in-scope input was not analysed, or none was."""
    listed = [str(reason) for reason in reasons if str(reason)]
    if not listed and analysed > 0:
        return []
    shown = listed[:MAX_REASONS_SHOWN] or ["no in-scope input was analysed"]
    more = len(listed) - MAX_REASONS_SHOWN
    detail = "; ".join(shown) + (f"; and {more} more" if more > 0 else "")
    return [{
        "id": "scan/incomplete",
        "rule": "scan-incomplete",
        "severity": "major",
        "summary": f"scan incomplete, status is fail until every in-scope input is analysed: {detail}",
    }]


def scan_status(findings: Iterable[Mapping[str, object]]) -> str:
    """``fail`` when any finding is blocking, otherwise ``ok``."""
    return "fail" if any(finding.get("severity") in BLOCKING_SEVERITIES for finding in findings) else "ok"
