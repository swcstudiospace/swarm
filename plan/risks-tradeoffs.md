# Risks & tradeoffs: scheduler × Rust/Wasmer/omp · Refs SPE-18069

Source inputs (Wave 2, read first — authoritative): `plan/inventory.md` (n1 facts),
`plan/decompose.md` (n2 units U1–U6 + ownership table), `plan/scheduler-options.md`
(n3 A/B/C), `plan/rust-wasmer-omp-options.md` (n4 boundary + full-core design),
`plan/missing-facts.md` (n5 unknowns + may-NOT-assume list).

Settled decisions (do not relitigate): matrix = flat fan-out, nexus = staged
fan-out/fan-in with barrier; Wasmer = FULL core sandbox (user override 2026-10-20);
perf = parity-or-better `[ASSUMPTION]` (n5 §6, not a measured SLO); cap ≤ 30
threads/session; Python truth; no mixed-engine joins within one session join
(U4.3 / n4 §3); out of scope: no 31st agent, no TS rewrite, no UI.

n5 may-NOT-assume list (binding on everything below — §4 selection respects it):
1. No Wasmer version / wasm target / WASI version / host-import list.
2. No FS/net sandbox policy, no module distribution story.
3. No `gsd-sdk/python|typescript|go` clients (stub Cargo.toml only).
4. No `ExtensionAPI` versioning/negotiation — TS surface frozen.
5. No per-path p95 baseline, no perf harness, no SLO numbers (q4 is assumption).
6. No thread snapshot-authority / resume / retention / crash-recovery semantics.
7. No full parity fixture corpus (piecemeal fixtures only).
8. No `Cargo.lock` — dependency closure unpinned.
9. No workflowz language — do not invent one.
10. No merged matrix/nexus abstraction — keep seams split.

n3 compile-first milestone (binding order): `gsd-scheduler` declares
`mod dag; policy; semaphore; clock; dynamic; hitl;` with no such files;
`submit` calls nonexistent `metrics.submitted.inc()` (only `inc_submitted` exists);
`submit` discards injected `failure_isolation` for `FailureIsolation::new()` from
`gsd-failure`, which has no `src/`; `gsd-nexus` is `Cargo.toml`-only. The first
milestone is making the sketch compile — no stage design, no sandbox sizing,
until the workspace builds.

---

## 1. Scheduler comparison: A / B / C on concurrency safety, determinism, production bar

Options per n3: **(A)** central DAG scheduler + bounded pool; **(B)** staged
matrix/nexus executors with barrier join; **(C)** actor-per-thread with supervisor
restart. n3 recommends B with A-mechanics per stage + C-supervisor isolation.
This section independently scores that recommendation against the production bar
so n7 inherits the reasoning, not just the verdict.

### 1.1 Head-to-head

| Criterion | (A) Central DAG + pool | (B) Staged + barrier (n3 winner) | (C) Actor + supervisor |
|---|---|---|---|
| Concurrency safety (races, cap, backpressure) | Strong. Single semaphore (global 30) + per-agent `max_parallel` + `notes.claims` exclusion + AIMD in one place; backpressure = stop refilling. Single lock-order domain (session → join → task → store per n4 §4). Risk: central loop is a single blast radius — one scheduling bug affects all tasks. | Strong, partitioned. Same A-mechanics inside each stage; session cap must be **partitioned across concurrent stages** or 3 stages × 30 = 90 violates the ≤ 30/session cap. Barrier is natural backpressure (join blocks next stage) but a straggler idles threads. Needs a session-level permit allocator + barrier deadlines (n3 mitigations). | Weakest at session scope. Cap = actor count is easy; global backpressure, per-agent `max_parallel` across actors, and AIMD need cross-actor agreement — i.e. a coordinator. 30 mailbox depths replace one queue; deadlock detection across mailboxes is harder than one `WaitForGraph`. Per-actor isolation is best; session-level safety is emergent. |
| Determinism (ordered merge + provenance + replay) | Bolt-on. Central completion log ordered by (stage, DAG-depth, creation, task-id) with provenance (producer, attempt, verdict envelope) must be added; replay needs dispatch order recorded, not just completion order. No structural merge point — a logging regression silently loses determinism. | Native. Barrier join **is** the ordered-merge point: stage outputs merged by declared key order with provenance attached before release; partial success is a first-class join result. Shuffle-durations fixture (n3 acceptance hook 2) proves finish-order independence. Cost: cross-stage deps must be compiled into stage boundaries; dynamic deps need the intra-stage escape hatch. | Poorest. 30 actors complete in arrival order; deterministic merge needs a central collector — which is (A)/(B) reintroduced. Highest risk of finish-order leaking into output order. |
| Deterministic builds | Neutral — scheduler choice does not affect the build. All options inherit the same OPEN: no `Cargo.lock`, no pinned toolchain (n5 §5). | Same as A, plus one more artifact to pin: stage-planner version affects execution shape, so the plan/stage-boundary encoding must be versioned with the build. | Same as A. |
| Typed errors (7-code map) | Direct. Central loop maps every terminal to one of the 7 `swarm/errors.py` codes (U1.5 `error_taxonomy_map.rs`); `PoolError::Exhausted` ≡ E-CAPACITY, `CycleDetected` ≡ E-DEP single-owner mappings. | Same as A per stage **plus** join-level mapping: barrier timeout → E-TIMEOUT (straggler named), failed participant = join input value (not hang), join-affinity violation → E-CONTRACT (U4.3). Slightly larger error surface but each code keeps one owner. | Scattered. Each actor maps its own errors; consistent E-POLICY scrubbing and E-CONTRACT shape across 30 actors needs a shared error crate + conformance test. More owners, same contract. |
| Backoff + jitter | Direct. AIMD + `retry.backoff_s`/`retry_after` + fail-streak halving live in one limiter (`aimd_limiter.rs`, U2.3). Proven by Python (`swarm_run.py:38-56`). | Same limiter per stage + join-level retry lane (`FAILED → RETRY → PLANNED/CLAIMED`, attempt ≤ 3, rework ≤ 2 per `LEGAL_TRANSITIONS`). Needs a rule for whose backoff governs a retried stage input (answer: task-level backoff from U2.3; barrier never retries by itself). | Per-actor backoff diverges under load (30 independent AIMD states oscillate). Needs a shared limiter service — central again. |
| Idempotency | Session-level dedup by (task-id, attempt) in one store; cancel-then-retry never double-applies because the central loop owns the transition table (U1.1). | Same, with join granularity: barrier re-emit after partial join must be idempotent by (stage-id, join-seq); retry lane re-enters the frontier as a new attempt, never a duplicate output. Requires the join-seq to be part of the idempotency key (n7 input). | Hardest. Two actors serving the same agent type can double-execute without a shared claims check; `notes.claims` overlap exclusion needs distributed agreement. |
| Limits (spawn, fuel, memory, serde) | One enforcement point: semaphore acquire-or-E-CAPACITY, bounded mpsc (`try_send` → E-CAPACITY), per-call fuel/memory via U5.3. | Same per stage + session-level allocator + barrier deadline (straggler → partial-join timeout, never hold threads across a barrier without a deadline — n3). Two limit layers must agree; the session allocator is authoritative on disputes. | Per-actor limits are easy to set, hard to sum: 30 actors × per-actor fuel can exceed the session budget unless a central allocator exists. |
| Correlation-ID logging | One logger: every dispatch/completion/cancel carries `toolCallId`/correlation + session id (n4 §6 ordering rule). | Same + `stage.join` spans (input cardinality, ordered output keys, partial-join flags per n3 hook 2). Strictly more informative for diagnosing where parallelism collapsed; cost is stage × session metric cardinality. | 30 local streams to stitch; session-level trace must be reconstructed by summing actor states (racy). Highest cardinality, weakest session assertions. |
| Concurrency / latency metrics | One metrics owner (`SchedulerMetrics` + gauge(concurrency), histogram(queue_wait), histogram(exec_time)). Single place to assert forced-parallel (n3 hook 1). | Same per stage + join-level metrics (barrier wait, straggler identity, partial-join rate). Dashboards aggregate stage → session by construction (n3 mitigation). | Concurrency must be reconstructed from 30 actor states (racy); session queue-wait is not directly observable. |
| Tests: happy + contention + fault-injection | Cheapest to cover: happy (P0 fan-out asserts max-concurrency == min(N,cap); chain+overlap asserts DAG respect), contention (60-task flood never exceeds 30), fault (worker exception → one-task E-INTERNAL; cancel → all leave within budget). | Same three **per stage** + join-level three: barrier order-independence under shuffled durations, partial-join with one FAILED, straggler-deadline naming. Larger suite, but each case has a structural hook (the barrier) to assert against. | Happy is easy per actor; contention and fault-injection at session scope need a 30-actor harness with message-order control — most expensive suite for weakest session guarantees. |

**Scheduler judgment.** (B) wins on determinism and timeout ownership, (A) wins
on simplicity and single-owner safety, (C) wins only on per-thread isolation —
which is adoptable as a pattern without adopting the topology. The n3 composition
(B shell, A engine per stage, C supervisor per thread) takes each winner's
strength and is the only combo with an owner at every timeout level
(thread < stage/matrix < session). Its distinctive costs — stage planner,
permit partitioning, two cancel paths, stage × session metric aggregation — are
real but mitigated by keeping (A) frontier logic intact inside each stage (the
escape hatch) and by one session-supervisor-owned cancel fan-out.

---

## 2. Rust/Wasmer comparison: strangler vs full-WASI vs gated fallback

n4 designs one concrete path: strangler `CoreBackend` trait + full-core wasip2
packaging + frozen TS bridge + 11-case parity harness. This section compares it
against its two natural alternatives so the selection in §4 is a choice, not a
ratification.

- **R-S (strangler, n4 design).** One `CoreBackend` trait in `gsd-adapter` (one
  method per frozen entrypoint, identical argv/exit-code/JSON schemas), per-call
  `GSD_ENGINE=python|rust` routing (default `python`), no mixed-engine joins
  (join-affinity guard → E-CONTRACT), migration order read-only → plan/assign →
  gates (byte-identical signatures) → `swarm_run` policy, Python truth on any
  divergence (flag reverts per path, fixture recorded).
- **R-F (full-WASI big-bang).** Whole Rust core ships as one `gsd-core.wasm`
  from day one; every call crosses host↔guest; Python retired at cutover, not
  per path. n4's packaging (§5: wasip2, hashed versioned artifact, deny-by-default
  FS/net, capability-gated imports) without n4's routing (§3).
- **R-G (gated fallback: host-native Rust first, Wasmer-gated).** Rust core runs
  natively on the host first behind the same `CoreBackend` seam; Wasmer sandbox
  enables per route behind `health` + parity gates with automatic fallback
  (sandbox fault → host-Rust → Python per `standard_handling`). Same parity
  harness as R-S; sandbox is a deployment qualifier, not the execution premise.

| Criterion | R-S strangler (n4) | R-F full-WASI big-bang | R-G gated fallback |
|---|---|---|---|
| Concurrency safety | Strong. Bounded Tokio pool (one runtime/host process, ≤ 30/session), single `Semaphore` acquire-or-E-CAPACITY, bounded mpsc, `Send+Sync` + one lock order, `CancellationToken` tree per session (n4 §4). Sandbox faults are per-call E-codes, not pool corruption. | Same mechanisms, larger blast radius per fault: a host-import or toolchain mis-size breaks **all** paths at once instead of one route. Concurrency code cannot be exercised without the sandbox, so scheduler bugs and sandbox bugs arrive together. | Strongest sequencing. Concurrency model proves out natively first (fast iteration, native debuggers/profilers), sandbox qualifies per route after. Same mechanisms as R-S once sandboxed. |
| Determinism | Preserved by construction: per-route byte-compare (exit code + parsed JSON) against Python truth; `rand` is host-seeded for replay (n4 §5 import 3); merge/provenance owned by scheduler (§1), unaffected by engine. | Riskiest. No per-route truth anchor at cutover — a divergence is a fleet-wide behavior change, and `'mismatch ⇒ Rust bug, revert flag'` (n4 §3) has no flag to revert. Determinism must be proven before any production traffic, lengthening the big-bang gate. | Same as R-S, plus a three-way comparator (Python vs host-Rust vs Wasm-Rust) that separates scheduler nondeterminism from sandbox nondeterminism when a fixture diverges. |
| Deterministic builds | Per-route pinning is possible (module + digest per strangler step) but the OPEN remains: no `Cargo.lock`, no `rust-toolchain.toml`, no wasm target stanza (n5 §5). R-S needs the pin before the **first** route flips, not before any compiles. | Requires the full pin (toolchain + Wasmer version + target + profile + digest, hash-verified in CI) before **anything** runs — latest possible feedback on the build story, earliest hard dependency on undecided n5 §5 facts. | Same pin as R-S, but host-native Rust proves the dependency closure first; Wasm pin follows per route. Splits one blocking decision into two smaller ones. |
| Typed errors | 1:1 seven-code map (n4 §2 table) enforced at the seam per route; wasmer trap → E-INTERNAL by default, narrower only with evidence; E-POLICY scrubbing preserved. Mismapped payloads surface per route, not fleet-wide. | Same table, no per-route bulkhead: a trap-mapping error (e.g. fuel-exhaustion misfiled as E-TIMEOUT vs E-INTERNAL) affects every path simultaneously. | Same as R-S; fallback itself is typed (sandbox-unavailable → E-DEP failover, not silent host-escape — U5.5 fail-closed). |
| Backoff + jitter | `ErrorHandler` semantics (5 attempts, 1 s + jitter, max 5; E-DEP/E-CAPACITY retryable, mutating calls never auto-retry) move into `gsd-scheduler`/`gsd-failure` per route; `RetryPolicy.retryable_error_codes` carries the n4 §2 retryable column. | Same semantics, unproven at scale until cutover; a backoff bug (tight E-CAPACITY loop) hits all paths at once. | Same as R-S; native-first lets backoff be load-tested without serde noise, then re-verified under sandbox latency. |
| Idempotency | Per-invocation routing + join-affinity guard: a retried call re-enters on the same engine; mutating calls never auto-retry (caller re-checks `swarm_status` first, D-07). | Join affinity is trivially satisfied (one engine) but call-level idempotency gets no per-route proving ground; the D-07 re-check path must be correct on day one for all lanes. | Same as R-S; fallback transitions (Wasm → host → Python) must preserve the idempotency key (task-id, attempt / stage-id, join-seq) — one extra rule beyond R-S. |
| Limits | Serde limits shared both ways (100 000 bytes/value, depth e.g. 32, unknown-field rejection; oversize → E-INPUT/E-CONTRACT, never silent truncation — n4 §6); fuel/memory per call (U5.3); `health` gate (artifact + hash + runtime + last-latency, failure ⇒ E-DEP). | Same limits, sized once for the whole core — any under-size (e.g. serde budget fit for pure core, not full core per q3) blocks everything. No route-by-route sizing feedback. | Same as R-S, sized per route; host-native baseline measures true guest-exec time so the serde budget is sized from data, not guesswork. |
| Correlation-ID logging | `runPy` stays the only spawner; concurrent invokes resolve in completion order, each result keeping its `toolCallId`/correlation; `omp-callback` is schema-validated within serde limits (n4 §5–6). | Same, but every log line pays the `log`-import crossing from day one; a logging-schema mistake blinds all paths. | Same as R-S; host phase validates the log schema natively before it crosses the `log` import. |
| Metrics (concurrency/latency) | Wasmer overhead metered separately per run (cold-start, warm-invoke, serde vs guest-exec, peak linear memory, fuel); gate fails if serde dominates without recorded mitigation even at wall-time parity (n4 §7). Per-route data accumulates into the full-core budget. | Same metrics, single verdict: parity-or-better for the whole core or no ship. No partial credit for 10 green routes while the 11th sizes the budget. | Same as R-S with a baseline advantage: host-native p95 per path is the denominator, so Wasm overhead (cold/warm/serde) is measured as a delta from day one. |
| Tests (happy/contention/fault) | 11-case parity harness per route (n4 §7: plan, transition legal+illegal, gate pass+fail-major byte-identical, 100 KB boundary, validation-error, empty, large, malformed, timeout, policy, classifier quadruple-mirror) + perf gate per strangler step. Smallest failing surface per run. | All 11 cases must pass for the whole core before any value ships; fault-injection (trap/OOM/capability-denial mid-matrix) is fleet-fatal in test, not route-scoped. Longest time-to-first-green. | Same 11 cases per route × two engines (host + Wasm) + fallback-transition cases (sandbox fault mid-join → typed failover, no partial-as-success). Slightly larger suite than R-S; buys the fastest545 diagnosis when a case fails. |
| n5-constraint fit | Best. Needs no Wasmer version/target/imports, FS/net policy, SDK clients, ExtensionAPI versioning, p95 baseline, snapshot semantics, parity corpus, or lockfile **up front** — each is supplied per route as its gate requires (see §6 inputs). | Worst. Needs nearly all n5 §5 + §6 + §4 answers before anything runs. Directly violates the "cheapest discovery per gate" spirit of n5. | Same as R-S, plus one postponement: Wasmer facts are needed only when the first route qualifies for sandbox, not when the first route ships in Rust. |
| Compile-first fit | Best. Native-Rust strangler steps (U4.1–U4.4) compile and prove policy **before** any `.wasm` exists; Wasmer units (U5.1–U5.5) land behind the seam. The n3 missing-module + `.inc()` + empty-crate work is all host-side. | Worst. Nothing runs until the wasip2 toolchain + module build (U5.1) + host bridge (U5.4) exist — the compile-first milestone becomes a sandbox-first milestone. | Best (shared with R-S). Host-native steps unblock the scheduler first; sandbox follows. |

**Rust/Wasmer judgment.** R-S dominates R-F on every risk axis that matters
before first green: smaller blast radius, per-route revert, per-route budget
sizing, no up-front dependence on open n5 facts. R-G strictly improves on R-S in
diagnosability (three-way comparator, host baseline as serde denominator) at the
price of maintaining one extra execution mode plus fallback-key discipline. R-F
is rejected: it couples the two hardest unknowns (scheduler correctness +
full-core sandbox cost) into a single cutover with no revert flag.

---

## 3. Determinism judgment: ordered merge with provenance, partial-success retry

Requirement (from n2 U3.5 + n3 hook 2): session output is a deterministic
function of (stage inputs, merge-key order, attempt history) — never of
finish order, engine choice, or retry timing. Verdict: achievable **iff** the
four rules below hold; the selected combo (§4) makes each rule structural rather
than conventional.

1. **Ordered merge with declared keys.** Every join (matrix completion log per
   U2.4, nexus barrier per U2.5) emits outputs sorted by the declared merge key
   (stage, DAG-depth, creation, task-id), not arrival order. The n3
   shuffle-durations fixture proves it: identical input set with permuted
   durations must yield byte-identical output order. Engine must not appear in
   the key (no mixed-engine joins guarantees this — U4.3 rejects straddles with
   E-CONTRACT).
2. **Provenance on every record.** Every merged output carries (producer
   thread-id, attempt number, verdict envelope incl. gate signatures where
   applicable, join-seq). Gate-verdict signatures must be byte-identical across
   engines (n4 §7 case 3) or provenance comparison is meaningless. Unknown-field
   rejection (n4 §6) protects provenance shape from silent drift.
3. **Partial success is a terminal join state, not an error.** `PARTIAL` (≥ 1
   SUCCEEDED + ≥ 1 FAILED/CANCELLED_TIMED_OUT, or frontier non-empty at timeout —
   n2 §3) flows to U3.5 aggregate with per-thread artifacts + lineage; retry
   re-enters via `FAILED → RETRY → PLANNED/CLAIMED` (attempt ≤ 3, rework ≤ 2).
   The barrier marks partial-success rather than failing (n3 hook 2d); the
   central-loop equivalent records the same flag on the completion log. Retry of
   a partial join uses (stage-id, join-seq, attempt+1) as the idempotency key —
   re-emit of an already-joined output with the same key is deduped, never
   duplicated.
4. **Replay from the dispatch log.** Deterministic replay needs dispatch order +
   merge keys + attempt history, not just completion order (n3 §comparison row).
   `rand` comes only from the host-seeded import (n4 §5); guest wall-clock reads
   are forbidden (host-owned `time` import) so timeouts replay from recorded
   deadlines, not ambient time. What replay does **not** cover (OPEN per n5
   §7): crash-resume authority (`context_window` vs `Checkpoint`), retention/GC
   — n7 must not design replay past cancel/timeout until A03/A07 answer.

Determinism anti-requirements (explicitly out): no wall-time in merge keys, no
engine tag in output order, no silent truncation/normalization at the serde
boundary (oversize → E-INPUT/E-CONTRACT per n4 §6), no cross-session state in
the merge (sessions are disjoint by `SessionId`; `SESSION_BRANCHED/MERGED`
records are lineage, not merge inputs).

---

## 4. Failure-mode → mitigation table

Each row: mode → detection → mitigation (owner unit) → residual risk. Error-code
citations are n4 §2; lifecycle citations are n2.

| # | Failure mode | Detection | Mitigation (owner) | Residual risk |
|---|---|---|---|---|
| F1 | Data race on shared scheduler/store state (scheduler, frontier, claims, TaskStore) | `cargo test` contention suite + lock-order assertion; `Send+Sync` bound violations fail at compile time | Single lock order session → join → task → store; `Arc<RwLock>/<Mutex>` only; no blocking I-O inside async workers (`spawn_blocking` or host side) (U2.3, n4 §4). Central-per-stage executors keep one lock domain per stage. | Lock-order discipline is conventional until a lock-order checker or loom-style test exists — n7 input. |
| F2 | Deadlock: DAG cycle, claims standoff, barrier waiting on a never-dispatched participant, permit-partition starvation | `detect_cycles` at ingest (E-DEP); `WaitForGraph` + deadlock flag (already in `gsd-pool`/`gsd-runtime`); barrier deadline fires | Cycle → `SchedulerError::CycleDetected` ≡ E-DEP (U2.1); barrier deadlines turn stragglers into partial-join timeouts (U2.5); session permit allocator prevents cross-stage starvation (§1); never hold threads across a barrier without a deadline (n3). | Dynamic deps discovered mid-stage can force a stage split — escape hatch (intra-stage DAG dispatch) must itself be deadlock-checked. |
| F3 | Unbounded spawn (task flood, retry storm, stage fan-out × stages) | Concurrency gauge + semaphore rejection counters; 60-task flood fixture | Global ≤ 30/session semaphore + per-agent `max_parallel` + claims exclusion (U2.3); acquire-or-E-CAPACITY (try-acquire with deadline, never unbounded wait); bounded mpsc, `try_send` → E-CAPACITY (n4 §4); session allocator partitions across stages (§1). | Partition policy (equal vs priority-weighted) is undecided — n7 input; wrong split starves a critical stage under flood. |
| F4 | Panic / timeout contagion (one thread's panic, hang, or trap kills siblings or the loop) | Per-thread outcome recorded as FAILED with cause; siblings' heartbeats continue (`session_monitor` read-only, U3.3) | Supervisor per thread: catch-unwind + timeout timers + trap mapping → per-thread FAILED per `LEGAL_TRANSITIONS`; worker-exception-isolates-to-one-task (E-INTERNAL, never siblings — U2.3); session cancel via `cancel/<corr>` + token tree, 10 s cooperative inside 15 s budget (U3.4, n4 §4). Barrier treats failed participant as join input value, not hang (U2.5). | Central-loop bug still has session blast radius — needs the fault-injection suite (panic/timeout/trap per thread, siblings assert DONE) from n3 hooks. |
| F5 | Wasmer trap / OOM / fuel-exhaustion mid-matrix | Fuel metering + linear-memory cap per call (U5.3); trap carries call identity + fuel consumed | Trap → E-INTERNAL default, narrower only with evidence; fuel-exhaustion → E-TIMEOUT only with a deadline mapping else E-INTERNAL (n4 §2 — keep the variants distinct); OOM → E-CAPACITY; session continues only if join semantics allow (U5.5), else session ERROR. Fail-closed, no silent host-escape. | Fuel→timeout misclassification flips retry behavior (E-TIMEOUT requeues once, E-INTERNAL does not) — keep distinct internal variants per n4 §2; n7 input. |
| F6 | Capability violation / fail-closed denial (missing token, FS/net overreach, plan-mode mutation, foreign gate) | Per-import token check (session id + allowed ops + expiry, minted per call — n4 §5); FS/net deny-by-default | Denial ⇒ E-POLICY fail-closed, escalate A10/A01 (n4 §2); omp message scrubbing preserved (`E-POLICY` token never in `Error.message`, details field only); tokens never cached across sessions. | Over-narrow default policy breaks legitimate full-core paths (q3 surface is large) — capability wish-list from the n4 spike (n5 §5) must precede U5.4. |
| F7 | Plugin unavailable / slow / crash mid-matrix (missing `.wasm`, host-import down, storage backend down, `omp-callback` failure, bridge timeout) | `health` (artifact + hash + runtime + last-latency; failure ⇒ E-DEP — n4 §6); per-call `timeoutMs` host-enforced (600 s; QA 1860 s); guest fuel is backstop, not authority | Missing artifact/import/backend → E-DEP with exp-backoff 1 s→60 s jitter max 5 (n4 §2); slow → host timeout ⇒ instance drop + E-TIMEOUT + checkpoint/lease handling; crash mid-join → partial join with the crashed participant marked (never partial-as-success); mutating calls never auto-retry (D-07 re-check first). | Timeout values are inherited from Python defaults, not measured (n5 §6 OPEN) — first p95 samples may force re-budget; n7 input. |
| F8 | Partial success mishandled (retry duplicates output, partial read as failure, straggler silently dropped) | Join-seq + attempt on every record; `PARTIAL`/`ERROR`/`SUCCESS` derived per n2 §3 table; partial-join rate metric | §3 rules: declared-key merge, provenance, PARTIAL as terminal join state, retry via `FAILED → RETRY → PLANNED/CLAIMED` (attempt ≤ 3, rework ≤ 2), idempotency key (stage-id, join-seq, attempt); straggler named in the partial join, never silently dropped (n3 hook 2). | Idempotency-key interlock across fallback transitions (R-G) needs an explicit test — §5 condition. |
| F9 | Secret leakage across the sandbox boundary (tokens, signatures, env passthrough, log over-capture) | Schema validation at the boundary (unknown-field rejection on error/verdict shapes; `error.code` allowlist — n4 §6); E-POLICY scrub assertion in the harness | Stripped env (only `SWARM_DIR`-equivalent, never ambient); per-call tokens, never cross-session cache; ed25519/hmac envelope handling stays host-side; E-POLICY message scrub unit-tested (n4 §7 case 10); payload caps (100 KB/value, depth 32) bound log over-capture. | Gate-signing keys and token-minting authority location (host-only vs HSM) undecided — A10 input, n7 must not assume. |
| F10 | Cross-thread injection (one thread's output steers another: prompt/artifact/claims poisoning, classifier divergence) | `notes.claims` overlap exclusion (U2.2 pure frontier function); FINDING/verdict schema validation; classifier quadruple-mirror check (n4 §7 case 11) | Independence detector excludes overlapping claims before dispatch (U2.2, no dispatch side effects); per-thread `capability_manifest` bind at create (U3.1); lifecycle hooks are notify-only, MUST NOT mutate state (U1.4); any classification change edits all three mirrors + fixture together (U6.4); session aggregate (U3.5) merges artifacts with lineage so a poisoned input is attributable. | Claims-overlap semantics for genuinely collaborative tasks (overlap intended) is undefined — A02 input; default-deny risks false serialization. |
| F11 | Scheduler regresses to serial (silent forced-parallel loss) | Forced-parallel fixture: N independent P0 tasks assert max-concurrency == min(N,cap) and every `dispatched_at` precedes every `completed_at` (n3 hook 1); concurrency gauge alert | Structural enforcement from (B): barrier observes arrival cardinality; a stage cannot silently serialize (n3). Central-loop path additionally asserts frontier width > 1 when DAG allows. | Gauge-only detection catches loss after the fact — keep the fixture as a gate, not a dashboard. |
| F12 | Deterministic-build drift (unpinned Wasmer/toolchain/dep changes behavior silently) | Hash-verified manifest in CI (not tag — n4 §5); module + digest emitted atomically; `init` hash-verifies `.wasm` (n4 §6) | Pin `rustc` + `wasmer` + target + profile in one manifest; content-addressed distribution, never implicit `latest`; `Cargo.lock` creation is the first build milestone (§5 order). | No lockfile exists today — every estimate below assumes the pin lands first; without it all parity evidence is non-reproducible. |

---

## 5. Selected combo with priced tradeoffs

**Select: (B) staged matrix/nexus + barrier, A-mechanics per stage, C-supervisor
per thread (n3) × R-S strangler with R-G fallback discipline (n4 §3 + §5–7).**

Concretely: session architecture is §1's B/A/C composition (U2.1–U2.6, U3.1–U3.6,
U1.1–U1.5 per the n2 ownership table); migration is n4's strangler (U4.1–U4.4:
`CoreBackend` seam, per-call routing default-python, join-affinity guard,
throwaway shims); sandbox is n4's full-core wasip2 packaging (U5.1–U5.5) enabled
per route behind `health` + parity + perf gates with typed fallback to host-Rust
then Python (the R-G discipline: sandbox as deployment qualifier, not premise).
omp stays frozen (U6.1–U6.4 pins + classifier triple-mirror rule).

### Why this combo (one line per axis)

- Concurrency safety: single-owner limits per stage + session allocator; one lock
  order; supervisor isolation per thread; smallest per-step blast radius
  (one route, one stage).
- Determinism: barrier as structural merge point + per-route byte-compare against
  Python truth + host-seeded `rand` + §3 replay rules.
- Production bar: every bar item has an owner and a gate (typed errors U1.5;
  backoff U2.3; idempotency U3.4/U4.3; limits U5.3; correlation IDs n4 §6;
  metrics per stage + join; 11-case harness per route).

### Priced tradeoffs (accepted explicitly)

1. **Full-core serde cost (q3, the largest price).** Every call crosses
   host↔guest as serialized JSON: argv in, exit-code/JSON out, plus mid-call
   `storage` and `omp-callback` round-trips — the highest-cost Wasmer mode by
   design (n4 §7 acknowledgment). Priced as: per-route budget of
   (cold-start, warm-invoke, serde marshal+unmarshal, guest-exec, peak memory,
   fuel), with the gate failing if serde dominates **even at wall-time parity**.
   Mitigations in order (n4 §7): input-file paths over inline blobs; WarmPool
   instance reuse (U1.3); batched lane-verb reads; validate-once-at-boundary;
   serde budget as first-class metric with alert threshold. NOT done: shared
   mutable memory bypass, import widening, TS native ABI — all void isolation.
   The host-Rust baseline (R-G discipline) prices this honestly: without a
   native denominator the sandbox overhead is unmeasurable, and per n5 §6 no
   baseline exists yet — the first 2–4 h timing sample (hot-path p50/p95 +
   hardware fingerprint) is a precondition of the first sandbox gate, not a
   follow-up.
2. **Compile-first delay on all design work.** The n3 missing modules, `.inc()`
   bug, `gsd-failure` empty crate, and manifest-only `gsd-nexus` must compile
   before any stage/sandbox work. Price: first milestone ships zero behavior
   (build + skeleton tests only). Sequencing: `Cargo.lock` + toolchain pin →
   missing scheduler modules + metrics fix + `gsd-failure` taxonomy → nexus
   skeleton types → strangler seam + first read-only route. No Wasmer work
   before the host build is green.
3. **Stage-planner + permit-partitioning complexity.** Cross-stage deps compiled
   into boundaries; session cap partitioned across stages; two cancel paths;
   stage × session metric cardinality. Price: one planner component, one
   allocator policy decision (equal vs priority-weighted — n7), one
   session-supervisor cancel fan-out, aggregated dashboards. Escape hatch (A
   frontier intact per stage) bounds the planner risk.
4. **Barrier straggler latency.** Slow tasks idle joins. Price: every barrier
   carries a deadline; stragglers become named partial-join timeouts in the
   retry lane; refill keeps other stages busy; never hold threads across a
   barrier without a deadline.
5. **Strangler duration + shim hygiene.** Per-route migration is slower than
   big-bang and carries temporary dual paths. Price: clean-cutover rule (migrate
   every caller, remove shim, no permanent dual-path — U4.4); Python-truth
   revert per path bounds each step's risk. R-F's single-cutover risk is
   explicitly rejected as the more expensive failure mode.
6. **Fallback-mode maintenance (R-G delta over pure R-S).** Three-way
   comparators and fallback-key discipline cost suite size (11 cases × two
   engines + transition cases). Price accepted because it buys the only fast
   diagnosis when a fixture diverges (scheduler bug vs sandbox bug) and the only
   honest serde denominator.

### Explicit non-goals restated (scope guard)

No workflowz language (§n5.9); no merged matrix/nexus abstraction (§n5.10); no
omp edits beyond frozen pins (U6.1–U6.4); no `ExtensionAPI` versioning assumed
(n5 §3 — breaking change is a flag-day, out of scope); no snapshot/resume
semantics assumed (n5 §7 — replay stops at cancel/timeout).

---

## 6. Concrete next inputs n7 needs (in dependency order)

1. **Build pin (A11 + A03):** `Cargo.lock` created; `rust-toolchain.toml` +
   `wasmer` version + wasm target (`wasm32-wasip2` default, `wasi` only with
   recorded justification) + profile pinned in one manifest, hash-verified in
   CI. Unblocks everything reproducible (F12). Decides n4 open O1/O2 per route.
2. **Compile-first closeout (implementers):** missing `gsd-scheduler` modules,
   `inc_submitted` fix, `gsd-failure` taxonomy source, `gsd-nexus` skeleton
   types — workspace `cargo build` green. n3 milestone exit.
3. **Hot-path p95 samples (A13 + A08):** `swarm_run` dispatch tick, one gate
   script, one `bridge.ts` round-trip — 50+ samples each, p50/p95 + hardware
   fingerprint recorded into the perf gate (n5 §6 cheapest discovery). Sets the
   serde denominator (tradeoff 1) and timeout re-budget evidence (F7).
4. **Wasmer half-day spike report (A03 + A11):** one `gsd-*` crate to the pinned
   target under the pinned Wasmer: what compiles vs stubbed (tokio/net/fs),
   candidate host imports (sized for FULL core per q3), WASI capability
   wish-list, FS/net policy draft, distribution story. Records the exact version
   + triple (n5 §5). Unblocks U5.1–U5.4 sizing (F5/F6).
5. **Parity-corpus backlog (A02 + A08):** uncovered-lane list (`ls scripts/`
   × `tests/` cross-ref, 1–2 h) promoted to fixture backlog; the 11-case harness
   (n4 §7) instantiated for the first read-only route. Unblocks the first
   strangler flip (F8, §3 rules).
6. **State-semantics answers (A03 + A07):** snapshot authority
   (`context_window` vs `Checkpoint`), resume-after-crash vs after-cancel,
   retention/GC, `live.json`-restore carryover or replacement (n5 §7 cheapest
   discovery: `gsd-state`/`gsd-pool` heads, 1 h). Unblocks recovery design; n7
   must not invent these.
7. **Allocator + backoff + idempotency decisions (scheduler owner):** stage
   permit-partition policy (F3); whose backoff governs retried stage inputs
   (§1); join-seq in the idempotency key + fallback-transition key preservation
   (§5 condition, F8); fuel→timeout vs internal classification rule (F5).
8. **Security inputs (A10):** gate-signing key + token-minting authority
   location (F9); FS/net deny-by-default exceptions for full core, if any (F6);
   fault-injection suite scope sign-off (F4/F11).
9. **omp versioning answer (A03 + A06):** host-owner confirmation whether
   `ExtensionAPI` unversioned-ness is pin-by-commit or omission (n5 §3, 30 min).
   Required before any contract-adjacent n-design; default remains frozen.
10. **n7 plan-shape constraint:** keep matrix/nexus seams split (n5.10), keep
    per-file ownership disjoint per the n2 table, and sequence milestones as
    §5 tradeoff 2 (pin → compile → spike → first read-only route) — no sandbox
    or stage design ahead of its gate evidence.
