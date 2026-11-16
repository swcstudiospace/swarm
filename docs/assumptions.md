# n6 Ambiguity Resolutions (Defaults) — GSD-Core Contracts, Decomposition, Execution Model, Pool Sizing, Result Aggregation

**Context:** Multithreading Subagent module (wave 1). Follows ultrathink plan from n7 synthesize (N7 — Ordered wave execution plan, Refs SPE-18070; n7-synthesize-parallel-execution-waves SPE-9183). Captures n6 (risks-tradeoffs.md SPE-18069) ambiguity resolutions with defaults. Sources (read first, authoritative, no relitigation):
- plan/inventory.md (N1, wave 1 discovery, SPE-18064)
- plan/decompose.md (N2, AI-multithreading upgrade, SPE-18068)
- plan/risks-tradeoffs.md (N6)
- plan/execution-waves.md (N7)
- AGENTS.md (working assumptions, n6 defaults adoption)
- .planning/phases/14.4-rust-scheduler-runtime-and-default-flip/ (rust scheduler, ultrathink graph `ut-mvvownjp-ff01b275`)
- Notion graph links (from STATE.md/WAVES.md/14.4-PLAN.md): https://app.notion.com/p/40abc1a0c7ae81b588b7e01197a8d77c?pvs=204 (and linked SPEs/graphs)
- Refs: SPE-33068 n6.8 etc., SPE-13819 n3.6-7, SPE-13575

No contradictions with plans. Python truth baseline preserved until parity gates. Cap ≤30 threads/session binding.

## GSD-Core Contracts (defaults and resolutions)
- Strangler seam (`gsd-adapter`): legacy routes gradually to GSD-Core; frozen TS/omp surface; `GSD_ENGINE` routing (python default, rust opt-in per-route); join-affinity guard → E-CONTRACT on mixed engines within session.
- Contracts frozen per n4/n6: argv/exit/JSON shapes identical; 100KB input cap; 0/1/2 exit; no ExtensionAPI versioning (default frozen).
- Error taxonomy (port of swarm/errors.py): E-INPUT, E-TIMEOUT, E-DEP, E-CAPACITY, E-CONTRACT, E-POLICY (fail-closed), E-INTERNAL. 1:1 map in gsd-failure; typed at seam.
- Idempotency key: (task-id, attempt) or (stage-id, join-seq); preserved across failover.
- No new workflowz language/DSL; DAG dispatch policy only (from inventory q1).
- Binding: no assumptions on Wasmer pins, FS/net policy, SDK clients, baselines, snapshot semantics, Cargo.lock (see n5 may-NOT list in plans).
- Other defaults: per-route parity (mismatch = Rust bug, revert flag, fixture); serde budget sized for full core (q3 in inventory); health + parity gates before flip.

## Decomposition Rules (defaults)
- Generic keyword based for independent/ordered (ports swarm_run.py force-parallel + reconcile + dispatchable + select_batch; U2 in decompose.md).
- U2.1 DAG builder: WorkflowDef/Task/Edge; cycle → E-DEP.
- U2.2 Independence detector: dependency-satisfied + claims-overlap exclusion + per-agent max_parallel; pure ready-frontier.
- U2.3 Dispatcher: priority (P0) → DAG-depth → creation; global cap + per-agent; AIMD; no mixed matrix/nexus mid-session (G-no-mixed-joins).
- Task data shape (q1 resolution): structured (id, description, phase?, priority?, deps list).
- Other: no 31st agent; disjoint blast radii → parallel lanes (A01); >1 independent acceptance → splittable.

## Execution Model
- Rust thread pools for parallel (gsd-pool + gsd-scheduler; std threads in sched-core per 14.4 recovery; matrix flat fan-out default).
- Matrix = flat fan-out (U2.4); Nexus = staged fan-out/fan-in + barrier (U2.5, per WorkflowDef.policy).
- Persistent pool refilled on completion; order by priority/DAG-depth/creation.
- Session = one GSD-Core session ≤30 chat threads (A01-A30 isolates) under SessionId/WorkflowDef/Budget.
- No mixed-joins; strangler (R-S) with read-only → plan/assign → gates → scheduler policy order; Python truth until green parity.
- Other defaults: G-compile-first (build before design); G-serde-budget; G-python-truth; G-strangler-order.

## Pool Sizing
- Pool size 30 = enforced configurable maximum (session cap 30; `--max-parallel` default 4, ceiling 32 in Python port; per-agent max_parallel; PoolConfig{max_threads:30} → Exhausted ≡ E-CAPACITY).
- WarmPool reuse per SubagentType; eviction grace.
- Enforced: semaphore acquire, AIMD degrade on rate-limit/fail-streak; backpressure stops refill.
- Configurable but hard max 30/session (binding, from inventory/decompose/execution-waves).

## Result Aggregation
- q2 aggregation = flat list of per-subtask results (matrix flat fan-out; collect completions as arrive, no barrier for matrix; U3.5 Aggregate: per-thread artifacts + lineage merged to session output; gate verdicts attached; partial-success accounting).
- q3 goal artifact = inline only (no separate doc).
- Result (flat) default; session aggregate status: LOADING/EMPTY/PARTIAL/ERROR/SUCCESS (derived in SessionState.metadata).
- U3.4 Join owns completion collection per join type (flat vs staged).
- Other: ordered by (stage, DAG-depth, creation, task-id) + provenance for determinism/replay; partial success first-class.

## Additional n6/n7 Resolutions and Defaults (complete list from plans)
- q1 (inventory): workflowz = DAG dispatch policy (no DSL).
- q2 (inventory): matrix vs nexus = flat vs staged join.
- q3 (inventory): Wasmer = FULL CORE sandbox.
- q4 (inventory): perf target = parity or better (assumption, not SLO).
- OMP sessions (AGENTS.md n6): signing key OPTIONAL (validate if present); requires prior 'on' (ultrathink); brief + free text; gsd-autonomous uses OMP concurrent (SPE-13819).
- Wave constraints (n7): no mixed joins; compile-first; python-truth; no invented facts (n5 list).
- Notion/Linear tracking: reuse existing rows; graph ut-mvvownjp-ff01b275 etc.
- References clarifications: AGENTS.md (n2/n7 wave 1 assumptions), 14.4-PLAN.md (ultrathink recovery), WAVES.md (n7 wave synthesis).
- No contradictions: all align with settled decisions, G-* rules, file-ownership.

See plan/*.md and ultrathink n7 for full waves. Update only with new evidence.

## Verification Notes (for links)
- docs/assumptions.md created per spec.
- To be linked from code headers (e.g. gsd-*/lib.rs) and plan files (inventory.md, decompose.md, execution-waves.md, risks-tradeoffs.md, AGENTS.md).
- Existence verified post-write; no full-file assumptions invented.
