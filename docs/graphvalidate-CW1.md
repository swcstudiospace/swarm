# Swarm dependency-graph validation — CW1 (baseline, no optimisation)

Validated from **actual code/schema only** — no invented topology.
Method: read the graph surfaces named by `INVENTORY.md` (§4) and `docs/graphcheck-SPE-38338.md`,
then ran read-only live probes (`PYTHONPATH=<repo> SWARM_PURE_PYTHON=1 python3 /tmp/graphvalidate_cw1_probe*.py`;
`.swarm/tasks.db` opened `mode=ro`). Gates/formatters skipped per assignment. No code changed.

Branch at validation time: `swarm/Tbe37-be` (`git branch --show-current`).

## 1. Where the graph actually lives

There is **no single static topology file**. Four surfaces hold dependency edges:

| Surface | Source | What it holds |
|---|---|---|
| Python planner | `swarm/graph_planner.py:1-28` (`TaskNode` id/files/dependencies/complexity/priority/duration; `plan_graph`, `topological_waves`, `validate_graph`; `CycleError`/`UnknownDependencyError`/`DuplicateNodeError`/`FileConflictError`) | Generic Kahn-wave validator; accepts any node set. **No committed nodes.** |
| Canonical fixtures (AC-39/40/59) | `swarm/graph_quality.py:629-667` — `canonical_validation_nodes()` (10 nodes/12 links), `canonical_order_nodes()` (5-node chain), `canonical_tracker_nodes()` (8-node chain, new vs SPE-38338) | The only pinned topologies. Validated live through `GraphPlanner`. |
| Persisted edges — two stores | `migrations/V0002__graph_engine.sql:25-30` + `contracts/sc-graph-engine.json` (`correlation_id`/`node_id`/`predecessor_id`) vs `swarm/taskstore.py:78,356-367,1271-1285` (`tasks.depends_on` JSON + `deps_satisfied()`/`ready()`) | Live `.swarm/tasks.db`: **`graph_edges` = 0 rows**, `tasks` = 276 rows. The persisted task DAG lives in `depends_on`, **not** in `graph_edges`. |
| Rust scheduler | `crates/gsd-scheduler/src/dag.rs:1-211` (`TaskDag`, `detect_cycles`, `get_ready_tasks`), `ready_frontier.rs:1-110`; proto `crates/gsd-proto/src/gsd.proto:79-88` (`Task`: id/subagent_type/dependencies/priority/retry/timeout/metadata) | Runtime DAG over submitted tasks. |

Schema check: `swarm/schemas/` holds `tool.contract.v1.json`, `swarm.event.v1.json`,
`swarm.live.v1.json`, `handover.v1.json`, `task.result.v1.json` — **no graph-schema file**;
edge shape comes from `contracts/sc-graph-engine.json` + `migrations/V0002__graph_engine.sql`.

## 2. Topological order (live run)

### 2a. Canonical fixtures

`canonical_validation_nodes()` (`swarm/graph_quality.py:629-642`):

```
subtasks=10  predecessor_links=12  cycles=0  dangling_links=0
waves:   (start) → (extra1, n1, n2, n3, n4) → (m1, m2, m3) → (last)
order:   start, extra1, n1, n2, n3, n4, m1, m2, m3, last
makespan=4.0  (sum of per-wave max durations)
```

Edges: `n1..n4, extra1 ← start`; `m1 ← n1,n2`; `m2 ← n2,n3`; `m3 ← n3`; `last ← m1,m2`.
Within-wave order is descending priority then ascending id (`swarm/graph_planner.py:196-197`).

`canonical_order_nodes()` chain: `a → b → c → d → e`, one node per wave, makespan 5.0.
Counts: `subtasks=5 predecessor_links=4 cycles=0 dangling=0`.

`canonical_tracker_nodes()` (`swarm/graph_quality.py:656-667`, AC-59): `t0 → t1 → … → t7`,
one node per wave, makespan 8.0. Counts: `subtasks=8 predecessor_links=7 cycles=0 dangling=0`.

### 2b. Persisted TaskStore graph (live, read-only)

Head/Kahn over all 276 `tasks.depends_on` edges (`swarm/taskstore.py:1272-1285`):

```
acyclic over TaskStore depends_on: waves=9  placed=276
wave sizes: [84, 34, 33, 13, 59, 15, 12, 25, 1]
dangling deps: 0   self-deps: 0
```

States: DONE 73, IN_REVIEW 66, PLANNED 78, BLOCKED 26, IN_PROGRESS 9, ESCALATED 12,
FAILED 3, CANCELLED 4, CHANGES_REQUESTED 2, CREATED 2, VALIDATED 1.
`SATISFIED_STATES = {APPROVED, DONE, IN_REVIEW}` (`swarm/taskstore.py:1273-1278`).
Ready now (PLANNED/RETRY/CREATED/VALIDATED with all deps satisfied): **13**.
Schedulable-state tasks still waiting on unsatisfied deps: **68** (e.g. `Te949-rel`,
`CIRN-join.r1`, `Tf0e5-arch/ux/data/be/fe/qa`) — see §6 for blocked vs waiting distinction.

## 3. Cycles (path reported)

Planner raises `CycleError` with the concrete path (`swarm/graph_planner.py:206-209`,
DFS in `_find_cycle` `:218-240`). Live probes:

```
CycleError: dependency cycle detected: a -> c -> b -> a        (a←c, b←a, c←b)
CycleError: dependency cycle detected: s -> s                  (self-loop)
UnknownDependencyError:  node 'x' depends on unknown node 'ghost'
DuplicateNodeError:      duplicate node id 'd'
FileConflictError:       nodes 'p1' and 'p2' both claim overlapping file
                         'docs/a.md' (vs 'docs/a.md') in parallel wave 0
```

`graph_validation_counts` (`swarm/graph_quality.py:681-695`) maps `CycleError → cycles=1`,
other `GraphError → cycles=0` (dangling counted separately). All three fixtures report `cycles=0`;
the persisted `depends_on` graph is acyclic (§2b). Rust `TaskDag` insertion cannot close a cycle
(deps must already exist, `dag.rs:3-5, 47-60`); `detect_cycles` (`dag.rs:116-121`, petgraph
`toposort`) is an independent check.

## 4. Unsatisfiable tool requirements

**Blocked by construction — neither graph surface has a tool field.**
Python `TaskNode` fields are exactly `(id, files, dependencies, complexity, priority, duration)`
(live `inspect.signature` probe; `swarm/graph_planner.py:73-82`). Rust `Task` proto fields are
`(id, subagent_type, input_payload, dependencies, priority, retry_policy, timeout, metadata)`
(`crates/gsd-proto/src/gsd.proto:79-88`). A node requiring a tool **cannot be expressed** on
either surface, so a per-node tool-requirement unsatisfiability would pass silently.

Per-agent tool requirements are enforced elsewhere and are currently satisfiable:
`agents.json` `capability_policy.tools = {min: 10, max: 15, enforce: true}` (skills identical),
live `registered_tools` per agent = min **12** / max **13** over 30 agents
(inside range; enforced by `swarm/tool_registry.py:444-473`, which errors on any agent outside
min/max). Risk to record: tool requirements are validated per-agent, never per-graph-node.

## 5. Deadlock / starvation risks

- **Python planner:** no worker cap, no queueing — Kahn waves either place every node or raise
  `CycleError` (`swarm/graph_planner.py:200-215`). No deadlock/starvation concept here.
- **Rust `TaskDag` (real deadlock risk):** `mark_failed` leaves dependents blocked forever
  (`dag.rs:22-23, 145-158` — "their dependents stay blocked"), and `remove_task` refuses any
  task that still has dependents (`dag.rs:71-81`). A failed node with live dependents can
  therefore neither complete, be retried by the DAG, nor be removed — recovery must come from
  outside the DAG (scheduler retry/drain), not the graph itself.
- **Starvation (Rust frontier):** admission is **greedy in workflow order**, capped at
  `free_slots` (`ready_frontier.rs:11, 44, 49-58`). With `free_slots=0` or a saturated
  `max_parallel`, later tasks wait with no aging/priority rule (`max_parallel=0`/missing → 1,
  `ready_frontier.rs:18, 82-87`). Python-planner priority sorting is planner-only.
- **Capacity (API):** worker cap 30, ceiling 32 (`swarm/graph_quality.py:90-91`); over-cap
  submits get `429 E-CAPACITY` (`swarm/graph_quality.py:906, 948`) — refusal, not scheduling.
- **TaskStore gate:** a failing gate pulls its producer back to `CHANGES_REQUESTED`, which
  un-satisfies its dependants (`swarm/taskstore.py:1273-1275`) — a downstream stall path
  outside the DAG itself.

## 6. Blocked nodes

| Blocker | Evidence |
|---|---|
| No persisted `graph_edges` topology to order | `graph_edges` 0 rows in `.swarm/tasks.db` (live read-only count); `graph_validation_body`/`ready_order_body` fall back to fixtures on empty store (`swarm/graph_quality.py:698-709, 734-743`). The live DAG is `tasks.depends_on` (§2b). |
| Tool requirements unexpressible per node | §4 — `TaskNode` + `Task` proto fields. Any tool-gated node is a recorded gap, not a planner refusal. |
| 68 schedulable tasks waiting on unsatisfied deps | Live count (§2b); e.g. `Te949-rel → [Te949-qa, Te949-rev, Te949-sec]`, `Tf0e5-*` chain behind `Tf0e5-req`. 13 are ready now. |
| Failed/cancelled dep dead-ends | By state: FAILED 3, CANCELLED 4, ESCALATED 12, BLOCKED 26. A dependant whose dep lands in a non-`SATISFIED_STATES` terminal state never becomes ready under `deps_satisfied()` unless reworked — e.g. `CIRN-join` is DONE while `CIRN-workflows`/`CIRN-helpers` are not in `SATISFIED_STATES`, so `CIRN-join.r1`/`CIRN-rev`/`CIRN-qa` stay blocked. |
| Failed-dependency dead end (Rust) | §5 — `dag.rs:71-81` + `:145-158`. |
| No starvation defence | §5 — greedy frontier, cap-refusal only. |

## 7. Sequential fallback (noted, not optimised)

Per `swarm/graph_planner.py:20-23`, sequential waves *may* safely reuse files, but a single
parallel wave refuses overlapping claims with `FileConflictError`. Baseline degraded mode when
parallel dispatch is unsafe: **run the wave order strictly one node at a time** — flatten each
wave into singletons in canonical order (§2a/§2b). This is already the natural shape for
dependent chains (`a → b → … → e`, `t0 → … → t7`), and `TaskStore.ready()` returns the same
set whether dispatched in one batch or sequentially. No priority promotion, retry or
rebalancing is proposed in this baseline.

## 8. Evidence trail

- Live probes (read-only, repo code): `/tmp/graphvalidate_cw1_probe.py` (fixtures, waves,
  order, makespan, cycle/unknown/duplicate/file-conflict/self-loop refusals, `TaskNode` fields,
  `graph_edges`/`tasks` counts, `agents.json` tool counts); `…_probe2.py` (blocked/dangling/
  self-dep counts, `SATISFIED_STATES`); `…_probe3.py` (Kahn over persisted `depends_on`: 9 waves,
  acyclic, 13 ready / 68 waiting).
- `INVENTORY.md` (§4 Python/Rust graph + scheduler symbols), `docs/graphcheck-SPE-38338.md`
  (prior baseline; this report adds the `canonical_tracker_nodes` fixture, the persisted
  `depends_on` topo order, and current counts: 276 tasks vs 242 then).
- Skipped per assignment: gates, formatters, full test suites. No code changed; only this file written.
