"""A01 orchestrator checks over a supplied plan, gate verdicts, and rework count.

Pure functions: every input comes from the payload. A01 uses them to decide which tasks
may dispatch, whether a task may be approved, and whether a failed gate gets another rework loop.

Tools:
- orch_dag_blocked: classify PLANNED/RETRY tasks as ready, waiting, or blocked, and report unorderable (cycle) tasks.
- orch_gate_coverage: check the latest verdict per gate against the gates a risk class requires.
- orch_rework_bound: apply the rework-loop cap (default 2, then ESCALATED) to a gate failure.
"""
from __future__ import annotations

import math
from typing import Any

from swarm.tools.support import respond

DISPATCHABLE = frozenset({"PLANNED", "RETRY"})
SATISFIED = frozenset({"IN_REVIEW", "APPROVED", "DONE"})  # dependency states that count as met
DEAD = frozenset({"ESCALATED", "CANCELLED"})  # dependency states that can never be met
DEFAULT_MAX_LOOPS = 2  # mirrors swarm.taskstore.MAX_REWORK_LOOPS; the payload may override it


def _unresolved(deps: dict[str, set[str]]) -> set[str]:
    """Kahn's algorithm over in-payload edges. Tasks it cannot order are cycle members or depend on one."""
    known = set(deps)
    pending = {tid: len(needs & known) for tid, needs in deps.items()}
    dependents: dict[str, list[str]] = {tid: [] for tid in deps}
    for tid, needs in deps.items():
        for need in needs & known:
            dependents[need].append(tid)
    ready = [tid for tid, count in pending.items() if count == 0]
    while ready:
        done = ready.pop()
        for tid in dependents[done]:
            pending[tid] -= 1
            if pending[tid] == 0:
                ready.append(tid)
    return {tid for tid, count in pending.items() if count > 0}


def dag_blocked(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        tasks = payload["tasks"]
        ids = [task["id"] for task in tasks]
        if len(set(ids)) != len(ids):
            dupes = sorted({tid for tid in ids if ids.count(tid) > 1})
            return {"state": "INVALID_INPUT", "field": "tasks", "message": f"duplicate task ids: {', '.join(dupes)}"}
        state = {task["id"]: task["state"] for task in tasks}
        deps = {task["id"]: set(task["depends_on"]) for task in tasks}
        unresolved = _unresolved(deps)
        ready: list[str] = []
        waiting: list[str] = []
        blocked: list[dict[str, Any]] = []
        for tid in sorted(state):
            if state[tid] not in DISPATCHABLE:
                continue
            missing = sorted(dep for dep in deps[tid] if dep not in state)
            dead = sorted(dep for dep in deps[tid] if state.get(dep) in DEAD)
            if tid in unresolved:
                cycle = sorted(dep for dep in deps[tid] if dep in unresolved)
                blocked.append({"id": tid, "reason": "cycle", "causes": cycle})
            elif missing:
                blocked.append({"id": tid, "reason": "missing_dependency", "causes": missing})
            elif dead:
                blocked.append({"id": tid, "reason": "dead_dependency", "causes": dead})
            elif all(state[dep] in SATISFIED for dep in deps[tid]):
                ready.append(tid)
            else:
                waiting.append(tid)
        return {
            "state": "SUCCESS",
            "ready": ready,
            "waiting": waiting,
            "blocked": blocked,
            "unresolved": sorted(unresolved),
        }

    return respond(payload, work)


def gate_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        rules = payload["rules"]
        risks = [rule["risk"] for rule in rules]
        if len(set(risks)) != len(risks):
            return {"state": "INVALID_INPUT", "field": "rules", "message": "duplicate risk class"}
        matched = [rule for rule in rules if rule["risk"] == payload["risk"]]
        if not matched:
            return {"state": "INVALID_INPUT", "field": "risk", "message": f"no rule for risk {payload['risk']!r}"}
        required = list(dict.fromkeys(matched[0]["gates"]))
        last: dict[str, str] = {}
        for row in payload["results"]:
            last[row["gate"]] = row["verdict"]  # results are chronological; the latest verdict per gate counts
        return {
            "state": "SUCCESS",
            "covered": all(last.get(gate) in ("pass", "waive") for gate in required),
            "passed": [gate for gate in required if last.get(gate) == "pass"],
            "waived": [gate for gate in required if last.get(gate) == "waive"],
            "failed": [gate for gate in required if last.get(gate) == "fail"],
            "missing": [gate for gate in required if gate not in last],
            "extra": sorted(gate for gate in last if gate not in required),
        }

    return respond(payload, work)


def rework_bound(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        cap = payload.get("max_loops", DEFAULT_MAX_LOOPS)
        loops = payload["rework_loops"]
        if payload["blocking_findings"] == 0:
            decision, after = "PASS", loops
        elif loops < cap:
            decision, after = "REWORK", loops + 1
        else:
            decision, after = "ESCALATED", loops
        return {
            "state": "SUCCESS",
            "decision": decision,
            "rework_loops": after,
            "loops_remaining": max(0, cap - after),
        }

    return respond(payload, work)


# --- v2.2 additions: round dispatch under concurrency ceilings and budget breach decisions ---

WARN_AT_DEFAULT = 0.8  # share of a budget at which A01 starts watching a run
EPSILON = 1e-9  # float slack so 0.8 x 300000 compares equal to 240000


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and (isinstance(value, int) or math.isfinite(value))


def _shown_ratio(ratio: float, breached: bool) -> float:
    """Four decimals, but never 1.0 for a dimension that did not breach (299999.99 of 300000 tokens shows 0.9999)."""
    shown = round(ratio, 4)
    return shown if breached or shown < 1.0 else 0.9999


def dispatch_batch(payload: dict[str, Any]) -> dict[str, Any]:
    """Pick which ready tasks start this round: per-agent ceiling first, then the global cap.

    Order is priority (higher first, default 0), then the order supplied. The per-agent test is
    swarm.blast_radius.take_agent_slot, the same function the runner uses.
    """

    def work() -> dict[str, Any]:
        from swarm.blast_radius import take_agent_slot

        ready, running, ceilings = payload["ready"], payload["running"], payload["ceilings"]
        for field, mapping in (("running", running), ("ceilings", ceilings)):
            if not isinstance(mapping, dict):
                return {"state": "INVALID_INPUT", "field": field, "message": f"{field} must be an object of agent id to integer"}
        for agent, count in running.items():
            if not _is_int(count) or count < 0:
                return {"state": "INVALID_INPUT", "field": "running", "message": f"running[{agent!r}] must be an integer >= 0"}
        for agent, ceiling in ceilings.items():
            if not _is_int(ceiling) or ceiling < 1:
                return {"state": "INVALID_INPUT", "field": "ceilings", "message": f"ceilings[{agent!r}] must be an integer >= 1"}
        seen: set[str] = set()
        for index, task in enumerate(ready):
            if task["id"] in seen:
                return {"state": "INVALID_INPUT", "field": "ready", "message": f"duplicate task id {task['id']!r}"}
            seen.add(task["id"])
            if task["agent_id"] not in ceilings:
                return {
                    "state": "INVALID_INPUT",
                    "field": "ceilings",
                    "message": f"no ceiling for agent {task['agent_id']!r} (task {task['id']!r}); pass its max_parallel",
                }
        free = max(0, payload["global_cap"] - sum(running.values()))
        counts = dict(running)
        ordered = sorted(enumerate(ready), key=lambda row: (-row[1].get("priority", 0), row[0]))
        start: list[str] = []
        deferred: list[dict[str, Any]] = []
        for _index, task in ordered:
            agent, ceiling = task["agent_id"], ceilings[task["agent_id"]]
            if not take_agent_slot(dict(counts), agent, ceiling):
                deferred.append({"id": task["id"], "reason": "agent_ceiling"})
            elif len(start) >= free:
                deferred.append({"id": task["id"], "reason": "global_cap"})
            else:
                take_agent_slot(counts, agent, ceiling)
                start.append(task["id"])
        return {
            "state": "SUCCESS",
            "start": start,
            "deferred": deferred,
            "slots": {agent: max(0, ceiling - counts.get(agent, 0)) for agent, ceiling in sorted(ceilings.items())},
            "global_free": free - len(start),
        }

    return respond(payload, work)


def budget_guard(payload: dict[str, Any]) -> dict[str, Any]:
    """Decide OK, WARN, HARD_STOP, or ESCALATED from usage against budget and earlier breaches.

    A dimension breaches at 100 % of its budget. The first breach hard-stops (checkpoint, requeue once with an
    annotated budget); a breach after a prior one escalates (agents/A01 section 3, rule 5).
    """

    def work() -> dict[str, Any]:
        budget, usage = payload["budget"], payload["usage"]
        warn_at = payload.get("warn_at", WARN_AT_DEFAULT)
        if not _is_num(warn_at) or not 0 < warn_at < 1:
            return {"state": "INVALID_INPUT", "field": "warn_at", "message": "warn_at must be a number above 0 and below 1"}
        raw: dict[str, float] = {}
        for dim in ("tokens", "wall_s", "cost_usd"):
            limit = budget["max_" + dim]
            used = usage[dim]
            if not _is_num(limit) or limit <= 0:
                return {"state": "INVALID_INPUT", "field": "budget", "message": f"max_{dim} must be a number above 0"}
            if not _is_num(used) or used < 0:
                return {"state": "INVALID_INPUT", "field": "usage", "message": f"{dim} must be a number >= 0"}
            try:
                ratio = used / limit
            except OverflowError:  # an int too large for a float ratio: reported below as not comparable
                ratio = math.inf
            if not math.isfinite(ratio):
                return {"state": "INVALID_INPUT", "field": "usage", "message": f"{dim} is too large relative to max_{dim} to compare"}
            raw[dim] = ratio
        breached = [dim for dim, ratio in raw.items() if ratio >= 1 - EPSILON]
        prior = payload["prior_breaches"]
        if breached and prior >= 1:
            decision, action = "ESCALATED", "escalate"
        elif breached:
            decision, action = "HARD_STOP", "requeue_once_annotated"
        elif max(raw.values()) >= warn_at - EPSILON:
            decision, action = "WARN", "note_usage"
        else:
            decision, action = "OK", "continue"
        return {
            "state": "SUCCESS",
            "decision": decision,
            "ratios": {dim: _shown_ratio(ratio, dim in breached) for dim, ratio in raw.items()},
            "breached": breached,
            "next_action": action,
            "breaches_after": prior + 1 if breached else prior,
        }

    return respond(payload, work)
