# Swarm Graph Model Design (SPE-38341)

**Issue:** SPE-38341 (design only, no code)
**Branch:** `swarm/T74c2-be` (`agent-swarm`, repo `swcstudiospace/swarm`)
**Status:** Design — schemas + validation + test checklist. No implementation.
**Wave 1 inputs (read, not re-derived):**
- `docs/graphcheck-SPE-38338.md` — baseline validation of the Swarm dependency graph from code/schema only (Python planner, canonical fixtures, persisted edges, Rust scheduler).
- `docs/reconcile-SPE-38338.md` — roadmap/state reconciliation; 12 outstanding phases, 5 blockers (B1–B5).
- Decisions carried forward (assignment context, not file-derived): **fakes only** (no live Linear/Notion writes), **learning disabled** until policy, **approver = user via pause**, **tool-unless-judgement** (prefer tool calls; use judgement only where a tool cannot answer).

**What this doc is:** the target model for how a Swarm graph is *described, validated, scheduled and degraded*. It replaces the current single-`TaskNode` planner (`swarm/graph_planner.py`) and the storage-only edge table (`graph_edges`, M-2026-002) with an explicit three-node-type model plus typed edges, without changing any existing behaviour until the follow-up implementation task lands. Every "today" claim below is cited to actual source; every "proposed" claim is marked as such.

**Scope guard:** design only. No code, no migration, no contract version change, no gate run (gates/formatters skipped per assignment). Test checklist in §9 is a checklist for the implementer, not a test run.

---

## 1. Current state (ground truth)

### 1.1 The Python planner is generic and tool-blind

`swarm/graph_planner.py` (332 lines) is a deterministic validator + scheduler over `TaskNode`:

- `TaskNode` fields are exactly `(id, files, dependencies, complexity, priority, duration)` — verified live in graphcheck §4 via `inspect.signature` probe (`graph_planner.py:73-82`). There is **no tool field, no owner field, no typed I/O, no success-criteria field, no state field**.
- Semantics (`graph_planner.py:1-27`): Kahn topological waves; within-wave order by descending `priority` then ascending `id` (`_sort_key`, `graph_planner.py:196-197`); duration = explicit `duration` else `complexity` (default `1.0`); makespan = sum over waves of the longest duration in the wave (`plan_graph`, `graph_planner.py:287-319`); parallel-wave file-claim overlap refused with `FileConflictError` (equal paths or nested paths, `_paths_overlap`, `graph_planner.py:243-245`); unknown dependencies → `UnknownDependencyError`, duplicate ids → `DuplicateNodeError`, cycles → `CycleError` with the concrete path (`_find_cycle`, `graph_planner.py:218-240`).
- The module is intentionally isolated: "No CLI or registry integration: importing this module is the whole surface" (`graph_planner.py:27`). There is **no committed node set** — only in-memory fixtures (`swarm/graph_quality.py:620-644`: `canonical_validation_nodes()` 10 nodes/12 links, `canonical_order_nodes()` 5-node chain). Live baseline (graphcheck §2): `subtasks=10 predecessor_links=12 cycles=0 dangling_links=0`, waves `(start) → (extra1, n1, n2, n3, n4) → (m1, m2, m3) → (last)`, makespan `4.0`.

### 1.2 Persisted edges exist but carry no semantics

- `migrations/V0002__graph_engine.sql:25-30` (`graph_edges`, M-2026-002): columns `(id, correlation_id, node_id, predecessor_id, priority, ts)`, `UNIQUE (correlation_id, node_id, predecessor_id)`. Direction is *predecessor → node*.
- `contracts/sc-graph-engine.json:9-11` pins the same three keys plus `agent_id, capability, engine, entry_id, crate_id, language` (SC-GraphEngine 1.0.0, migration M-2026-002). **There is no edge-type column, no strictness column, no payload column.**
- Live state (graphcheck §§1, 6): **0 `graph_edges` rows** in `.swarm/tasks.db` (242 `tasks` rows, 0 edges). No persisted topology exists today; `swarm/graph_quality.py:675-686` falls back to the fixture on empty/failed read.

### 1.3 The Rust scheduler has the same shape, plus real failure semantics

`crates/gsd-scheduler/src/dag.rs` (211 lines): edges point *dependency → dependent*; a task may only depend on tasks already added, so insertion cannot close a cycle (`dag.rs:3-5`); `detect_cycles` (`dag.rs:115-120`) is an independent check; `critical_path_len` / `downstream_depths` exist (`dag.rs:177-202`); `get_ready_tasks` returns tasks whose dependencies are all complete, in insertion order (`dag.rs:159-175`).

`crates/gsd-scheduler/src/ready_frontier.rs` (110 lines): `ready_frontier` is pure — a task is ready iff dependencies complete, not completed/in-flight, agent has a free `max_parallel` slot, and requested file claims do not conflict with active claims or with a claim admitted earlier in the same frontier. Admission is **greedy in workflow order**, capped at `free_slots`; `max_parallel = 0` (or a missing agent entry) is coerced to 1 (`ready_frontier.rs:18`). No aging, no priority rule (the Python planner's priority sort is planner-only).

**Recorded risks (graphcheck §5, binding on this design):**
- Rust `mark_failed` leaves dependents blocked forever (`dag.rs:22-23, 140-141`), and `remove_task` refuses any task that still has dependents (`dag.rs:68-70`) — a failed node with live dependents can neither complete, be retried by the DAG, nor be removed. Recovery must come from outside the DAG.
- Starvation: greedy frontier + slot saturation means later tasks wait with no defence; the only capacity mitigation is API refusal — worker cap 30, ceiling 32 (`graph_quality.py:85-86`), over-cap submits get `429 E-CAPACITY`.

### 1.4 Tool requirements are validated per-agent, not per-node

`agents.json` `capability_policy.tools = {min: 10, max: 15, enforce: true}`; live `registered_tools` per agent are min 12 / max 13 (inside range; `swarm/tool_registry.py:444-473`). Tool contract shape: `swarm/schemas/tool.contract.v1.json` (required: `name, owner, purpose, input_schema, output_schema, errors, timeout_s, idempotency, permitted_callers, entrypoint, handler, examples`; name is lane-prefixed, e.g. `be_`, `shared_`; `idempotency ∈ {idempotent, at-least-once, unsafe}`).

**The gap (graphcheck §4, load-bearing for this design):** because `TaskNode` has no tool field, a node requiring a tool *cannot be expressed*, so a tool-requirement unsatisfiability would pass silently. Tool counts are enforced per-agent, never per-graph-node.

### 1.5 What the design must not do

Constraints from Wave 1 and the repo:
- **Fakes only** — any graph fixture, test double or tracker mirror used to exercise this model must be a fake/TBD; no live Linear/Notion writes (SPE-38338 realtime-tracker limit, `docs/reconcile-SPE-38338.md` §4).
- **Learning disabled** — the model must not require a learning component; any `learning` parameter is out of scope until policy exists (HTIL notes are user-recorded, not ratified policy — `docs/reconcile-SPE-38338.md` §4).
- **Approver = user via pause** — any human gate in the model (success-criteria approval, ship, phase close) is a pause point for the user, never auto-approved.
- **Tool-unless-judgement** — validation and scheduling steps that can be tool-checked must be tool-checked; judgement is reserved for what tools cannot answer (see §7.4).
- Counterparties to keep in band: SPE-38340 (requirements), SPE-38339 (frontier-starvation/sequential-fallback design — this doc §7.3/§8 stay compatible with it rather than duplicating it).

---

## 2. Design principles

1. **One resource = one node type.** A task (work), an agent (owner), and a tool (capability) are different kinds of thing with different lifecycles. Conflating them in one `TaskNode` is why tool requirements silently pass and ownership is implicit. Three node types, one graph.
2. **Every edge is typed and strict.** An untyped `predecessor_id` cannot distinguish "must finish before" from "is assigned to" from "provides data that". Four edge types (§5), each with a strictness that changes validation and scheduling.
3. **Validation is total and early.** A graph that cannot run must be refused before any dispatch — with the concrete evidence (cycle *path*, unknown binding *name*, unsatisfiable *tool name*) in the error, never a bare boolean.
4. **Parallelism is derived, not asserted.** Waves, critical path and makespan are computed from the validated graph by the same deterministic algorithm the planner already uses (Kahn + priority/id order), not by hand-written wave numbers.
5. **Degrade loudly, then degrade.** Deadlock and starvation are detected and reported as first-class findings; the sequential fallback (§8) is the last resort, not the default.
6. **Schemas pin the contract.** Node and edge payloads are JSON-Schema-shaped (§4, §5); the store keeps the payload verbatim so a reader never has to guess. Existing `graph_edges` rows remain readable — migration is additive only (repo convention: `CREATE TABLE IF NOT EXISTS`, no `ALTER`, no `DROP` — `V0002__graph_engine.sql:15-17`).

---

## 3. Graph overview

```
┌─────────────┐   data-flow / dependency (strict)   ┌─────────────┐
│  TASK NODE  │ ───────────────────────────────────▶ │  TASK NODE  │
│ (work unit) │                                     │ (work unit) │
└──────┬──────┘                                     └──────▲──────┘
       │ assignment (strict)                                │ assignment
       ▼                                                    │
┌─────────────┐   tool-binding (strict)    ┌─────────────┐   │ assignment
│ AGENT NODE  │ ──────────────────────────▶│  TOOL NODE  │   │ (strict)
│ (owner)     │                            │ (capability)│   │
└─────────────┘                            └─────────────┘   │
       ▲                                                   │
       └────────────── tool-binding (soft, optional) ───────┘
```

- **Task nodes** are the only nodes that get scheduled (waves, critical path, makespan). An Agent or Tool node never appears in a wave.
- **Agent nodes** own tasks (assignment edges) and declare which tools they are permitted to call (tool-binding edges).
- **Tool nodes** are capabilities satisfying `swarm/schemas/tool.contract.v1.json`; a task that *requires* a tool is checked against the Tool nodes its owning Agent is bound to.
- **Data-flow edges** carry typed payloads between tasks (inputs/outputs). A task's declared inputs must be satisfied by incoming data-flow edges (or by an explicit external source); a task's declared outputs that no downstream edge references are legal but flagged (see §6.3).

The model subsumes the current shapes: today's `dependencies` list = incoming *dependency* edges; today's `files` list = the task's file claims (§4.1); today's persisted `graph_edges` row = one dependency edge (`predecessor_id` → `node_id`).

---

## 4. Node types

### 4.1 Task node

One unit of work. Extends today's `TaskNode` (which stays the scheduling projection — §4.4) with owner, typed I/O, tool requirements, success criteria, state and idempotency.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `id` | string | yes | Unique task id (e.g. `T-884`). Duplicate ids are refused (`DuplicateNodeError` today; §6.1). |
| `owner` | string | yes | Agent id (`A01`..`A30`) that owns execution. Must resolve to an Agent node (§6.2). A task's owner today is implicit in the assigning agent; here it is explicit. |
| `title` | string | yes | Short human-readable title (tracker mirror headline; fakes-only where it leaves the repo). |
| `inputs` | array of `IORef` | yes (may be empty) | Typed inputs the task needs before it can start. Each `IORef = { name: string, schema: string (schema name or uri), source: "graph" \| "external" }`. `source: "graph"` must be satisfied by an incoming data-flow edge carrying the same `name`; `source: "external"` is an explicit, auditable declaration (e.g. a file the operator provides). |
| `outputs` | array of `IORef` | yes (may be empty) | Typed outputs the task produces. Referenced by outgoing data-flow edges or consumed by a success criterion. |
| `tool_reqs` | array of `ToolReq` | yes (may be empty) | Tools this task requires. `ToolReq = { tool: string (tool name), min_version?: string, reason: string }`. This is the field whose absence today makes tool requirements unexpressible (graphcheck §4) — see §6.2 for the check. Empty means the task needs no tools beyond its owner's general permission. |
| `success_criteria` | array of string | yes | Machine-checkable (where possible) criteria the task must satisfy to count as done, e.g. `"unit tests pass"`, `"contract_conformance=pass"`, `"critical path unchanged"`. Free-form strings are allowed, but each criterion should name its evidence source (test suite, gate, script). Criteria are judged by the owner's gates, **not** by the graph itself — the graph only records that criteria exist (§7.4, tool-unless-judgement). |
| `state` | enum | yes | `queued` \| `assigned` \| `running` \| `blocked` \| `satisfied` \| `failed` \| `superseded`. Lifecycle: `queued → assigned → running → satisfied | failed`; `blocked` is entered when a strict dependency fails (§7.3); `superseded` is terminal for tasks replaced by a replanned graph. State transitions are recorded, not inferred. |
| `idempotency` | enum | yes | `idempotent` \| `at-least-once` \| `unsafe`. Mirrors `tool.contract.v1.json` (`idempotency` enum). `idempotent` = re-running yields the same result and is safe to retry; `at-least-once` = retries are permitted but the effect may be applied more than once (downstream must tolerate duplicates); `unsafe` = must not be retried automatically (see §7.3 — an `unsafe` task that fails blocks its strict dependents rather than being retried). |
| `files` | array of string | yes | File claims (paths) the task will write. Same semantics as today: parallel (same-wave) tasks must not overlap (`FileConflictError`); sequential waves may reuse (§6.1). |
| `complexity` / `priority` / `duration` | number | no | Scheduling weights, as today (`graph_planner.py:73-82`). Defaults: `complexity=1.0`, `priority=0`, `duration=None` (falls back to complexity). |
| `correlation_id` | string | yes | The swarm run this graph belongs to (matches `graph_edges.correlation_id`, `sc-graph-engine.json:9`). One graph = one `correlation_id`. |

**Validation invariants (Task):**

- T1. `id` is non-empty and unique within the graph (else `DuplicateNodeError`, §6.1).
- T2. `owner` resolves to a declared Agent node (else unknown-binding, §6.2). A task with no owner is refused, never defaulted — today ownership is implicit, and the design makes it explicit so a missing owner is a visible gap instead of a silent assumption.
- T3. Every `inputs[]` entry with `source: "graph"` has exactly one incoming data-flow edge of the same name from a task in the graph (else §6.2). `source: "external"` entries must carry a non-empty `reason` in a companion field (implementer's choice of field name; the schema allows either — recommend `source_ref: string` naming the external artefact).
- T4. Every outputs reference is well-formed: an output name may be carried by zero or more outgoing data-flow edges (zero = terminal output, legal but reported as a finding §6.3).

### 4.2 Agent node

The owner/executor. One Agent node per agent that owns at least one task in the graph (A01–A30, `agents.json`).

| Field | Type | Required | Semantics |
|---|---|---|---|
| `id` | string | yes | Agent id (`A05`, `A06`, …). Must match the `owner` of its tasks. |
| `capability` | string | yes | The capability the agent provides for this graph (e.g. `code.backend`). One agent may appear with one capability per graph. |
| `max_parallel` | integer ≥ 1 | no | Slot cap for the frontier (default 1; 0 is coerced to 1, matching `ready_frontier.rs:18`). Used by starvation detection (§7.3). |
| `tools_permitted` | array of string | no | Tool names (or lane prefixes) this agent is allowed to call. The per-agent allowlist drawn from the registry (`swarm/tool_registry.py`); the graph does not invent tools. If omitted, the agent's registered tools (12–13 today, inside the `agents.json` 10–15 policy) are the bound set. |
| `learning` | boolean | no | Reserved. **Learning is disabled until policy** (Wave 1 decision). A graph must not set this to `true`; validation refuses `learning: true` with a clear error until policy lands. Kept in the schema so the reservation is explicit, not accidental. |
| `correlation_id` | string | yes | Owning run, as in §4.1. |

An Agent node carries no `files`, no `duration`, and never appears in a wave. Its role is binding: tasks point *to* their owner, the owner points *to* its tools.

### 4.3 Tool node

One capability a task may require. Mirrors `swarm/schemas/tool.contract.v1.json` so the graph never invents a tool shape the registry does not enforce.

| Field | Required | Semantics |
|---|---|---|
| `name` | yes | Lane-prefixed unique tool name (`be_*`, `shared_*`, …), as in `tool.contract.v1.json:21`. |
| `owner` | yes | Agent id `A01`–`A30` or `shared` (`tool.contract.v1.json:22`). |
| `purpose` | yes | One-sentence purpose (`tool.contract.v1.json:23`). |
| `input_schema` / `output_schema` | yes | JSON-Schema-shaped objects (`tool.contract.v1.json:24-25`). The graph checks that a task's data-flow payload schema name is *compatible* with the tool's declared schema where the tool is the producer — for design purposes, compatibility = same schema name; full schema diffing is out of scope. |
| `errors` | yes | Subset of `{INVALID_INPUT, UNAUTHORIZED, DEPENDENCY_UNAVAILABLE, TIMEOUT, PARTIAL_SUCCESS}` (`tool.contract.v1.json:28`). The graph surfaces `DEPENDENCY_UNAVAILABLE`/`TIMEOUT` as the only tool errors that affect scheduling retry decisions. |
| `timeout_s` | yes, > 0 | Tool timeout (`tool.contract.v1.json:30`). Feeds the task's duration estimate but does not change its wave. |
| `idempotency` | yes | `idempotent` \| `at-least-once` \| `unsafe` (`tool.contract.v1.json:31`). A task whose `tool_reqs` include an `unsafe` tool inherits the `unsafe` floor for its own `idempotency` (a task cannot be safer than its tool). |
| `permitted_callers` | yes, ≥ 1 | Agent ids allowed to call this tool (`tool.contract.v1.json:32`). Checked at §6.2. |
| `entrypoint` / `handler` | yes | Callable reference (`tool.contract.v1.json:33-34`). |
| `side_effect_tier` | no | `READ` (default) \| `WRITE` \| `DESTRUCTIVE` \| `EXTERNAL` (`tool.contract.v1.json:36-38`). Read-only unless declared; `WRITE` writes only inside the swarm state dir. A `DESTRUCTIVE`/`EXTERNAL` tool node is refused by graph validation (the registry checker rejects them too — `tool.contract.v1.json:38`). |

A Tool node is *declared*, not created by the graph: the set of Tool nodes in a graph must be a subset of the registered tools (`swarm/tool_registry.py`). Validation §6.2 checks membership; the graph never invents a tool.

### 4.4 Projection onto today's planner

The scheduling projection of this model is exactly today's `TaskNode` (§1.1): `(id, files, dependencies, complexity, priority, duration)`, where `dependencies` = the set of incoming *strict dependency* edges (§5.1) only. Soft dependency edges do not affect waves (§5.5). `TaskNode` gains **no new field in this design** — the reason is deliberate: the tool-field gap (graphcheck §4) is closed at the *graph* level (§6.2), not by widening the scheduler's node type, so the Rust DAG and Python planner stay mechanically identical. When the follow-up implementation lands, `validate_graph` over the projection remains the authoritative wave/topology algorithm; the new validations (§6) run *before* it.

### 4.5 Schemas (design-level, JSON-Schema-shaped)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:swarm:graph:swarm.graph.node.task:1.0.0",
  "title": "swarm.graph.node.task",
  "type": "object",
  "required": ["id","owner","title","inputs","outputs","tool_reqs","success_criteria","state","idempotency","files","correlation_id"],
  "additionalProperties": true,
  "properties": {
    "id": {"type": "string", "minLength": 1},
    "owner": {"type": "string", "pattern": "^A(0[1-9]|[12][0-9]|30)$"},
    "title": {"type": "string", "minLength": 1},
    "inputs": {"type": "array", "items": {"$ref": "#/$defs/io_ref"}},
    "outputs": {"type": "array", "items": {"$ref": "#/$defs/io_ref"}},
    "tool_reqs": {"type": "array", "items": {"type": "object", "required": ["tool","reason"],
      "properties": {"tool": {"type": "string"}, "min_version": {"type": "string"}, "reason": {"type": "string"}}}},
    "success_criteria": {"type": "array", "items": {"type": "string"}},
    "state": {"enum": ["queued","assigned","running","blocked","satisfied","failed","superseded"]},
    "idempotency": {"enum": ["idempotent","at-least-once","unsafe"]},
    "files": {"type": "array", "items": {"type": "string"}},
    "complexity": {"type": "number", "default": 1.0},
    "priority": {"type": "number", "default": 0},
    "duration": {"type": ["number","null"], "default": null},
    "correlation_id": {"type": "string", "minLength": 1}
  },
  "$defs": {
    "io_ref": {"type": "object", "required": ["name","schema","source"],
      "properties": {"name": {"type": "string"}, "schema": {"type": "string"},
        "source": {"enum": ["graph","external"]}, "source_ref": {"type": "string"}}}
  }
}
```

Agent node: required `["id","capability","correlation_id"]`, `id` same `A..` pattern, `max_parallel` integer ≥ 1, `tools_permitted` array of string, `learning` boolean with const `false` (refused when `true` — §4.2).

Tool node: required are exactly the `tool.contract.v1.json:6-19` list; `name` lane-prefix pattern per `tool.contract.v1.json:21`; `idempotency` enum per `:31`; `side_effect_tier` enum per `:36-38` with `DESTRUCTIVE`/`EXTERNAL` refused by validation. The graph schema delegates — a Tool node that satisfies this schema and is registered satisfies both.

**Compatibility:** `task.result.v1.json` and `handover.v1.json` (`swarm/schemas/`) are untouched — a task's *result* still reports `task_id` + `state` there; this design's `state` is the graph-internal lifecycle (§4.1), not the result envelope.

---

## 5. Edge types

Every edge has `id` (unique), `from`, `to`, `type`, `strictness`, `correlation_id`, and an optional `payload` (verbatim JSON). Endpoint kinds are constrained per type (Table 5.1). Direction convention: **all edges point from the prerequisite/provider to the thing that needs it** — same direction as `graph_edges` (`predecessor_id → node_id`) and the Rust DAG (*dependency → dependent*, `dag.rs:3`).

### 5.1 Dependency edge — "must finish before"

```json
{ "id": "e1", "from": "T-A", "to": "T-B", "type": "dependency", "strictness": "hard", "correlation_id": "C-1" }
```

- Endpoints: Task → Task. Both must be Task nodes.
- Semantics: `to` may not start (or enter a wave) until `from` is `satisfied`. This is the *only* edge type that participates in wave/topology computation (`_waves`, `graph_planner.py:200-215`).
- Maps to today's `dependencies` list and to `graph_edges(node_id=to, predecessor_id=from)`.

### 5.2 Assignment edge — "is owned by"

```json
{ "id": "e2", "from": "A05", "to": "T-B", "type": "assignment", "strictness": "hard", "correlation_id": "C-1" }
```

- Endpoints: Agent → Task. Direction: owner → task (the agent assigns itself the work). Today's `graph_edges` does not carry this; the assignment is today implicit in who runs the task.
- Semantics: exactly one hard assignment edge per task; it must agree with the task's `owner` field (T2). A second hard assignment edge is a contradiction (two owners), refused (§6.1, A2). Records *who is accountable*, which the scheduler needs for slot accounting (`max_parallel`, `ready_frontier.rs`).

### 5.3 Tool-binding edge — "is permitted/required to use"

```json
{ "id": "e3", "from": "A05", "to": "shared_toolchain", "type": "tool-binding", "strictness": "soft", "correlation_id": "C-1" }
```

- Endpoints: Agent → Tool (agent is permitted to call the tool), and optionally Task → Tool with `strictness: "hard"` when a task *requires* the tool (`tool_reqs`). Two forms, one type:
  - **Agent → Tool (soft):** permission. The agent's `tools_permitted` set, materialised as edges so a reader sees the binding in the graph instead of inferring it from the registry.
  - **Task → Tool (hard):** requirement. Present iff the task's `tool_reqs` names that tool. This edge is what makes the graphcheck §4 gap *visible*: a task requiring a tool with no binding path is a validation error (§6.2), not a silent pass.
- Semantics: binding edges never affect waves. They affect *satisfiability* (§6.2) and *retry safety* (an `unsafe` tool-binding hardens the dependent task's idempotency floor — §4.3).

### 5.4 Data-flow edge — "provides typed input to"

```json
{ "id": "e4", "from": "T-A", "to": "T-B", "type": "data-flow", "strictness": "hard",
  "payload": {"name": "spec", "schema": "swarm.graph.node.task"}, "correlation_id": "C-1" }
```

- Endpoints: Task → Task.
- Semantics: `from`'s output named `payload.name` (with `payload.schema`) feeds `to`'s input of the same name. Satisfies task invariant T3. A data-flow edge **implies ordering** (producer must finish before consumer) — it is therefore always `strictness: "hard"` for scheduling purposes (see §5.5) *unless* the consumer's input is declared `source: "external"` and the edge is informational (see the soft form below). Data-flow edges are the only edges whose payload is semantically checked (name/schema match, §6.2).

### 5.5 Strictness

| Strictness | Meaning | Scheduling effect | Validation effect |
|---|---|---|---|
| `hard` | The dependency is mandatory. The target cannot run until the source is satisfied. | Participates in waves, critical path, deadlock detection. | Unsatisfied hard edge at dispatch time ⇒ target is `blocked` (§7.3); a hard edge into a `failed`/`unsafe` source is a deadlock finding (§6.3). |
| `soft` | Preferred ordering or optional binding. The target *may* run without the source. | Does **not** participate in waves, critical path, or makespan. Used for advisory ordering and permission edges (Agent → Tool). | Unsatisfied soft edge is reported, never blocks. At most one soft edge between the same ordered pair (§6.1). |

**Rules:**

- S1. Every edge declares strictness explicitly; there is no default. (This is a change from today: `graph_edges` has no strictness column at all — §1.2. A store migration adding the column is additive and backward-compatible; existing rows read as `hard`, which is today's behaviour, since today's `dependencies` is always mandatory.)
- S2. Data-flow edges are `hard` unless the consuming input is `source: "external"`. An `external`-sourced data-flow edge is informational: it records where the input came from without making the external artefact a graph node.
- S3. Cycle detection runs over **hard dependency + hard data-flow edges only**. Soft edges can never create a cycle refusal (they are advisory), but a soft edge that closes a cycle *at the hard level* is still a cycle — softness does not launder a real cycle.
- S4. Assignment and tool-binding (Task → Tool) edges are always `hard` (ownership and a stated requirement are not advisory).

Table 5.1 (endpoint kinds):

| Edge type | From | To | Default strictness |
|---|---|---|---|
| `dependency` | Task | Task | `hard` |
| `assignment` | Agent | Task | `hard` |
| `tool-binding` | Agent | Tool | `soft` |
| `tool-binding` | Task | Tool | `hard` |
| `data-flow` | Task | Task | `hard` (`soft` only for `external`-sourced inputs, S2) |

Table 5.2 (edge schema):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "urn:swarm:graph:swarm.graph.edge:1.0.0",
  "title": "swarm.graph.edge",
  "type": "object",
  "required": ["id","from","to","type","strictness","correlation_id"],
  "additionalProperties": true,
  "properties": {
    "id": {"type": "string", "minLength": 1},
    "from": {"type": "string", "minLength": 1},
    "to": {"type": "string", "minLength": 1},
    "type": {"enum": ["dependency","assignment","tool-binding","data-flow"]},
    "strictness": {"enum": ["hard","soft"]},
    "payload": {"type": ["object","null"]},
    "correlation_id": {"type": "string", "minLength": 1}
  }
}
```

**Store mapping (additive, per repo convention):** `graph_edges` today stores one row per dependency edge (§1.2). The model stores one row per edge of any type, with `edge_type` and `strictness` columns added (`CREATE TABLE IF NOT EXISTS` — no `ALTER` of existing columns, no `DROP`; a new table version records the widened row shape, existing rows read as `dependency`/`hard`). This doc does not write that migration; it pins the shape the migration must hold.

---

## 6. Validation

Validation order is fixed — each step refuses the whole graph before the next runs (fail closed; no partial dispatch):

**V1 structural → V2 bindings → V3 tool requirements → V4 schedule → V5 policy findings.**

### 6.1 Structural validation (§§4–5 invariants)

- **V1.1 Duplicate nodes/edges:** duplicate task id ⇒ `DuplicateNodeError` (`duplicate node id 'd'` — live probe, graphcheck §3); duplicate edge id ⇒ same class. Duplicate `(from, to, type)` triple ⇒ duplicate-edge error.
- **V1.2 Unknown endpoints:** an edge whose `from`/`to` is not a declared node ⇒ `UnknownDependencyError` (`node 'x' depends on unknown node 'ghost'` — live probe, graphcheck §3). The error names the *missing* id, not just the edge.
- **V1.3 Endpoint kinds:** an edge whose endpoints violate Table 5.1 (e.g. `dependency` from an Agent node) ⇒ `INVALID_INPUT` naming the edge id and the offending kind.
- **V1.4 Assignment uniqueness (A2):** more than one hard assignment edge into the same task ⇒ contradiction error. Zero assignment edges ⇒ T2 error (missing owner). The task's `owner` field must equal the assignment edge's `from`.
- **V1.5 Soft-edge uniqueness:** at most one soft edge per ordered pair `(from, to)` (S5 review point — if this proves too strict in implementation, the rule relaxes to "at most one soft edge per ordered pair *and type*"; the design's intent is that advisory ordering is unambiguous).

### 6.2 Binding and tool-requirement validation (the gap closure)

- **V2.1 Owner resolution:** every task's `owner` resolves to a declared Agent node (T2). Unresolved ⇒ unknown-binding error naming the task id and the missing agent id.
- **V2.2 Input coverage (T3):** every `inputs[]` entry with `source: "graph"` has exactly one matching incoming hard data-flow edge (same `name`, compatible `schema` name). Missing ⇒ unknown-binding error naming the task, the input name, and that no producer edge exists. Duplicate producer edges for the same input name ⇒ ambiguity error (two producers, one input — refused, not merged).
- **V2.3 Tool requirement satisfiability (the graphcheck §4 closure):** for every task, every `tool_reqs[]` entry must satisfy **all three**:
  1. the named tool exists as a declared Tool node (else: unknown-binding error `task 'T' requires unknown tool 'X'`),
  2. the task's owning Agent has a tool-binding edge to that Tool node (Agent → Tool, §5.3) *or* the tool appears in the agent's `tools_permitted` (else: tool-requirement error `task 'T' requires tool 'X' but owner 'A05' is not bound to it`),
  3. the Tool node's `permitted_callers` includes the owning Agent (else: tool-requirement error naming the tool and the caller). This is the tool-unless-judgement boundary: all three are mechanically checkable, so all three are checked by tool (§7.4).
  
  Today this check *cannot run* — `TaskNode` has no field to check against. After this design, a tool-gated node that cannot be satisfied is a **refusal with both ids named**, never a silent pass.
- **V2.4 Tool registration:** every Tool node's `name` is in the registered tool set (`swarm/tool_registry.py`). Unknown name ⇒ unknown-binding error. The graph never invents a tool (§4.3).
- **V2.5 Idempotency floor:** a task's declared `idempotency` must be at least as strict as the most restrictive `idempotency` among its required tools (`unsafe` dominates `at-least-once` dominates `idempotent`). A task declaring `idempotent` while requiring an `unsafe` tool ⇒ validation error (the task is claiming a retry safety its tool does not have).

### 6.3 Cycle, file-conflict and schedule validation

- **V4.1 Cycle detection (hard edges only, S3):** over hard `dependency` + hard `data-flow` edges. A cycle ⇒ `CycleError` **with the concrete cycle path** — today's behaviour must be preserved verbatim: `dependency cycle detected: a -> c -> b -> a` (`graph_planner.py:206-209`, `_find_cycle` `:218-240`; probe in graphcheck §3). The path is the evidence; a bare "cycle exists" is not an acceptable error. Cycle detection runs *before* wave computation so no partial plan is ever produced.
- **V4.2 File conflicts:** same-wave task pairs with overlapping `files` (equal or nested paths, `_paths_overlap`) ⇒ `FileConflictError` naming both node ids and both paths (`nodes 'p1' and 'p2' both claim overlapping file 'docs/a.md' (vs 'docs/a.md') in parallel wave 0` — live probe, graphcheck §3). Sequential waves may reuse files (today's rule, §1.1).
- **V4.3 Self-edges and trivial cycles:** a hard edge with `from == to` is a 1-cycle and is refused in V4.1 with path `x -> x`.

### 6.4 Findings (reported, not refused)

These do not block validation but are recorded in the validation report (the implementer must emit them — a finding silently dropped is a bug):

- **F1. Unreferenced output:** a task output no data-flow edge references and no success criterion mentions (T4) — probably fine (terminal output), but reported so a forgotten wire-up is visible.
- **F2. Soft edge never satisfied:** a soft edge whose source is `failed`/`superseded` — advisory only; reported.
- **F3. Agent with no tasks:** an Agent node owning zero tasks in this graph — likely a copy-paste error; reported.
- **F4. Risky idempotency:** an `unsafe` task on the critical path (§7.2) — its failure blocks the whole downstream chain; reported with the path so the owner sees the blast radius.
- **F5. Over-cap plan:** the plan's peak parallelism exceeds the global worker cap (30, ceiling 32 — `graph_quality.py:85-86`) — reported as starvation-risk (§7.3), not refused (refusal is the API's `429 E-CAPACITY` job, not the graph's).

---

## 7. Analysis: order, parallelism, critical path, deadlock, starvation

All analysis runs on the **validated** graph (V1–V4 pass) and all of it derives from the same deterministic core as today (Kahn waves, priority/id order, duration/complexity) so the analysis and the planner can never disagree.

### 7.1 Topological order and parallelism

- **Order:** `order = flatMap(waves)` — within-wave order by descending `priority`, then ascending `id` (unchanged from today, `graph_planner.py:13-14, 196-197`). The order is a function of the graph alone; input ordering never affects it. Both the Python planner (`validate_graph`) and the Rust `TaskDag` (`get_ready_tasks`, insertion order) produce valid topological orders; this design's canonical order is the Python planner's, and the Rust frontier's order is *a* valid linearisation, not *the* canonical one — the two are cross-checked, not equated (parity-gated, ADR-0001 — Python canonical oracle, Rust parity-gated).
- **Parallelism report:** for each wave, `width = |wave|` and `span = max duration in wave` (as today). The report states the graph's **parallelism profile**: maximum width, number of waves, and the width sequence. On the canonical fixture (§1.1): widths `1, 5, 3, 1`, makespan `4.0` — the design reproduces this exactly on the fixture projection (test C5, §9).
- **Parallel work bound:** total work `Σ duration` vs makespan `Σ wave spans` gives the average parallelism `Σ duration / makespan` and the idle fraction `1 − makespan/(Σ duration)` — reported so a plan that wastes most of its tasks' duration on synchronisation is visible before dispatch.

### 7.2 Critical path

- **Definition:** the longest path (by summed effective duration) through hard dependency + hard data-flow edges, from a source task (no incoming hard edges) to a sink task (no outgoing hard edges). Ties broken deterministically: lexicographic by node id at each step, matching the planner's id-order tie-break.
- **Why it matters:** the critical path *is* the makespan floor — no schedule of this graph can finish faster than the critical-path length, and any task on it has **zero float** (any delay delays the whole graph). Float for a non-critical task = `makespan − (longest path through the task)`.
- **Implementation note:** the Rust side already computes longest downstream chains (`critical_path_len`, `downstream_depths` — `dag.rs:177-202`). The design ports that notion to the Python planner as `critical_path(nodes) -> (path, length)` and cross-checks the two (parity, ADR-0001). The canonical fixture's critical path is asserted in test C6 (§9).
- **Interaction with idempotency (F4):** a critical path containing an `unsafe` or `at-least-once` task is a single point of failure for the graph's makespan; the analysis must report it (finding F4) so the owner can decide — with the user as approver — whether to re-plan, harden the task, or accept the risk. The graph never auto-retries an `unsafe` task to protect the makespan (§7.3).

### 7.3 Deadlock and starvation detection

**Deadlock (structural, pre-dispatch):**

- **D1. Cycle = deadlock.** A hard-edge cycle (V4.1) is a deadlock by definition — no member can ever start. Already refused in validation.
- **D2. Failure dead-end:** a task whose strict dependency is `failed` (state) and whose own `idempotency` is `unsafe` (must not be retried) can never become ready — the Rust failure mode recorded in graphcheck §5 (`mark_failed` + blocked dependents + `remove_task` refusal). The graph detects this at analysis time (not just at runtime) as: *hard path exists from a `failed`/`unsafe` task to a `queued` task*. It reports the path as a deadlock finding. **Recovery is an explicit replanning decision** (retry with a *new* task version, reassign the owner, or supersede the chain — §4.1 `superseded`), never a silent DAG mutation: the DAG refuses to remove a task that still has dependents (`dag.rs:68-70`) and the graph design honours that refusal by requiring a new graph revision rather than an in-place edit.
- **D3. Orphaned binding:** a task whose required Tool node is `DESTRUCTIVE`/`EXTERNAL` is refused in V2/V4 (§§4.3, 6.2) before it can deadlock the run — recorded here because it is the same *shape* of failure (a node that can never run) even though it is caught earlier.

**Starvation (dynamic, at dispatch):**

- **S-A. Slot starvation:** if `free_slots = 0` or the owning agent's `max_parallel` is saturated, the task waits (`ready_frontier.rs:49-50`). The frontier is greedy in workflow order with no aging (graphcheck §5): a later task can wait indefinitely behind earlier tasks. **Design rule:** the *analysis* reports the wait as a starvation finding with the task id, the saturated agent/slot, and how long it has waited; the *mitigation* (aging, priority promotion) is explicitly **not** this design — it belongs to SPE-38339 (frontier-starvation design), which this doc stays compatible with (the finding fields above are the contract that design consumes).
- **S-B. Capacity starvation:** a plan whose peak width exceeds worker cap 30 (F5) will hit `429 E-CAPACITY` at the API. The analysis reports it pre-dispatch with the peak wave; the API remains the enforcer.
- **S-C. Soft-edge starvation:** a task waiting on a soft edge's source that never runs. Soft edges never block (S5), so this is a finding (F2), not a deadlock — but if the *only* wire-up between two chains is soft, the analysis reports the chains as *decoupled* so the owner knows the ordering is advisory.

### 7.4 Tool-unless-judgement boundary

| Decision | Tool or judgement? | Why |
|---|---|---|
| Cycle present? Which path? | **Tool** (V4.1, DFS with path) | Mechanically decidable; the path is the evidence. |
| Owner/tool/input binding resolves? | **Tool** (V2.1–V2.4) | Mechanical set-membership against declared nodes + registry. |
| Task ready in this wave? | **Tool** (Kahn) | Deterministic from hard edges. |
| Tool unsatisfiable? | **Tool** (V2.3, three-part check) | The whole point of closing the §4 gap. |
| Deadlock shape present? | **Tool** (D1–D3) | Graph reachability; reported with the path. |
| Is this success criterion met? | **Tool where a gate exists** (named test/gate per criterion), else **judgement** | A criterion naming a test suite is checked by running it; a criterion like "reads well" is a user pause (approver = user). The graph records the criterion; it never invents the verdict. |
| Accept the F4 risk / supersede a dead chain? | **Judgement, user-approved** | Trade-offs (retry vs re-plan vs accept), not computability. Approver = user via pause. |
| Relax a validation rule? | **Never automatic** — user ruling or a contract change request to A03 | Guards against silent divergence. |

---

## 8. Sequential fallback

When parallel dispatch is unsafe or impossible, the graph degrades to a **strictly sequential schedule**: one task at a time, in canonical topological order (§7.1), each wave collapsed to singletons.

**Triggers (any one suffices):**

1. **File conflict the planner cannot resolve** — two same-wave tasks overlap files (V4.2). Today this refuses the whole plan; the fallback runs the wave's tasks one at a time in canonical order (sequential waves *may* safely reuse files — `graph_planner.py:22-23`). The conflict is reported, not hidden: the fallback output names the tasks and paths that would have conflicted.
2. **Deadlock finding D2** (failed/`unsafe` upstream) — the unaffected chains run sequentially; the dead chain is excluded and reported (it needs a replanned revision, §7.3 — the fallback never retries an `unsafe` task).
3. **Starvation finding S-A/S-B** — slots/capacity cannot honour the parallel width; sequential dispatch honours any non-zero slot budget.
4. **Operator request** — explicit, recorded in the plan (who asked, when). A silent fallback is a bug.

**Properties of the fallback:**

- **F-1. Same order, one at a time.** The sequential order is the canonical topological order (§7.1), so fallback and parallel schedules always agree on *sequence*; only concurrency differs. This is the baseline degraded mode already noted in graphcheck §7 (`a → b → c → d → e` chain is naturally sequential).
- **F-2. Makespan is recomputed as `Σ duration` (not `Σ wave spans`)** and reported alongside the parallel makespan so the cost of degradation is visible.
- **F-3. Tool bindings and idempotency are unchanged.** Fallback does not relax V2 — a task that cannot run in parallel still cannot run without its tools.
- **F-4. File claims are honoured trivially.** Single-task dispatch cannot conflict, so V4.2 is reported-but-not-refused in fallback mode.
- **F-5. No priority promotion, retry or rebalancing** is proposed (graphcheck §7 preserves this boundary): fallback is a *correct* degradation, not an *optimised* one. Optimisation belongs to a later task, gated by the same contracts.

---

## 9. Unit test checklist (for the implementer)

Each item names the test, the setup, the assertion, and the source it pins. **No code in this task** — this is the checklist the follow-up implementation must satisfy. Fixtures are fakes-only (Wave 1 decision); no live Linear/Notion, no persisted-topology dependency (today's `graph_edges` is empty — §1.2 — so tests must construct graphs in memory).

### 9.1 Cycles (C)

| ID | Test | Assertion |
|---|---|---|
| C1 | Self-cycle: `x` depends on `x` (hard dep edge) | `CycleError` with path `x -> x`. |
| C2 | 3-cycle: `a ← c`, `b ← a`, `c ← b` (hard dep edges) | `CycleError` with path `a -> c -> b -> a` (live probe string, graphcheck §3). Exact string asserted. |
| C3 | Cycle via data-flow: hard data-flow edges `a → b → a` | `CycleError` (data-flow participates in S3). |
| C4 | Soft-only cycle: soft edges forming a cycle, no hard edges | **Passes** validation (S3: soft edges never refuse). Asserted so a future change cannot silently tighten it without updating this test. |
| C5 | Canonical fixture projection: build the 10-node fixture (`graph_quality.py:620-633`) as Task nodes + hard dep edges | Waves `(start) → (extra1,n1,n2,n3,n4) → (m1,m2,m3) → (last)`, order `start, extra1, n1, n2, n3, n4, m1, m2, m3, last`, makespan `4.0` — the live baseline (graphcheck §2), reproduced exactly. |
| C6 | Chain fixture: `a → b → c → d → e` | 5 waves of 1, makespan `5.0` (graphcheck §2). |

### 9.2 Topological order (T)

| ID | Test | Assertion |
|---|---|---|
| T1 | Diamond: `a → b`, `a → c`, `b → d`, `c → d` (priorities equal) | Order starts `a`, ends `d`; `b` before `c` (ascending id tie-break). Deterministic across input permutations — assert with 3 shuffled inputs. |
| T2 | Priority: `a` and `b` both ready, `b.priority > a.priority` | `b` before `a` in the order (descending priority, `graph_planner.py:13-14`). Input-order independence asserted. |
| T3 | Unknown dependency: `x` depends on `ghost` | `UnknownDependencyError: node 'x' depends on unknown node 'ghost'` (live probe wording). |
| T4 | Duplicate id: two tasks `d` | `DuplicateNodeError: duplicate node id 'd'`. |
| T5 | Soft edge excluded: `a` soft→`b`, no other link | `b` in wave 0 (soft edge does not delay). Contrast with hard edge asserting `b` in wave 1. |
| T6 | Parallelism report: diamond with durations `{a:2, b:1, c:3, d:1}` | Makespan `2 + 3 + 1 = 6`; widths `1, 2, 1`; Σ duration `7`, average parallelism `7/6`; idle fraction `1 − 6/7`. Exact floats. |

### 9.3 Critical path (P)

| ID | Test | Assertion |
|---|---|---|
| P1 | Two branches: `a→c` (durations 5, 1), `b→c` (durations 1, 1) | Critical path `[a, c]`, length `6`; `b` float `4`. |
| P2 | Tie: `a→c` and `b→c`, equal durations | Deterministic winner by ascending id (`a`), asserted for both input orderings. |
| P3 | Data-flow path: critical path runs through a hard data-flow edge, not a dep edge | Path includes the data-flow tail; length sums both edge kinds. |
| P4 | Rust parity (fake/provenance-only): documented expected `critical_path_len` per node for the diamond | Cross-check table recorded (Python vs `dag.rs:177-202` notion). Recorded as a table, not a live Rust call, unless the implementation task wires parity. |
| P5 | F4 flag: critical path contains an `unsafe` task | Finding F4 emitted naming the task and the full path. |

### 9.4 Deadlock and starvation (D/S)

| ID | Test | Assertion |
|---|---|---|
| D1 | Failed/`unsafe` upstream: `a` (`failed`, `unsafe`) → `b` (hard dep, `queued`) | Deadlock finding D2 emitted with path `a -> b`; `b` reported `blocked`. No retry attempted (assert no state change on `a`). |
| D2 | `remove_task`-style mutation refused: attempt to delete `a` while `b` depends on it | Refused with a clear error (mirror of `dag.rs:68-70`); graph unchanged. |
| D3 | Orphaned chain: `a → b → c`, `b` unsatisfiable tool (V2.3 fails) | Validation refuses the graph naming `b` and the tool — the whole refusal happens *before* D2 analysis (order V2 < V4). |
| S1 | Slot starvation: `free_slots=0`, two ready tasks | Both reported starved (S-A) naming the slot; neither dispatched; no deadlock claim (starvation ≠ deadlock — distinct findings). |
| S2 | Agent saturation: agent `max_parallel=1`, two ready tasks owned by it | First admitted, second starved (S-A) naming agent + cap. |
| S3 | Over-cap: plan peak width `35` > cap `30` | Finding F5 emitted with wave + peak; validation still passes (API enforces `429`, graph reports). |

### 9.5 Bindings and tool requirements (B)

| ID | Test | Assertion |
|---|---|---|
| B1 | Unknown owner: task `owner: A99` (or undeclared agent) | Unknown-binding error naming task + missing agent (V2.1). |
| B2 | Double ownership: two hard assignment edges into one task | Contradiction error (V1.4/A2). |
| B3 | Missing producer: input `source: graph`, no data-flow edge | Unknown-binding error naming task + input name (V2.2). |
| B4 | Duplicate producer: two data-flow edges feeding one input name | Ambiguity error (V2.2). |
| B5 | **Tool gap closure (graphcheck §4):** task requires `shared_toolchain`, owner A05 has no binding to it | Tool-requirement error naming task, tool, and owner (V2.3-2). *This is the headline regression test for the gap.* |
| B6 | Tool not permitted: tool's `permitted_callers` excludes owner | Tool-requirement error (V2.3-3). |
| B7 | Unknown tool: `tool_reqs` names an unregistered tool | Unknown-binding error (V2.4) — graph never invents a tool. |
| B8 | Idempotency floor: task declares `idempotent`, requires an `unsafe` tool | Validation error (V2.5). Contrast: task declaring `unsafe` passes. |
| B9 | `learning: true` on an Agent node | Refused (learning disabled until policy — §4.2). |
| B10 | `DESTRUCTIVE`/`EXTERNAL` Tool node | Refused (registry rejects them too — `tool.contract.v1.json:38`). |

### 9.6 Sequential fallback (F)

| ID | Test | Assertion |
|---|---|---|
| F1 | File conflict: two same-wave tasks claim `docs/a.md` | Parallel plan refuses with `FileConflictError` (live probe wording, graphcheck §3). |
| F2 | Fallback on F1: same graph, fallback requested | Runs one at a time in canonical order; makespan `Σ duration` (F-2); conflict reported, not refused (F-4). |
| F3 | Fallback excludes dead chain (D1 graph) | Unaffected tasks run sequentially; dead chain excluded and reported; `unsafe` task not retried (F-3). |
| F4 | Fallback preserves bindings: fallback graph with an unsatisfiable tool | Still refused (F-3: fallback does not relax V2). |
| F5 | Fallback order = parallel order's canonical sequence | Same task sequence, concurrency 1 (F-1). Asserted on the diamond. |

### 9.7 Schema and store shape (H)

| ID | Test | Assertion |
|---|---|---|
| H1 | Edge with missing `strictness` | Schema refusal (S1: no default). |
| H2 | Edge endpoint-kind violation: `dependency` from an Agent node | `INVALID_INPUT` naming edge id (V1.3). |
| H3 | Task `state` outside the enum | Schema refusal (§4.1 states). |
| H4 | Existing-store back-compat: a `graph_edges` row written in today's shape (no `edge_type`/`strictness` columns) | Reads back as `dependency`/`hard` (S1 store mapping). |

---

## 10. Non-goals and open questions

**Non-goals:** implementation, migration DDL, contract version bumps (SC-GraphEngine stays 1.0.0 until the implementation task bumps it), frontier priority/aging policy (SPE-38339), tracker mirroring, learning.

**Open questions for the implementer / A01:**

1. **Q1. Soft-edge uniqueness scope (V1.5):** per ordered pair, or per ordered pair *and type*? Design recommends pair-only (unambiguous advisory ordering); relax if a real graph needs two soft edges between the same nodes.
2. **Q2. External-source reference field (T3):** the design recommends `source_ref: string` on `IORef`. Confirm field name before the implementation task pins the schema.
3. **Q3. Sequential-fallback trigger 1:** today a file conflict *refuses*; this design makes refusal-vs-fallback an explicit operator/plan choice (§8). If the project wants conflicts to always refuse (no fallback), delete trigger 1 and keep F1 only — a one-line design amendment, noted here so it is a decision, not drift.
4. **Q4. Cross-language canonical order:** Python planner order is canonical; Rust frontier order is a valid linearisation (§7.1). If parity testing (ADR-0001) demands byte-identical orders, the Rust side adopts the priority/id sort — flagged, not decided here.

---

## 11. Trace (every "today" claim cited)

| # | Claim | Source |
|---|---|---|
| 1 | `TaskNode` fields exactly `(id, files, dependencies, complexity, priority, duration)` | `swarm/graph_planner.py:73-82`; graphcheck §4 |
| 2 | Kahn waves, priority-desc/id-asc order, makespan = Σ wave max durations | `swarm/graph_planner.py:1-27, 196-197, 200-215, 287-319` |
| 3 | `CycleError` with path `a -> c -> b -> a`; `UnknownDependencyError`/`DuplicateNodeError`/`FileConflictError` wordings | `swarm/graph_planner.py:206-209, 218-240, 248-259`; graphcheck §3 |
| 4 | 10-node/12-link fixture, waves + makespan 4.0; 5-node chain makespan 5.0 | `swarm/graph_quality.py:620-644`; graphcheck §2 |
| 5 | `graph_edges (correlation_id, node_id, predecessor_id, priority, ts)`, direction predecessor → node | `migrations/V0002__graph_engine.sql:25-30`; `contracts/sc-graph-engine.json:9-11` |
| 6 | 0 `graph_edges` rows in live `.swarm/tasks.db` | graphcheck §§1, 6 |
| 7 | Rust DAG: dependency → dependent; cycles independent check; `critical_path_len`; `mark_failed` blocks dependents; `remove_task` refuses tasks with dependents | `crates/gsd-scheduler/src/dag.rs:3-5, 22-23, 68-70, 115-120, 140-141, 177-202` |
| 8 | Frontier: greedy in workflow order, `free_slots` cap, `max_parallel=0 → 1`, no aging | `crates/gsd-scheduler/src/ready_frontier.rs:18, 49-50` |
| 9 | Worker cap 30 / ceiling 32, `429 E-CAPACITY` | `swarm/graph_quality.py:85-86, 14-15` |
| 10 | Tool policy `{min:10, max:15, enforce:true}`, live per-agent counts 12–13 | `agents.json` `capability_policy.tools`; `swarm/tool_registry.py:444-473`; graphcheck §4 |
| 11 | Tool contract required fields, lane-prefix names, idempotency enum, side-effect tiers | `swarm/schemas/tool.contract.v1.json:6-19, 21, 31, 36-38` |
| 12 | Sequential fallback baseline (chain is naturally sequential; no promotion/retry/rebalancing) | `swarm/graph_planner.py:22-23`; graphcheck §7 |
| 13 | 12 outstanding phases, 5 blockers B1–B5; fakes-only, learning disabled, approver=user, tool-unless-judgement | `docs/reconcile-SPE-38338.md` §§2–4 (decision context) |
| 14 | Additive-only store convention (`IF NOT EXISTS`, no `ALTER`/`DROP`) | `migrations/V0002__graph_engine.sql:15-17` |

*Design doc only. Gates/formatters skipped per assignment. No code changed.*
