# N2 — Decomposition: AI-multithreading upgrade · Refs SPE-18068

Source facts: plan/inventory.md (n1, authoritative). Python truth: scripts/swarm_run.py:38-56
(force-parallel reconcile+dispatchable+select_batch policy), swarm/errors.py:13-20 (7 codes).
Rust base: crates/gsd-pool/src/lib.rs (ThreadPool, ChatThreadHandle, PoolConfig),
crates/gsd-scheduler/src/lib.rs (MatrixScheduler), crates/gsd-proto/src/gsd.proto (ThreadState,
SessionState, WorkflowDef, Budget, NexusMessage).

Settled (do not relitigate): workflowz = DAG dispatch policy, no new language; matrix = flat
fan-out, nexus = staged fan-out/fan-in with barrier join; Wasmer = FULL core sandbox
(user override); perf = parity-or-better; cap ≤ 30 threads/session; out of scope: no 31st
agent, no TS plugin rewrite, no UI redesign.

## 1. Thread lifecycle unit

Single owner of per-thread state. All transitions validated here; no other unit mutates
thread state directly.

### 1.1 States (spec-level) → proto + pool trace

| Spec state | proto `ThreadState` enum (gsd.proto:139-148) | Pool / runtime fact |
|---|---|---|
| IDLE | `IDLE (0)` | `ThreadEntry.state` initial; handle parked in `WarmPool` (gsd-pool/lib.rs:209-231) or freshly created `ChatThreadHandle` (lib.rs:181-198) not yet assigned |
| QUEUED | `INITIALIZING (1)` | Handle acquired from `ThreadPool::acquire`, task admitted to DAG, awaiting dispatcher. No new proto value: QUEUED ≡ INITIALIZING + membership in scheduler ready-queue |
| RUNNING | `RUNNING (2)` | `ThreadEntry.state = RUNNING`; worker executing (Python analog: daemon worker thread claimed a session) |
| BLOCKED | `WAITING_DEPENDENCY (3)` or `SUSPENDED (4)` | BLOCKED-on-dep ≡ `WAITING_DEPENDENCY`; BLOCKED-on-HITL/budget/capability ≡ `SUSPENDED`. Distinguished by `ThreadState.metadata["block_reason"]` (`dep` \| `hitl` \| `budget` \| `capability`) |
| SUCCEEDED | `COMPLETED (5)` | Terminal success; emits `StateEventType.THREAD_COMPLETED`; Python analog: exit-0 + artifact accepted |
| FAILED | `FAILED (6)` | Terminal failure; emits `THREAD_FAILED`; carries `swarm/errors.py` code (E-INPUT/E-DEP/E-CAPACITY/E-CONTRACT/E-POLICY/E-INTERNAL; E-TIMEOUT goes to CANCELLED_TIMED_OUT) |
| CANCELLED_TIMED_OUT | `TERMINATED (7)` | Terminal cancel/timeout; distinguished by `metadata["terminal_reason"]` (`cancelled` \| `timed_out`); Python analog: `cancel/<corr>` + process-group kill path |

No proto change required. `CANCELLED_TIMED_OUT` and `QUEUED`/`BLOCKED` refinements live in
metadata + owning-unit interpretation, keeping `gsd.proto` frozen.

### 1.2 Units

- **U1.1 Thread state machine** — owns allowed-transition table (IDLE→QUEUED→RUNNING→{BLOCKED→RUNNING, SUCCEEDED, FAILED, CANCELLED_TIMED_OUT}; BLOCKED self-loop on re-block; no exit from the three terminal states), `InvalidState` rejection maps to `PoolError::InvalidState`. Owned files: `crates/gsd-pool/src/thread_state_machine.rs`, `crates/gsd-pool/src/thread_state_transition_table.rs`.
- **U1.2 Handle manager** — owns `ChatThreadHandle` issuance, `ThreadEntry` registry (`threads: DashMap<ChatThreadId, …>`), acquire/release against `PoolConfig{max_threads:30}` → `PoolError::Exhausted` ≡ E-CAPACITY. Owned files: `crates/gsd-pool/src/handle_registry.rs`.
- **U1.3 Warm pool** — owns `WarmPool` pop/push per `SubagentType`, `warm_pool_size`, eviction grace, checkpoint hook. Owned files: `crates/gsd-pool/src/warm_reuse.rs`.
- **U1.4 Lifecycle hooks** — owns `LifecycleEvent` dispatch (OnCreate/OnAssign/OnSuspend/OnResume/OnCheckpoint/OnComplete/OnFailure/OnTerminate) as notify-only observers; hooks MUST NOT mutate state. Owned files: `crates/gsd-pool/src/hook_dispatch.rs`.
- **U1.5 Error mapping** — owns Rust→Python error-code map: every terminal FAILED/CANCELLED_TIMED_OUT carries one of the 7 `swarm/errors.py` codes; `classify()` + backoff/jitter semantics ported as data, behavior verified by parity tests owned elsewhere. Owned files: `crates/gsd-failure/src/error_taxonomy_map.rs`.

## 2. Scheduler decomposition

Ports the `swarm_run.py:38-56` force-parallel policy. Persistent pool refilled on every
completion; order priority → DAG-depth → creation; caps, AIMD degrade, fail-streak circuit.

- **U2.1 DAG builder** — owns `WorkflowDef`/`Task`/`Edge` ingestion, `TaskDag::add_task`, cycle detection → `SchedulerError::CycleDetected` ≡ E-DEP on structural fault; CEL `Edge.condition` evaluation point. Owned files: `crates/gsd-scheduler/src/dag_build.rs`.
- **U2.2 Independence detector** — owns ready-frontier computation (`get_ready_tasks`), `reconcile + dispatchable` equivalent: dependency-satisfied + notes.claims-overlap exclusion + per-agent `max_parallel` check. Pure function over DAG + claims snapshot; no dispatch side effects. Owned files: `crates/gsd-scheduler/src/ready_frontier.rs`.
- **U2.3 Force-parallel dispatcher** — owns `select_batch` + refill-on-completion loop: priority (P0 first) → DAG-depth → creation-time ordering; global cap (`--max-parallel`, default 4, ceiling 32; session cap 30) + per-agent caps; AIMD limiter (rate-limit halve once per burst, success +1 after cooldown; fail-streak ≥3 distinct-task consecutive non-rate-limit failures halves once, success resets); transient-failure backoff (`retry.backoff_s`/`retry_after`); worker-exception-isolates-to-one-task (E-INTERNAL, never siblings). `SWARM_SCHEDULER=rounds` barrier fallback preserved as policy flag. Owned files: `crates/gsd-scheduler/src/force_parallel_dispatch.rs`, `crates/gsd-scheduler/src/aimd_limiter.rs`.
- **U2.4 Matrix join (flat fan-out)** — owns flat execution path: dispatch batch, collect completions as they arrive, no barrier. Backed by `MatrixScheduler` + `SchedulingPolicyTrait` (`CriticalPathThroughputHybrid` default) + `SemaphoreManager` resource classes. Owned files: `crates/gsd-scheduler/src/matrix_join.rs`.
- **U2.5 Nexus join (staged fan-out/fan-in with barrier)** — owns staged path: per-stage fan-out, `NexusMessage`/`NexusMessageEnvelope` coordination over `gsd-nexus` (pub/sub, request/response), barrier join at stage end before next stage; stuck-stage timeout → E-TIMEOUT. Owned files: `crates/gsd-scheduler/src/nexus_barrier_join.rs`.
- **U2.6 Policy + semaphore config** — owns `SchedulerConfig`, `set_policy`, `SemaphoreManager` class definitions. Owned files: `crates/gsd-scheduler/src/policy_config.rs`.

Matrix vs nexus selection is per-`WorkflowDef.policy`/workflowz DAG shape, decided before
dispatch; a single run never mixes join types mid-flight (see §4 no-mixed-joins rule).

## 3. Session lifecycle

One GSD-Core session = up to 30 chat threads (A01–A30 isolates) under one `SessionId`,
one `WorkflowDef`, one `Budget`.

Pipeline stages (strict order, each owned):

- **U3.1 Create** — owns `SessionState` init (`session_id`, `workflow`, `BudgetSnapshot`, empty `threads`/`branches`), capability-manifest bind per thread (from `CapabilityManifest` proto), budget reservation via `gsd-governor`. Owned files: `crates/gsd-state/src/session_create.rs`.
- **U3.2 Dispatch** — owns handing the ready frontier to U2.3; session-scoped claim of handles; writes QUEUED→RUNNING transitions via U1.1. Owned files: `crates/gsd-state/src/session_dispatch.rs`.
- **U3.3 Monitor** — owns heartbeat/stall detection (port of `SWARM_HEARTBEAT_S` beat + `SWARM_STALL_AFTER_S` silent-stream flag), per-run `swarm.run.*` event emission via `gsd-observability`, live snapshot (`.swarm/live.json` equivalent). Read-only over thread states; never transitions. Owned files: `crates/gsd-state/src/session_monitor.rs`.
- **U3.4 Join** — owns completion collection for the session's join type (delegates mechanics to U2.4/U2.5); timeout/cancel propagation (`cancel/<corr>` + grace `SWARM_CANCEL_GRACE_S=10` within `SWARM_SHUTDOWN_BUDGET_S=15` → `CANCELLED_TIMED_OUT`). Owned files: `crates/gsd-state/src/session_join.rs`.
- **U3.5 Aggregate** — owns result folding: per-thread artifacts (`Artifact` + `lineage`) merged to session output; gate verdicts (qa/rev/sec/rel signed verdicts) attached; partial-success accounting. Owned files: `crates/gsd-state/src/session_aggregate.rs`.
- **U3.6 Teardown** — owns handle release to warm pool or destroy, checkpoint write, budget settle, branch/merge record (`SESSION_BRANCHED/MERGED`). Owned files: `crates/gsd-state/src/session_teardown.rs`.

Session aggregate status (derived, stored in `SessionState.metadata["aggregate"]`):

| Status | Meaning | Derivation |
|---|---|---|
| LOADING | Session created, frontier not yet computed or first thread not RUNNING | After U3.1, before first U3.2 dispatch completes |
| EMPTY | Frontier empty with zero threads ever dispatched (no-op workflow / all filtered) | Frontier empty ∧ dispatched-count = 0 |
| PARTIAL | Joined with ≥1 SUCCEEDED and ≥1 FAILED/CANCELLED_TIMED_OUT, or frontier non-empty at timeout | U3.4 outcome mix |
| ERROR | Zero SUCCEEDED and ≥1 FAILED/CANCELLED_TIMED_OUT, or create/dispatch infra fault | U3.4 outcome mix / U3.1-U3.2 fault |
| SUCCESS | All dispatched threads SUCCEEDED, gate verdicts pass | U3.4 + U3.5 unanimous |

## 4. Rust migration boundary (strangler)

Python is the correctness truth until parity-gated cutover per call path.

- **U4.1 Strangler trait** — owns the single seam trait (one method per lane verb: `plan`, `status`, `assign`, gate verdicts, tool-bridge call) with identical input/output schemas on both sides; `100_000`-byte input cap and exit-contract (0/1/2) enforced at the seam. Owned files: `crates/gsd-adapter/src/strangler_seam.rs`.
- **U4.2 Per-call routing** — owns the routing table (lane-verb → python | rust) + parity-gate flag per route; default python; flip to rust only on green parity evidence for that route. Owns `--dry-run --json` contract tests pinning each route. Owned files: `crates/gsd-adapter/src/route_table.rs`.
- **U4.3 No mixed-engine joins** — owns the invariant: within one session join (U3.4), all participating threads execute on the same engine; router rejects sessions that would straddle (E-CONTRACT). Cross-engine reuse happens only across sessions, never within a barrier. Owned files: `crates/gsd-adapter/src/join_affinity_guard.rs`.
- **U4.4 Legacy shims** — owns throwaway Python-call shims deleted route-by-route at cutover; clean-cutover rule: migrate every caller, remove shim, no permanent dual-path. Owned files: `crates/gsd-adapter/src/legacy_shim_registry.rs`.

## 5. Wasmer controls for FULL core

Entire Rust core executes inside Wasmer sandbox (user override 2026-10-20); strongest
isolation, highest serde cost. Zero existing wasm hits — toolchain, module format,
host-import budget, FS/net policy are greenfield (n4/n5 design inputs; this section only
bounds the control units).

- **U5.1 Module build** — owns wasm target selection (`wasm32-wasip2` vs wasi), profile, artifact pipeline, reproducible build + hash pin. Owned files: `crates/gsd-security/src/wasmer_module_build.rs`.
- **U5.2 Module load** — owns Wasmer engine instantiation, module verify-then-load, version pin vs core. Owned files: `crates/gsd-security/src/wasmer_module_load.rs`.
- **U5.3 Resource limits** — owns fuel/memory/time budgets per thread call, serde-byte budget for full-core crossings (sized for full core, not pure core), OOM/fuel-exhaust → E-CAPACITY mapping. Owned files: `crates/gsd-security/src/wasmer_limits.rs`.
- **U5.4 Host bridge** — owns the explicit host-import allowlist (clock/sync/io surface from `gsd-runtime`; UDS-gRPC/memfd IPC via `gsd-ipc`; model/tool I/O), FS/net deny-by-default policy, capability-token check per import (`gsd-security` tokens). Owned files: `crates/gsd-security/src/wasmer_host_bridge.rs`.
- **U5.5 Fallback** — owns fail-closed behavior on sandbox fault: `PoolError::Sandbox` ≡ E-POLICY/E-INTERNAL per cause; no silent host-escape; session continues only if join semantics allow (else session ERROR). Owned files: `crates/gsd-security/src/wasmer_fallback.rs`.

## 6. omp preservation decomposition

TypeScript plugin contract frozen — no rewrite. Rust/session work MUST NOT change omp's
observable behavior: spawn `python3 scripts/<script>.py --json`, exit-contract mapping,
input-file writer, session inflight group-kill, mirrored constants.

- **U6.1 Bridge/spawn contract** — pins `omp/src/bridge.ts` behavior: argv shape, `--json` stdout protocol, exit→verdict mapping, group-kill on session cancel. Any Rust-side change that alters spawned-command bytes or stdout schema needs an omp-mirror review (still no omp edit without separate approval). Owned files: `crates/gsd-sdk/src/omp_bridge_pins.rs` (verification spec; `omp/src/bridge.ts` itself stays read-only authority, not owned).
- **U6.2 Constants mirror** — pins `omp/src/tools.ts` mirrors: `ID_PATTERN`, 14 STATES, 7 PATTERNS, `GATE_SCRIPTS`, `SEVERITIES` (BLOCKING=major), FINDING schema, 100KB cap, `QA_GATE_TIMEOUT_MS`. Rust enums/schemas MUST stay byte-compatible. Owned files: `crates/gsd-sdk/src/omp_constants_mirror.rs`.
- **U6.3 Guard/hooks/context/commands/bar** — pins `guard.ts`, `hooks.ts` (incl. triple-mirrored prompt classifier + `tests/fixtures/classifier_prompts.json`, verified via `python3 -m pytest tests/test_hook.py -q` + bun `classifyPrompt` check), `context.ts`, `commands.ts` (`/swarm`), `swarm_bar.ts`. No behavior change from this program. Owned files: `crates/gsd-sdk/src/omp_surface_pins.rs`.
- **U6.4 Classifier triple-mirror rule** — any change touching classification MUST edit all three copies (`swarm/signal_detector.py`, `hooks/user_prompt_submit.py`, `omp/src/hooks.ts`) + fixture together. Enforcement checklist owned here. Owned files: `crates/gsd-sdk/src/omp_classifier_rule.rs`.

## File-ownership table (disjoint)

No two units share a file. Existing sources (`gsd.proto`, `gsd-pool/lib.rs`,
`gsd-scheduler/lib.rs`, `swarm_run.py`, `swarm/errors.py`, `omp/src/*`) are read-only
authorities, not owned by any unit below.

| Unit | Owned files (exclusive) |
|---|---|
| U1.1 Thread state machine | `crates/gsd-pool/src/thread_state_machine.rs`, `crates/gsd-pool/src/thread_state_transition_table.rs` |
| U1.2 Handle manager | `crates/gsd-pool/src/handle_registry.rs` |
| U1.3 Warm pool | `crates/gsd-pool/src/warm_reuse.rs` |
| U1.4 Lifecycle hooks | `crates/gsd-pool/src/hook_dispatch.rs` |
| U1.5 Error mapping | `crates/gsd-failure/src/error_taxonomy_map.rs` |
| U2.1 DAG builder | `crates/gsd-scheduler/src/dag_build.rs` |
| U2.2 Independence detector | `crates/gsd-scheduler/src/ready_frontier.rs` |
| U2.3 Force-parallel dispatcher | `crates/gsd-scheduler/src/force_parallel_dispatch.rs`, `crates/gsd-scheduler/src/aimd_limiter.rs` |
| U2.4 Matrix join | `crates/gsd-scheduler/src/matrix_join.rs` |
| U2.5 Nexus join | `crates/gsd-scheduler/src/nexus_barrier_join.rs` |
| U2.6 Policy + semaphore config | `crates/gsd-scheduler/src/policy_config.rs` |
| U3.1 Create | `crates/gsd-state/src/session_create.rs` |
| U3.2 Dispatch | `crates/gsd-state/src/session_dispatch.rs` |
| U3.3 Monitor | `crates/gsd-state/src/session_monitor.rs` |
| U3.4 Join | `crates/gsd-state/src/session_join.rs` |
| U3.5 Aggregate | `crates/gsd-state/src/session_aggregate.rs` |
| U3.6 Teardown | `crates/gsd-state/src/session_teardown.rs` |
| U4.1 Strangler trait | `crates/gsd-adapter/src/strangler_seam.rs` |
| U4.2 Per-call routing | `crates/gsd-adapter/src/route_table.rs` |
| U4.3 No mixed-engine joins | `crates/gsd-adapter/src/join_affinity_guard.rs` |
| U4.4 Legacy shims | `crates/gsd-adapter/src/legacy_shim_registry.rs` |
| U5.1 Module build | `crates/gsd-security/src/wasmer_module_build.rs` |
| U5.2 Module load | `crates/gsd-security/src/wasmer_module_load.rs` |
| U5.3 Resource limits | `crates/gsd-security/src/wasmer_limits.rs` |
| U5.4 Host bridge | `crates/gsd-security/src/wasmer_host_bridge.rs` |
| U5.5 Fallback | `crates/gsd-security/src/wasmer_fallback.rs` |
| U6.1 Bridge/spawn contract | `crates/gsd-sdk/src/omp_bridge_pins.rs` |
| U6.2 Constants mirror | `crates/gsd-sdk/src/omp_constants_mirror.rs` |
| U6.3 Guard/hooks/context/commands/bar | `crates/gsd-sdk/src/omp_surface_pins.rs` |
| U6.4 Classifier rule | `crates/gsd-sdk/src/omp_classifier_rule.rs` |

No code written by this decomposition. All files above are unit-ownership assignments for
implementers; creation/validation belongs to downstream phases.
