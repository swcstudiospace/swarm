# N7 — Ordered wave execution plan · Refs SPE-18070

Source inputs (read first — authoritative): `plan/inventory.md` (n1 facts),
`plan/decompose.md` (n2 units U1–U6 + file-ownership table), `plan/scheduler-options.md`
(n3 B-shell/A-engine/C-supervisor + acceptance hooks + compile-first milestone),
`plan/rust-wasmer-omp-options.md` (n4 strangler + full-core + harness),
`plan/missing-facts.md` (n5 unknowns + may-NOT-assume list),
`plan/risks-tradeoffs.md` (n6 selected combo + 10 ordered n7 inputs).

Settled decisions (do not relitigate): matrix = flat fan-out, nexus = staged
fan-out/fan-in with barrier join; Wasmer = FULL core sandbox (user override 2026-10-20);
perf = parity-or-better `[ASSUMPTION]` (n5 §6, not a measured SLO); cap ≤ 30
threads/session; Python truth on any divergence; no mixed-engine joins within one
session join (U4.3 / n4 §3); combo = B-shell/A-engine/C-supervisor × R-S strangler
with R-G fallback discipline; out of scope: no 31st agent, no TS rewrite, no product UI
design (the Phase 13 browser runtime and its status surface are in scope), no workflowz language, no merged matrix/nexus abstraction. Browser delivery
(Phase 13, COOP/COEP-isolated runtime) is in scope per the 2026-10-22 user decision.

## Binding constraints on all waves (n5 may-NOT-assume list)

1. No Wasmer version / wasm target triple / WASI version / host-import list assumed — Wave A0/A-spike pins them; Wave C work starts only on the recorded pin.
2. No FS/net sandbox policy, no module distribution story assumed — U5.4 lands only after the n4 spike report (Wave A).
3. No `gsd-sdk/python|typescript|go` clients assumed (verified absent, stub Cargo.toml only) — Wave C validates against the stub + `omp-callback`, never against assumed SDK clients.
4. No `ExtensionAPI` versioning/negotiation — TS surface frozen; any breaking `omp-api.ts` question waits on the host-owner answer (n6 input 9); default frozen.
5. No per-path p95 baseline, harness, or SLO numbers — q4 is an assumption; Wave A records the first samples; Wave D enforces parity-or-better against them.
6. No thread snapshot-authority / resume / retention / crash-recovery semantics — replay stops at cancel/timeout; Wave B/C must not invent them (wait on A03/A07 answers, n6 input 6).
7. No full parity fixture corpus — fixtures are piecemeal; Wave A builds the 11-case harness per route starting with read-only.
8. No `Cargo.lock` — Wave A0 creates it; all parity evidence before the pin is non-reproducible (n6 F12).
9. No workflowz language or session-script format — port `swarm_run.py` force-parallel policy, never a DSL.
10. No merged matrix/nexus abstraction — U2.4 and U2.5 stay separate seams with separate files.

## Global sequencing rules (enforced by dependencies below)

- **G-compile-first:** workspace `cargo build` green before any stage design or sandbox sizing (n3 milestone; n6 tradeoff 2). Order: pin → compile → spike → first read-only route.
- **G-no-mixed-joins:** one session join = one engine; U4.3 rejects straddles with E-CONTRACT.
- **G-strangler-order:** read-only → plan/assign → gates (byte-identical signatures) → `swarm_run` scheduler policy (n4 §3). A route flips default only on green parity evidence for that route.
- **G-serde-budget:** every Wasmer gate records cold-start, warm-invoke, serde marshal+unmarshal, guest-exec, peak linear memory, fuel; gate fails if serde dominates even at wall-time parity (n4 §7). Mitigations in order: input-file paths over inline blobs; WarmPool reuse; batched lane-verb reads; validate-once-at-boundary; serde budget as first-class metric. NEVER: shared-mutable-memory bypass, import widening, TS native ABI.
- **G-python-truth:** mismatch ⇒ Rust bug; flag reverts for that path; fixture recorded.

File-ownership rule: every unit below owns its files exclusively. n2-owned `src/*.rs`
files are reused verbatim from the n2 table (never reassigned); compile-skeleton,
harness, and acceptance files are new paths disjoint from the n2 table.

---

## Wave A — Discovery / parity harness (precondition for everything)

Exit gate: reproducible build + recorded baselines + frozen contract + first-route harness ready. No behavior code ships.

### A0. Build pin + compile-first closeout — Owner: A11 devops + A03 architect (implementers)

- Owned files (exclusive, new/skeleton only — disjoint from n2 table): `Cargo.lock`,
  `rust-toolchain.toml`, `.cargo/config.toml`, `crates/gsd-scheduler/src/dag.rs`,
  `crates/gsd-scheduler/src/policy.rs`, `crates/gsd-scheduler/src/semaphore.rs`,
  `crates/gsd-scheduler/src/clock.rs`, `crates/gsd-scheduler/src/dynamic.rs`,
  `crates/gsd-scheduler/src/hitl.rs`, `crates/gsd-failure/src/lib.rs` (+ taxonomy module
  declared there), `crates/gsd-nexus/src/lib.rs` (skeleton types only).
  (n2 units own `dag_build.rs`, `policy_config.rs`, etc. — untouched here. Fixes the
  `metrics.submitted.inc()` → `inc_submitted` call and the discarded
  `failure_isolation` injection in place with no behavior change.)
- Change summary: pin `rustc` + `wasmer` version + wasm target (`wasm32-wasip2` default,
  `wasi` only with recorded justification) + profile in one manifest, hash-verified in CI
  (n6 inputs 1–2); create the missing modules so the sketched scheduler/nexus code
  compiles; `gsd-failure` gets its error-enum source; `gsd-nexus` gets skeleton
  pub/sub + `NexusMessage` envelope types only (no barrier logic — that is B6).
- Verification: `cargo build --workspace` green; `ls Cargo.lock rust-toolchain.toml`;
  `grep -rn "wasmer\|wasm32" Cargo.toml rust-toolchain.toml` shows the pin.
- Done-criteria: workspace builds with zero errors; pin manifest hash-verified in CI;
  no stage/sandbox logic added (behavior diff empty).
- Depends on: nothing (first).

### A-spike. Wasmer half-day spike report — Owner: A03 architect + A11 devops

- Owned files: `plan/wasmer-spike.md` (report only — no code).
- Change summary: compile one `gsd-*` crate to the A0-pinned target under the pinned
  Wasmer; report (a) what compiles vs stubbed (tokio/net/fs), (b) candidate host imports
  sized for FULL core per q3, (c) WASI capability wish-list, (d) FS/net policy draft,
  (e) distribution story; record exact Wasmer version + triple (n5 §5; n6 input 4).
- Verification: report exists with version + triple + compile/stub table; A10 signs off
  fault-injection scope (n6 input 8, partial).
- Done-criteria: U5.1–U5.4 sizing inputs recorded; FS/net exceptions (if any) approved
  by A10; open n4 O1 (mono vs per-lane modules) / O2 (component-model vs plain JSON
  `invoke`) decided per route and recorded.
- Depends on: A0 (needs the pin).

### A1. Parity-corpus backlog + 11-case harness skeleton — Owner: A02 requirements + A08 QA

- Owned files: `tests/parity/corpus_backlog.md`, `tests/parity/harness.rs`
  (or `tests/parity/*.py` runner), `tests/parity/fixtures/` (new records).
- Change summary: enumerate `scripts/<lane>_<verb>.py` × `tests/` cross-ref (1–2 h) into
  the uncovered-lane backlog (n5 §4; n6 input 5); instantiate the n4 §7 11-case harness
  (plan happy path; transition legal+illegal; gate pass+fail-major; 100 KB boundary;
  validation-error ⇒ exit 2/E-INPUT; empty; large; malformed ⇒ E-INPUT/E-INTERNAL;
  timeout ⇒ E-TIMEOUT; policy ⇒ scrubbed E-POLICY; classifier quadruple-mirror) for the
  first read-only route (`orch_status` reads / `swarm_tool` catalog).
- Verification: `ls scripts/ | wc -l` cross-ref reviewed into the backlog;
  harness runs green against Python truth for the first read-only route.
- Done-criteria: backlog lists every uncovered lane; 11 cases all pass vs Python on the
  first read-only route; byte-compare (exit code) + parsed-compare (JSON) enforced.
- Depends on: A0 (reproducible baseline); A-spike for nothing (independent — may run parallel).

### A2. Hot-path p95 baseline samples — Owner: A13 observability + A08 QA

- Owned files: `tests/perf/baseline.md` (p50/p95 + hardware fingerprint), `tests/perf/run.sh`
  (sampling script, new).
- Change summary: time the three hot paths 50+ samples each on one host — `swarm_run`
  dispatch tick, one gate script, one `bridge.ts` round-trip — record p50/p95 + hardware
  fingerprint (n5 §6 cheapest discovery; n6 input 3). This is the serde denominator for
  G-serde-budget and the timeout re-budget evidence (n6 F7).
- Verification: `tests/perf/run.sh` reproduces the recorded numbers within noise;
  baseline.md carries hardware fingerprint + sample counts.
- Done-criteria: p50/p95 recorded for all three paths; values promoted into the Wave D
  perf gate as the parity-or-better reference.
- Depends on: nothing but the host (parallel with A0/A1).

### A3. omp contract freeze (read-only record) — Owner: A03 architect + A06 frontend

- Owned files: `tests/omp_contract/freeze.md`, `tests/omp_contract/vectors.json`
  (recorded argv/exit-code/JSON vectors — new test data, not pin specs).
  (`crates/gsd-sdk/src/omp_*.rs` pin specs belong to Wave C U6 — untouched here.)
- Change summary: freeze-record `omp/src/bridge.ts` behavior (argv shape, `--json` stdout
  protocol, exit→verdict mapping, group-kill) and `omp/src/tools.ts` mirrors (ID_PATTERN,
  14 STATES, 7 PATTERNS, GATE_SCRIPTS, SEVERITIES BLOCKING=major, FINDING schema, 100 KB
  cap, QA_GATE_TIMEOUT_MS) as byte-level vectors; confirm the `ExtensionAPI`
  versioning answer with omp host owners (pin-by-commit vs omission, 30 min — n6 input 9;
  default stays frozen regardless).
- Verification: vectors replay against current `bridge.ts`/`tools.ts` byte-identical;
  `python3 -m pytest tests/test_hook.py -q` + bun `classifyPrompt` check green for the
  triple-mirror baseline.
- Done-criteria: frozen vectors committed; any later Rust-side change altering
  spawned-command bytes or stdout schema triggers an omp-mirror review; versioning
  answer recorded (or default-frozen explicitly kept).
- Depends on: nothing (parallel with A0–A2).

Wave A gate: A0 + A-spike + A1 + A2 + A3 all meet done-criteria. Waves B/C/D must not
start behavior work ahead of this gate (n6 input 10).

---

## Wave B — Scheduler-join + strangler seam (host-native Rust first)

All `src/*.rs` files reused verbatim from the n2 ownership table. Order inside the wave:
thread/error foundation → DAG/frontier → dispatcher → joins → session → seam.
Sandbox (Wave C) must not start until B closes on host-native behavior.

### B1. Thread state machine + handle manager + error mapping — Owner: scheduler implementer (U1.1, U1.2, U1.5)

- Owned files: `crates/gsd-pool/src/thread_state_machine.rs`,
  `crates/gsd-pool/src/thread_state_transition_table.rs`,
  `crates/gsd-pool/src/handle_registry.rs`,
  `crates/gsd-failure/src/error_taxonomy_map.rs`.
- Change summary: allowed-transition table (IDLE→QUEUED→RUNNING→{BLOCKED→RUNNING,
  SUCCEEDED, FAILED, CANCELLED_TIMED_OUT}; BLOCKED via `metadata["block_reason"]`;
  terminal states sealed; `InvalidState` → `PoolError::InvalidState`); `ChatThreadHandle`
  issuance + `DashMap` registry against `PoolConfig{max_threads:30}` →
  `PoolError::Exhausted` ≡ E-CAPACITY; Rust→Python 7-code map with `classify()` +
  backoff/jitter data (behavior verified by A1/D harnesses, not here).
- Verification: `cargo test -p gsd-pool -p gsd-failure` (transition table incl. illegal-transition
  rejection; Exhausted on 31st acquire); illegal `FAILED→RUNNING` rejected.
- Done-criteria: all transitions validated in one owner; no other unit mutates thread
  state; every terminal carries one of the 7 codes.
- Depends on: Wave A gate (A0 build).

### B2. Warm pool + lifecycle hooks — Owner: scheduler implementer (U1.3, U1.4)

- Owned files: `crates/gsd-pool/src/warm_reuse.rs`, `crates/gsd-pool/src/hook_dispatch.rs`.
- Change summary: `WarmPool` pop/push per `SubagentType`, `warm_pool_size`, eviction
  grace, checkpoint hook (also the later warm-instance reuse for G-serde-budget);
  `LifecycleEvent` dispatch as notify-only observers (hooks MUST NOT mutate state).
- Verification: `cargo test -p gsd-pool` (reuse-hit + eviction + hook no-mutation assertion).
- Done-criteria: warm reuse measurable (hit counter); hook-mutates-state test fails closed.
- Depends on: B1.

### B3. DAG builder + independence detector — Owner: scheduler implementer (U2.1, U2.2)

- Owned files: `crates/gsd-scheduler/src/dag_build.rs`, `crates/gsd-scheduler/src/ready_frontier.rs`.
- Change summary: `WorkflowDef`/`Task`/`Edge` ingestion, `TaskDag::add_task`, cycle
  detection → `SchedulerError::CycleDetected` ≡ E-DEP; ready-frontier pure function
  (`reconcile + dispatchable`: dep-satisfied + `notes.claims`-overlap exclusion +
  per-agent `max_parallel`; no dispatch side effects).
- Verification: `cargo test -p gsd-scheduler` (cycle fixture ⇒ E-DEP; overlap fixture
  excluded from frontier; pure-function: repeated calls identical).
- Done-criteria: cycle detection at ingest; frontier excludes claims-overlaps; function
  has no side effects (test asserts state unchanged).
- Depends on: B1 (error codes), Wave A gate.

### B4. Force-parallel dispatcher + AIMD limiter — Owner: scheduler implementer (U2.3)

- Owned files: `crates/gsd-scheduler/src/force_parallel_dispatch.rs`,
  `crates/gsd-scheduler/src/aimd_limiter.rs`.
- Change summary: port `swarm_run.py:38-56` verbatim — refill-on-completion;
  priority→DAG-depth→creation order; global cap (default 4, ceiling 32, clamped to
  30/session) + per-agent caps; AIMD (rate-limit halve once per burst, +1 after
  cooldown; fail-streak ≥3 halves once, success resets); `retry.backoff_s` transient
  backoff; worker-exception → one-task E-INTERNAL, never siblings;
  `SWARM_SCHEDULER=rounds` barrier fallback as policy flag.
- Verification: `cargo test -p gsd-scheduler` (order fixture; 60-task flood never exceeds
  30; rate-limit burst halves once; fail-streak halves; single worker panic ⇒ one FAILED).
- Done-criteria: n3 acceptance hook 1 green on host (N independent P0 ⇒
  max-concurrency == min(N,cap), all `dispatched_at` before all `completed_at`;
  chain+overlap fixture serializes the chain while overlapping the independent task).
- Depends on: B3 (frontier), B2 (handles).

### B5. Matrix join + policy/semaphore config — Owner: scheduler implementer (U2.4, U2.6)

- Owned files: `crates/gsd-scheduler/src/matrix_join.rs`,
  `crates/gsd-scheduler/src/policy_config.rs`.
- Change summary: flat fan-out path (dispatch batch, collect as they arrive, no barrier)
  on `MatrixScheduler` + `SchedulingPolicyTrait` (`CriticalPathThroughputHybrid` default)
  + `SemaphoreManager`; `SchedulerConfig`/`set_policy`/class definitions. Completion log
  ordered by (stage, DAG-depth, creation, task-id) with provenance (producer, attempt,
  verdict envelope) — dispatch order recorded for replay (n6 §3 rule 4).
- Verification: `cargo test -p gsd-scheduler` (arrival-order shuffle ⇒ identical output
  order; every record carries provenance).
- Done-criteria: finish-order independence proven on host; single-run join type fixed
  before dispatch (G-no-mixed-joins holds trivially — one join type exists so far).
- Depends on: B4.

### B6. Nexus barrier join — Owner: scheduler implementer (U2.5)

- Owned files: `crates/gsd-scheduler/src/nexus_barrier_join.rs`.
- Change summary: staged fan-out/fan-in over `gsd-nexus` (`NexusMessage` envelope,
  pub/sub + request/response); barrier merges by declared key order with provenance
  before next-stage release; barrier deadline turns stragglers into named partial-join
  timeouts (never hold threads across a barrier without a deadline); failed participant
  = join input value, not hang; matrix-vs-nexus selected per `WorkflowDef.policy`
  before dispatch, never mixed mid-flight.
- Verification: `cargo test -p gsd-scheduler -p gsd-nexus` (shuffle-durations ⇒
  byte-identical order; one-FAILED fixture ⇒ siblings DONE, failure in retry lane,
  join marked partial — n3 hook 2d; straggler named at deadline).
- Done-criteria: n3 acceptance hook 2 green on host (a–d: inputs-before-close, order
  equals merge-key order, provenance present, partial-success terminal state).
- Depends on: B5 (matrix path + policy), A0 (`gsd-nexus` skeleton).

### B7. Session create + dispatch — Owner: session implementer (U3.1, U3.2)

- Owned files: `crates/gsd-state/src/session_create.rs`, `crates/gsd-state/src/session_dispatch.rs`.
- Change summary: `SessionState` init (session_id, workflow, `BudgetSnapshot`, empty
  threads/branches) + capability-manifest bind per thread + governor reservation;
  ready-frontier handoff to B4 with QUEUED→RUNNING via B1; state-semantics answers
  (n6 input 6: snapshot authority, resume rules, retention, `live.json` carryover)
  consumed as recorded — never invented (if unanswered, recovery stays unimplemented).
- Verification: `cargo test -p gsd-state` (create binds manifest + budget; dispatch
  writes QUEUED→RUNNING through the B1 table; cap ≤ 30 enforced at claim).
- Done-criteria: one session = ≤ 30 threads, one workflow, one budget; no thread
  dispatched without a capability bind.
- Depends on: B1, B4; A3 (contract vectors constrain argv shapes).

### B8. Session join + teardown (deterministic join owner) — Owner: session implementer (U3.4, U3.6)

- Owned files: `crates/gsd-state/src/session_join.rs`, `crates/gsd-state/src/session_teardown.rs`.
- Change summary: completion collection delegating mechanics to B5/B6; timeout/cancel
  propagation (`cancel/<corr>` + 10 s cooperative inside 15 s budget →
  CANCELLED_TIMED_OUT with `metadata["terminal_reason"]`); join-seq in the idempotency
  key (stage-id, join-seq, attempt) with re-emit dedup; teardown releases to warm pool
  or destroys, writes checkpoint, settles budget, records branch/merge lineage.
  Allocator/backoff/idempotency decisions recorded (n6 input 7: stage permit-partition
  policy; task-level backoff governs retried stage inputs; fuel→timeout vs internal
  classification kept distinct).
- Verification: `cargo test -p gsd-state` (cancel ⇒ all threads leave within budget;
  partial-join re-emit with same key deduped; n3 hook 3 cap/timeout/cancel fixtures
  green on host).
- Done-criteria: n3 acceptance hook 3 green (flood ≤ 30; thread-timeout ⇒ FAILED;
  barrier-deadline ⇒ partial with straggler named; session cancel within budget);
  cancel-then-retry never double-applies.
- Depends on: B6, B7, B2 (warm release).

### B9. Strangler seam + per-call routing (read-only first) — Owner: adapter implementer (U4.1, U4.2)

- Owned files: `crates/gsd-adapter/src/strangler_seam.rs`, `crates/gsd-adapter/src/route_table.rs`.
- Change summary: single `CoreBackend` seam (one method per frozen entrypoint, identical
  argv/exit-code/JSON schemas; 100 KB cap + 0/1/2 contract enforced at the seam);
  per-invocation `GSD_ENGINE=python|rust` routing (default `python`); first route is
  read-only (`orch_status` reads / `swarm_tool` catalog) with `--dry-run --json`
  contract tests pinning it (G-strangler-order step 1).
- Verification: A1 harness 11 cases green on both engines for the read-only route;
  `GSD_ENGINE=rust` vs `python` byte-compare clean.
- Done-criteria: first read-only route flippable per-call with parity evidence;
  default remains `python`; mismatch reverts with fixture recorded (G-python-truth).
- Depends on: B7/B8 (session paths exist to route), A1 (harness), A3 (frozen vectors).

### B10. Join-affinity guard + legacy shims — Owner: adapter implementer (U4.3, U4.4)

- Owned files: `crates/gsd-adapter/src/join_affinity_guard.rs`,
  `crates/gsd-adapter/src/legacy_shim_registry.rs`.
- Change summary: affinity guard pins engine at join creation, rejects straddling
  sessions with E-CONTRACT (G-no-mixed-joins); throwaway Python-call shims with
  clean-cutover rule (migrate every caller, remove shim, no permanent dual-path).
- Verification: `cargo test -p gsd-adapter` (straddle attempt ⇒ E-CONTRACT; shim
  registry empty for cut-over routes).
- Done-criteria: no session join spans engines (test-proven); no permanent dual-path
  survives any cutover.
- Depends on: B9.

Wave B gate: n3 hooks 1–3 green on host-native Rust; first read-only strangler route
flipped per-call with 11-case parity; G-strangler-order respected (nothing past
read-only flips yet).

---

## Wave C — Wasmer sandbox + omp validation + observability

Sandbox is a deployment qualifier per route behind `health` + parity + perf gates, with
typed fallback host-Rust → Python (R-G discipline). Full-core sizing per the A-spike
(q3). All `src/*.rs` files from the n2 table, disjoint from Wave B.

### C1. Wasmer module build + load — Owner: A03 architect + A11 devops (U5.1, U5.2)

- Owned files: `crates/gsd-security/src/wasmer_module_build.rs`,
  `crates/gsd-security/src/wasmer_module_load.rs`.
- Change summary: wasm target/profile/artifact pipeline from the A0 pin (n4 O1/O2
  decisions as recorded); reproducible build + hash pin; single hashed versioned module
  (`gsd-core.<semver>.wasm` + digest emitted atomically); content-addressed
  distribution, never implicit `latest`; engine instantiation with
  verify-then-load + version pin vs core.
- Verification: `cargo build -p gsd-security`; module + digest artifacts produced;
  `init` hash-verifies `.wasm` (wrong hash ⇒ E-DEP).
- Done-criteria: reproducible module + digest; `init` rejects unverified artifacts.
- Depends on: Wave B gate (host behavior proven first), A-spike (sizing).

### C2. Wasmer resource limits + serde budget — Owner: security implementer (U5.3)

- Owned files: `crates/gsd-security/src/wasmer_limits.rs`.
- Change summary: fuel/memory/time budgets per thread call sized for FULL core;
  serde-byte budget for full-core crossings (not pure-core); OOM/fuel-exhaust →
  E-CAPACITY mapping; fuel-exhaustion → E-TIMEOUT only with a deadline mapping, else
  E-INTERNAL (distinct internal variants kept — n6 F5); G-serde-budget metrics emitted
  every run with alert threshold.
- Verification: `cargo test -p gsd-security` (over-budget ⇒ E-CAPACITY; fuel-exhaust
  with/without deadline ⇒ E-TIMEOUT/E-INTERNAL respectively; serde-dominated run fails
  the gate even at wall-time parity).
- Done-criteria: limits enforced per call; serde budget sized from the A2 denominator
  via the host-Rust baseline delta (R-G discipline).
- Depends on: C1, A2 (baseline denominator).

### C3. Wasmer host bridge — Owner: security implementer (U5.4)

- Owned files: `crates/gsd-security/src/wasmer_host_bridge.rs`.
- Change summary: explicit allowlist sized for whole-core — `log` (bounded batch),
  `time` (host-owned monotonic; guest never reads wall time), `rand` (host-seeded for
  replay), `storage` (capability-tokened, per-session namespace; only mutating import),
  `omp-callback` (schema-validated within serde limits); deny-by-default FS (preopen
  only needed `<SWARM_DIR>/results` + inputs) and net (default none); per-import
  capability-token check (per-call minted, never cross-session cached).
  Explicit non-imports: no spawn, no ambient FS/net, no env passthrough except the
  stripped `SWARM_DIR`-equivalent, no direct `Bun.spawn`.
- Verification: `cargo test -p gsd-security` (tokenless import ⇒ E-POLICY fail-closed;
  FS/net overreach denied; guest wall-clock read impossible by construction).
- Done-criteria: full-core call completes through the five imports only; over-narrow
  defaults fixed against the A-spike capability wish-list (n6 F6).
- Depends on: C1, A-spike; A10 sign-off on FS/net exceptions (n6 input 8).

### C4. Wasmer fallback (fail-closed) — Owner: security implementer (U5.5)

- Owned files: `crates/gsd-security/src/wasmer_fallback.rs`.
- Change summary: sandbox fault ⇒ `PoolError::Sandbox` ≡ E-POLICY/E-INTERNAL per cause;
  no silent host-escape; typed failover host-Rust → Python preserving the idempotency
  key (task-id, attempt / stage-id, join-seq) across transitions (n6 §5 condition);
  session continues only if join semantics allow, else session ERROR; mutating calls
  never auto-retry (D-07 re-check first).
- Verification: `cargo test -p gsd-security` (trap mid-join ⇒ typed failover, crashed
  participant marked, never partial-as-success; fallback preserves idempotency key).
- Done-criteria: every sandbox fault maps to a typed E-code with failover; no
  partial output surfaces as success.
- Depends on: C2, C3, B10 (affinity + shim discipline).

### C5. omp bridge/spawn + constants pins — Owner: SDK implementer (U6.1, U6.2)

- Owned files: `crates/gsd-sdk/src/omp_bridge_pins.rs`, `crates/gsd-sdk/src/omp_constants_mirror.rs`.
  (`omp/src/*` stay read-only authority.)
- Change summary: verification specs pinning `bridge.ts` behavior (argv, `--json`
  protocol, exit→verdict mapping, group-kill) and `tools.ts` mirrors (ID_PATTERN,
  14 STATES, 7 PATTERNS, GATE_SCRIPTS, SEVERITIES, FINDING schema, 100 KB cap,
  QA_GATE_TIMEOUT_MS) byte-compatible; `init`/`invoke`/`shutdown`/`health` lifecycle
  (`health` failure ⇒ E-DEP); per-call `timeoutMs` host-enforced (600 s; QA 1860 s);
  serde limits both ways (100 KB/value, depth 32, unknown-field rejection,
  `error.code` allowlist; oversize ⇒ E-INPUT/E-CONTRACT, never truncation);
  E-POLICY scrubbing preserved; retries idempotent-only with `ErrorHandler` backoff.
- Verification: A3 vectors replay green through the Rust bridge; retryable-column
  codes match n4 §2 (`RetryPolicy.retryable_error_codes`).
- Done-criteria: Rust bridge byte-compatible with frozen vectors; no `omp/src/*` edit.
- Depends on: A3 (vectors), B9 (seam), C3 (`omp-callback` schema).

### C6. omp guard/hooks/surface + classifier rule — Owner: SDK implementer (U6.3, U6.4)

- Owned files: `crates/gsd-sdk/src/omp_surface_pins.rs`, `crates/gsd-sdk/src/omp_classifier_rule.rs`.
- Change summary: pins for `guard.ts`, `hooks.ts` (triple-mirrored classifier +
  fixture), `context.ts`, `commands.ts` (`/swarm`), `swarm_bar.ts` — no behavior change;
  classifier rule: any classification-touching change edits all three copies
  (`swarm/signal_detector.py`, `hooks/user_prompt_submit.py`, `omp/src/hooks.ts`) +
  `tests/fixtures/classifier_prompts.json` together.
- Verification: `python3 -m pytest tests/test_hook.py -q` + bun `classifyPrompt` check;
  quadruple-mirror case (n4 §7 case 11) green.
- Done-criteria: classifier agreement across all copies + fixture; zero omp behavior drift.
- Depends on: A3, C5.

### C7. Session monitor + aggregate (observability) — Owner: A13 observability + session implementer (U3.3, U3.5)

- Owned files: `crates/gsd-state/src/session_monitor.rs`, `crates/gsd-state/src/session_aggregate.rs`.
- Change summary: heartbeat/stall detection (port of `SWARM_HEARTBEAT_S` +
  `SWARM_STALL_AFTER_S`), `swarm.run.*` events via `gsd-observability`, live snapshot
  (read-only over states, never transitions); result folding with lineage + gate
  verdicts + partial-success accounting; derived aggregate statuses LOADING / EMPTY /
  PARTIAL / ERROR / SUCCESS per the n2 §3 table; per-stage + join metrics
  (gauge(concurrency), histogram(queue_wait), histogram(exec_time), barrier wait,
  straggler identity, partial-join rate) aggregated stage → session by construction.
- Verification: `cargo test -p gsd-state` (status derivation table incl. EMPTY no-op
  workflow and PARTIAL mix; stall flag fires; queue-wait/exec-time histograms present
  per stage and aggregated per session).
- Done-criteria: all five aggregate statuses derivable and tested; concurrency-level,
  queue-wait, exec-time observable per session.
- Depends on: B8 (join outcomes), B4/B6 (metrics sources).

Wave C gate: first route runs sandboxed behind `health` + 11-case parity + G-serde-budget;
omp vectors + classifier checks green; five session statuses + concurrency metrics live.

---

## Wave D — Acceptance proof (no new behavior; proves the waves)

Each unit owns new acceptance files only (disjoint from n2 + Waves A–C). Strangler order
applies inside D4: read-only → plan/assign → gates → scheduler; each step flips only on
its own green evidence.

### D1. Parallel-start / join trace proof — Owner: A08 QA

- Owned files: `tests/acceptance/parallel_trace.rs` (or `.py` runner), `tests/acceptance/traces/`.
- Change summary: replays `thread.start`/`thread.finish` + `stage.join` spans asserting
  (a) all stage inputs started before join close, (b) output order equals merge-key order
  under shuffled durations, (c) provenance on every output, (d) one-FAILED ⇒ siblings
  DONE + retry lane (`FAILED→RETRY→PLANNED/CLAIMED`, attempt ≤ 3, rework ≤ 2) + partial
  join flag (n3 hook 2; n6 §3 rules).
- Verification: runner green on host-Rust and sandboxed engines; shuffle seed varied.
- Done-criteria: order-independence + provenance + partial-success proven on both engines.
- Depends on: Wave B gate + Wave C gate.

### D2. Isolation injection (panic/timeout/trap per thread) — Owner: A08 QA + A10 security

- Owned files: `tests/acceptance/isolation_injection.rs`, `tests/acceptance/isolation_cases.json`.
- Change summary: injects worker panic, thread timeout, and Wasmer trap per thread;
  asserts per-thread FAILED per `LEGAL_TRANSITIONS`, siblings DONE, monitor heartbeats
  continue, cancel fan-out meets the 15 s budget (n6 F4; n3 hooks).
- Verification: runner green; central-loop-bug blast-radius case (all-fail) surfaces as
  session ERROR, never silent.
- Done-criteria: isolation proven per thread on both engines; fault suite scope signed
  by A10 (n6 input 8 closeout).
- Depends on: B8, C4.

### D3. Wasmer fault injection (trap/OOM/fuel/capability) — Owner: A10 security + A08 QA

- Owned files: `tests/acceptance/wasmer_faults.rs`, `tests/acceptance/wasmer_fault_cases.json`.
- Change summary: trap/OOM/fuel-exhaustion mid-matrix + capability-denial cases;
  asserts E-code mapping (trap ⇒ E-INTERNAL default; fuel ⇒ E-TIMEOUT iff deadline
  mapping else E-INTERNAL — distinct variants; OOM ⇒ E-CAPACITY; denial ⇒ scrubbed
  E-POLICY), fail-closed with no host-escape, session continues only if join semantics
  allow (n6 F5/F6).
- Verification: runner green on the sandboxed engine; E-POLICY scrub assertion
  (message free of the token) passes.
- Done-criteria: every sandbox fault maps to its specified code; misclassification
  (fuel→timeout) test guards retry behavior.
- Depends on: C2–C4.

### D4. Interop round-trip (strangler-ordered) — Owner: A08 QA + A06 frontend

- Owned files: `tests/acceptance/interop_roundtrip.rs`, `tests/acceptance/interop_vectors.json`.
- Change summary: per-route round-trips in G-strangler-order (read-only → plan/assign →
  gates with byte-identical signatures → scheduler policy) asserting argv/exit-code/JSON
  parity vs Python truth, `runPy`-only spawning, completion-order resolution with
  per-caller `toolCallId`/correlation preserved, group-kill emitting no results,
  E-POLICY scrubbing, and G-no-mixed-joins (straddle ⇒ E-CONTRACT).
- Verification: runner green per route on host-Rust, then sandboxed; Python-truth
  mismatch reverts that route with fixture recorded.
- Done-criteria: each route flips default only on its own green evidence; no route
  skipped ahead of order; omp surface untouched.
- Depends on: B9/B10, C4/C5, A1 (harness), A3 (vectors).

### D5. Perf comparison (parity-or-better + serde budget) — Owner: A13 observability + A08 QA

- Owned files: `tests/acceptance/perf_gate.rs` (or runner config), `tests/acceptance/perf_results.md`.
- Change summary: per-path p95 + throughput vs the A2 baseline on same hardware for
  every flipped route; Wasmer overhead recorded separately (cold-start, warm-invoke,
  serde vs guest-exec, peak memory, fuel); gate fails on regression OR on
  serde-dominance without recorded mitigation (G-serde-budget); timeout re-budget
  evidence fed back if first samples force it (n6 F7).
- Verification: runner green with results committed; host-Rust p95 as serde denominator
  (R-G discipline) shown per route.
- Done-criteria: parity-or-better per path holds; serde budget holds per route;
  numbers reproducible from the committed config.
- Depends on: A2 (baseline), D4 (routes flipped), C2 (budget).

Wave D gate (program exit): D1–D5 green; strangler order complete through scheduler
policy; serde budget held; omp frozen; no invented semantics (snapshot/resume still
OPEN per constraint 6 — explicitly not proven here).

---

## Dependency graph (summary)

```text
A0 ─┬─→ A-spike ─→ C1 ─→ C2 ─→ C4 ─→ D2/D3
    ├─→ B1 ─→ B2 ─→ B4 ─→ B5 ─→ B6 ─→ B8 ─→ C7 ─→ D1
    │         │              ↘ B3 ↗         ↗
    │         └→ B7 ─────────→ B8 ─→ B9 ─→ B10 ─→ C4/C5 ─→ D4 ─→ D5
    ├─→ A1 ─→ B9/D4 (harness)          C2/C3 ─→ D3
    ├─→ A2 ─→ C2 ─→ D5 (denominator/budget)
    └─→ A3 ─→ B7/B9, C5/C6, D4 (frozen vectors)
A-spike ─→ C3 (imports/policy) ─→ C4
A10/A03/A07 answers (n6 inputs 6,8,9) gate C3, C7-recovery, D2 — never invented.
```

## n6-input → wave-unit mapping

| n6 input | Unit |
|---|---|
| 1 build pin | A0 |
| 2 compile-first closeout | A0 |
| 3 hot-path p95 samples | A2 → D5 |
| 4 Wasmer spike report | A-spike → C1/C3 |
| 5 parity-corpus backlog | A1 → B9/D4 |
| 6 state-semantics answers | B7 (consume-only), C7 (recovery stays out until answered) |
| 7 allocator/backoff/idempotency | B8 |
| 8 security inputs | A-spike (partial), C3, D2 |
| 9 omp versioning answer | A3 |
| 10 plan-shape discipline | this document (gates between waves) |

No code written by this plan. All `src/*.rs` paths are ownership assignments from the
n2 table; all `tests/*`, `Cargo.lock`, `rust-toolchain.toml`, `.cargo/config.toml`,
and `plan/wasmer-spike.md` paths are new assignments for implementers.
