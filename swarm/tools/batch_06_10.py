"""Lane tools for A06–A10. File-disjoint from the other Phase 3 batches."""
from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from swarm.tools.support import respond, script, target_root


def a11y_scan(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        issues = script("fe_a11y_check").check_text(payload["html"], "snippet.html")
        return {"state": "SUCCESS", "issue_count": len(issues)}

    # respond() in swarm/tools/support.py is untyped, so its result is Any; work() always returns a result dict.
    return cast(dict[str, Any], respond(payload, work))


def migration_down(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        reversible = script("data_migration_check").has_down(Path("migration.py"), payload["sql"])
        return {"state": "SUCCESS", "reversible": reversible}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def runner_detect(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        if payload["scan"] is not True:
            return {"state": "INVALID_INPUT", "field": "scan", "message": "scan must be true"}
        names = [item["name"] for item in script("qa_gate").detect_runners(target_root())]
        return {"state": "SUCCESS", "runner_count": len(names), "has_pytest": "pytest" in names}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def diff_size(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        for field in ("added", "deleted", "max_lines"):
            if payload[field] < 0:
                return {"state": "INVALID_INPUT", "field": field, "message": f"{field} must be >= 0"}
        over = payload["added"] + payload["deleted"] > payload["max_lines"]
        return {"state": "SUCCESS", "over_budget": over}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def line_entropy(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        text = payload["text"]
        if not text:
            return {"state": "INVALID_INPUT", "field": "text", "message": "text must be non-empty"}
        return {"state": "SUCCESS", "entropy": script("sec_gate")._entropy(text)}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict
