# Swarm dependency-graph baseline (SPE-38338)

Baseline validation of the Swarm dependency graph **from actual code/schema only** —
no invented topology. No optimisation: this is the baseline record, not a plan.

Method: read the graph sources named by `INVENTORY.md` (§4), then run
`swarm.graph_planner` live (`python3 /tmp/graphcheck_run.py`, read-only).
Gates/formatters were skipped per assignment.

## 1. Where the graph actually lives

There is **no single static topology file** (no committed task DAG). The graph
exists in four places; validation below covers each:

| Surface | Source | What it holds |
|---|---|---|
| Python planner | `swarm/graph_planner.py:1-332` — `TaskNode` (id, files, dependencies, complexity, priority, duration), `plan_graph`, `topological_waves`, `validate_graph`, `CycleError`/`UnknownDependencyError`/`DuplicateNodeError`/`FileConflictError` | Generic validator; accepts any node set. **No committed nodes.** |
| Canonical fixtures (AC-39/AC-40) | `swarm/graph_quality.py:620-644` — `canonical_validation_nodes()` (10 nodes, 12 links), `canonical_order_nodes()` (5-node chain) | The only pinned topology. Validated live through `GraphPlanner`. |
| Persisted edges | `migrations/V0002__graph_engine.sql:25-30` (`graph_edges`, M-2026-002) + `contracts/sc-graph-engine.json:9-11` (`correlation_id`, `node_id`, `predecessor_id`) | Live `.swarm/tasks.db`: **0 `graph_edges` rows** (242 `tasks` rows, 0 edges). No persisted topology today. |
| Rust scheduler | `crates/gsd-scheduler/src/dag.rs:1-211` (`TaskDag`, `detect_cycles`, `get_ready_tasks`), `ready_frontier.rs:1-110` | Runtime DAG over submitted tasks; insertion cannot close a cycle (`dag.rs:3-5`). |

Schema check: `swarm/schemas/` holds `tool.contract.v1.json`,
`swarm.event.v1.json`, `swarm.live.v1.json`, `handover.v1.json`,
`task.result.v1.json` — **no graph-schema file**; edge shape comes from
`contracts/sc-graph-engine.json` + the migration above.

## 2. Topological order (live run)

`canonical_validation_nodes()` (`swarm/graph_quality.py:620-633`):

```
subtasks=10  predecessor_links=12  cycles=0  dangling_links=0
waves:   (start) → (extra1, n1, n2, n3, n4) → (m1, m2, m3) → (last)
order:   start, extra1, n1, n2, n3, n4, m1, m2, m3, last
makespan=4.0  (sum of per-wave max durations)
```

Edges: `n1..n4, extra1 ← start`; `m1 ← n1,n2`; `m2 ← n2,n3`; `m3 ← n3`;
`last ← m1,m2`. Within-wave order is by descending priority then ascending id
(`swarm/graph_planner.py:13-14, 196-197`).

`canonical_order_nodes()` chain: `a → b → c → d → e`, waves 1 node each,
makespan 5.0. Counts: `subtasks=5 predecessor_links=4 cycles=0 dangling=0`.

## 3. Cycles (path reported)

Planner raises `CycleError` with the concrete cycle path
(`swarm/graph_planner.py:206-209`, `_find_cycle` `:218-240`). Probe
(`a←c, b←a, c←b`):

```
CycleError: dependency cycle detected: a -> c -> b -> a
```

Other live probes:

```
UnknownDependencyError:  node 'x' depends on unknown node 'ghost'
DuplicateNodeError:      duplicate node id 'd'
FileConflictError:       nodes 'p1' and 'p2' both claim overlapping file
                         'docs/a.md' (vs 'docs/a.md') in parallel wave 0
```

Rationale: `graph_validation_counts` (`graph_quality.py:658-672`) maps
`CycleError → cycles=1`, other `GraphError → cycles=0` (dangling counted
separately); both fixtures report `cycles=0`.

## 4. Unsatisfiable tool requirements

**Blocked by construction — the planner has no tool field.**
`TaskNode` fields are exactly `(id, files, dependencies, complexity,
priority, duration)` (live `inspect.signature` probe, `graph_planner.py:73-82`).
A node requiring a tool **cannot be expressed**, so a tool-requirement
unsatisfiability would pass silently. Tool counts are enforced elsewhere and
currently satisfiable: `agents.json` `capability_policy.tools` is
`{min: 10, max: 15, enforce: true}`, and live `registered_tools` per agent
are min **12** / max **13** (inside the range; `swarm/tool_registry.py:444-473`).
Risk to record: tool requirements are validated per-agent, not per-graph-node.

## 5. Deadlock / starvation

- **Python planner:** no worker cap, no queueing — Kahn waves either place
  every node or raise `CycleError` (`graph_planner.py:200-215`). No deadlock
  or starvation concept exists here.
- **Rust `TaskDag` (real deadlock risk):** `mark_failed` leaves dependents
  blocked forever (`dag.rs:22-23, 140-141`), and `remove_task` refuses any
  task that still has dependents (`dag.rs:68-70`). A failed node with live
  dependents can therefore neither complete, be retried by the DAG, nor be
  removed — dependents stay blocked. Recovery must come from outside the DAG
  (scheduler retry/drain), not from the graph itself.
- **Starvation (Rust frontier):** `ready_frontier` admits **greedily in
  workflow order** and caps at `free_slots` (`ready_frontier.rs:49-50`). If
  `free_slots=0` or an agent's `max_parallel` is saturated, later tasks wait
  with no aging/priority rule (Python planner's priority sort is planner-only;
  `max_parallel=0` is coerced to 1, `ready_frontier.rs:18`).
- **Capacity (API):** worker cap 30, ceiling 32 (`graph_quality.py:85-86`);
  over-cap submits get `429 E-CAPACITY` (`graph_quality.py:14-15`) — the
  only starvation mitigation is refusal, not scheduling.

## 6. Blocked nodes

| Blocker | Evidence |
|---|---|
| No persisted topology to order | `graph_edges` 0 rows in `.swarm/tasks.db` (live read); only in-memory fixtures + store-when-present (`graph_quality.py:675-686`, falls back to fixture on empty/failed read). |
| Tool requirements unexpressible | `TaskNode` fields (§4). Any tool-gated node is a recorded gap, not a planner refusal. |
| Failed-dependency dead end (Rust) | §5 — `dag.rs:68-70` + `:140-141`. |
| No starvation defence | §5 — greedy frontier, cap-refusal only. |

## 7. Sequential fallback (noted, not optimised)

Per `graph_planner.py:22-23`, sequential waves *may* safely reuse files, but
a single wave refuses overlapping claims with `FileConflictError`. Fallback
when parallel dispatch is unsafe: **run the wave order strictly one node at
a time** (chain each wave into singletons), which the planner already does
naturally for dependent chains (`a → b → c → d → e`, §2). That is the
baseline degraded mode; no priority promotion, retry or rebalancing is
proposed here.

## 8. Evidence trail

- Live run: `python3 /tmp/graphcheck_run.py` (1.09 s) — counts, waves, order,
  makespan, cycle path, 3 refusal probes, `TaskNode` fields, `graph_edges`
  count, `tasks` count.
- `INVENTORY.md:138-181` (Python/Rust graph + scheduler symbols),
  `:236-247` (cheap-check baseline; full suites not run).
- Skipped per assignment: gates, formatters, full test suites. No code changed.
