# Deterministic Orchestration Core — GSD Outstanding Phases (SPE-38340)

**Issue:** SPE-38340 (Wave2 design trio; siblings SPE-38341 graph model, SPE-38339
delta/blockers) · **Branch:** `swarm/T74c2-be` (repo `agent-swarm`)
**Correlation:** `e303690f-87c2-4faf-baaa-c02dc38a5c94` (from `.swarm/live.json`)
**Status:** design — no code, no live Linear/Notion writes, gates/formatters skipped per assignment.
**Baseline inputs (Wave1, read):** `docs/reconcile-SPE-38338.md` (12 outstanding
phases, 5 blockers B1–B5), `docs/graphcheck-SPE-38338.md` (canonical 10-node
order `start, extra1, n1, n2, n3, n4, m1, m2, m3, last`, 0 cycles, `TaskNode`
has no tool field, frontier starvation), `.planning/ROADMAP.md`,
`.planning/STATE.md` (frontmatter `status: gaps_found`, `current_phase: 14.4`,
2026-11-18 audit `done=false`).
**Standing decisions (assignment + `DECISIONS.md`):** fakes only (no live
Linear/Notion writes; TBD IDs); learning disabled until policy; approver =
user via pause; tool-unless-judgement (prefer tool evidence over judgement);
Python stdlib-only, Python is the parity oracle / default route (D4),
no auto-retry until per-tool idempotency inventory exists (D5),
`gsd-autonomous` runner stays out of scope — this design covers the
deterministic discuss/plan/execute lifecycle core only, not the autonomous
runner (D6).

> This doc is the `architecture.blueprint` + `api.contract` + `adr.set`
> single-writer artifact for A03. Consumers: A05 (backend), A06 (frontend),
> A07 (data), A08 (QA), A10 (security co-sign for trust-boundary notes).

---

## Table of Contents

1. Goals, non-goals, constraints
2. Outstanding-phase baseline (the 12 + 5 blockers)
3. C4 blueprint, layering, fitness
4. Lifecycle: discuss / plan / execute + phase states
5. Source-of-truth table (STATE vs Linear vs Notion)
6. Atomic `.planning/STATE.md` writes (temp + rename)
7. Issue mapping: 1-to-many + stable idempotency keys (fakes only)
8. Reconciliation: drift / orphans / conflicts
9. Offline queue + transient-only retry + dead-letter
10. Data schemas (machine-readable)
11. NFR allocation + budgets
12. Fitness functions (CI)
13. ADRs (proposed)
14. Tech-stack selection
15. Threat model (STRIDE sketch)
16. Fake-based test plan — checklist (acceptance)
17. Cutover (no live writes)
18. Open questions / user decisions
19. Evidence trace

---

## 1. Goals, non-goals, constraints

**Goals**

- One deterministic state machine drives the 12 outstanding GSD phases
  through discuss → plan → execute with legal-transition enforcement,
  so a phase can never be silently relabelled to bypass `assess`
  (the disputed 2026-11-16 COMPLETE relabels on 14.4/14.7/15 are the
  anti-pattern this design forbids).
- One atomic file-write rule for `.planning/STATE.md` (temp + `os.replace`),
  single-writer leased, so a crash can never publish a torn STATE.
- One 1-to-many phase→issue mapping with stable idempotency keys, backed
  only by fakes (TBD IDs) until a human provides real tracker IDs.
- One reconciliation rule set for drift, orphans and conflicts across
  STATE / Linear (workflow) / Notion (context).
- One offline-capable sync path: durable queue → transient-only retry →
  dead-letter with redrive; non-transient failures never retry.

**Non-goals (explicitly out)**

- No product code, no schema DDL, no UX tokens, no environment/infra
  changes, no live Linear/Notion writes, no auto-retry on uninventoried
  tools, no new crates/DSL (D2 workflowz = DAG dispatch policy only),
  no PyO3 expansion (D4), no `gsd-autonomous` runner work (D6).
- No learned policy: learning stays disabled until a policy artefact
  is ratified; HTIL notes are user-recorded choices, not policy.

**Hard constraints**

| # | Constraint | Source |
|---|---|---|
| C1 | Fakes only. Tracker IDs are `TBD-…`. No live POST/PUT to Linear or Notion in design, tests, or cutover until user supplies real IDs + approves. | Assignment; STATE Run Log (free-limit fail-open) |
| C2 | Approver = user via pause. Every B1–B5-style contradiction is a pause point; the core must be able to park in `human_needed` / `blocked_hold` and wait. | Assignment; STATE Blockers |
| C3 | Tool-unless-judgement. Where a tool can produce evidence (parser, planner, reconciler), the design requires the tool output over human judgement. | Assignment |
| C4 | Python stays default; Rust opt-in per route only on parity evidence. | DECISIONS.md D4 |
| C5 | No auto-retry on any tool until its row exists in the per-tool idempotency/side-effects inventory. | DECISIONS.md D5 |
| C6 | `gaps_found` is never relabelled to bypass assess; a phase close requires verifier evidence. | ROADMAP.md:69 |
| C7 | Concurrency cap ≤ 30 workers/session; `plan_graph` cycle/dangling refusal is authoritative at plan time. | `swarm/graph_planner.py`; graphcheck §2–§3 |

---

## 2. Outstanding-phase baseline (the 12 + 5 blockers)

The 12 outstanding phases are exactly the reconcile doc §2 order (quoted from
`.planning/ROADMAP.md` `Depends on` edges; STATE audit 2026-11-18 counts
6 passed / 1 human_needed / 10 gaps_found / 14.4-not-counted). The core must
be able to represent each of these without guessing a state:

| # | Phase | ROADMAP state (verbatim shortened) | Core consequence |
|---|---|---|---|
| 1 | 6 — Compile-first pin and workspace build | `human_needed` | Must support `human_needed` as a first-class resting state with required `awaiting` evidence. |
| 2 | 8 — Wasmer full-core spike report | `gaps_found` | `gaps_found` must carry structured gap list (3 NOT MET), not a bare flag. |
| 3 | 9 — Host-native scheduler join and strangler seam | `gaps_found` | Partial row progress must not auto-close the phase (verifier re-run required). |
| 4 | 11 — PyO3 extension and wheel CI | `gaps_found` | CI evidence is a distinct gate from local run; lifecycle needs `awaiting_ci_evidence`. |
| 5 | 12 — Port units and strangler flips | frontmatter `gaps_found` vs body `pass_with_recorded_debt` | Internal contradiction must be representable as `conflict` and paused, never auto-resolved. |
| 6 | 13 — Browser deliverable | `gaps_found` | Standard gaps_found path. |
| 7 | 14 — Acceptance proof | `gaps_found` (D1 vs status) | Decision-vs-status conflict must be pausable (`conflict` → user ruling). |
| 8 | 14.1 — Hot-path fixes / redaction | `gaps_found` (criteria met, debt open) | Needs `passed_with_recorded_debt` distinct from `passed`. |
| 9 | 14.4 — Rust scheduler runtime + flip | `gaps_found/INCOMPLETE (held)` — BLOCKER | Held gate (`blocked_hold`) with gate evidence list; no flip without S7 approval. |
| 10 | 14.6 — Chat-thread runtime | `gaps_found` (GE-1 open by user choice) | User-choice gate must be represented as an explicit open gate, not hidden. |
| 11 | 14.7 — OMP plugin + MCP | `gaps_found`, no VERIFICATION, AC NOT PASSED | De-facto dependency on held 14.4 must be a schedulable edge; cannot be scheduled as runnable while upstream is held. |
| 12 | 15 — Audit, cleanup and ship | `INCOMPLETE`, blocked before audit | Ship requires explicit user confirmation token; unattended run forbidden. |

**Blockers B1–B5** (reconcile §3) are all *contradiction* class:
milestone status (B1), 14.4 three-way state (B2), disputed COMPLETE
relabels (B3, the C6 rule), 14.7 dependency mismatch (B4), Phase 15
start-next-milestone conflict (B5). Design rule: **the core never edits
STATE or ROADMAP by hand to resolve a contradiction.** It detects the
contradiction (`drift`/`conflict` — §8), parks the phase in `blocked_hold`
or `human_needed`, and emits a reconciliation report for the user to rule on.

**Graph baseline (graphcheck doc):** no persisted topology today
(`graph_edges` 0 rows in `.swarm/tasks.db`, 242 `tasks` rows); only
in-memory fixtures + Rust `TaskDag` at runtime. The core therefore must
not assume a stored DAG: it validates dependencies at plan time via
`plan_graph` (cycle path, dangling, duplicate, file-conflict refusals)
and records the validated order as data, not as a live scheduler claim
(§4.3). Two recorded Rust risks are carried as design assumptions:
failed-dependency dead-end (`dag.rs:68-70,140-141`) and greedy-frontier
starvation (`ready_frontier.rs:49-50`, no aging). The fake test plan
(§16, T-STARVE, T-DEAD) must cover both.

---

## 3. C4 blueprint, layering, fitness

### 3.1 Containers

| Container | Responsibility | Decides |
|---|---|---|
| `orchestrator` (api) | A01-facing facade: `transitionPhase`, `getPhaseState`, `mapIssue`, `enqueueSync`, reconcile trigger. Owns single-writer lease on STATE. | — |
| `phase_lifecycle` (service) | Discuss/plan/execute state machine + transition guards + pause gates. | — |
| `state_store` (data) | Parse/serialize `.planning/STATE.md` frontmatter + Current Position; atomic publish. | — |
| `issue_mapper` (data) | 1-to-many phase→issue records (fake TBD) + idempotency-key derivation/registry. | — |
| `reconciler` (service) | Drift/orphan/conflict detection across STATE ↔ Linear-fake ↔ Notion-context. | — |
| `sync_queue` (service) | Durable offline queue, transient-only retry, dead-letter + redrive. | — |
| `tracker_adapter` (data) | Fake Linear (workflow) + read-only Notion (context) adapters; **no live transport in fake mode** (hard deny). | — |
| `phase_state` (data) | Persisted projection: phase states, mapping table, queue, DLQ (SQLite `.swarm/tasks.db` tables defined in §10). | — |

Fitness evidence (registry tools, §19): `arch_c4_fanout` SUCCESS,
`container_count=8`, `relation_count=11`, `max_fan_out=3`,
`over_budget=[]`, `isolated=[]` against `fan_out_budget=4`;
`arch_cycle_check` SUCCESS, `has_cycle=false`, `node_count=6`,
`cyclic_node_count=0`, `cyclic_components=[]`;
`arch_layer_violations` SUCCESS, `violation_count=0`, `checked_edges=16`,
`violations=[]`, `unassigned=[]`.

### 3.2 Relations (C4 → implementation)

```
orchestrator -> phase_lifecycle, state_store, reconciler
phase_lifecycle -> state_store, issue_mapper
reconciler -> state_store, issue_mapper, tracker_adapter
sync_queue -> tracker_adapter, phase_lifecycle
tracker_adapter -> phase_state
```

`sync_queue -> phase_lifecycle` is a *callback on terminal sync outcome*
(dead-letter / reconciled), never a synchronous RPC back into the
lifecycle; the reverse edge is realised as an event, which is why the
static module graph `arch_cycle_check` stays acyclic (no back edge in
the checked edge set).

### 3.3 Layering

`layer_order = [api, service, data, kernel]` (outermost → innermost;
a module may depend on its own layer or any later one).

| Module | Layer |
|---|---|
| orchestrator | api |
| phase_lifecycle, reconciler, sync_queue | service |
| state_store, issue_mapper, tracker_adapter | data |
| atomic_file, idempotency, clock | kernel |

Checked edges (16) in §19: every service→(data|kernel), data→kernel,
api→service|data. Zero violations. Kernel modules already exist in the
repo: `swarm/atomic_file.py` (`write_text_atomic`), `swarm/memory.py`
(`idempotency_key`), `swarm/state_store.py` (GSD STATE.md mirror target).
The design reuses them; it does not invent new kernel primitives.

### 3.4 Contract (versioned, additive only)

Contract id `API-OrchestrationCore`, `openapi 3.1`, version `0.1.0`
(bump `minor` from `0.0.0`, `required_bump=minor`, `compliant=true` per
`arch_semver_bump`). `arch_contract_ops` SUCCESS, `operations=5`:

| # | operationId | Method + path | Purpose |
|---|---|---|---|
| 1 | `getPhaseState` | `GET /phases/{phase_id}/state` | Read one phase's state + gates. |
| 2 | `transitionPhase` | `POST /phase-transitions` | Request a lifecycle transition (guarded). |
| 3 | `mapIssue` | `POST /issues/map` | Create-or-update the 1-to-many mapping (idempotent). |
| 4 | `enqueueSync` | `POST /sync/enqueue` | Durably enqueue a tracker sync intent (offline-safe). |
| 5 | `listDeadLetter` | `GET /sync/dead-letter` | List DLQ entries (redrive input). |

Breaking change: **false** (initial version; no base contract to diff).
Full schemas in §10. This contract is fake-transport only: the
`tracker_adapter` refuses any non-fake base URL (§7.2).

---

## 4. Lifecycle: discuss / plan / execute + phase states

### 4.1 Two-axis model (the core distinction)

The reconciliation failures happen because three different notions are
conflated. The core separates them:

- **Workflow stage** (GSD discuss/plan/execute): *where* the phase is
  being worked — `undiscussed → discussing → discussed → planning →
  planned → executing → verification → done-hold`. Exactly one stage is
  active at a time.
- **Phase verdict** (assess outcome): *what the verifier said* —
  `unverified | passed | passed_with_recorded_debt | gaps_found |
  human_needed | failed | not_run`. A phase is `done` only when
  stage = `done-hold` **and** verdict ∈ {`passed`,
  `passed_with_recorded_debt`} with a verifier evidence digest; anything
  else is outstanding. This single rule makes B3 (relabel-to-bypass)
  unrepresentable.
- **Gate/hold overlay**: independent of the two axes — `held`
  (R2-style review gate, S7 flip gate, CI-evidence gate), `blocked_hold`
  (contradiction parked for user ruling), `paused_user_choice` (e.g.
  GE-1 open by user choice), `conflict_hold` (internal contradiction).
  Holds take precedence in scheduling: a held phase is never runnable,
  regardless of stage/verdict.

Example (Phase 14.4): stage `executing`, verdict `gaps_found`,
overlay `held(R2 independent-review)` → not runnable, not done.
Example (Phase 14.3): stage `done-hold`, verdict
`passed_with_recorded_debt`, overlay none → done (debt recorded, not
re-opened — reconcile §2, STATE:900).

### 4.2 Phase states (verdict enum — normative)

```
unverified | passed | passed_with_recorded_debt | gaps_found
             | human_needed | failed | not_run
```

`passed_with_recorded_debt` requires a non-empty `debt[]` list (each row
`{item, owner, accepted_by, accepted_at}`); without it the transition is
refused. `human_needed` requires `awaiting: {kind, detail}` where
`kind ∈ {verifier_rerun, ci_evidence, user_ruling, user_confirmation,
external_approval}` — Phase 6 is `human_needed{verifier_rerun}`, Phase 15
ship is `human_needed{user_confirmation}`.

### 4.3 Transition table (normative, exhaustive)

Any transition not in this table is illegal and must be refused with
`E-CONTRACT` (and logged to the reconciliation report as `conflict`).
The table is the single source the fake tests enumerate.

| From → To | Guard (all must hold) |
|---|---|
| `undiscussed → discussing` | — (entry) |
| `discussing → discussed` | CONTEXT artefact exists |
| `discussing → human_needed` | — |
| `discussed → planning` | discussion resumed after contradiction resolved |
| `planning → planned` | PLAN artefact + `plan_graph` validation passes (0 cycles, 0 dangling) + frontier check passes (T-STARVE) |
| `planned → executing` | no active `held/blocked_hold/conflict_hold`; upstream verdicts all done-eligible (§4.4) |
| `executing → verification` | EXEC artefact set present |
| `verification → passed` | VERIFICATION artefact + verifier digest + 0 unmet criteria |
| `verification → passed_with_recorded_debt` | as `passed` + non-empty debt[] + `accepted_by=user` |
| `verification → gaps_found` | gap list non-empty (each gap: criterion, evidence) |
| `* → gaps_found` | verifier or tool evidence of unmet criterion (never by relabel) |
| `* → human_needed` | pause gate fired; `awaiting` recorded |
| `* → blocked_hold` | contradiction detected (drift/conflict) — user ruling required |
| `gaps_found → verification` | re-verification requested after gap closure |
| `human_needed → verification` | `awaiting` satisfied + evidence digest attached |
| `blocked_hold → verification` | user ruling recorded (`ruling_ref`) + contradiction cleared |
| `failed → executing` | retry intent + failed node has a recovery plan (Rust dead-end guard, graphcheck §5) |

Forbidden transitions (each maps to a B-blocker): `verification →
passed` without VERIFICATION digest (B3), `executing → done-hold`
while any gate is held (B2), `blocked_hold → executing` without ruling
(B1/B5), `human_needed → passed` without satisfying `awaiting` (B5).

### 4.4 Scheduling rule (dependencies, frontier, starvation)

- Runnable set = phases with stage `planned|executing`, verdict ∉
  done-eligible, overlay empty, and **all upstream verdicts done-eligible**
  per the ROADMAP `Depends on` DAG. Upstream `held/blocked_hold` blocks
  downstream (14.7-Wave-2 blocked on 14.4 is the canonical case; B4
  requires the de-facto edge 14.4→14.7 to be recorded, not the official
  14→14.7 edge — the reconciler flags the mismatch as `drift`).
- **Starvation defence (graphcheck §5):** frontier admission is *not*
  greedy workflow order. It is (priority desc, phase id asc) *with
  aging*: a runnable phase waiting more than `starve_threshold`
  (default 3 waves) is promoted ahead of newly-runnable higher-priority
  phases. `free_slots=0` (capacity) defers, never drops, the phase.
  This is a design requirement on any future scheduler binding; the
  Python planner's priority sort alone does not satisfy it.
- **Dead-end defence (graphcheck §5):** a `failed` phase's dependents
  are never permanently blocked: the lifecycle requires either
  `failed → executing` (recovery plan) or an explicit
  `superseded_by` ruling before any dependent is scheduled. The
  Rust `mark_failed`/`remove_task` dead-end must not be mirrored in
  the lifecycle.

---

## 5. Source-of-truth table

| Data | Source of truth | Why | Mirrors (derived, never authoritative) |
|---|---|---|---|
| **Phase verdict + gates/holds** | `.planning/STATE.md` frontmatter + Current Position (parsed) | Only artefact that carries both the milestone assessment (`done=false`) and per-phase verifier state; ROADMAP Status lines are hand-written prose that has already diverged 3 times (B1/B2). | ROADMAP Status (prose, read-only mirror); Linear workflow status (derived) |
| **ROADMAP `Depends on` DAG** | `.planning/ROADMAP.md` (parsed) | Defines scheduling edges; STATE does not carry the DAG. Mismatches with de-facto edges are `drift` (B4). | Scheduler order (derived); `graph_edges` (future, today 0 rows) |
| **Workflow status** (open/in-progress/done on tracker) | **Linear — fake** (TBD IDs) | The tracker is the workflow tool for humans; STATE must not be edited by hand to chase tracker state. | STATE mapping record (derived, reconciliation input) |
| **Discussion/context notes** | **Notion — read-only context** (existing task `https://app.notion.com/p/40abc1a0c7ae81b588b7e01197a8d77c` cited in STATE:134) | Context is evidence/reference, never a state store. No Notion write is part of this design (C1). | Reconciliation report excerpts (citations only) |

**Precedence rule (single rule, no judgement case-by-case):**
`STATE verdict > Linear workflow status` for phase completion;
`Notion` never overrides either. A Linear `done` for a STATE
`gaps_found` phase is a `conflict` (paused for user ruling), never an
auto-close. A Linear open for a STATE `passed` phase is `drift`
(auto-correctable in fake mode, §8.2).

---

## 6. Atomic `.planning/STATE.md` writes (temp + rename)

### 6.1 Rule (normative)

1. **Single writer.** Only `state_store` writes `.planning/STATE.md`,
   under a lease (`swarm` lease discipline: `BEGIN IMMEDIATE` on
   `tasks.db`; lease id recorded in the write intent). Two writers
   ⇒ the loser retries or parks in `blocked_hold`; it never interleaves.
2. **Atomic publish.** Serialize the full file (frontmatter + body)
   to a unique hidden sibling `.{name}.{pid}.{rand8}.tmp` in the same
   directory, then `os.replace(tmp, target)` — exactly the existing
   `swarm/atomic_file.py::write_text_atomic` contract (unique per-process
   suffix so two threads of one process cannot clobber the same temp;
   reader sees old or new complete file, never torn). No fsync is bought:
   STATE is a projection that is rebuilt; durability across power loss
   is out of scope (atomic_file docstring).
3. **Temp hygiene.** On any exception the temp is unlinked
   (`missing_ok=True`); orphan `.*.tmp` files are reaped by the
   reconciler (reported as `drift`, never silently deleted in fake mode).
4. **Read-modify-write discipline.** Read → parse frontmatter →
   apply the single guarded transition (§4.3) → recompute derived
   numbers (§6.3) → serialize → atomic publish. The file is never
   appended in place; the 2026-11-18 audit tail is rebuilt from the
   verifier records, not hand-appended.

### 6.2 Frontmatter schema (`.planning/STATE.md` YAML frontmatter)

```yaml
---
gsd_state_version: "1.0"
milestone: v2.4
milestone_name: Rust Core and Python Bindings
status: gaps_found            # unverified|passed|passed_with_recorded_debt|gaps_found|human_needed|failed|not_run
stopped_at: "<short string, must not contradict status>"
last_updated: "2026-11-17T03:40:00.000Z"   # RFC3339; set by writer, never hand-edited
last_activity: 2026-11-17
last_activity_desc: "<one line>"
progress:
  total_phases: 18            # recomputed: count of phase records in ROADMAP DAG
  completed_phases: 6         # recomputed: verdict in {passed, passed_with_recorded_debt}
  total_plans: 53             # recomputed from PLAN artefacts
  completed_plans: 29         # recomputed
  percent: 33                 # recomputed: round(100*completed_phases/total_phases)
current_phase: 14.4           # recomputed: highest-order runnable open phase (scheduling rule §4.4)
current_phase_name: "<roadmap title>"
---
```

Invariants enforced by the parser (fake tests T-STATE-*):

- `percent` must equal the recomputed value; a hand-edited mismatch is
  `drift` (the current file violates this: frontmatter 33% vs Current
  Position bar 39% — reconcile §4, flagged, not hand-fixed).
- `current_phase` must be explainable by §4.4 (runnable set + order);
  a stale value is `drift`.
- `status` must be the reconciled milestone verdict: derived from
  per-phase verdicts (any `gaps_found/human_needed/held` ⇒ not
  `passed`); `done=true` requires every phase done-eligible (B1 blocks
  this today).

### 6.3 Body discipline

`Current focus`, `Current Position`, `Operator Next Steps` are
**generated projections** of (frontmatter + phase records), not
hand-written prose. Contradiction between `Current focus`
("remains INCOMPLETE") and `Current Position` ("complete … 2026-11-16")
(B1) becomes impossible by construction: both are rendered from the
same record. Until the generator exists, the reconciler treats any
such contradiction as `conflict` and parks the milestone (never
auto-edits).

---

## 7. Issue mapping: 1-to-many + stable idempotency keys (fakes only)

### 7.1 Mapping model

One GSD phase maps to **one primary Linear issue + zero-or-more
child issues** (1-to-many). A phase may also reference exactly one
Notion context page (read-only). Real tracker IDs are **TBD** in this
design: every record carries a fake id and a `tbd_real_id` slot so the
fake→real cutover is a single field fill, not a migration.

```json
{
  "phase_id": "14.4",
  "linear": {
    "primary_fake_id": "TBD-LIN-0001",
    "child_fake_ids": ["TBD-LIN-0002"],
    "tbd_real_id": null,
    "status_fake": "in_progress",
    "idempotency_keys": {
      "create_primary": "9f2c…(64 hex)",
      "create_child_k": "…"
    }
  },
  "notion": {
    "page_ref": "https://app.notion.com/p/40abc1a0c7ae81b588b7e01197a8d77c",
    "mode": "read_only",
    "tbd_real_id": null
  },
  "updated_at": "2026-11-27T00:00:00Z"
}
```

Storage: `phase_issue_map` table (§10) — the only place fake IDs live.
Fake IDs are deterministic: `TBD-<TRACKER>-<phase_id>-<k>` padded, so
fixtures and assertions reproduce byte-for-byte.

### 7.2 Idempotency keys (stable, retry-safe)

Key derivation reuses the repo's established pattern
(`swarm/memory.py::idempotency_key`: SHA-256 hex of the compact JSON
array, no separator ambiguity, `expected_version` deliberately excluded
so a version re-submit is the same write). For tracker writes:

```
idempotency_key = sha256_hex( compact_json([
  correlation_id,          // swarm correlation id (one request = one id)
  phase_id,                // e.g. "14.4"
  op,                      // "create_primary" | "create_child:0" | "update_primary" | ...
  tracker,                 // "linear" | "notion"
  subject,                 // normalised tracker subject, e.g. fake id or title slug
  body_hash                // sha256_hex of the canonical request body
]) )
```

Properties (each is a fake test assertion, §16):

- **Stable across retries of one logical write** (same inputs ⇒ same
  key, even after a lost reply). **Distinct across different logical
  writes** (different op/subject/body ⇒ different key). No key ever
  embeds `attempt`, timestamp, or any wall-clock value — retry count
  is metadata, not identity (memory.py documents the same rule).
- **Create is `create-or-return`:** posting a create with an existing
  key returns the existing fake record without creating a duplicate
  (T-CREATE). **Update is last-writer-wins on `updated_at` within the
  same key domain,** with the loser logged as `conflict` in the
  reconciliation report — never an error, never a silent overwrite
  (T-CONFLICT).
- **Fake transport hard-deny:** the adapter accepts only a
  `fake://` base URL. Any attempt to configure a real Linear/Notion
  URL, or any network call to a real host, is refused with `E-DEP`
  *before* any byte is sent (T-NOLIVE). This is the C1 enforcement
  point; there is no feature flag that can override it in fake mode.

### 7.3 Operations

- `mapIssue(create)`: idempotent create of primary (+ children).
  First call creates; replay with same key returns the same record
  with `deduped: true` and **no new row** (T-CREATE).
- `mapIssue(update)`: idempotent update of status/body under the
  update key. Replay returns the same record (T-UPDATE).
- `mapIssue(read)`: returns the mapping + last sync outcome
  (`synced | queued | dead_lettered`) from `phase_state`.

---

## 8. Reconciliation: drift / orphans / conflicts

The reconciler runs on demand (`POST /phase-transitions` with
`trigger=reconcile`) and on a schedule (bounded: 1 run per 60 s in fake
mode; wall-clock only, no SLO — D3). It compares three snapshots:
**A** = parsed STATE + phase records (source of truth for verdicts),
**B** = Linear-fake workflow snapshot, **C** = Notion context refs.
Output is a `ReconciliationReport` (schema §10); it never mutates
STATE directly — mutations go through `state_store` under §6.

### 8.1 Classification (normative, exhaustive)

| Class | Definition | Example from the 12 | Default action |
|---|---|---|---|
| `drift` | Same fact, stale copy. One side is a derived mirror that lags its source. | STATE 33% vs 39% bar; Phase 9 rows 26/37 stale NOT MET/PARTIAL; B4 official vs de-facto 14.7 edge. | Auto-correct the mirror in fake mode; log `drift` row with both values + source citation. |
| `orphan` | A record exists on one side with no counterpart on the other. | Linear-fake issue with no phase (extra `TBD-LIN-0099`); phase with no mapping record; orphan `.*.tmp` in `.planning/`. | Never delete in fake mode. Report as `orphan` with `candidate_parent` suggestion; human adopts or explicitly discards (user pause). |
| `conflict` | Both sides assert contradictory facts with no precedence winner. | Phase 12 frontmatter vs body; Phase 14 D1 vs `gaps_found`; Linear `done` vs STATE `gaps_found`. | **Park** the phase in `blocked_hold`/`conflict_hold`; require `ruling_ref`; write nothing to either side. |

Precedence (§5) resolves `drift` automatically; it never resolves a
`conflict` — precedence says which side is *believed*, the user decides
what is *done* (C2). This is the direct fix for B1–B5.

### 8.2 Report + fake flows

`ReconciliationReport{detections: [{class, phase_id, field, a_value,
b_value, source_a, source_b, disposition}], summary: {drift: n,
orphans: n, conflicts: n}, generated_at}`. Dispositions:
`auto_corrected` (drift only, fake mode), `reported` (orphan),
`parked` (conflict). Every detection carries file:line citations
(the Wave1 reconcile doc's trace table is the format to match).

---

## 9. Offline queue + transient-only retry + dead-letter

### 9.1 Durable enqueue (offline-safe)

Every tracker intent is first written to the outbox table (§10) in the
same SQLite transaction as the phase-state change that caused it
(transactional outbox). `POST /sync/enqueue` therefore succeeds while
offline; delivery is a separate, replayable step. Queue entries are
immutable once enqueued; status transitions are the only mutation.

### 9.2 Retry policy: transient-only (D5-shaped)

A failure is **transient** (retryable) iff its code is in
`{E-TIMEOUT, E-DEP-transport, E-CAPACITY}` — the same code families as
the repo's `RetryPolicy.retryable_error_codes` (STATE decisions).
Everything else (`E-INPUT`, `E-CONTRACT`, `E-INTERNAL`, guard refusals,
`E-DEP` for fake-host-deny) is **terminal** and goes straight to the
dead-letter queue. No generic 5xx retry (D5: no auto-retry until the
per-tool inventory row exists — the fake inventory ships with exactly
one row per tracker op, §16 T-RETRY).

| Parameter | Value | Notes |
|---|---|---|
| Max attempts | 5 (1 initial + 4 retries) | Bounded; NFR-RETRY-BOUND limit 5 |
| Backoff | `min(30s, 1s * 2^attempt) ± 300ms jitter` | Mirrors STATE row-22 cap (60 s Python / 30 s cap) with B4 backoff; 300 ms jitter is the repo's inferred value, recorded as such |
| Jitter | deterministic in fake mode (seeded from idempotency key) | Fake tests assert exact schedule; live mode may randomise |
| Terminal codes | immediate DLQ, no retry | T-NONTRANSIENT |

### 9.3 Dead-letter + redrive

Exhausted-transient and terminal failures both land in the DLQ
(schema §10) with `{idempotency_key, last_error, attempts, enqueued_at,
terminal_at, entry_state: pending_redrive | redriven | discarded}`.
`GET /sync/dead-letter` lists `pending_redrive` entries. Redrive
re-enqueues with the **same** idempotency key (so a redriven create is
still a `create-or-return`, never a duplicate) and a **new** attempt
series. `discarded` requires an explicit user action and is the only
path that ever removes a queue/DLQ row; the reconciler never deletes
orphans (C2, §8).

---

## 10. Data schemas (machine-readable)

All schemas are fake-transport only. Field naming: snake_case in JSON,
matching the repo's `contracts/*.json`. The OpenAPI fragment below is
the contract behind the §3.4 operation count (5 ops).

### 10.1 OpenAPI fragment (contract `API-OrchestrationCore`, version `0.1.0`)

```yaml
openapi: 3.1.0
info: {title: Orchestration Core (fake-only), version: "0.1.0"}
paths:
  /phases/{phase_id}/state:
    get:
      operationId: getPhaseState
      parameters: [{name: phase_id, in: path, required: true, schema: {type: string}}]
      responses: {"200": {description: ok, content: {application/json: {schema: {$ref: "#/components/schemas/PhaseState"}}}}}
  /phase-transitions:
    post:
      operationId: transitionPhase
      requestBody: {required: true, content: {application/json: {schema: {$ref: "#/components/schemas/TransitionRequest"}}}}
      responses:
        "200": {description: ok}
        "409": {description: illegal transition (E-CONTRACT)}
        "423": {description: parked (blocked_hold / human_needed)}
  /issues/map:
    post:
      operationId: mapIssue
      requestBody: {required: true, content: {application/json: {schema: {$ref: "#/components/schemas/IssueMapRequest"}}}}
      responses: {"200": {description: ok, content: {application/json: {schema: {$ref: "#/components/schemas/IssueMap"}}}}}
  /sync/enqueue:
    post:
      operationId: enqueueSync
      requestBody: {required: true, content: {application/json: {schema: {$ref: "#/components/schemas/SyncIntent"}}}}
      responses: {"202": {description: accepted (queued)}}
  /sync/dead-letter:
    get:
      operationId: listDeadLetter
      responses: {"200": {description: ok, content: {application/json: {schema: {type: array, items: {$ref: "#/components/schemas/DeadLetterEntry"}}}}}}
```

### 10.2 Core JSON schemas

```json
{
  "$defs": {
    "PhaseVerdict": {"enum": ["unverified","passed","passed_with_recorded_debt","gaps_found","human_needed","failed","not_run"]},
    "WorkflowStage": {"enum": ["undiscussed","discussing","discussed","planning","planned","executing","verification","done-hold"]},
    "HoldKind": {"enum": ["held","blocked_hold","conflict_hold","paused_user_choice"]},
    "AwaitingKind": {"enum": ["verifier_rerun","ci_evidence","user_ruling","user_confirmation","external_approval"]},
    "PhaseState": {
      "type": "object",
      "required": ["phase_id","stage","verdict","holds","debt","evidence","updated_at","correlation_id"],
      "properties": {
        "phase_id": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]+)*$"},
        "stage": {"$ref": "#/$defs/WorkflowStage"},
        "verdict": {"$ref": "#/$defs/PhaseVerdict"},
        "holds": {"type": "array", "items": {"type": "object", "required": ["kind","reason"], "properties": {"kind": {"$ref": "#/$defs/HoldKind"}, "reason": {"type": "string"}, "gate_ref": {"type": "string"}}}},
        "awaiting": {"anyOf": [{"type": "object", "required": ["kind","detail"], "properties": {"kind": {"$ref": "#/$defs/AwaitingKind"}, "detail": {"type": "string"}}}, {"type": "null"}]},
        "debt": {"type": "array", "items": {"type": "object", "required": ["item","owner","accepted_by","accepted_at"], "properties": {"item": {"type": "string"}, "owner": {"type": "string"}, "accepted_by": {"type": "string"}, "accepted_at": {"type": "string", "format": "date-time"}}}},
        "evidence": {"type": "object", "properties": {"artefact": {"type": "string"}, "verifier_digest": {"type": "string", "pattern": "^[0-9a-f]{64}$"}, "delta_ref": {"type": "string"}}},
        "updated_at": {"type": "string", "format": "date-time"},
        "correlation_id": {"type": "string"}
      }
    },
    "TransitionRequest": {
      "type": "object",
      "required": ["phase_id","to_stage","correlation_id"],
      "properties": {
        "phase_id": {"type": "string"}, "to_stage": {"$ref": "#/$defs/WorkflowStage"},
        "expected_verdict": {"$ref": "#/$defs/PhaseVerdict"},
        "evidence_digest": {"type": "string"},
        "ruling_ref": {"type": "string", "description": "required when leaving blocked_hold/conflict_hold"},
        "correlation_id": {"type": "string"}
      }
    },
    "IssueMap": {
      "type": "object",
      "required": ["phase_id","linear","correlation_id","updated_at"],
      "properties": {
        "phase_id": {"type": "string"},
        "linear": {"type": "object", "required": ["primary_fake_id","tbd_real_id"], "properties": {
          "primary_fake_id": {"type": "string", "pattern": "^TBD-LIN-[0-9-]+$"},
          "child_fake_ids": {"type": "array", "items": {"type": "string", "pattern": "^TBD-LIN-[0-9-]+$"}},
          "tbd_real_id": {"type": ["string","null"]},
          "idempotency_keys": {"type": "object", "additionalProperties": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
          "status_fake": {"type": "string"}
        }},
        "notion": {"type": "object", "properties": {"page_ref": {"type": "string", "format": "uri"}, "mode": {"enum": ["read_only"]}, "tbd_real_id": {"type": ["string","null"]}}},
        "correlation_id": {"type": "string"}, "updated_at": {"type": "string", "format": "date-time"}
      }
    },
    "SyncIntent": {
      "type": "object",
      "required": ["intent_id","phase_id","op","tracker","idempotency_key","enqueued_at","correlation_id"],
      "properties": {
        "intent_id": {"type": "string"}, "phase_id": {"type": "string"},
        "op": {"type": "string"}, "tracker": {"enum": ["linear","notion"]},
        "idempotency_key": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "enqueued_at": {"type": "string", "format": "date-time"},
        "correlation_id": {"type": "string"},
        "state": {"enum": ["enqueued","delivering","delivered","failed_transient","dead_lettered"]},
        "attempt": {"type": "integer", "minimum": 1},
        "last_error": {"type": ["string","null"]}
      }
    },
    "DeadLetterEntry": {
      "type": "object",
      "required": ["dlq_id","idempotency_key","intent_id","last_error","entry_state","enqueued_at","terminal_at"],
      "properties": {
        "dlq_id": {"type": "string"}, "idempotency_key": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "intent_id": {"type": "string"}, "last_error": {"type": "string"},
        "entry_state": {"enum": ["pending_redrive","redriven","discarded"]},
        "enqueued_at": {"type": "string", "format": "date-time"}, "terminal_at": {"type": "string", "format": "date-time"}
      }
    },
    "ReconciliationReport": {
      "type": "object",
      "required": ["generated_at","summary","detections"],
      "properties": {
        "generated_at": {"type": "string", "format": "date-time"},
        "summary": {"type": "object", "required": ["drift","orphans","conflicts"], "properties": {"drift": {"type": "integer"}, "orphans": {"type": "integer"}, "conflicts": {"type": "integer"}}},
        "detections": {"type": "array", "items": {"type": "object", "required": ["class","field","a_value","b_value","disposition"], "properties": {
          "class": {"enum": ["drift","orphan","conflict"]}, "phase_id": {"type": "string"},
          "field": {"type": "string"}, "a_value": {"type": "string"}, "b_value": {"type": "string"},
          "source_a": {"type": "string"}, "source_b": {"type": "string"},
          "disposition": {"enum": ["auto_corrected","reported","parked"]}
        }}}
      }
    },
    "IssueMapRequest": {
      "type": "object",
      "required": ["phase_id","op","correlation_id"],
      "properties": {
        "phase_id": {"type": "string"}, "op": {"enum": ["create_primary","create_child","update_primary"]},
        "child_index": {"type": "integer", "minimum": 0}, "body": {"type": "object"},
        "correlation_id": {"type": "string"}
      }
    }
  }
}
```

### 10.3 SQLite projection (`.swarm/tasks.db`, new tables)

```sql
CREATE TABLE IF NOT EXISTS phase_state (
  phase_id TEXT PRIMARY KEY, stage TEXT NOT NULL, verdict TEXT NOT NULL,
  holds_json TEXT NOT NULL DEFAULT '[]', awaiting_json TEXT,
  debt_json TEXT NOT NULL DEFAULT '[]', evidence_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL, correlation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS phase_issue_map (
  phase_id TEXT PRIMARY KEY, primary_fake_id TEXT NOT NULL, child_fake_ids_json TEXT NOT NULL DEFAULT '[]',
  tbd_real_id TEXT, keys_json TEXT NOT NULL DEFAULT '{}',
  notion_page_ref TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_outbox (
  intent_id TEXT PRIMARY KEY, phase_id TEXT NOT NULL, op TEXT NOT NULL, tracker TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
  last_error TEXT, enqueued_at TEXT NOT NULL, correlation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_dlq (
  dlq_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL REFERENCES sync_outbox(intent_id),
  idempotency_key TEXT NOT NULL, last_error TEXT NOT NULL, entry_state TEXT NOT NULL DEFAULT 'pending_redrive',
  enqueued_at TEXT NOT NULL, terminal_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reconcile_reports (
  report_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, report_json TEXT NOT NULL
);
```

No `graph_edges` assumption: edge persistence stays 0-row until a
future, separately approved change (graphcheck §6).

---

## 11. NFR allocation + budgets

`arch_nfr_allocation` SUCCESS: `nfr_count=4`, `allocated=[NFR-STATE-ATOMIC,
NFR-LIFECYCLE-DET, NFR-RETRY-BOUND, NFR-DEDUP]`, `unallocated=[]`,
`unmeasurable=[]`, `coverage_pct=100`, `fully_allocated=true`.

| NFR | Binding (clause / budget) | Measured how (fake mode) |
|---|---|---|
| NFR-STATE-ATOMIC (quantitative, limit 500 — §19) | `state_store` §6 atomic-rename clause | Kill −9 mid-write ⇒ file parses, frontmatter invariant holds; orphan-temp reaped (T-STATE-ATOMIC) |
| NFR-LIFECYCLE-DET (quantitative, limit 0 illegal transitions) | `phase_lifecycle` transition table (§4.3) | Exhaustive fake enumeration: every (from,to) pair not in table refused with `E-CONTRACT` (T-LIFECYCLE) |
| NFR-RETRY-BOUND (quantitative, limit 5) | `sync_queue` component budget (§9.2) | Transient fault injection: exactly ≤5 attempts, then DLQ; terminal codes attempt count = 1 (T-RETRY, T-NONTRANSIENT) |
| NFR-DEDUP (quantitative, limit 0 duplicates) | `issue_mapper` idempotency clause (§7.2) | Replay burst (same key ×N, incl. after lost reply): exactly 1 fake record, `deduped:true` (T-CREATE, T-OFFLINE-REPLAY) |

Wall-clock only; no p95/SLO is committed (D3). Availability,
error-rate and latency SLOs are **not** allocated and must not be
invented at implementation time.

---

## 12. Fitness functions (CI)

All checks are deterministic and fake-mode runnable (no network, no
tracker credentials). A CI violation on main blocks downstream CLAIMED
tasks.

| # | Function | Pass rule |
|---|---|---|
| F1 | Layer direction | `arch_layer_violations` = 0 (layers `[api, service, data, kernel]`) |
| F2 | Acyclicity | `arch_cycle_check` = false on the module graph (§3.2 edge set) |
| F3 | Coupling budget | `arch_c4_fanout` `over_budget=[]` at budget 4 |
| F4 | Contract validity | `arch_contract_check` on every OpenAPI/AsyncAPI file; 5 ops for this contract; breaking finding ⇒ major bump + migration + consumer acks (A05/A06), L3 human approval |
| F5 | NFR allocation | `arch_nfr_allocation` `fully_allocated=true` (100%) |
| F6 | STATE invariants | Parser recomputes `percent`/`current_phase`/`status`; mismatch ⇒ `drift` report, never silent edit |
| F7 | No-live guard | Adapter constructed with non-`fake://` URL ⇒ construction refused (T-NOLIVE); grep CI for real tracker hostnames in orchestration core = 0 hits |

---

## 13. ADRs (proposed — L2, record via `scripts/arch_adr.py`)

Slug evidence: `arch_adr_slug` SUCCESS, slug
`deterministic-orchestration-core-for-outstanding-gsd-phases`.
These ADRs are **proposed** in this doc and must be recorded
individually (status starts `proposed`; transition
`proposed → accepted` is the allowed lifecycle step) — they are not
recorded here because that would write `docs/adr/` outside the
`docs/design-orchestration-SPE-38340.md` target. All are L2 (no novel
infrastructure, no new first-party dependency, no breaking contract).

| ADR (proposed) | Decision | Why not the alternative |
|---|---|---|
| ADR-orch-01 — STATE is phase-state source of truth; Linear is workflow; Notion is context (§5) | Single precedence rule, conflicts park for user ruling | Alternative (Linear-wins) would let tracker state bypass verifier assess — the B3 defect. |
| ADR-orch-02 — Atomic temp+rename via existing `write_text_atomic` (§6) | Zero new kernel, reader never sees torn file | Alternative (in-place edit) reproduces today's 3-way STATE divergence. |
| ADR-orch-03 — Idempotency keys as `sha256(compact_json([...]))` without attempt (§7.2) | Reuses `memory.py` pattern; replay-safe incl. lost reply | Alternative (attempt in key) double-writes on every lost-reply retry. |
| ADR-orch-04 — Transient-only retry (≤5) + DLQ (§9) | Satisfies D5; terminal failures never burn attempts | Alternative (retry-all) violates D5 and the no-uninventoried-retry gate. |
| ADR-orch-05 — Two-axis lifecycle + hold overlay (§4) | Makes relabel-to-bypass unrepresentable (C6) | Alternative (single status flag) is exactly what produced B1/B2. |
| ADR-orch-06 — Fake-forever transport with hard-deny (§7.2) | Cheap, safe cutover path to TBD-real IDs | Alternative (flag-gated live mode) risks a live write before user IDs exist. |

---

## 14. Tech-stack selection

Catalog-first (A03 decision order): **Python stdlib only** —
`sqlite3` (projection), `pathlib`/`os.replace` (atomic write via
`swarm/atomic_file.py`), `hashlib`+`json` (idempotency keys),
`dataclasses`/`enum` (state machine). No new dependency, no new crate,
no new service (L2). Supply-chain note: stdlib-only means SBOM adds no
new rows; the dependency allowlist is unchanged. Wire format stays
JSON/YAML already in the repo. Any future real-tracker transport must
re-enter through an ADR + security co-sign (A10) — it would be L3
(external-facing) and is out of scope here.

---

## 15. Threat model (STRIDE sketch)

Trust boundaries: (a) orchestrator → file system (`.planning/` is
writable by the host; a compromised tool could rewrite STATE),
(b) orchestrator → tracker adapter (fake today; real transport would be
external-facing), (c) queue/DLQ → redrive (payload contains phase
titles/context excerpts).

- **Spoofing:** hand-edited STATE frontmatter asserting `passed`
  without verifier digest → parser refuses (`evidence.verifier_digest`
  required for `passed`/`passed_with_recorded_debt`); fake-host-deny
  prevents confused-deputy writes to real trackers.
- **Tampering:** torn/partial STATE after crash → atomic-rename +
  parser invariants (F6) reject the torn file; attacker-edited
  `.planning/*.tmp` orphans are reported, never trusted.
- **Repudiation:** every transition, enqueue, redrive and ruling
  carries `correlation_id` + `updated_at`; reconciliation report
  preserves both diverged values with citations.
- **Information disclosure:** idempotency keys are hashes (no payload);
  queue/DLQ payloads are fake-only in this phase; Notion context is
  reference-only (no write path exists to leak context back).
- **Denial of service:** unbounded retry is the DoS vector — bounded
  at 5 attempts + DLQ terminal path; `free_slots=0` defers instead of
  dropping (§4.4).
- **Elevation of privilege:** relabel-to-bypass is an integrity
  bypass (C6) — the two-axis model + hold overlay remove the path
  rather than policing it.

A10 co-sign is required before any *real* transport or any schema
DDL change; the fake-only design above needs no co-sign to implement
against fakes.

---

## 16. Fake-based test plan — checklist (acceptance)

All tests run against fakes only: fake tracker adapter (`fake://` base),
fake clock (seeded), fake disk-fault injector. No network, no real
Linear/Notion, no live credentials. Each test names the design clause
it proves. **SKIP gates/formatters** applies to the *design* authoring
(this doc); these tests run in the implementation tranche.

### 16.1 Lifecycle (discuss / plan / execute + states)

- [ ] **T-LIFECYCLE** — Exhaustive transition enumeration: for every
  (from,to) pair, listed transitions succeed, all others return
  `E-CONTRACT` and park nothing. Proves §4.3 / NFR-LIFECYCLE-DET (0
  illegal accepts).
- [ ] **T-DISCUSS-PLAN-GATE** — `discussing → planned` without CONTEXT or
  PLAN artefact is refused; with artefacts + `plan_graph` pass it
  succeeds. Proves §4.3 gates.
- [ ] **T-PASSED-REQUIRES-DIGEST** — `verification → passed` without
  VERIFICATION digest, or with unmet criteria, is refused (T-B3).
  Proves C6 / ADR-orch-05.
- [ ] **T-DEBT-REQUIRES-ACCEPTANCE** — `passed_with_recorded_debt`
  with empty `debt[]` or `accepted_by≠user` is refused. Proves §4.2.
- [ ] **T-HUMAN-NEEDED** — Phase-6-shaped `human_needed{verifier_rerun}`;
  exit only after `awaiting` evidence + digest. Proves §4.2/§4.3.
- [ ] **T-HELD-BLOCKS** — Held phase (14.4-shaped `held(R2)`) is never
  runnable; dependents (14.7-shaped) wait; no flip transition exists.
  Proves §4.4 / B2.
- [ ] **T-USER-CONFIRM-SHIP** — Phase-15-shaped ship without
  `user_confirmation` token is refused even when all other gates pass.
  Proves B5 / §4.3.
- [ ] **T-PARSE-B1** — Seed frontmatter `status:gaps_found` +
  Current-Position "complete" contradiction ⇒ parser emits `conflict`,
  milestone parks in `blocked_hold`; no auto-edit occurs. Proves §6.3/B1.

### 16.2 Atomic STATE writes

- [ ] **T-STATE-ATOMIC** — Kill −9 between temp-write and `os.replace`
  (fault injector): file parses, F6 invariants hold, orphan `.*.tmp`
  reported as `drift` by reconciler. Proves NFR-STATE-ATOMIC / §6.
- [ ] **T-STATE-SINGLEWRITER** — Two concurrent writers racing the
  lease: exactly one publishes; the other retries or parks; no
  interleaved file. Proves §6.1 / `BEGIN IMMEDIATE`.
- [ ] **T-STATE-PERCENT** — Hand-edit `percent` (33 vs recomputed) ⇒
  `drift` with both values; never silently accepted. Proves §6.2.

### 16.3 Issue mapping + idempotency (fakes only)

- [ ] **T-CREATE** — First `create_primary` creates 1 record; replay
  with same idempotency key returns same record, `deduped:true`, row
  count unchanged. Proves §7.3 / NFR-DEDUP.
- [ ] **T-CREATE-CHILDREN** — 1-to-many: 1 parent + 3 children map with
  deterministic fake IDs `TBD-LIN-…`; each child has distinct key;
  re-running whole mapping is a no-op. Proves §7.1.
- [ ] **T-UPDATE** — Update replay with same key ⇒ same record; update
  with new body ⇒ new key, same fake ID, `updated_at` advances.
  Proves §7.2 semantics.
- [ ] **T-CONFLICT** — Concurrent fake updates to same subject (two
  writers, different bodies): last-writer-wins on `updated_at`,
  loser logged as reconciliation `conflict`. Proves §7.2.
- [ ] **T-NOLIVE** — Construct adapter with `https://linear.example…`
  or `https://notion.example…` base URL ⇒ refused with `E-DEP` before
  any network attempt; CI grep for real tracker hostnames in the
  orchestration core = 0 hits. Proves C1 / F7.

### 16.4 Reconciliation (drift / orphans / conflicts)

- [ ] **T-DRIFT** — Seed Linear-fake `done` vs STATE `gaps_found`
  (mirror direction per §8.1) for a mirror field ⇒ `drift`,
  `auto_corrected` in fake mode, both values in report. Proves §8.1.
- [ ] **T-ORPHANS** — Seed orphan Linear-fake issue (no phase),
  orphan phase (no mapping), orphan `.planning/.*.tmp` ⇒ 3 `orphan`
  rows, `disposition: reported`, **zero deletions**. Proves §8.
- [ ] **T-CONFLICT-PARK** — Seed Phase-12-shaped frontmatter/body
  contradiction ⇒ `conflict`, `disposition: parked`,
  `blocked_hold` set; no auto-fix on either side. Proves §8 / C2.
- [ ] **T-REPORT-CITATIONS** — Every detection carries `source_a` /
  `source_b` file:line citations; report validates against the
  `ReconciliationReport` schema. Proves §8.2.

### 16.5 Offline queue + retry + DLQ

- [ ] **T-OFFLINE-ENQUEUE** — Tracker down: `enqueueSync` succeeds
  (202), intent persisted in outbox; delivery attempted on recovery
  in FIFO order. Proves §9.1 / transactional outbox.
- [ ] **T-OFFLINE-REPLAY** — Transport accepts but reply is lost:
  retry with same idempotency key ⇒ `create-or-return`, still exactly
  1 fake record. Proves §7.2 lost-reply rule / NFR-DEDUP.
- [ ] **T-RETRY** — Inject `E-TIMEOUT` 3× then success ⇒ delivered,
  attempt 4, exact seeded-jitter backoff schedule asserted
  (1s,2s,4s). Proves §9.2 / NFR-RETRY-BOUND (≤5).
- [ ] **T-RETRY-EXHAUST** — Inject `E-TIMEOUT` forever ⇒ exactly 5
  attempts, then DLQ entry `pending_redrive`. Proves §9.2.
- [ ] **T-NONTRANSIENT** — Inject `E-INPUT` / `E-CONTRACT` / `E-INTERNAL`
  / fake-host-deny ⇒ exactly 1 attempt, immediate DLQ, no backoff.
  Proves §9.2 terminal rule / D5.
- [ ] **T-REDIVE** — Redrive DLQ entry ⇒ re-enqueued with same
  idempotency key, new attempt series; resulting fake create still
  dedupes. Proves §9.3.
- [ ] **T-DLQ-DISCARD** — `discarded` only via explicit user action;
  reconciler never discards orphans automatically. Proves §9.3/C2.

### 16.6 Scheduler-shaped safety (from graphcheck)

- [ ] **T-STARVE** — Greedy-order trap: low-priority runnable phase
  waits >3 waves under capacity pressure ⇒ promoted ahead of
  newly-runnable higher-priority phases; nothing starved
  indefinitely. Proves §4.4 aging rule.
- [ ] **T-DEAD** — Failed phase with live dependents ⇒ dependents park
  (not permanently blocked); require `failed → executing` recovery
  or `superseded_by` ruling before any dependent schedules. Proves
  §4.4 dead-end guard.
- [ ] **T-CYCLE-REFUSAL** — `a←c, b←a, c←b` plan ⇒ `CycleError` with
  path `a -> c -> b -> a`; dangling `ghost` ⇒ `UnknownDependencyError`.
  Proves §4.3 planning gate (graphcheck §3).

### 16.7 Negative / integrity (sampled, not exhaustive)

- [ ] **T-KEY-SEPARATION** — Different op/subject/body ⇒ different
  keys; same logical write retried ⇒ same key. (Property test over
  1000 random field combos.)
- [ ] **T-SCHEMA** — Every §10 schema round-trips: invalid fixture in
  each schema is rejected by the parser (12 negative cases).
- [ ] **T-33-39** — The actual current STATE (33% vs 39%, B1) loads
  into the reconciler and yields exactly the B1 `conflict` +
  `drift` rows from the Wave1 report.

---

## 17. Cutover (no live writes)

1. Implement against fakes (§16 green). 2. Record ADRs (§13) via
`scripts/arch_adr.py`. 3. Run `arch_contract_check` on the contract
fragment (F4). 4. User supplies real Linear/Notion IDs **only** via
`tbd_real_id` fill + explicit approval; the fake→real transport switch
is a separate, A10-co-signed, L3 tranche — never a flag flip in this
design. 5. ROADMAP/STATE hand-edits to clear B1–B5 are forbidden at
every step; contradictions clear only by user ruling + re-verify.

## 18. Open questions / user decisions (the B1–B5 rulings, restated)

- **B1:** Which STATE line (frontmatter vs Current Position vs
  Operator Next Steps) is authoritative for milestone v2.4 status?
- **B2:** Does the updated 14.4 VERIF close the phase, or does the
  R2 gate still hold?
- **B3:** What evidential standard (per C6) may ever mark a phase
  COMPLETE — verifier digest required (this design's answer)?
- **B4:** Correct ROADMAP to 14.4→14.7 (de-facto), or split 14.7 into
  Wave 1 / Wave-2-gated?
- **B5:** Is Phase 15 audit/cleanup/ship authorised, or is the
  "start next milestone" line struck?

Non-blocking defaults assumed (per A03 error-handling, `assumed: true`):
anti-starvation threshold 3 waves; retry cap 5 with 300 ms seeded
jitter (repo value, inferred); deterministic fake IDs. Each must be
confirmed or replaced before implementation.

## 19. Evidence trace

| Claim | Source |
|---|---|
| 12 outstanding phases + 5 blockers B1–B5 | `docs/reconcile-SPE-38338.md` §1–§3 |
| Canonical order, 0 cycles, TaskNode no tool field, frontier starvation, dead-end | `docs/graphcheck-SPE-38338.md` §2–§5 |
| STATE frontmatter `gaps_found`, `current_phase: 14.4`, 33%; 2026-11-18 audit `done=false` | `.planning/STATE.md:5-17, 899` |
| `write_text_atomic` (unique temp + `os.replace`, no fsync) | `swarm/atomic_file.py:15-24` |
| Idempotency key = sha256 of compact JSON array, no attempt, no expected_version | `swarm/memory.py:85-105` |
| GSD STATE.md mirror target off by default | `swarm/state_store.py:30-33` |
| D1–D6 (TS host, DAG dispatch only, wall-clock only, Python default, D5 no auto-retry, D6 no autonomous runner) | `DECISIONS.md` |
| C4 fan-out: 8 containers, 11 relations, max 3, budget 4, 0 over-budget, 0 isolated | `arch_c4_fanout` SUCCESS (§3.1) |
| Layer violations: 0 of 16 edges | `arch_layer_violations` SUCCESS (§3.3) |
| Cycle check: `has_cycle=false`, 6 nodes, 0 cyclic | `arch_cycle_check` SUCCESS (§3.2) |
| Contract ops = 5; semver `0.0.0 → 0.1.0` minor, compliant | `arch_contract_ops`, `arch_semver_bump` SUCCESS (§3.4) |
| NFR allocation 4/4, 100%, fully allocated | `arch_nfr_allocation` SUCCESS (§11) |
| ADR slug `deterministic-orchestration-core-for-outstanding-gsd-phases` | `arch_adr_slug` SUCCESS (§13) |

Tool citations are mandatory evidence per the A03 output contract:
every count/verdict above names the registry tool (`arch_*`) and its
returned key field. No scan result, digest, or verdict is invented;
the `arch_contract_check` script gate (F4) is *specified* here and runs
in the implementation tranche — the assignment's SKIP-gates instruction
covers this design-doc authoring, and no `api.contract` file is published
by this doc alone.

---

*End of design. Next: record ADR set (§13), implement against §16 fakes,
run contract check (F4), file B1–B5 for user ruling (§18).*
