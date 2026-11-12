"""Lane tools for A11–A15. File-disjoint from the other Phase 3 batches."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from swarm.tools.support import respond, script, target_root


def ci_kind(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        kind = script("devops_ci_check").classify(Path(payload["rel"]))
        if kind is None:
            return {"state": "INVALID_INPUT", "field": "rel", "message": "path is not a CI or IaC manifest"}
        return {"state": "SUCCESS", "kind": kind}

    return respond(payload, work)


def build_systems(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        if payload["scan"] is not True:
            return {"state": "INVALID_INPUT", "field": "scan", "message": "scan must be true"}
        found = script("devops_build_record").detect_build_systems(target_root())
        return {"state": "SUCCESS", "systems": ",".join(found)}

    return respond(payload, work)


def canary_steps(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        plan = script("rel_plan").build_plan(payload["release_id"], payload["risk"], [], ["quality"], [])
        return {
            "state": "SUCCESS",
            "steps_pct": ",".join(str(step) for step in plan["steps_pct"]),
            "approval": plan["approval"],
        }

    return respond(payload, work)


def error_burn(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        if payload["objective"] >= 1 or payload["objective"] < 0:
            return {"state": "INVALID_INPUT", "field": "objective", "message": "objective must be in [0, 1)"}
        slo = script("obs_slo")
        if payload["total"] > 0:
            problem = slo.count_problem(payload["good"], payload["total"])
            if problem:
                return {"state": "INVALID_INPUT", "field": problem[0], "message": problem[1]}
        sli, rate = slo.burn(payload["good"], payload["total"], payload["objective"])
        if sli is None or rate is None:
            return {"state": "INVALID_INPUT", "field": "total", "message": "total must be positive"}
        return {"state": "SUCCESS", "sli": sli, "burn_rate": rate}

    return respond(payload, work)


def spec_pin(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        return {"state": "SUCCESS", "pin": script("maint_deps")._pin_state(payload["spec"])}

    return respond(payload, work)


def heading_order(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        docs = script("docs_bundle")
        body = docs.strip_code(payload["markdown"])
        previous = 0
        skips = 0
        h1 = 0
        for line in body.splitlines():
            match = docs.HEAD_RE.match(line)
            if not match:
                continue
            level = len(match.group(1))
            if level == 1:
                h1 += 1
            if previous and level > previous + 1:
                skips += 1
            previous = level
        return {"state": "SUCCESS", "h1": h1, "skips": skips}

    return respond(payload, work)
