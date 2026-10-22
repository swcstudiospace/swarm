# Scheduler options for forced-parallel matrix/nexus · Refs SPE-18067

Source of truth for behavior: `scripts/swarm_run.py:38-56` (DESIGN v2.2 D5),
`swarm/taskstore.py` (14 states, `LEGAL_TRANSITIONS`, `MAX_REWORK_LOOPS=2`,
`DEFAULT_MAX_ATTEMPTS=3`, `GATES_BY_RISK`), `plan/inventory.md` (settled q1–q4).
Rust starting point: `crates/gsd-scheduler/src/lib.rs` (`MatrixScheduler`),
`crates/gsd-pool/src/lib.rs` (`ThreadPool::acquire`), `crates/gsd-nexus/Cargo.toml`.

Settled (do not relitigate): matrix = flat fan-out; nexus = staged
fan-out/fan-in with barrier join; cap ≤ 30 threads/session (inventory §Out of
scope); perf = parity-or-better; Python scheduler is the behavior baseline.

## Repo-grounded baseline the winner must preserve

Python (`swarm_run.py:38-56`):

- Persistent daemon worker pool, refilled on **every completion** (`reconcile` +
  `dispatchable` + `select_batch` after each finish — one slow task never holds
  back newly ready work). `SWARM_SCHEDULER=rounds` restores the old per-round
  barrier only as fallback.
- Order: priority (P0 first), DAG depth, creation time.
- Caps: global `--max-parallel` (default 4, ceiling 32 — Rust target clamps to
  ≤ 30/session per inventory); per-agent manifest `max_parallel`; `notes.claims`
  keep overlapping tasks apart.
- AIMD degrade: provider rate-limit halves effective parallelism once per burst
  (`swarm.degraded`, additive recovery after `SWARM_RATE_COOLDOWN_S`); transient
  failure waits `swarm.retry.backoff_s`; isolated failure never lowers
  parallelism (retry/escalation ladder); `SWARM_FAIL_STREAK` (default 3) halves
  parallelism on consecutive distinct-task failures, success resets.
- Cancel: `cancel/<correlation_id>` + process-group kill, `SWARM_CANCEL_GRACE_S`
  (10) cooperative wait inside `SWARM_SHUTDOWN_BUDGET_S` (15).
- Worker exception fails that one task (`E-INTERNAL`), never siblings.

Taskstore (`swarm/taskstore.py`): only A01 calls `transition`; terminal states
`DONE`/`CANCELLED` have no outgoing edges; `FAILED → {RETRY, ESCALATED,
CANCELLED}`; `RETRY → {PLANNED, CLAIMED, ESCALATED}`; rework loops capped at 2;
gate risk classes in `GATES_BY_RISK`.

Rust status (evidence, not aspiration):

- `MatrixScheduler` exists as a skeleton: `submit` (governor budget reserve →
  DAG insert → cycle check → policy priority → `ThreadPool::acquire`), config
  default `max_concurrent_tasks: 30`, `SemaphoreManager` global 30 permits,
  `WaitForGraph` + deadlock detection flag. Gaps: `pub mod dag; policy;
  semaphore; clock; dynamic; hitl;` are declared but **no such files exist**;
  `submit` calls `self.metrics.submitted.inc()` but `SchedulerMetrics` only
  defines `inc_submitted` (AtomicU64 has no `.inc`); the passed-in
  `failure_isolation` is discarded and replaced with `FailureIsolation::new()`
  from `gsd-failure`, which has **no `src/`**; `SemaphoreManager::get_or_create`
  is dead code; `submit` acquires a pool handle but never drives task execution,
  never re-queues on completion (no refill loop), never orders by
  priority/DAG-depth/creation. Nothing here yet enforces forced-parallel.
- `gsd-nexus` is `Cargo.toml` only (no `src/`): pub/sub, request/response, CRDT,
  broadcast are manifest keywords, not code. Any nexus barrier join is
  greenfield design.
- `gsd-pool::ThreadPool::acquire` exists; `gsd-runtime::sync::WaitForGraph`
  exists; `gsd-governor::Governor` and `gsd-observability::TracingLayer` exist
  as types. Supervisor-style per-thread isolation exists nowhere.

## Options

### (A) Central DAG scheduler + bounded pool

One `MatrixScheduler`-style central loop owns the DAG, computes the ready
frontier (`get_ready_tasks`), and hands tasks to a bounded pool
(`SemaphoreManager` global 30 + per-resource semaphores + `ThreadPool`).
Refill-on-completion replicates `swarm_run.py` reconcile/dispatch/select_batch.

### (B) Staged matrix/nexus executors with barrier join

Flat matrix stages do fan-out execution; nexus stages do fan-out/fan-in with an
explicit barrier join coordinated over nexus channels (the settled q2
definition). Each stage has its own frontier + cap; the barrier merges stage
outputs deterministically before the next stage dispatches. `SWARM_SCHEDULER=
rounds`-style per-round barrier already exists in Python as the fallback, so
the barrier semantics have a tested precedent — but per-stage (not global).

### (C) Actor-per-thread with supervisor restart

Each of the ≤ 30 chat threads is an independent actor with its own mailbox;
a supervisor owns restart policy (panic/timeout/trap → per-thread `FAILED`,
mailbox drained, siblings untouched). No central ready-queue: actors pull work
advertisements or receive direct assignment.

## Comparison (8 criteria)

| Criterion | (A) Central DAG + pool | (B) Staged matrix/nexus + barrier | (C) Actor-per-thread + supervisor |
|---|---|---|---|
| Forced-parallel-by-default enforcement | Strong: single choke point can refuse serial dispatch, assert frontier width > 1 when DAG allows, apply priority/DAG-depth/creation order exactly as Python. Weakest link is scheduler throughput, not policy. | Strongest: stage shape *is* the parallelism contract (flat fan-out vs staged fan-in); a stage cannot silently serialize because the barrier join observes arrival cardinality. Enforcement is structural, not just a code path. | Weakest: parallelism is emergent (30 actors each deciding to pull). Forced-parallel needs an extra enforcer watching mailboxes — at which point it is (A) with extra hops. Default failure mode is 30 idle actors, not 30 busy threads. |
| Dependency-DAG handling | Native: petgraph DAG + `detect_cycles` + `get_ready_tasks` already sketched in `lib.rs`; maps 1:1 to `depends_on`/`dag_depth` in taskstore. Cross-stage deps are trivial (one graph). | Good within a stage; cross-stage deps must be compiled into stage boundaries up front (stage DAG over task DAG). Dynamic restructuring (`dynamic` module, currently missing) is harder: a newly discovered dep can force a stage split or a barrier violation. Needs stage planner + escape hatch for intra-stage DAG dispatch. | Poor: DAG lives nowhere; must be replicated or centralized per dependency check. Either each actor holds the full graph (30 copies to keep consistent) or a DAG service answers queries (central scheduler by another name). Cycle/deadlock detection across mailboxes is strictly harder than `WaitForGraph` in one process. |
| Cap / backpressure / queueing | Direct: one global semaphore (30, already in `SemaphoreManager`) + per-agent `max_parallel` + `notes.claims` exclusion; AIMD degrade (`swarm.degraded`) is a single variable. Backpressure = stop refilling. Proven by Python. | Good but duplicated: each stage needs its own cap and the session cap must be partitioned across concurrent stages (else 3 stages × 30 = 90). Barrier provides natural backpressure (slow join blocks next stage) but that same blocking risks idle threads while one straggler holds the join. | Awkward: cap = actor count, but queueing is 30 independent mailbox depths. Global backpressure needs a coordinator; per-agent `max_parallel` needs cross-actor agreement (two actors serving the same agent type must share a permit). AIMD halving has no natural owner. |
| Per-thread queue / channel / cancel / deadline | Single mechanism: pool handle + one cancel path (`cancel/<corr>` → cooperative wait → pgroup kill, 10 s inside 15 s budget). Per-thread deadlines are scheduler timers. Simple, matches Python exactly. | Two mechanisms: intra-stage dispatch (same as A) plus stage-scoped channels over nexus (request/response + broadcast per manifest). Cancel must propagate to both the stage frontier and the barrier waiters. More surface, but stage-scoped deadlines compose better with progressive timeouts (see below). | Best single-thread ergonomics: mailbox = per-thread queue, channel = actor address, cancel = poison-pill + supervisor kill. But session-wide cancel (one `cancel/<corr>` → all 30 threads within the 15 s budget) needs a broadcast the actor model must rebuild — which is exactly what nexus already promises. |
| Supervisor isolation (panic/timeout/trap → per-thread FAILED) | Absent today: `lib.rs` discards the injected `failure_isolation`, `gsd-failure` has no source, worker panic path is unimplemented (Python: exception → `E-INTERNAL` for that task only). Must be built: catch-unwind per task + timeout timers + trap mapping, all in the central loop. One bug in the loop affects all tasks. | Same build cost as (A) per stage, plus barrier must treat a failed participant as a join input (failure value, not hang). Partial-stage failure is well-defined (see merge row). Supervisor patterns still needed inside each stage executor. | Native: this is what supervisors are for. Per-actor panic/timeout/trap → `FAILED`, mailbox drained, siblings provably untouched; restart policy per thread type. Strongest isolation story, smallest blast radius for a scheduler bug (supervisor survives worker bugs and vice versa). |
| Deterministic ordered merge + provenance + partial-success retry | Must be bolted on: central completion log ordered by (stage, DAG-depth, creation, task id) with per-record provenance (producer, attempt, verdict envelope). Partial success = frontier continues while `FAILED → RETRY → PLANNED/CLAIMED` cycles per `LEGAL_TRANSITIONS` (attempt ≤ 3, rework ≤ 2). Deterministic replay needs the log to record dispatch order, not just completion order. | Native: the barrier join *is* the ordered merge point — stage outputs merged by a declared key order with provenance attached before release to the next stage. Partial success is a first-class join result (some inputs `DONE`, some `FAILED`→retry lane, stragglers timed out) rather than an afterthought. Retry re-enters the next stage as ordinary input. Replay = re-run the stage with the same input set. | Hardest: 30 actors complete in arrival order; deterministic merge needs a separate sequencer collecting from all mailboxes — a central join by another name, but without the stage structure to key it on. Provenance must be assembled from 30 local logs. Partial-success retry = re-injecting messages, risking duplicates without idempotent handlers. |
| Progressive timeouts (thread / matrix / session) | Flat: thread timer + session timer both live in the central loop; matrix-level timeout has no natural scope (the whole run is one matrix). Workable but coarse. | Native fit: thread timeout (per task) < stage/matrix timeout (barrier deadline — stragglers become join timeouts, not hangs) < session timeout (supervisor budget, Python's 15 s shutdown). Each level has an owner and a defined escalation (task retry → stage partial-join → session cancel). | Thread timeout is natural (per-actor timer); session timeout needs coordination across 30 timers; matrix/stage timeout has no scope at all without stages. Converges to (B) timeouts once stages are added. |
| Observability (concurrency level, queue wait, exec time) | One metrics owner: `SchedulerMetrics` (submitted/completed/failed/cancelled + `scheduling_overhead_ns`) extends naturally to gauge(concurrency), histogram(queue_wait), histogram(exec_time). Single place to assert. | Same per stage + join-level metrics (barrier wait time, straggler identity, partial-join rate) — strictly more informative for diagnosing *where* parallelism collapsed. Cost: metric cardinality × stages; dashboards must aggregate stage → session. | 30 local metric streams to aggregate; concurrency level must be reconstructed by summing actor states (racy). Queue wait per mailbox is easy; session-level queue wait is not. Highest cardinality, weakest session-level assertions. |

## Recommendation

**Adopt (B) staged matrix/nexus executors with barrier join, built on (A)
mechanics inside each stage, with (C) supervisor patterns for per-thread
isolation.**

Concretely: each stage runs a central-DAG + bounded-pool executor that ports
`swarm_run.py` DESIGN v2.2 D5 verbatim (refill-on-completion, priority →
DAG-depth → creation order, ≤ 30/session global permits, per-agent
`max_parallel`, claims exclusion, AIMD degrade, worker-exception → per-task
`E-INTERNAL`); stages coordinate through nexus channels with barrier joins
that produce the deterministic ordered merge; and every thread runs under a
supervisor (catch-unwind + timeout + trap → per-thread `FAILED` per
`LEGAL_TRANSITIONS`, siblings untouched, session cancel via
`cancel/<corr>` + kill inside the 15 s budget).

Why this composition wins: (B) gives structural forced-parallel enforcement,
a native ordered-merge point, and the only timeout model with an owner at each
level; (A) inside each stage keeps the proven Python policy without
reinventing dispatch; (C) patterns supply the isolation story that neither
(A) nor (B) has today (`gsd-failure` is an empty crate directory).

Tradeoffs accepted explicitly:

- Stage planner cost: cross-stage dependencies must be compiled into stage
  boundaries; fully dynamic DAGs (new dep discovered mid-run) need an escape
  hatch (intra-stage DAG dispatch or stage split). Mitigation: keep (A)
  frontier logic intact inside the stage so unstageable work still runs.
- Barrier straggler risk: a slow task can idle a join while threads sit free.
  Mitigation: barrier deadlines turn stragglers into partial-join timeouts
  (retry lane), and the refill loop keeps other stages' threads busy; never
  hold threads across a barrier without a deadline.
- Two cancel paths (stage frontier + barrier waiters) and stage × session
  metric aggregation. Mitigation: one cancel fan-out owned by the session
  supervisor; dashboards aggregate stage → session by construction.
- Greenfield nexus: `gsd-nexus` is manifest-only, and `gsd-scheduler`'s
  `dag/policy/semaphore/clock/dynamic/hitl` modules are declared but missing —
  the first milestone is making the sketched code compile, not stage design.

## Rejection reasons

- (A) alone rejected: no structural forced-parallel guarantee (a central-loop
  regression silently serializes); no merge point (deterministic replay and
  partial-success join must be bolted onto a completion log); flat timeout
  model (matrix scope missing); single-loop blast radius for isolation bugs.
  Correct as a stage executor, insufficient as the session architecture.
- (C) alone rejected: forced-parallel becomes emergent rather than enforced;
  DAG handling, global cap/AIMD, deterministic merge, and session cancel all
  require rebuilding a central coordinator — ending with (A)/(B) plus 30
  mailboxes of accidental complexity. Adopt its supervisor discipline, not its
  topology.

## Acceptance hooks

1. **Concurrency assertion (forced-parallel):** run a fixture DAG with N
   independent P0 tasks (N = session cap, ≤ 30) and assert observed max
   concurrent executions == min(N, cap) and every task's `dispatched_at`
   precedes every task's `completed_at` (no serial chain). Second fixture: P0
   chain (A→B→C) runs serially while an independent P0 task overlaps it —
   asserts DAG respect + parallelism coexist. Grounding: Python order
   priority/DAG-depth/creation; `SchedulerMetrics` gauge(concurrency).
2. **Parallel-start / join trace check:** emit a trace span per thread
   (`thread.start`, `thread.finish` with task id, attempt, provenance) plus a
   span per barrier (`stage.join` with input cardinality, ordered output keys,
   partial-join flags). The check replays the trace and asserts: (a) all stage
   inputs started before the join closed; (b) join output order equals the
   declared merge-key order regardless of finish order (shuffle the fixture
   durations to prove it); (c) every output carries provenance (producer,
   attempt, verdict envelope); (d) a fixture with one `FAILED` task shows
   siblings `DONE`, the failure in the retry lane (`FAILED → RETRY →
   PLANNED/CLAIMED`, attempt ≤ 3), and the join marked partial-success rather
   than failed. Grounding: `LEGAL_TRANSITIONS`, `MAX_REWORK_LOOPS=2`,
   `DEFAULT_MAX_ATTEMPTS=3`, `ACCEPTED_ISSUER_STATES`.
3. **Cap / timeout / cancel hooks:** cap fixture asserts live threads never
   exceed 30/session under a 60-task flood (histogram of concurrency level);
   progressive-timeout fixture asserts thread timeout → per-task `FAILED`,
   stage barrier deadline → partial join with straggler named, session cancel
   (`cancel/<corr>`) → all threads leave within the shutdown budget (Python:
   10 s cooperative inside 15 s). Observability hook: queue-wait and exec-time
   histograms present per stage and aggregated per session.
