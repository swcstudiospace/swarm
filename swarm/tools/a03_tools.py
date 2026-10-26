"""A03 architect tools: pure checks over release versions, contract fields, module and
container graphs, ADR status changes and NFR budgets. Every input comes from the payload.

Tools:
- arch_semver_bump: classify a release step and test it against the bump the change requires.
- arch_field_breaks: list breaking and additive changes between two field sets of one contract side.
- arch_layer_violations: find import edges that point up an ordered layering.
- arch_cycle_check: find dependency cycles (strongly connected components) in module edges.
- arch_c4_fanout: count C4 containers and relations and flag containers over a fan-out budget.
- arch_adr_transition: check an ADR status change against the lifecycle used by scripts/arch_adr.py.
- arch_nfr_budget: compare measured NFR values with their budgets; an unmeasured budget fails closed.
- arch_nfr_allocation: check every NFR is bound to a clause or component budget that can be measured.
- arch_breaking_readiness: autonomy level and outstanding acks, migration plan and approval for a change.
- arch_adr_supersession: validate the supersession links and statuses of an ADR set.
"""
from __future__ import annotations

import re
from typing import Any

from swarm.tools.support import respond

_CORE_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_BUMP_RANK = {"none": 0, "patch": 1, "minor": 2, "major": 3}
_ADR_NEXT = {
    "proposed": {"accepted", "rejected"},
    "accepted": {"deprecated", "superseded"},
    "deprecated": {"superseded"},
    "rejected": set(),
    "superseded": set(),
}


def _invalid(field: str, message: str) -> dict[str, str]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _core(value: object) -> tuple[int, int, int] | None:
    """MAJOR.MINOR.PATCH as integers; None for prerelease, build metadata or malformed text."""
    match = _CORE_VERSION.fullmatch(value) if isinstance(value, str) else None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) if match else None


def semver_bump(payload: dict[str, Any]) -> dict[str, Any]:
    """Bump between two release versions, and whether it satisfies the change's required bump.

    A breaking change requires a major bump and an additive change a minor bump, regardless of
    the 0.x major, matching the prompt's rule that breaking contract changes need a major version.
    """

    def work() -> dict[str, Any]:
        base = _core(payload["base_version"])
        head = _core(payload["head_version"])
        if base is None:
            return _invalid("base_version", "must be MAJOR.MINOR.PATCH with no prerelease or build suffix")
        if head is None:
            return _invalid("head_version", "must be MAJOR.MINOR.PATCH with no prerelease or build suffix")
        if head < base:
            bump = "downgrade"
        elif head == base:
            bump = "none"
        elif head[0] > base[0]:
            bump = "major"
        elif head[1] > base[1]:
            bump = "minor"
        else:
            bump = "patch"
        if payload["breaking_change"]:
            required = "major"
        elif payload["additive_change"]:
            required = "minor"
        else:
            required = "none"
        compliant = bump != "downgrade" and _BUMP_RANK[bump] >= _BUMP_RANK[required]
        return {"state": "SUCCESS", "bump": bump, "required_bump": required, "compliant": compliant}

    return respond(payload, work)


def _index(fields: list[dict[str, Any]], label: str) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    index: dict[str, dict[str, Any]] = {}
    for field in fields:
        if field["name"] in index:
            return None, f"duplicate field name {field['name']} in {label}"
        index[field["name"]] = field
    return index, None


def field_breaks(payload: dict[str, Any]) -> dict[str, Any]:
    """Compare two field sets of one contract side.

    Removed fields and type changes are breaking on both sides. On the request side a new required
    field or an optional-to-required change is breaking. On the response side a required-to-optional
    change is breaking. Every other change is additive.
    """

    def work() -> dict[str, Any]:
        side = payload["side"]
        base, problem = _index(payload["base_fields"], "base_fields")
        if base is None:
            return _invalid("base_fields", problem or "")
        head, problem = _index(payload["head_fields"], "head_fields")
        if head is None:
            return _invalid("head_fields", problem or "")
        breaking: list[str] = []
        additive: list[str] = []
        for name in sorted(base):
            if name not in head:
                breaking.append(f"removed field {name}")
                continue
            before, after = base[name], head[name]
            if before["type"] != after["type"]:
                breaking.append(f"type of {name} changed {before['type']} -> {after['type']}")
            if before["required"] and not after["required"]:
                (breaking if side == "response" else additive).append(f"field {name} became optional")
            elif after["required"] and not before["required"]:
                (breaking if side == "request" else additive).append(f"field {name} became required")
        for name in sorted(head):
            if name in base:
                continue
            if head[name]["required"] and side == "request":
                breaking.append(f"new required field {name}")
            else:
                additive.append(f"added field {name}")
        return {"state": "SUCCESS", "breaking_change": bool(breaking), "breaking": breaking, "additive": additive}

    return respond(payload, work)


def layer_violations(payload: dict[str, Any]) -> dict[str, Any]:
    """Edges whose target sits in a layer above the source layer.

    layer_order runs from the outermost layer (index 0) to the innermost. A module may depend on its
    own layer or any later one. Edges touching an unassigned module are reported, not judged.
    """

    def work() -> dict[str, Any]:
        order = payload["layer_order"]
        if len(set(order)) != len(order):
            return _invalid("layer_order", "layer names must be unique")
        rank = {layer: index for index, layer in enumerate(order)}
        layer_of: dict[str, str] = {}
        for item in payload["assignments"]:
            module, layer = item["module"], item["layer"]
            if module in layer_of:
                return _invalid("assignments", f"module {module} is assigned twice")
            if layer not in rank:
                return _invalid("assignments", f"layer {layer} is not in layer_order")
            layer_of[module] = layer
        violations: list[str] = []
        unassigned: set[str] = set()
        checked = 0
        for edge in payload["edges"]:
            src, dst = edge["from"], edge["to"]
            if src not in layer_of or dst not in layer_of:
                unassigned.update(module for module in (src, dst) if module not in layer_of)
                continue
            checked += 1
            if rank[layer_of[dst]] < rank[layer_of[src]]:
                violations.append(f"{src} ({layer_of[src]}) -> {dst} ({layer_of[dst]})")
        return {
            "state": "SUCCESS",
            "violation_count": len(violations),
            "checked_edges": checked,
            "violations": violations,
            "unassigned": sorted(unassigned),
        }

    return respond(payload, work)


def _strongly_connected(adj: dict[str, set[str]], radj: dict[str, set[str]]) -> list[list[str]]:
    """Kosaraju's algorithm, iterative so deep graphs cannot exhaust the recursion limit."""
    seen: set[str] = set()
    finished: list[str] = []
    for root in sorted(adj):
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(sorted(adj[root])))]
        while stack:
            node, children = stack[-1]
            for child in children:
                if child not in seen:
                    seen.add(child)
                    stack.append((child, iter(sorted(adj[child]))))
                    break
            else:
                stack.pop()
                finished.append(node)
    group_of: dict[str, int] = {}
    groups: list[list[str]] = []
    for root in reversed(finished):
        if root in group_of:
            continue
        index = len(groups)
        group_of[root] = index
        members: list[str] = []
        todo = [root]
        while todo:
            node = todo.pop()
            members.append(node)
            for parent in radj[node]:
                if parent not in group_of:
                    group_of[parent] = index
                    todo.append(parent)
        groups.append(sorted(members))
    return groups


def cycle_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Cyclic components of a dependency graph. A self-edge counts as a cycle of one node."""

    def work() -> dict[str, Any]:
        adj: dict[str, set[str]] = {}
        radj: dict[str, set[str]] = {}
        for edge in payload["edges"]:
            src, dst = edge["from"], edge["to"]
            adj.setdefault(src, set()).add(dst)
            adj.setdefault(dst, set())
            radj.setdefault(dst, set()).add(src)
            radj.setdefault(src, set())
        cyclic = [
            members
            for members in _strongly_connected(adj, radj)
            if len(members) > 1 or members[0] in adj[members[0]]
        ]
        cyclic.sort(key=lambda members: members[0])
        return {
            "state": "SUCCESS",
            "has_cycle": bool(cyclic),
            "node_count": len(adj),
            "cyclic_node_count": sum(len(members) for members in cyclic),
            "cyclic_components": [", ".join(members) for members in cyclic],
        }

    return respond(payload, work)


def c4_fanout(payload: dict[str, Any]) -> dict[str, Any]:
    """Container count, distinct relation count and per-container fan-out against a budget.

    Fan-out is the number of distinct containers a container relates to. Isolated containers have
    neither outgoing nor incoming relations.
    """

    def work() -> dict[str, Any]:
        containers = payload["containers"]
        known = set(containers)
        if len(known) != len(containers):
            return _invalid("containers", "container names must be unique")
        targets: dict[str, set[str]] = {name: set() for name in containers}
        for rel in payload["relations"]:
            src, dst = rel["from"], rel["to"]
            if src not in known or dst not in known:
                return _invalid("relations", f"unknown container in {src} -> {dst}")
            if src == dst:
                return _invalid("relations", f"container {src} relates to itself")
            targets[src].add(dst)
        incoming = {dst for out in targets.values() for dst in out}
        fan_out = {name: len(out) for name, out in targets.items()}
        budget = payload["fan_out_budget"]
        over = sorted(
            ((name, count) for name, count in fan_out.items() if count > budget),
            key=lambda item: (-item[1], item[0]),
        )
        return {
            "state": "SUCCESS",
            "container_count": len(containers),
            "relation_count": sum(fan_out.values()),
            "max_fan_out": max(fan_out.values(), default=0),
            "over_budget": [f"{name}={count}" for name, count in over],
            "isolated": sorted(name for name in containers if fan_out[name] == 0 and name not in incoming),
        }

    return respond(payload, work)


def adr_transition(payload: dict[str, Any]) -> dict[str, Any]:
    """Whether an ADR may move between statuses. Statuses match scripts/arch_adr.py.

    proposed -> accepted | rejected; accepted -> deprecated | superseded; deprecated -> superseded.
    rejected and superseded are terminal. Moving to superseded needs superseded_by.
    """

    def work() -> dict[str, Any]:
        src, dst = payload["from_status"], payload["to_status"]
        superseded_by = payload.get("superseded_by")
        if src == dst:
            allowed, reason = False, f"status is already {src}"
        elif not _ADR_NEXT[src]:
            allowed, reason = False, f"{src} is terminal"
        elif dst not in _ADR_NEXT[src]:
            allowed, reason = False, f"{src} cannot move to {dst}"
        elif dst == "superseded" and not superseded_by:
            allowed, reason = False, "superseded requires superseded_by, the id of the superseding ADR"
        else:
            allowed = True
            reason = f"{src} -> {dst} is allowed"
            if dst == "superseded":
                reason += f" by {superseded_by}"
        return {"state": "SUCCESS", "allowed": allowed, "reason": reason}

    return respond(payload, work)


def nfr_budget(payload: dict[str, Any]) -> dict[str, Any]:
    """Measured NFR values checked against their budgets.

    max budgets breach when the value exceeds the limit; min budgets breach when it falls below.
    A budget with no measurement is unmeasured and keeps within_budget false (fail closed).
    """

    def work() -> dict[str, Any]:
        budgets = payload["budgets"]
        names = [item["metric"] for item in budgets]
        if len(set(names)) != len(names):
            return _invalid("budgets", "budget metrics must be unique")
        measured: dict[str, float] = {}
        for item in payload["measurements"]:
            if item["metric"] in measured:
                return _invalid("measurements", f"metric {item['metric']} is measured twice")
            measured[item["metric"]] = item["value"]
        breaches: list[str] = []
        unmeasured: list[str] = []
        checked = 0
        for item in budgets:
            metric, limit, direction = item["metric"], item["limit"], item["direction"]
            if metric not in measured:
                unmeasured.append(metric)
                continue
            value = measured[metric]
            checked += 1
            if direction == "max" and value > limit:
                breaches.append(f"{metric} {value} > {limit} (max)")
            elif direction == "min" and value < limit:
                breaches.append(f"{metric} {value} < {limit} (min)")
        return {
            "state": "SUCCESS",
            "checked": checked,
            "within_budget": not breaches and not unmeasured,
            "breaches": breaches,
            "unmeasured": unmeasured,
        }

    return respond(payload, work)


def nfr_allocation(payload: dict[str, Any]) -> dict[str, Any]:
    """Whether every NFR is bound to a contract clause or component budget that can be measured.

    A quantitative NFR counts as allocated only when at least one of its bindings carries a numeric
    limit. A qualitative NFR (for example an auth scheme) needs one binding of any kind. An NFR with no
    binding is unallocated; a quantitative NFR bound only without a limit is unmeasurable. Bindings for
    ids that are not in `nfrs` are listed as unknown and keep fully_allocated false (fail closed).
    """

    def work() -> dict[str, Any]:
        nfrs = payload["nfrs"]
        ids = [item["id"] for item in nfrs]
        if any(not item.strip() for item in ids):
            return _invalid("nfrs", "NFR ids must not be blank")
        if len(set(ids)) != len(ids):
            return _invalid("nfrs", "NFR ids must be unique")
        kind_of = {item["id"]: item["kind"] for item in nfrs}
        bound: dict[str, list[dict[str, Any]]] = {}
        unknown: set[str] = set()
        seen: set[tuple[str, str, str]] = set()
        for binding in payload["bindings"]:
            if not binding["target_ref"].strip():
                return _invalid("bindings", "target_ref must not be blank")
            key = (binding["nfr_id"], binding["target_kind"], binding["target_ref"])
            if key in seen:
                return _invalid("bindings", f"binding {binding['nfr_id']} -> {binding['target_ref']} is listed twice")
            seen.add(key)
            if binding["nfr_id"] in kind_of:
                bound.setdefault(binding["nfr_id"], []).append(binding)
            else:
                unknown.add(binding["nfr_id"])
        allocated: list[str] = []
        unallocated: list[str] = []
        unmeasurable: list[str] = []
        for nfr_id in ids:
            bindings = bound.get(nfr_id)
            if not bindings:
                unallocated.append(nfr_id)
            elif kind_of[nfr_id] == "quantitative" and not any("limit" in b for b in bindings):
                unmeasurable.append(nfr_id)
            else:
                allocated.append(nfr_id)
        return {
            "state": "SUCCESS",
            "nfr_count": len(ids),
            "allocated": allocated,
            "unallocated": unallocated,
            "unmeasurable": unmeasurable,
            "unknown_nfr_ids": sorted(unknown),
            "coverage_pct": round(100 * len(allocated) / len(ids), 1),
            "fully_allocated": len(allocated) == len(ids) and not unknown,
        }

    return respond(payload, work)


def breaking_readiness(payload: dict[str, Any]) -> dict[str, Any]:
    """Autonomy level and outstanding requirements before a contract change may be published.

    Breaking or external-facing changes are L3 and always need human approval (breaking_contract). A
    breaking change also needs a migration plan and, from every consumer, an acknowledgement of the exact
    head version; an acknowledgement of any other version does not count. Other changes are L2. `ready`
    is true only when nothing is outstanding and no human approval is required.
    """

    def work() -> dict[str, Any]:
        head = payload["head_version"]
        if _core(head) is None:
            return _invalid("head_version", "head_version must be MAJOR.MINOR.PATCH")
        consumers = payload["consumers"]
        if any(not name.strip() for name in consumers):
            return _invalid("consumers", "consumer names must not be blank")
        if len(set(consumers)) != len(consumers):
            return _invalid("consumers", "consumer names must be unique")
        breaking = payload["breaking_change"]
        level = "L3" if breaking or payload["external_facing"] else "L2"
        acked = {ack["consumer"] for ack in payload["acks"] if ack["version"] == head}
        missing_acks = [name for name in consumers if name not in acked] if breaking else []
        missing: list[str] = []
        needs: list[str] = []
        if breaking and not payload["migration_plan_present"]:
            missing.append("migration_plan")
            needs.append("migration plan")
        if missing_acks:
            missing.append("consumer_acks")
            needs.extend(f"consumer ack of {head}: {name}" for name in missing_acks)
        if level == "L3":
            needs.append("human-approval: breaking_contract")
        return {
            "state": "SUCCESS",
            "autonomy_level": level,
            "missing_acks": missing_acks,
            "missing": missing,
            "ready": not needs,
            "needs": needs,
        }

    return respond(payload, work)


def adr_supersession(payload: dict[str, Any]) -> dict[str, Any]:
    """Integrity of an ADR set's supersession links (each ADR names at most one it supersedes).

    dangling: X -> Y where Y is not in the set. cycles: ADRs that supersede each other through a loop
    (a self-link is a cycle of one). forks: an ADR superseded by two or more ADRs. status_mismatches: an
    ADR whose status disagrees with its links: superseded without a successor, still live although an
    accepted successor exists, superseded by a successor that is only proposed, or superseded by a
    rejected ADR. heads: ADRs nothing supersedes; chains: each head with two or more links, newest first.
    """

    def work() -> dict[str, Any]:
        adrs = payload["adrs"]
        ids = [item["adr_id"] for item in adrs]
        if any(not item.strip() for item in ids):
            return _invalid("adrs", "adr_id must not be blank")
        if len(set(ids)) != len(ids):
            return _invalid("adrs", "adr_id values must be unique")
        status = {item["adr_id"]: item["status"] for item in adrs}
        link = {item["adr_id"]: item["supersedes"] for item in adrs if item.get("supersedes")}
        successors: dict[str, list[str]] = {}
        dangling: list[str] = []
        for src in ids:
            dst = link.get(src)
            if dst is None:
                continue
            if dst in status:
                successors.setdefault(dst, []).append(src)
            else:
                dangling.append(f"{src} -> {dst}")
        mismatches: list[str] = []
        for dst in ids:
            by = successors.get(dst, [])
            if status[dst] == "superseded" and not by:
                mismatches.append(f"{dst} is superseded but no ADR supersedes it")
            for src in by:
                if status[src] == "rejected":
                    mismatches.append(f"{dst} is superseded by {src}, which is rejected")
                elif status[src] == "proposed" and status[dst] == "superseded":
                    mismatches.append(f"{dst} is superseded by {src}, which is only proposed")
                elif status[src] != "proposed" and status[dst] != "superseded":
                    mismatches.append(f"{dst} is {status[dst]} although {src} ({status[src]}) supersedes it")
        forks = sorted(f"{dst} <- {', '.join(sorted(by))}" for dst, by in successors.items() if len(by) > 1)
        cycles = _supersession_cycles(ids, link)
        heads = sorted(adr for adr in ids if adr not in successors)
        chains: list[str] = []
        for head in heads:
            walk, seen = [head], {head}
            while walk[-1] in link and link[walk[-1]] in status and link[walk[-1]] not in seen:
                walk.append(link[walk[-1]])
                seen.add(walk[-1])
            if len(walk) > 1:
                chains.append(" -> ".join(walk))
        return {
            "state": "SUCCESS",
            "dangling": dangling,
            "status_mismatches": mismatches,
            "forks": forks,
            "chains": chains,
            "cycles": cycles,
            "heads": heads,
            "valid": not (dangling or mismatches or forks or cycles),
        }

    return respond(payload, work)


def _supersession_cycles(ids: list[str], link: dict[str, str]) -> list[str]:
    """Each loop of single-successor links once, members sorted and joined by ', '."""
    cycles: dict[str, str] = {}
    for start in ids:
        path: list[str] = []
        node: str | None = start
        while node is not None and node not in path:
            path.append(node)
            node = link.get(node)
        if node is not None:
            loop = sorted(path[path.index(node):])
            cycles[loop[0]] = ", ".join(loop)
    return [cycles[key] for key in sorted(cycles)]
