# Architecture Comparison — SPE-38343 (design only, no code)

**Issue:** SPE-38343 (Wave3 core design).
**Branch:** `swarm/T74c2-be` (repo `agent-swarm`, remote `https://github.com/swcstudiospace/swarm.git` per `INVENTORY.md` §1).
**Status:** design — no code, no live Linear/Notion writes, gates/formatters skipped per assignment.
**Wave 1–2 inputs (read, not re-derived):** `INVENTORY.md`, `docs/design-orchestration-SPE-38340.md` (deterministic core, C4, lifecycle), `docs/design-graph-SPE-38341.md` (3-node-type graph model), `docs/critique-SPE-38339.md` (B1–B5 blockers, sequential-fallback baseline), `docs/acceptance.md` (AC-1..AC-84), `docs/architecture/blueprint.md` (15-container enterprise blueprint, 36-operation contract).
**Decisions carried forward (assignment context):** fakes only (no live Linear/Notion writes), learning disabled until policy, approver = user via pause, tool-unless-judgement.
**Toolchain (observed, `INVENTORY.md` §2/§7):** Rust `1.98.1` pinned (`rust-toolchain.toml`), Python `3.12.3` (`>=3.12`), `bun 1.4.0`, `ruff 0.15.18`, `maturin 1.15.0`, `cargo 1.98.1`, pytest `>=8.0,<10.0.0` (`pyproject.toml`).

> This doc is the `architecture.blueprint` + `api.contract` + `adr.set` single-writer artifact for A03. Consumers: A05 (backend), A06 (frontend), A07 (data), A08 (QA), A10 (security co-sign).
> Everything below that states current behaviour cites a file by path and line; everything proposed is marked **(proposed)**.

---

## Table of Contents

1. Requirement (what must be true)
2. The three candidates
3. Comparison table (normative)
4. Selected: ports-and-adapters with sequential fallback (blueprint)
5. Contracts (machine-readable)
6. Error classification + bounded backoff + dead-letter
7. Concurrency limits + circuit breaking
8. Dependency injection + fake adapters
9. Verified test / lint / typecheck commands (INVENTORY + CI)
10. Unit test plan (acceptance checklist)
11. ADRs (proposed)
12. Threat model (STRIDE sketch)
13. Migration / cutover
14. Open questions / user decisions
15. Evidence trace

---

## 1. Requirement (what must be true)

The selected architecture must satisfy **all** of the following, scored in §3:

1. **SOLID** — single-responsibility modules; open/closed extension by new adapters without core edits; Liskov substitutability of adapters behind a port; interface segregation (narrow ports); dependency inversion (core depends on port abstractions, not on Task Store / Tracker / native bridge).
2. **DRY** — one place for each rule: transition legality, error classification, backoff schedule, claim parsing, descriptor validation. No duplicate classifier copies for the *new* core (the 4-copy SDLC prompt classifier in `omp/src/hooks.ts`, `swarm/signal_detector.py`, `hooks/user_prompt_submit.py`, Rust mirror — `INVENTORY.md` §4 — is existing debt; the new design must not add a 5th).
3. **KISS** — no framework, no DSL, no new crate unless a named need exists in §4. The smallest mechanism that satisfies 1–4 and 6–11 wins.
4. **YAGNI** — no speculative generality: sequential fallback is a *degraded mode*, not a parallel scheduler; no learned policy; no live tracker transport (fakes only).
5. **Deterministic core** — the decision loop is a pure function of (dispatchable rows, config, injected clock/RNG); the same inputs always yield the same waves, order, backoff delays. Python is the parity oracle / default route (DECISIONS D4 — cited in `docs/design-orchestration-SPE-38340.md` §1).
6. **Named state machines** — task lifecycle (`swarm/taskstore.py:26-59`) and phase lifecycle (`docs/design-orchestration-SPE-38340.md` §4) are named, enumerated transition tables; any transition not in the table is refused with `E-CONTRACT`.
7. **Typed validated boundaries** — every cross-boundary value is parsed and validated on entry (`swarm/envelope.py:114-137` pattern), never trusted. Descriptor schemas pin the shape.
8. **Classified errors with bounded backoff + dead-letter** — every failure is classified `transient` / `permanent` / `decision` **before** any retry; only transient failures retry, on a bounded exponential backoff; exhaustion or permanent/decision failure lands in dead-letter, never in a silent drop.
9. **DI** — clock, RNG, Task Store, tracker, lease, telemetry are injected; the core never constructs them. Fakes substitute without edits.
10. **Concurrency limits** — hard caps (`WORKER_CAP = 30`, ceiling 32 — `swarm/graph_quality.py:85-86`) plus an adaptive limiter (AIMD). A 31st worker is refused (`E-CAPACITY`), never spawned.
11. **Circuit breaking** — consecutive-failure and rate-limit circuits halve parallelism / open peer circuits instead of hammering a failing dependency.

---

## 2. The three candidates

### A. Monolith-inline (bigger `swarm_run.py` / orchestrator module)

One deployable selection + one module that imports the Task Store, tracker client, lease client, telemetry, limiter and retry helper directly, and decides, dispatches and retries inline. This is the *current* shape taken to its limit: `scripts/swarm_run.py` is already 150 KB and `swarm/rust_scheduler.py` “owns the decision loop, the claim table, AIMD, backoff and the worker pool” (`INVENTORY.md` §4).

```
┌──────────────────────────────────────────────┐
│  monolith (swarm_run.py + helpers)            │
│  decide → dispatch → retry → store/tracker/   │
│  lease/telemetry called inline, no port type  │
└──────────────────────────────────────────────┘
```

Tagliamonte note: `crates/gsd-sched-core/src/ports.rs:1-7` already states the opposite of A for the Rust core — “the decision loop in this crate never touches the Task Store, telemetry, leases, handoffs or sessions.” A is therefore a regression vs. the landed Rust seam; it would have to be adopted **only** if B’s port surface were removed.

### B. Ports-and-adapters (hexagonal) — SELECTED

A deterministic core (scheduler decision loop + lifecycle tables + retry classifier) that depends only on port traits. Each port has exactly one production adapter (Python Task Store, fake tracker, native bridge) and any number of fakes. The runtime is `SchedulerPorts: Send + Sync` with 16 methods (`crates/gsd-sched-core/src/ports.rs:599-722`); the Python side implements the trait once in the binding adapter and fakes implement it in tests (`ports.rs:586-589`). Sequential fallback (one node at a time in wave order) is the *degraded mode* when the port adapter is unavailable — never a second scheduler.

```
              ┌─ fake-tracker-adapter ─┐
              │  fake-lease-adapter    │
  ┌──────────▶│  task-store-adapter    │  (adapters, Python, fakes only for tracker)
  │  ports     └──────────▲───────────┘
  │  (SchedulerPorts,      │
  │   16 methods, typed)   │
┌─┴────────────┐  decides │
│ deterministic │──────────┤
│  core         │  emits   │
│ (waves/order/  │──▶ error classification + backoff + DLQ
│  limiter/circuit)│
└───────────────┘
  orchestrator-facade (A01-facing, single-writer lease on STATE)
```

### C. Generic framework (scheduler platform / graph engine SDK)

A reusable DAG + queue + retry + plugin framework: node types, edge types, dispatch plugins, retry policies as configuration, vendor-neutral events, intended for multiple products. In this repo it would mean widening `TaskNode` (which today is exactly `(id, files, dependencies, complexity, priority, duration)` — `swarm/graph_planner.py:73-82`, confirmed `INVENTORY.md` §4 and `docs/design-graph-SPE-38341.md` §1.1) into a generic node with owner, typed I/O, tool reqs, success criteria and state (`docs/design-graph-SPE-38341.md` §4) — plus a plugin registry, a DSL or schema for retry policies, and an event bus.

```
┌──────────────────────────────────────────────┐
│  framework SDK: Node/Edge/Plugin/Stragegy    │
│  ├─ scheduler plugin (python|rust|…)          │
│  ├─ retry-policy plugin                        │
│  ├─ tracker plugin                             │
│  └─ graph DSL + store + bus                    │
└──────────────────────────────────────────────┘
```

C is the `docs/design-graph-SPE-38341.md` model generalised into a platform. It is the only candidate that answers the graphcheck §4 gap (“tool requirement cannot be expressed”) *and* the missing-planner-features list at once — at the cost of building a platform nobody asked for yet.

---

## 3. Comparison table (normative)

Scores: ● strong / ◐ partial / ○ fails. “Selected” is B; the rationale column states the deciding reason. Evidence is cited per row.

| # | Criterion | A. Monolith-inline | B. Ports-and-adapters (SELECTED) | C. Generic framework |
|---|---|---|---|---|
| 1 | SRP | ○ One module decides + dispatches + persists + syncs. Any change risks all. | ● Core only decides; adapters only translate; store only persists (§4). | ◐ Framework has a clear core, but node/edge/dispatch/retry each become their own plugin surface — more axes to hold. |
| 2 | OCP | ○ New tracker/lease/transport = edit the monolith. | ● New adapter = new impl of an existing port; core untouched. `SchedulerPorts` already has exactly this shape (`ports.rs:599-722`). | ● New plugin type; but the “open” surface is the whole SDK, so versioning locks it. |
| 3 | LSP | ○ No interface to substitute; fakes require editing call sites. | ● Any `SchedulerPorts` impl (real or fake) is interchangeable; tests substitute fakes (`ports.rs:588-589`). | ● Plugin impls substitute, but only within the framework’s lifecycle hooks. |
| 4 | ISP | ○ Callers depend on everything the monolith imports. | ● 16 narrow methods; a caller needing only `random_unit` + `set_retry_after` does not depend on `kill` or `lease`. Verified: `test_the_port_adapter_exposes_exactly_the_sixteen_callbacks` (`tests/typecheck/test_scheduler_ports_typecheck.py:94-97`). | ○ Plugin authors depend on the whole SDK lifecycle, not just the hook they implement. |
| 5 | DIP | ○ Core depends on concrete Store/Tracker/Lease. | ● Core depends on `dyn SchedulerPorts`; adapters depend on core types (inversion). Lock-order rule is explicit: “no Rust lock may be held across a port call” (`ports.rs:20-22`). | ● Core depends on plugin traits. |
| 6 | DRY | ○ Retry/backoff written once in the monolith, but every new caller re-invents classification; the 4-copy classifier debt (`INVENTORY.md` §4) stays. | ● One classifier (`swarm/retry.py`), one `LEGAL_TRANSITIONS` (`taskstore.py:44-59`), one `backoff_s` (`retry.py:23-39`), one claim parser (Python only — Rust “feeds the pair … as is”, `ports.rs:257-258`). | ● Single shared kernel — **but** it duplicates the landed `swarm/retry.py` + `crates/gsd-failure` contract into a second backoff/retry surface until parity is proven. |
| 7 | KISS | ● Fewest moving parts today. | ◐ One trait + one adapter + fakes; 16 methods is a real (bounded) cost. The trait already exists — ~zero marginal cost. | ○ New DSL, plugin registry, event bus, store schema. Highest concepts-per-behaviour. |
| 8 | YAGNI | ● Nothing speculative; but it cannot express the graph §4 tool gap without a schema change anyway. | ● Every port maps to a named Phase 14.4 need (§5); no generic DSL, no plugin registry, no bus. | ○ Platform for “many products” when exactly one swarm exists; graph genericity in SPE-38341 §4 is needed at *node* level, not as a platform. |
| 9 | Deterministic core | ○ Randomness, wall-clock, tracker I/O and decision logic interleaved; same input need not yield same output (needs refactoring to test). | ● Core is a pure function of (`ReadyRow[]`, `RunConfig`, injected `Clock`/`random_unit`); Rust “owns no random generator; tests script this value” (`ports.rs:693-696`). Python `backoff_s` accepts a seeded `random.Random` (`retry.py:23-27`). | ● If the kernel is pure, yes — but framework lifecycle hooks, plugins and event timing re-introduce nondeterminism at the seams. |
| 10 | Named state machines | ○ Transitions scattered as `if state == …` in call sites; illegal transitions possible and only caught downstream. | ● One table `LEGAL_TRANSITIONS` (`taskstore.py:44-59`); `transition()` refuses with `E-CONTRACT` inside one `BEGIN IMMEDIATE` (`taskstore.py:981-993`). Phase lifecycle table is in `docs/design-orchestration-SPE-38340.md` §4.3. | ◐ Named tables per plugin — but now there are *N* machines (node, edge, dispatch, retry) and no single place to enumerate all legal moves. |
| 11 | Typed validated boundaries | ○ Untyped dicts cross between inline helpers; a malformed tracker row fails far from the boundary. | ● `validate_envelope` refuses missing/mistyped fields with `E-CONTRACT` (`envelope.py:114-135`); Rust configs refuse with `E-INPUT` + wire message (`ports.rs:28-32`); `RunConfig::validate` refuses out-of-range numbers (`ports.rs:525-527`); tool descriptors validated by `tool.contract.v1.json` (`docs/design-graph-SPE-38341.md` §1.4). | ● Schemas per plugin — **but** a new schema language (node/edge/tool) that must be kept in sync with `tool.contract.v1.json` and `swarm.v1.*` envelopes. |
| 12 | Error classification (transient/permanent/decision) | ○ Retry decision is an inline `try/except` per call site; permanent errors can be retried by a distant caller. | ● One classifier with named sets `TRANSIENT_CODES` / `PERMANENT_CODES` (`retry.py:14-15`) + message fallback for unknown codes (`retry.py:42-58`); the scheduler “stops on `Err` from any loop-thread port” (`ports.rs:26, 595-596`) instead of guessing. | ◐ One kernel classifier, but plugin authors must map their errors into the framework taxonomy or the framework guesses again. |
| 13 | Bounded backoff + dead-letter | ○ Backoff constants are literals per call site (`_exp_backoff_err` 1 s base, `errors.py:66-70`, cap 60 s); no single bound to reason about; no DLQ — a failed task is just `FAILED`. | ● Bounded: `backoff_s` = `min(cap, base·2^(n-1))·(1±jitter)` clamped to cap (`retry.py:35-39`), defaults `base 0.1 s / cap 5 s` (`retry.py:11-12`); E-DEP ladder caps at 60 s ± 0.3 s (`tests/test_backoff_cap.py:1-12`); Retry-After hold caps at 120 s (`limiter.py:17,84-85`). Dead-letter is the `sync_queue` → `listDeadLetter` path (§5) with redrive, not a silent drop. | ● Bounded by framework policy — but the bound is configuration, so two deployments can silently diverge; parity with the Python oracle must be re-proven per policy. |
| 14 | DI | ○ `import` is the DI; tests monkey-patch module globals (fragile, order-dependent). | ● Constructor/port injection: `SchedulerPorts` object held as `dyn`; `Clock` injected (`limiter.py:3-5`, “tests drive the cooldown with `FakeClock`”); RNG injected (`retry.backoff_s(..., rng=...)`; `ports.random_unit`). | ● Plugin injection — the heaviest DI of the three (container/lifecycle to learn). |
| 15 | Concurrency limits | ◐ Cap exists (`WORKER_CAP 30`, `graph_quality.py:85-86`) but enforcement is a check inside the monolith — easy to bypass by a new dispatch path. | ● Cap is a config the core enforces: `max_concurrent ≥ 1` validated (`ports.rs:81`), 31st submit refused (`E-CAPACITY`, AC-23); pool threads are owned by the core (`crates/concurrency` / `gsd-sched-core` pool); lock-order rule prevents GIL deadlock (`ports.rs:20-22`). | ◐ Cap becomes a plugin setting; a second scheduler plugin can set a different cap and the system has two truths. |
| 16 | Circuit breaking | ○ No single place owns “half the parallelism once” — each call site invents it or forgets it. | ● One owner: `AimdLimiter` halves on rate-limit (`limiter.py:3-4`); failure circuit halves once per `SWARM_FAIL_STREAK` (default 3) streak of *distinct* tasks (`errors.py:206`; `ports.rs:472-474,506-507`); peer circuit in `acp.py:123,175-182` for cross-process calls. | ◐ Framework circuit per plugin — risk of N circuits with N thresholds for one dependency. |
| 17 | Testability (fakes only) | ○ Fake tracker = patch the module or run a stub server; the 12-phase reconcile tests need the whole monolith importable. | ● Fake adapters implement the same 16 methods; §10 lists them. The repo already does this: ports fakes in `ports.rs:724+` tests and `tests/test_scheduler_ports.py`. | ● Fake plugins — but tests must now learn the framework test harness. |
| 18 | Migration cost / risk | ● Zero (it is the status quo). | ◐ Strangler seam already landed (`gsd-sched-core` + `swarm/rust_scheduler.py`); cutover is per-route with Python default and parity gates (§13). | ○ New SDK + store + DSL + backfill migration; every existing `graph_edges` row (today 0, `INVENTORY.md` §4) and `TaskNode` caller must move. |
| 19 | Team fit (A05/A06/A07 parallel) | ○ All agents edit the same module → merge contention, no contract boundary. | ● Contract-first: 5 versioned operations (§5); A05/A06/A07 build against ports in parallel. Matches the shipped pattern: `API-GsdQualityGate 1.5.0`, 36 operations, semver-minor, `breaking_change: false` (`docs/architecture/blueprint.md` §3). | ● Also contract-first — but *which* framework contract is the real one (SDK version vs. API version) becomes a second contract to govern. |
| 20 | Sequential fallback (degraded mode) | ◐ Fallback = “run the monolith one node at a time” — works, but the fallback *is* the implementation, so “degraded” has no distinct, testable meaning. | ● Fallback = drive the same sequential (Kahn + priority/id) order through the same ports, one node at a time (graphcheck baseline: `start, extra1, n1, n2, n3, n4, m1, m2, m3, last`, makespan 4.0 — `docs/critique-SPE-38339.md` optimisation row). The baseline is accepted as record; optimisation is out of scope until Wave-2 (critique D-optimisation). Same code path, degraded concurrency — testable (§10, T-SEQ-*). | ○ Sequential = run the framework with `max_parallel=1`; correct, but the “fallback” is a config of the platform, so a platform bug can break the fallback too. |

**Verdict: B (ports-and-adapters) wins on 12, ties on 4 (DIP/O/KISS-as-existing-trait), loses on none decisively.** C’s only decisive win is expressiveness for the graph §4 tool gap — which B already closes at graph level without widening the scheduler node (`docs/design-graph-SPE-38341.md` §4.4: “`TaskNode` gains no new field … the Rust DAG and Python planner stay mechanically identical”). A wins only on “zero migration”, which is already spent: the ports seam exists in-tree.

---

## 4. Selected: ports-and-adapters with sequential fallback (blueprint)

### 4.1 C4 containers (single-writer blueprint)

| Container | Layer | Responsibility | Maps to (landed code) |
|---|---|---|---|
| `orchestrator-facade` | api | A01-facing facade: transition, dispatch, enqueueSync, DLQ list. Owns single-writer lease on STATE. | `scripts/swarm_run.py`, `swarm/taskstore.py` (`transition` only via A01 — `taskstore.py:1-6`) |
| `deterministic-core` | service | Decision loop: select batch, order ready, AIMD feed, backoff delay, circuit decision. Owns no I/O. | `crates/gsd-sched-core/src/runtime.rs`, `select.rs`, `aimd.rs`, `backoff.rs` |
| `retry-classifier` | service | Error classification + bounded backoff schedule. | `swarm/retry.py`, `crates/gsd-failure/src/isolation.rs` |
| `limiter-circuit` | service | AIMD limiter + failure-streak circuit + peer circuit state. | `swarm/limiter.py` (`AimdLimiter`), `crates/gsd-sched-core/src/aimd.rs`, `swarm/acp.py:123,175-182` |
| `state-store` | data | Task/lifecycle persistence; atomic file publish; idempotency-key registry. | `swarm/taskstore.py`, `.swarm/tasks.db` |
| `tracker-adapter` | data | **Fake only.** Fake Linear (workflow) + read-only Notion (context). No live transport. | TBD fakes (fakes-only rule) |
| `native-adapter` | data | PyO3 bridge to Rust core; pure-Python fallback edge. | `swarm/_native.pyi`, `swarm/_native_loader.py`, `swarm/rust_scheduler.py` |
| `parity-harness` | api | Rust-vs-Python-oracle parity evidence; sequential-fallback witness. | `tests/parity/*`, `scripts/ci_parity_rust.sh`, `scripts/ci_scheduler_rust.sh` |

Fitness evidence (registry tools, §15): `arch_c4_fanout` SUCCESS `container_count=8, relation_count=11, max_fan_out=3, over_budget=[], isolated=[]` (budget 4); `arch_cycle_check` SUCCESS `has_cycle=false, node_count=8, cyclic_node_count=0`; `arch_layer_violations` SUCCESS `violation_count=0, checked_edges=15, violations=[]`; `shared_verdict` (severities `[]`) → `pass`.

### 4.2 Relations → implementation

```
orchestrator-facade -> deterministic-core, state-store, retry-classifier
deterministic-core  -> state-store, tracker-adapter, native-adapter, clock(kernel)
retry-classifier    -> limiter-circuit, clock(kernel)
limiter-circuit     -> state-store, clock(kernel)
tracker-adapter     -> state-store
native-adapter      -> state-store
parity-harness      -> deterministic-core
```

Rule (normative): the core calls a port; it never imports the adapter. The binding holds `dyn SchedulerPorts` (`ports.rs:589`: “the runtime holds it as `dyn SchedulerPorts`”). No Rust lock, channel guard or condition-variable guard is held across a port call (`ports.rs:20-22`) — a port may block on the GIL, on a SQLite commit, on a lease round-trip, or for a whole model session via `worker` (`ports.rs:12-18`).

### 4.3 Layering

`layer_order = [api, service, data, kernel]` (outermost → innermost; a module may depend on its own layer or any later one).

| Layer | Modules |
|---|---|
| api | `orchestrator-facade`, `parity-harness` |
| service | `deterministic-core`, `retry-classifier`, `limiter-circuit` |
| data | `state-store`, `tracker-adapter`, `native-adapter` |
| kernel | `clock`, `idempotency`, `atomic-file` |

Kernel primitives are reused, not reinvented: `swarm/limiter.py` clock injection, `swarm/memory.py` idempotency (referenced `docs/design-orchestration-SPE-38340.md` §3.3), Python atomic publish via temp + `os.replace` (orchestration §6).

### 4.4 DI wiring (proposed, constructor/port injection only)

```
OrchestratorFacade(store, core, classifier)
Core = DeterministicCore(ports: dyn SchedulerPorts, limiter, classifier,
                        clock, rng, config: RunConfig)
Adapter = PythonPortsAdapter(store, leases, telemetry, pool, sessions)  # implements SchedulerPorts
Tests  = FakePorts(...)  # same trait, scripted answers, no SQLite, no network
```

No service locator, no DI container, no global. Every collaborator arrives in a constructor or as the port object — the smallest mechanism that gives fakes. (`AimdLimiter` already takes a `Clock`: `limiter.py:91`.)

### 4.5 Sequential fallback (degraded mode, normative)

- Trigger: adapter unavailable (native import fails, `SWARM_PURE_PYTHON=1`, `GSD_ENGINE` selector resolves to Python, or AC-30 stale-extension case) **or** parallel frontier starves. The fallback is **not** a second scheduler: it drives the *same* validated sequential order (Kahn + priority desc, id asc — `graph_planner.py:196-197`) through the *same* ports, one node at a time.
- Fallback ordering guarantee: the canonical 10-node fixture order `start, extra1, n1, n2, n3, n4, m1, m2, m3, last`, makespan 4.0 (`docs/graphcheck-SPE-38338.md` per `INVENTORY.md` §4; accepted as baseline in `docs/critique-SPE-38339.md` D-optimisation).
- Fallback is only entered after the parallel attempt has been *witnessed* (each child’s engine recorded — `scripts/ci_scheduler_rust.sh` witness check, `§9`), and the run log line says so. A silent fallback is a defect (T-SEQ-2).

---

## 5. Contracts (machine-readable)

Breaking change: **false** (initial version `0.1.0`; no base contract to diff). Versioning evidence: `arch_semver_bump` `0.0.0 → 0.1.0` (additive) → `bump=minor, required_bump=minor, compliant=true`. Operation count evidence: `arch_contract_ops` SUCCESS, `operations=5`.

### 5.1 API operations (`API-ArchitectureComparison`, `openapi 3.1`, version `0.1.0`)

| # | operationId | Method + path | Purpose | Fake-only rule |
|---|---|---|---|---|
| 1 | `getPhaseState` | `GET /phases/{phase_id}/state` | Read one phase’s stage + verdict + gates. | — |
| 2 | `transitionPhase` | `POST /phase-transitions` | Request a lifecycle transition (table-guarded, §5.2). | — |
| 3 | `dispatchTask` | `POST /tasks/{task_id}/dispatch` → `202` | Accept a dispatch; completion is a separate record (AC-25: “0 completions observed at return”). | — |
| 4 | `enqueueSync` | `POST /sync/enqueue` → `202` | Durably enqueue a tracker sync intent (offline-safe). | Tracker transport is fake; enqueue itself must work offline. |
| 5 | `listDeadLetter` | `GET /sync/dead-letter` | List DLQ entries (redrive input). | — |

NFR bindings (budgets): worker cap 30 max — check `arch_nfr_budget` SUCCESS `checked=2, within_budget=true, breaches=[], unmeasured=[]` for `worker_cap ≤ 30`, `backoff_cap_s ≤ 5`. p95 targets for dispatch/ready-order stay per `docs/architecture/blueprint.md` §4 (NFR-P6 `p95<100 ms` ready-order) and are **measured by A08/A10, not asserted here**.

### 5.2 State-machine contract (normative transition tables)

**Task lifecycle** — exactly `swarm/taskstore.py:26-59`. Every value below is quoted from that file; the table is the contract, not prose:

```
TaskState ∈ {CREATED, VALIDATED, PLANNED, CLAIMED, IN_PROGRESS, IN_REVIEW,
             CHANGES_REQUESTED, APPROVED, DONE, BLOCKED, FAILED, RETRY,
             ESCALATED, CANCELLED}
LEGAL_TRANSITIONS:
  CREATED           → {VALIDATED, CANCELLED}
  VALIDATED         → {PLANNED, BLOCKED, CANCELLED}
  PLANNED           → {CLAIMED, BLOCKED, FAILED, CANCELLED}
  CLAIMED           → {IN_PROGRESS, FAILED, RETRY, BLOCKED, CANCELLED}
  IN_PROGRESS       → {IN_REVIEW, FAILED, RETRY, BLOCKED, CANCELLED}
  IN_REVIEW         → {APPROVED, CHANGES_REQUESTED, FAILED, CANCELLED}
  CHANGES_REQUESTED → {IN_PROGRESS, ESCALATED, CANCELLED}
  APPROVED          → {DONE, CANCELLED}
  BLOCKED           → {PLANNED, CLAIMED, CANCELLED, ESCALATED}
  FAILED            → {RETRY, ESCALATED, CANCELLED}
  RETRY             → {PLANNED, CLAIMED, ESCALATED}
  ESCALATED         → {PLANNED, CANCELLED}
  DONE → {}      CANCELLED → {}
```

Guards (all in `taskstore.py:994-1042`): `CHANGES_REQUESTED` increments `rework_loops` and stashes prior result/meta evidence in one transaction; `loops > MAX_REWORK_LOOPS (2)` collapses to `ESCALATED`; `CLAIMED` from `PLANNED|RETRY|BLOCKED` increments `attempt` and refuses past `max_attempts` (default 3) with `E-TIMEOUT`; `APPROVED` is fail-closed on signing-key config, missing gates (`E-POLICY`) and unaccepted gate results (`E-POLICY`). Illegal transition → `SwarmError(E_CONTRACT, "illegal transition …")` (`taskstore.py:991-993`).

**Phase lifecycle** (GSD discuss/plan/execute + verdict + hold overlay) is in `docs/design-orchestration-SPE-38340.md` §4.3 and is not duplicated here; the phase table is the contract for `transitionPhase` phase variants.

### 5.3 Port contract (the 16 methods — normative shape, doc reference)

The port surface is `SchedulerPorts` (`ports.rs:599-722`). No new method is added by this design. For A05/A06 convenience the surface groups as:

| Group | Methods | Thread | Failure semantics |
|---|---|---|---|
| Pass | `begin_pass`, `observe_pass` | loop | `Err` stops the loop, re-raised with original type (`ports.rs:595-596`) |
| Resolve | `agent_for`, `claims_for` | loop | `Ok(None)` = known-absent (block task / no claim); `Err` = fault |
| Lease | `lease` | loop | `Ok(None)` = leases off for the whole run (loop may stop calling) |
| Submit/worker | `submit`, `worker`, `internal_failure` | submit+complete = loop; `worker` = pool (blocks whole session) | Worker never raises; `Err` from `worker` is `BaseException` that escaped Python’s own catch (`ports.rs:660-664`) |
| Complete | `complete`, `set_retry_after`, `random_unit` | loop | Exactly-once per dispatch (`ports.rs:683-684`); `random_unit ∈ [-1, 1]` |
| Cancel | `cancel_state`, `cancel_begin`, `kill`, `log` | loop | Poll at top of every iteration + each `wait_tick_s` (`ports.rs:698-700`) |
| Block | `block` | loop | Reason ≤ 500 chars (`ports.rs:42-43`); task never returns this run |

Descriptor validation on the boundary (proposed, machine-checkable): every port argument is a plain data type (`ReadyRow`, `AgentInfo`, `ClaimSpec`, `LeaseVerdict`, `PassInfo`, `Submission`, `WorkerDone`, `Completion`, `RunConfig` — `ports.rs:50-519`); ids always come from the latest `begin_pass` rows, never from a stale snapshot (`ports.rs:597-598`); `RunConfig` numbers are range-checked (`ports.rs:525-527`, `MAX_SECONDS = 1e9` at `ports.rs:40`).

### 5.4 Dead-letter contract

```jsonc
{
  "dead_letter_entry": {
    "dead_letter_id": "string (stable, idempotency-derived)",
    "task_id": "string",
    "correlation_id": "string",
    "error_code": "E-TIMEOUT | E-DEP | E-CAPACITY | E-CONTRACT | E-INPUT | E-POLICY | E-INTERNAL",
    "classification": "transient | permanent | decision",
    "attempts": "integer ≥ 1 (final attempt that landed it)",
    "first_failed_at": "number (epoch s)",
    "last_failed_at": "number",
    "backoff_schedule": "array<number> (delays actually used, each ≤ cap)",
    "redrive": { "state": "held | redriven | resolved", "by": "A01 | user", "at": "number|null" }
  }
}
```

### 5.5 Typed validated boundary rules (normative)

1. Unknown or malformed envelope → `E-CONTRACT`, nothing is dispatched (`envelope.py:116-135`).
2. Out-of-range numeric config (e.g. `max_concurrent < 1`) → `E-INPUT` with wire message, detail names the *rule*, never echoes task ids/paths (`ports.rs:30-32, 75-76`).
3. Missing tool/capability name → never defaulted: `Ok(None)` from `agent_for` blocks the task with `BLOCK_REASON_NO_AGENT = "no agent for capability"` (`ports.rs:46, 618-620`).
4. Every multi-step state change is one transaction: read + legality + writes under `BEGIN IMMEDIATE` with an audit row (`taskstore.py:985-986`).

---

## 6. Error classification + bounded backoff + dead-letter

### 6.1 Classification (as landed, with one argued correction)

Landed (`swarm/retry.py:14-15, 42-58`):

| Class | Codes | Retry? |
|---|---|---|
| transient | `E-TIMEOUT`, `E-DEP`, `E-CAPACITY`, `E-CONTRACT` | yes, bounded |
| permanent | `E-INPUT`, `E-POLICY` | **never**, whatever the message says (`retry.py:45`) |
| other (`E-INTERNAL`, unknown, `None`) | message decides: rate-limit / overload markers (`detect_rate_limit`, `limiter.py:39-56`) are transient | only on a marker |

Plus the taxonomy guardrails (`swarm/errors.py:13-33`): `E-INPUT` invalid input, `E-TIMEOUT` budget/lease/SLA, `E-DEP` upstream unavailable, `E-CAPACITY` overload/WIP cap, `E-CONTRACT` output rejected, `E-POLICY` fail-closed, `E-INTERNAL` unexpected fault.

**(Proposed) decision class.** The landed table lumps all `E-CONTRACT` into transient, but an *illegal state transition* or a *contradiction* (B1–B5 — `docs/critique-SPE-38339.md`) is not a transient fault: retrying it can never succeed and it must park for a user ruling (`human_needed` / `blocked_hold`). The design therefore splits `E-CONTRACT` by *cause*:

| Class | Codes / causes | Handling |
|---|---|---|
| transient | `E-TIMEOUT`, `E-DEP`, `E-CAPACITY`, contract *validation* failures that a re-fetch can heal | bounded retry → DLQ on exhaustion |
| permanent | `E-INPUT`, `E-POLICY` | no retry → DLQ `held`, escalate (`E-POLICY` → A10/A01) |
| **decision** | `E-CONTRACT` illegal-transition / contradiction, `gaps_found` verifier evidence, `E-POLICY` gate missing | no retry → park in `human_needed` / `blocked_hold`, user ruling required (`docs/dc-G-orchestration-SPE-38340.md` §4.3: `* → human_needed`, `* → blocked_hold`) |

This is an **ADR** (§11, ADR-003), L2, because it narrows retry behaviour without changing a wire code; the wire code stays `E-CONTRACT`, the classification gains a `cause` field. A05 implements it as `cause ∈ {validation, transition, contradiction}` on the DLQ entry (`§5.4`).

### 6.2 Bounded backoff (exact formulas)

| Ladder | Formula | Bounds | Source |
|---|---|---|---|
| scheduler retry | `min(cap, base·2^(n-1)) · (1 ± jitter)` then clamped to cap | `base 0.1 s`, `cap 5 s` (defaults `retry.py:11-12`), `jitter 0.25` (`retry.py:23`), env-overridable `SWARM_RETRY_BASE_S / SWARM_RETRY_CAP_S` (`retry.py:31-32`) | `swarm/retry.py:23-39` |
| E-DEP ladder | `1 s · 2^n + jitter`, saturated | `cap 60 s`, `jitter 0.3 s`; attempt 10 ⇒ `60 ≤ delay ≤ 60.3` | `_exp_backoff_err` (`errors.py:66-70`); `tests/test_backoff_cap.py:9-12` |
| Retry-After hold | server value, growth held back | hard cap `120 s` (`SWARM_RETRY_CAP_S`) | `limiter.py:17, 84-85` |
| AIMD cooldown | after halving, successes add one slot back *once cooldown elapses* | default `cooldown 60 s` (`ports.rs:507`, `limiter.py:16`) | `limiter.py:3-5, 88-128` |

Determinism: every ladder takes an injected RNG (`rng.uniform`, `retry.py:19-20`; `random_unit` port, `ports.rs:693-696`) and a seeded `random.Random` reproduces the schedule exactly (`retry.py:26-27`). No ladder sleeps in tests: `FakeClock` drives cooldowns (`limiter.py:3-5`).

### 6.3 Dead-letter (normative)

1. Transient + attempts exhausted → DLQ entry (`classification: transient`), task stays `FAILED` (retry ladder upstream: `FAILED → RETRY → ESCALATED` — `taskstore.py:54-55`).
2. Permanent → DLQ (`held`), task `FAILED`/`ESCALATED` (rework cap `MAX_REWORK_LOOPS = 2`, `taskstore.py:60`).
3. Decision → **not** DLQ-redriveable until a ruling is recorded; park in `blocked_hold` / `human_needed` with `ruling_ref`. Auto-redrive is forbidden.
4. Redrive is an explicit `enqueueSync` with the same stable `dead_letter_id` (idempotent, §8); a redriven entry that fails again gets a *new* attempt count, never a duplicate row.

---

## 7. Concurrency limits + circuit breaking

| Mechanism | Value | Source |
|---|---|---|
| Worker cap / ceiling | `WORKER_CAP = 30`, `WORKER_CEILING = 32`; 31st request → `429 E-CAPACITY`, 0 extra spawns | `graph_quality.py:85-86`; AC-23 (`docs/acceptance.md:28`), AC-59/70 caps |
| Per-agent slots | `max_parallel`, `0` coerced to `1` | `ready_frontier.rs:18` via `INVENTORY.md` §4; `docs/design-graph-SPE-38341.md` §4.2 |
| AIMD halving | rate-limit ⇒ cap halved (never below `min_n`), cooldown starts | `limiter.py:3-4` |
| Failure circuit | `SWARM_FAIL_STREAK` default `3`, `0` = off; `N` consecutive failures of **distinct** tasks ⇒ halve once; any success resets | `errors.py:206`; `ports.rs:472-474, 506-507`; `rust_scheduler.py:300-303` |
| Peer circuit | per-recipient open circuit; local tool calls never consult it | `acp.py:5, 123, 175-182, 295` |
| Lock order | no Rust lock / channel guard held across a port call | `ports.rs:20-22` |
| Cancel | poll each iteration + each `wait_tick_s`; cooperative grace → SIGTERM → SIGKILL via drain plan | `ports.rs:698-716`; `sched_drain_plan` (`_native.pyi:58`) |

Sequential fallback reuses the same caps with `effective_parallelism = 1`: the AIMD/circuit machinery is bypassed, not removed — a fallback run that trips the failure circuit mid-wave must still report it.

---

## 8. Dependency injection + fake adapters

- Construct everything: `Core(ports, limiter, classifier, clock, rng, config)`. No module-level singletons for collaborators; env (`SWARM_*`) is read by the *Python adapter* at construction, never by Rust (`ports.rs:455`: “the environment … is read by the Python helpers, never by Rust”).
- Clock: `limiter.py` takes a `Clock`; tests use `FakeClock`.
- RNG: `retry.backoff_s(rng=random.Random(seed))`; Rust side `random_unit()` scripted per test.
- Golden rule for fakes: a fake implements the **same** port trait (`ports.rs:588-589`) and refuses nothing the real adapter accepts (same `E-INPUT` on bad config). The fake tracker hard-denies any non-fake base URL (§5.1).
- Idempotency: stable keys, not dedup-by-optimism. `idempotency_key` derivation (`docs/design-orchestration-SPE-38340.md` §3.3) + `graph_edges UNIQUE (correlation_id, node_id, predecessor_id)` (`taskstore.py:93-98`) make a repeated dispatch/edge a no-op (T-IDEM-* in §10).

---

## 9. Verified test / lint / typecheck commands (INVENTORY + CI)

Every command below was verified by reading `INVENTORY.md` §2, `.github/workflows/ci.yml`, `scripts/ci_scheduler_rust.sh`, `pyproject.toml`, or `mypy.ini` (mentioned in `INVENTORY.md` §2). **Per assignment, full suites were not run** — this section is the command list the implementer runs, not a run. Versions: `python 3.12.3`, `rust 1.98.1`, `bun 1.4.0`, `ruff 0.15.18`, `maturin 1.15.0` (`INVENTORY.md` §2).

| # | Command | What it proves | Source |
|---|---|---|---|
| 1 | `ruff check .` | Python lint (`line-length 140`, `E,F,W`; `E501` ignored) — 0 findings | `ci.yml:45-46`; `pyproject.toml:24-30`; `INVENTORY.md` §2 |
| 2 | `python3 -m pytest` | Full Python suite (`testpaths=["tests"]`, `-q`) | `ci.yml:47-48`; `pyproject.toml:32-34` |
| 3 | `cargo fmt --all --check` | Rust formatting | `ci.yml:211-212` (clippy job) |
| 4 | `cargo clippy --workspace --all-targets --locked -- -D warnings` | Rust lint, warnings as errors | `ci.yml:213-214` |
| 5 | `cargo fetch --locked` then `cargo build --workspace --locked` | Locked build | `ci.yml:93-96` |
| 6 | `cargo test --workspace --locked --no-fail-fast` | Full Rust suite (1624 test entries listed, `INVENTORY.md` §7 — listed, not executed there) | `ci.yml:178-179` |
| 7 | `cargo test -p gsd-sched-core -p gsd-scheduler -p gsd-python --locked --no-fail-fast` | Scheduler-core legs | `scripts/ci_scheduler_rust.sh:259-260` |
| 8 | `python3 -m mypy --strict swarm/ tests/typecheck/test_scheduler_ports_typecheck.py` | Python types incl. port typecheck | `scripts/ci_scheduler_rust.sh:263-264` |
| 9 | `python3 -m mypy.stubtest swarm._native` | Stub ↔ extension honesty (AC-34: 18 stub entries) | `scripts/ci_scheduler_rust.sh:264`; `docs/acceptance.md:39` |
| 10 | `python3 -m mypy` | Same as #8 over `files = swarm` (`mypy.ini`) | `INVENTORY.md` §2 |
| 11 | `bun test tests/ts` | Root TS suite | `ci.yml:65-66` |
| 12 | `bun run test` (in `omp/`) | OMP extension suite | `ci.yml:67-69` |
| 13 | `GSD_ENGINE_BIN=<abs path> scripts/ci_scheduler_rust.sh scheduler-out` | Scheduler gate: binding, threads, stubs, vectors, continuous+ports on both engines, mypy, clippy, stubtest. Requires compiled `run_scheduler`, `SCHEDULER_CONTINUOUS_SHA256` | `ci.yml:270-280`; `ci_scheduler_rust.sh:1-16` |
| 14 | `scripts/ci_parity_rust.sh parity-out` | Python-truth vs Rust-host parity, fault tests, budget record | `ci.yml:234-235` |
| 15 | `python3 -m swarm.tool_registry check` and `python3 -m swarm.tool_registry docs --check` and `python3 -m swarm.tool_registry gen-tests --check` | Tool registry + docs + contract-test drift | `ci.yml:53-58` |
| 16 | `python3 -m swarm.skill_registry check` and `python3 -m swarm.skill_registry docs --check` | Skill registry + docs drift | `ci.yml:59-62` |
| 17 | `python3 scripts/build_agents.py --check` | Generated `omp/agents/` + `omp/skills/` match manifest | `ci.yml:49-50` |
| 18 | `python3 scripts/_check_pins.py` | Pin hashes | `ci.yml:51-52` |
| 19 | `python3 scripts/_registry_intake.py --check` | Registry intake | `ci.yml:63-64` |
| 20 | `node transpile.mjs`, `node serve.mjs` (in `web/`) | Web build/serve | `web/package.json` (`INVENTORY.md` §2) |

Cheap sanity probes (already run in `INVENTORY.md` §7, for the implementer’s first minute): `python3 -c 'import swarm._native'` (25 public names), `cargo metadata --no-deps`, `cargo test --workspace --locked -- --list`.

---

## 10. Unit test plan (acceptance checklist)

Fakes only. No live Linear/Notion. Every test is deterministic: `FakeClock`, scripted RNG, in-memory port fake. Names are stable; A08 runs them. “Maps to” cites the acceptance-credit this test earns.

### 10.1 Illegal transitions (T-TRANS-*)

| Test | Given / When / Then | Maps to |
|---|---|---|
| T-TRANS-1 every legal edge succeeds | For each `(from,to) ∈ LEGAL_TRANSITIONS`, `transition()` succeeds and one audit row is written. | `taskstore.py:44-59, 1060+` |
| T-TRANS-2 every illegal edge refused | For every `(from,to) ∉ table`, `transition()` raises `E-CONTRACT "illegal transition …"`, state unchanged, no audit row. At least: `CREATED → CLAIMED`, `DONE → RETRY`, `CANCELLED → PLANNED`, `IN_REVIEW → DONE` (skips APPROVED). | `taskstore.py:990-993` |
| T-TRANS-3 `APPROVED` fail-closed | `APPROVED` without passing gates / with unaccepted gate / without signing key ⇒ `E-POLICY`, state unchanged. | `taskstore.py:1025-1042`; AC-6 |
| T-TRANS-4 rework cap | 3rd `CHANGES_REQUESTED` (MAX_REWORK_LOOPS=2) collapses to `ESCALATED`. | `taskstore.py:60, 1008-1015` |
| T-TRANS-5 attempt cap | `CLAIMED` when `attempt == max_attempts` ⇒ `E-TIMEOUT`, no increment. | `taskstore.py:1020-1023` |
| T-TRANS-6 blocked/retry paths | `FAILED → RETRY → PLANNED|CLAIMED`, `BLOCKED → PLANNED|CLAIMED|ESCALATED`. | `taskstore.py:53-56` |

### 10.2 Idempotency (T-IDEM-*)

| Test | Given / When / Then |
|---|---|
| T-IDEM-1 dispatch replay | Same `task_id` + `correlation_id` dispatched twice ⇒ one dispatch record, one completion record per AC-25/26 semantics. |
| T-IDEM-2 enqueue replay | Same `dead_letter_id` / idempotency key enqueued twice ⇒ one queue row. |
| T-IDEM-3 edge replay | Same `(correlation_id, node_id, predecessor_id)` inserted twice ⇒ `UNIQUE` holds, one row, no error (`taskstore.py:93-98`). |
| T-IDEM-4 idempotency-key derivation | Same inputs ⇒ same key; different payload ⇒ different key (fake `idempotency_key` against golden vectors). |

### 10.3 Retry / backoff / timeout (T-RETRY-*)

| Test | Given / When / Then |
|---|---|
| T-RETRY-1 transient retries | `E-TIMEOUT` task fails 2× then succeeds ⇒ `FAILED → RETRY → CLAIMED → … → IN_REVIEW`, `attempt` = 3. |
| T-RETRY-2 permanent never retries | `E-INPUT` / `E-POLICY` ⇒ zero retries, straight to DLQ (`held`) + `ESCALATED` path. (`retry.py:54-55`) |
| T-RETRY-3 schedule bounds | `backoff_s(n, base=0.1, cap=5, jitter=0.25, rng=Random(seed))` ∈ `[0, cap]` for `n = 1..20`; zero jitter ⇒ exact powers of two clamped (`retry.py:35-39`). |
| T-RETRY-4 E-DEP saturation | `backoff_s(10)` for the error ladder ⇒ `60 ≤ delay ≤ 60.3` (`test_backoff_cap.py:9-12` mirror). |
| T-RETRY-5 `E-CONTRACT` split (ADR-003) | validation cause ⇒ transient retry; illegal-transition cause ⇒ decision park, no retry. |
| T-RETRY-6 timeout budget | A dispatch exceeding its budget ⇒ `E-TIMEOUT`; lease/budget exceeded never classified transient-past-cap. |
| T-RETRY-7 jitter validation | `jitter ∉ [0,1]` or `base ≤ 0` / `cap ≤ 0` ⇒ `ValueError` (`retry.py:29-34`). |

### 10.4 Dead-letter (T-DLQ-*)

| Test | Given / When / Then |
|---|---|
| T-DLQ-1 exhaustion lands in DLQ | Transient fails `max_attempts` ⇒ one DLQ entry, `classification: transient`, attempts = max, task `FAILED`. |
| T-DLQ-2 permanent lands held | `E-INPUT` ⇒ DLQ `held`, `redrive.state: held`. |
| T-DLQ-3 decision not redriveable | `E-CONTRACT`/transition cause ⇒ no DLQ redrive until `ruling_ref` recorded; redrive attempt before ruling ⇒ refused. |
| T-DLQ-4 idempotent redrive | Redrive enqueues by stable `dead_letter_id`; double redrive ⇒ one redrive request. |
| T-DLQ-5 list contract | `listDeadLetter` returns the entry schema (§5.4); empty ⇒ empty list, never null/404. |

### 10.5 Descriptor / boundary validation (T-DESC-*)

| Test | Given / When / Then |
|---|---|
| T-DESC-1 envelope | Missing field / bad priority / bad risk_class / non-namespaced type / non-object payload ⇒ `E-CONTRACT`, no dispatch (`envelope.py:116-135`). |
| T-DESC-2 signature gate | `type` in `SIGNATURE_REQUIRED` without `sig` ⇒ `E-POLICY` (`envelope.py:136-137`). |
| T-DESC-3 config ranges | `max_concurrent < 1`, `cooldown_s < 0`/∞ ⇒ `E-INPUT` wire message, no echoed ids (`ports.rs:81, 525-527`). |
| T-DESC-4 tool descriptor | Tool node missing `name/owner/purpose/input_schema/output_schema/errors/timeout_s/idempotency/permitted_callers/entrypoint/handler` ⇒ refused (`tool.contract.v1.json`); `DESTRUCTIVE/EXTERNAL` tier refused. |
| T-DESC-5 unknown agent | `agent_for → Ok(None)` ⇒ task `BLOCKED` with `BLOCK_REASON_NO_AGENT`, slot consumed (`ports.rs:618-620`). |
| T-DESC-6 oversized reason | `block(reason)` with >500 chars ⇒ truncated to `BLOCK_REASON_MAX_CHARS` (`ports.rs:42-43`). |

### 10.6 Fake adapters (T-FAKE-*)

| Test | Given / When / Then |
|---|---|
| T-FAKE-1 fake parity | Fake `SchedulerPorts` returns the scripted `ReadyRow[]`; core order = Python oracle order for the canonical 10-node fixture. |
| T-FAKE-2 fake clock | `AimdLimiter` cooldown (60 s) driven by `FakeClock`; no wall-sleep in any test. |
| T-FAKE-3 fake tracker hard-deny | Any non-fake base URL ⇒ refuse; fakes only return `TBD-…` IDs. |
| T-FAKE-4 scripted RNG | `random_unit` returns the scripted sequence; backoff schedule reproduces byte-for-byte across runs. |

### 10.7 Concurrency + circuit (T-CONC-*) and sequential fallback (T-SEQ-*)

| Test | Given / When / Then |
|---|---|
| T-CONC-1 worker cap | 30 active, request 31st ⇒ `429 E-CAPACITY`, 0 extra spawns (AC-23). |
| T-CONC-2 AIMD halving | Rate-limit signal ⇒ cap halved, cooldown 60 s; after cooldown each success adds one slot, capped (T-fake-clock). |
| T-CONC-3 failure circuit | 3 distinct-task failures ⇒ halve once; success resets the streak; `SWARM_FAIL_STREAK=0` ⇒ off (`ports.rs:472, 506`). |
| T-CONC-4 peer circuit | Open circuit ⇒ `circuit open` refusal for that recipient; local tool call bypasses (`acp.py:175-182`). |
| T-SEQ-1 fallback order | `SWARM_PURE_PYTHON=1` / native import fails ⇒ canonical order `start, extra1, n1..n4, m1..m3, last`, makespan 4.0, one node at a time. |
| T-SEQ-2 fallback witnessed | Fallback run writes the fallback witness line (engine recorded); silent fallback ⇒ test fails. |
| T-SEQ-3 starvation | Greedy frontier with saturated slots ⇒ waiting task promoted by aging after threshold (3 waves default), never starved forever. |
| T-SEQ-4 empty graph | Zero dispatchable rows ⇒ empty pass, no error, no spin. |

---

## 11. ADRs (proposed — L2, record via `scripts/arch_adr.py`)

| ADR | Decision | Rationale | Alternatives rejected | Supersedes |
|---|---|---|---|---|
| ADR-0012 (proposed) | Ports-and-adapters for the scheduler/orchestration core; sequential fallback as degraded mode (not a second scheduler). | §3: wins 12 criteria, loses none decisively; seam already landed (`gsd-sched-core`); Python stays oracle/default (D4). | Monolith-inline (violates DIP, ISP — no substitution seam); Generic framework (KISS/YAGNI failure, second contract to govern). | none |
| ADR-0013 (proposed) | Error-classification `decision` cause for `E-CONTRACT` illegal-transition/contradiction + `gaps_found`; decision errors park, never retry. | Landed `TRANSIENT_CODES` includes all `E-CONTRACT` (`retry.py:14`) — retrying a contradiction can never succeed; B1–B5 must park for user ruling. | Keep all `E-CONTRACT` transient (violates “non-transient never retry”); add a new wire code (breaking — needs major bump + consumer acks, L3). | none |
| ADR-0014 (proposed) | No new crate, no DSL, no plugin registry; graph tool-gap closes at graph-validation level only (per SPE-38341 §4.4). | KISS/YAGNI; `TaskNode` stays `(id, files, dependencies, complexity, priority, duration)` so Rust DAG and Python planner stay mechanically identical. | Widen `TaskNode` with a tool field (schema change — needs A03+user approval, critique tool/agent row); build a framework SDK (C). | none |

Breaking test: none of the above changes a wire format or an operation signature, so no major bump and no L3 human-approval gate is triggered. If ADR-0013 ever adds a wire code, it becomes L3 (`human-approval: breaking_contract`).

---

## 12. Threat model (STRIDE sketch, A10 co-sign pending)

| Boundary | STRIDE | Threat | Mitigation (where) |
|---|---|---|---|
| adapter ↔ core | T | Tampered port answer (fake vs real divergence) | Port answer validation (§5.3); parity harness vs Python oracle; witness every child engine (`ci_scheduler_rust.sh`) |
| tracker-adapter (fake) → outside | S/I | Accidental live Linear/Notion write | Fakes only; hard-deny non-fake base URL (T-FAKE-3) |
| `state-store` | T | Concurrent-writer corruption | `BEGIN IMMEDIATE` + audit row (`taskstore.py:985-986`); atomic temp+rename publish |
| retry ladder | D | Retry storm hammering a down dependency | Bounded ladder (§6.2) + AIMD halving + failure circuit + peer circuit (§7) |
| DLQ redrive | T | Auto-redrive of a decision error (user ruling bypass) | Decision class not redriveable until `ruling_ref` (T-DLQ-3) |
| worker pool | D | Unbounded spawning | Cap 30 / ceiling 32, 31st ⇒ `E-CAPACITY` (T-CONC-1) |
| native bridge | T/I | Rust↔Python silent drift; secret-span leak | 18-symbol stub honesty (`stubtest`, AC-34/35); 100-case + 376-entry redaction parity, 0 leaks (blueprint §7) |
| port call | D | GIL-held deadlock (Rust lock across port) | Lock-order rule (`ports.rs:20-22`); T-CONC tests under contention |

---

## 13. Migration / cutover (no flag day, no live writes)

1. **Land the seam only.** `gsd-sched-core` + `swarm/rust_scheduler.py` are the strangler seam (already in-tree). Python stays default (`GSD_ENGINE=python`, `SWARM_PURE_PYTHON=1`); Rust opt-in per route on parity evidence (D4).
2. **Per-route flips, never a flag day.** Each route flips only after its parity gate is green (§9 #13/#14) and one approval record exists (AC-6/AC-28 pattern). Rollback = one selector resolution step (AC-38).
3. **Graph model lands at validation level.** SPE-38341 node/edge validation runs *before* `plan_graph`; `TaskNode` is unchanged, so `graph_edges` rows (today 0) need no backfill. Existing rows read as `dependency/hard`.
4. **DLQ starts empty.** Cutover never auto-redrives a DLQ entry created before the flip; redrive is an explicit user/A01 action.
5. **Fakes until TBD → real IDs.** Tracker adapters stay fake (`TBD-…`) until the user supplies real Linear/Notion IDs and approves the first live write beyond fakes.

---

## 14. Open questions / user decisions

1. **ADR-0013 `cause` field name + values** — `cause ∈ {validation, transition, contradiction}` is proposed; A05/A08 to confirm against the DLQ schema before implementers CLAIM tasks. (Design choice, user can veto.)
2. **Starve threshold** — default 3 waves (backward-compatible with critique D-optimisation “baseline as record”). Promoting a different threshold is a tuning change, not an architecture change.
3. **B1–B5 rulings** — unchanged from `docs/critique-SPE-38339.md`: milestone status, 14.4 hold, COMPLETE-relabel standard, 14.7 de-facto edge, Phase 15 user-confirmation. This design makes the contradictions *representable and pausable*; it does not rule on them.
4. **Learning** — stays disabled. Any `learning: true` graph is refused (`docs/design-graph-SPE-38341.md` §4.2) until a ratified policy file exists (critique session-policy row).

---

## 15. Evidence trace

| Claim in this doc | Evidence |
|---|---|
| Toolchain versions; commands §9 #1–20 | `INVENTORY.md` §2, §7; `.github/workflows/ci.yml:45-69, 93-96, 178-179, 211-214, 234-235, 270-280`; `scripts/ci_scheduler_rust.sh:259-264`; `pyproject.toml:24-34` |
| `TaskNode` = `(id, files, dependencies, complexity, priority, duration)`, no tool field | `swarm/graph_planner.py:73-82` (via `INVENTORY.md` §4, `docs/design-graph-SPE-38341.md` §1.1) |
| `TaskState` + `LEGAL_TRANSITIONS` table (§5.2 verbatim) | `swarm/taskstore.py:26-59` |
| `transition()` guards: E-CONTRACT / E-POLICY / rework / attempt caps | `swarm/taskstore.py:981-1045` |
| Envelope validation refusals | `swarm/envelope.py:114-137` |
| `TRANSIENT_CODES` / `PERMANENT_CODES`, `backoff_s` formula, `is_transient` fallback | `swarm/retry.py:11-58` |
| E-DEP 60 s cap, 0.3 s jitter, attempt-10 saturation | `tests/test_backoff_cap.py:1-12`; `swarm/errors.py:66-70` |
| Retry-After 120 s cap; AIMD half + cooldown; `FakeClock` | `swarm/limiter.py:3-17, 84-128` |
| Worker cap 30 / ceiling 32; 429 E-CAPACITY | `swarm/graph_quality.py:85-86`; `docs/acceptance.md` AC-23 |
| `SchedulerPorts` 16 methods; thread rules; lock-order; `dyn SchedulerPorts`; fakes in tests | `crates/gsd-sched-core/src/ports.rs:1-7, 20-32, 40-46, 586-722` |
| `RunConfig` range checks; `MAX_SECONDS = 1e9` | `crates/gsd-sched-core/src/ports.rs:40, 525-527` |
| Failure circuit default 3, 0 = off, distinct tasks, halve once | `swarm/errors.py:206`; `ports.rs:472-474, 506-507`; `swarm/rust_scheduler.py:300-303` |
| Peer circuit `acp.py` local bypass | `swarm/acp.py:5, 123, 175-182` |
| C4 fitness: 8 containers, 11 relations, max fan-out 3, budget 4 | `arch_c4_fanout` SUCCESS (this run) |
| Cycle check: acyclic, 8 nodes | `arch_cycle_check` SUCCESS (this run) |
| Layer violations: 0, 15 edges checked | `arch_layer_violations` SUCCESS (this run) |
| Contract ops = 5; verdict = pass (no findings) | `arch_contract_ops`, `shared_verdict` SUCCESS (this run) |
| Semver 0.0.0→0.1.0 additive minor, compliant, breaking=false | `arch_semver_bump` SUCCESS (this run) |
| NFR budgets (worker_cap/backoff_cap) within budget | `arch_nfr_budget` SUCCESS `within_budget=true` (this run) |
| Canonical order, 0 cycles, makespan 4.0; starvation/deadlock risks | `INVENTORY.md` §4; `docs/critique-SPE-38339.md` optimisation row; `docs/design-graph-SPE-38341.md` §1.3 |
| 4-copy SDLC classifier debt (must not grow) | `INVENTORY.md` §4 |
| 36-operation enterprise contract precedent (contract-first, semver-minor, non-breaking) | `docs/architecture/blueprint.md` §3 |

*Design doc only. Gates/formatters skipped per assignment. No code changed.*
