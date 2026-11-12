# Rust + FULL-core Wasmer + TS omp interop — design options · Refs SPE-18066

Settled (do not relitigate): Wasmer = FULL core sandbox (user override 2026-10-20) —
the entire Rust core runs inside Wasmer behind narrow host functions; highest serde
cost is accepted. TS omp plugin is frozen authority (no rewrite). Python is
correctness truth; incremental strangler migration; never mix engines within one
session join. Perf = parity-or-better. Concurrency cap ≤ 30 threads/session unless a
load test proves lower safer.

Classifier constraint (applies to any omp-surface work): the prompt classifier lives
in THREE mirrored copies — `swarm/signal_detector.py`,
`hooks/user_prompt_submit.py` fallback, `omp/src/hooks.ts` — plus
`hooks/autonomous_run.py` `classify()`, all pinned by
`tests/fixtures/classifier_prompts.json`. Any interop change touching
classification must change all copies + the fixture together.

Repo facts this design freezes against: `plan/inventory.md`; `omp/src/bridge.ts`
(single `runScript` spawner, exit-contract mapping); `omp/src/tools.ts`
(ID_PATTERN, 14 STATES, PATTERNS, GATE_SCRIPTS, 100 KB cap, QA gate timeout);
`swarm/errors.py` (7 codes + `classify()` + handling); `crates/gsd-proto/src/gsd.proto`
(thread/session model); `crates/gsd-sdk` + `crates/gsd-adapter` (stub manifests only).

## 1. Python entrypoint / I-O / error-shape inventory to freeze

### 1.1 Entrypoints (all under `<SWARM_ROOT>/scripts/`, invoked as `python3 scripts/<s>.py <args> --json`)

| Script | Role |
|---|---|
| `swarm_run.py` (2452 lines) | Continuous scheduler: persistent worker pool refilled on every completion; order priority/DAG-depth/creation; `--max-parallel` default 4, ceiling 32; per-agent `max_parallel`; AIMD degrade on rate-limit/fail-streak; cancel via `cancel/<corr>` + process-group kill. This is the force-parallel policy to port. |
| `orch_plan.py`, `orch_status.py` (`--ingest`/`--transition`), `orch_assign.py` | Plan / status-transition / assignment |
| `qa_gate.py`, `rev_gate.py`, `sec_gate.py`, `rel_plan.py` | Signed gate verdicts (owners pinned by `GATE_AGENTS`) |
| `swarm_tool.py` | Registry call bridge, 100 000-byte input cap |
| 30+ `<lane>_<verb>.py` + `scripts/ts/` Bun twins (36 passthrough, 5 native) | Lane verbs |

Libraries (`swarm/`): `errors.py` (7 codes), `taskstore.py` (14 states + transitions +
gates), `tool_registry.py`, `skill_registry.py`, `envelope.py` (ed25519/hmac),
`telemetry.py`, `handover.py`, `lifecycle.py`, `script_base.py` (argv/exit contract),
`verdicts.py` (`GATE_SCRIPTS`), `gates.py` (`SEVERITIES`, `make_finding`).

### 1.2 argv / JSON / exit-code contract (frozen; Rust must reproduce byte-compatibly)

- argv: `python3 <SWARM_ROOT>/scripts/<script>.py <--flag=value…> --json`, `cwd=root`,
  `stdin=ignore`, `SWARM_DIR=<dir>` in env, `SWARM_TASK_ID`/`SWARM_CORRELATION_ID`
  stripped. Script-stem allowlist: `/^[a-z][a-z0-9_]*$/`.
- `SWARM_DIR` resolution: env `SWARM_DIR` (absolute) → `<git toplevel of cwd>/.swarm` →
  `<cwd>/.swarm`. Bridge-owned input files (`--ingest`, `--per-target-findings`):
  `<SWARM_DIR>/results/<kind>-<sanitised toolCallId>.json`, `.gitignore` = `*`
  (never overwrite).
- Exit codes: `0` = success with JSON object on stdout; `1` = expected failure with
  `{"status":"fail",…}` JSON object on stdout (returned, not thrown);
  `2` = error with `{"error":{"code":E-XXX,"message":…}}` JSON object on stdout
  (thrown as `SwarmToolError(code,…)`); `2` with empty stdout = usage error
  (`E-INPUT` from stderr tail 400 chars). Non-object stdout at any code = `E-INTERNAL`.
  Anything else (unexpected code, `status≠fail` at 1, unknown `error.code` at 2) =
  `E-INTERNAL` with stderr/stdout tail.
- Cancellation: never return after abort (half-applied mutation must not read as
  success). `TimeoutError` abort reason → `E-TIMEOUT`; session shutdown →
  `E-INTERNAL` with "re-check with swarm_status" hint; bridge timeout kill
  (SIGTERM → SIGKILL after 3 s grace in own process group) → `E-TIMEOUT`.
  Defaults: 600 s bridge timeout; QA gate 1860 s (1800 s gate default + margin so the
  bridge never kills a test run first).
- omp mirrors frozen in `tools.ts`: `ID_PATTERN`, 14 STATES, 7 PATTERNS (`custom`
  needs `--plan <path>`, unexposed), `GATE_SCRIPTS`, `SEVERITIES`
  (BLOCKING = `major`), FINDING schema (no verdict field), 100 KB input cap.

### 1.3 The 7 error codes (frozen strings; `E-POLICY` message scrubbing preserved)

`E-INPUT`, `E-TIMEOUT`, `E-DEP`, `E-CAPACITY`, `E-CONTRACT`, `E-POLICY`
(fail-closed; `Error.message` must not contain the `E-POLICY` token — scrub prefix
and all occurrences, fall back to `"refused"`), `E-INTERNAL`. Standard handlings
(`errors.py::_HANDLING`): E-INPUT → nack + FAILED + notify producer; E-TIMEOUT →
checkpoint, release lease, requeue once, then ESCALATED; E-DEP → exp-backoff
1 s→60 s jitter max 5 then degraded; E-CAPACITY → refuse bid + backpressure;
E-CONTRACT → bounded fix loop; E-POLICY → fail-closed, escalate A10/A01;
E-INTERNAL → checkpoint + crash, A01 requeue, 3 strikes ⇒ ESCALATED.

## 2. Python → Rust error-mapping table

Rust error model MUST map these 1:1 (code strings are wire contract; `classify()`
semantics reproduced, not reinvented).

| Python (`swarm/errors.py`) | Rust (`gsd-failure` / core error enum) | Wire `error.code` | Retryable (omp `RetryPolicy.retryable_error_codes`) | Notes |
|---|---|---|---|---|
| `E-INPUT` (`FileNotFoundError`/`KeyError`/`ValueError` via `classify()`) | `CoreError::Input` (serde/argv/ID-pattern/validation failures; wasmer arg-marshal failures land here) | `E-INPUT` | No | Includes exit-2-empty-stdout usage errors; validation-error fixtures must hit this |
| `E-TIMEOUT` (`TimeoutError`; `ErrorHandler` timeout path) | `CoreError::Timeout` (Tokio timeout, fuel-exhaustion timeout, host `clock` deadline) | `E-TIMEOUT` | Yes, once (requeue-once then ESCALATED) | Fuel-exhaustion ≠ timeout unless deadline mapped; keep distinct internal variant |
| `E-DEP` (`ConnectionError`/`OSError`; missing script; `python3` spawn failure) | `CoreError::Dependency` (missing `.wasm` artifact, host-import unavailability, storage-backend down) | `E-DEP` | Yes (exp-backoff 1 s→60 s jitter, max 5) | `swarmRoot`/`script not found`/`cannot start` analogues |
| `E-CAPACITY` (overload / WIP cap) | `CoreError::Capacity` (semaphore rejection, bounded-channel full, pool saturated) | `E-CAPACITY` | Yes (backpressure, no tight loop) | Semaphore acquire must fail fast with this, never block past deadline |
| `E-CONTRACT` (output rejected by consumer/schema) | `CoreError::Contract` (JSON-schema/FINDING/verdict-shape rejection, proto validation) | `E-CONTRACT` | Yes, bounded fix loop per agent spec | Malformed-payload fixtures must hit this, not E-INTERNAL |
| `E-POLICY` (`PermissionError`; plan-mode/gate-owner refusals; fail-closed) | `CoreError::Policy` (capability-token denial, FS/net deny-by-default, plan-mode refusal) | `E-POLICY` | No (fail-closed; escalate A10/A01) | Preserve omp message scrubbing; details stay in `details` field only |
| `E-INTERNAL` (catch-all; unexpected exit codes; non-object stdout) | `CoreError::Internal` (wasmer trap/panic, unexpected host error, invariant violation) | `E-INTERNAL` | No (A01 requeue path owns retry, 3 strikes ⇒ ESCALATED) | Wasmer traps default here; map to narrower codes only with evidence |

`SwarmError.to_dict()` shape (`code`, `message` prefixed `"E-XXX: "`, `details`,
`handling`) is the JSON `error` object Rust must emit. `ErrorHandler` semantics
(5 attempts, 1 s + jitter backoff, degrade-to-sequential only when a caller latches
it, consecutive-failure circuit isolates rather than degrades) move into
`gsd-scheduler`/`gsd-failure`, not into the bridge.

## 3. Strangler boundary

- Rust trait mirrors Python behavior: one `CoreBackend` trait (proposed home:
  `gsd-adapter`, already the declared legacy-migration layer) with one method per
  frozen entrypoint (`run_swarm`, `orch_plan/status/assign`, the four gates,
  `swarm_tool`, lane-verb passthrough). Method signatures take the same
  `--flag=value` argv vector + `SWARM_DIR` + deadline and return the same
  exit-code/JSON union (§1.2). `gsd-sdk` stays the client-facing SDK stub to grow;
  `gsd-adapter` is the strangler seam — do not blur them.
- Per-call dispatcher flag: routing is per invocation, not per process. Env-gated
  selector (e.g. `GSD_ENGINE=python|rust`, default `python`) read at each `runPy`
  call; the selected engine handles the whole call. No mixed-engine joins: a single
  session join (matrix fan-out or nexus barrier) MUST execute all its member tasks
  on one engine; the dispatcher pins the engine at join creation and rejects joins
  that would span engines with `E-CONTRACT`.
- Migration order: read-only paths first (`orch_status` reads, `swarm_tool`
  catalog kinds), then plan/assign, then gates (signed verdicts need byte-identical
  signatures — gate last, behind the parity harness §7), then `swarm_run`
  scheduler policy. Each step ships with its recorded fixtures green on both
  engines before the flag default flips for that path.
- Python remains truth on any divergence: mismatch ⇒ Rust bug, flag reverts for
  that path, fixture recorded.

## 4. Concurrency model

- Bounded Tokio pool: one `tokio::runtime` per host process (not per call, not per
  Wasm instance); worker threads ≤ 30/session (binding cap from inventory; load
  test may prove lower safer, never higher without evidence). `swarm_run.py`
  semantics ported: refill-on-completion, priority/DAG-depth/creation ordering,
  `--max-parallel` default 4 / ceiling 32 clamped to the 30-session cap,
  per-agent `max_parallel`, AIMD degrade on rate-limit/fail-streak.
- Admission: single `tokio::sync::Semaphore` (permits = session cap) on every
  spawn path; acquire-or-`E-CAPACITY` (try-acquire with deadline, never unbounded
  wait). Bounded mpsc channels between scheduler/dispatcher/Wasmer workers
  (explicit capacity, `try_send` → `E-CAPACITY` on full); no unbounded channels
  anywhere on the hot path.
- `Send` + `Sync`: all state crossing the pool boundary is `Send + Sync`; instance
  handles are `!Sync`-confined (owned by one worker task). Shared handles use
  `Arc<RwLock<…>>`/`Arc<Mutex<…>>` with one documented lock order
  (session → join → task → store) — lock ordering violations are `E-INTERNAL`
  bugs, not runtime retries. No blocking (`std::fs`, `writeInputFile`-style I-O)
  inside async workers; bridge off with `spawn_blocking` or keep on the host side.
- Cancellation: `CancellationToken` (or `AbortSignal`-equivalent) per call +
  per-session token for group-kill parity with omp `Inflight`. Cancel ⇒ drop the
  Wasm instance handle, release semaphore permit + lease, checkpoint per
  `standard_handling`, never surface partial output as success (§1.2 D-07 rule).
  Process-group-kill analogue: one token tree per session; session shutdown cancels
  the whole tree (SIGTERM→SIGKILL grace becomes token-cancel→instance-drop with a
  3 s force-drop escalation).

## 5. Wasmer packaging for FULL core

FULL core means the whole Rust core (scheduler, state, gates, registries,
lifecycle — not a pure-compute slice) compiles to one Wasm module; size the
artifact and host surface accordingly.

- Target: `wasm32-wasip2` (WASIp2 / component-model interfaces where available;
  fall back to `wasm32-wasi` only with recorded justification). Pinned toolchain:
  `rustc` + `wasmer` versions pinned in one manifest (workspace root or
  `rust-toolchain.toml` + lockfile entry), verified in CI by hash, not by tag.
- Artifact: single hashed, versioned module (e.g. `gsd-core.<semver>.wasm`,
  `sha256:` recorded alongside; build emits module + digest file atomically).
  Reproducible build flags; no unpinned nightly features. Distribution is a
  content-addressed download/cache lookup, never an implicit `latest`.
- Sandboxing: linear memory cap + fuel/metering limit per call (fuel-exhaustion
  maps to `E-TIMEOUT` only when a deadline mapping exists, else `E-INTERNAL` per
  §2); deny-by-default FS (preopen only `<SWARM_DIR>/results` write + script-read
  inputs actually needed — full core still gets no ambient FS) and deny-by-default
  net (no sockets unless a named capability grants them; default none).
- Capability-gated host imports, sized for whole-core (this is the FULL-core cost
  center — a pure-core list would be missing half of these):
  1. `log` — tracing events out (bounded batch, fixed schema).
  2. `time` — monotonic clock + deadlines (host-owned; guest never reads wall time
     directly, keeping `E-TIMEOUT` semantics host-authoritative).
  3. `rand` — seeded/HostRng bytes (deterministic replay under a fixed seed).
  4. `storage` — TaskStore/state read-write behind capability tokens
     (`gsd-security`), namespaced per session; the only state-mutating import.
  5. `omp-callback` — structured JSON callback into the TS bridge (validated
     against the same serde limits as §6; guest cannot spawn processes or touch
     the network through it).
  Explicit non-imports: no process spawn, no ambient FS/net, no env-var passthrough
  except the stripped `SWARM_DIR`-equivalent, no direct `Bun.spawn`.
- Capability tokens (`gsd-security`): every import checks a per-call token
  (session id + allowed ops + expiry); denial ⇒ `E-POLICY` fail-closed. Tokens
  are minted host-side per dispatcher call and never cached across sessions.

## 6. TS bridge (frozen plugin, narrow additions only)

`runPy` stays the only spawner; Rust calls enter through the same `Bridge`
signature (`BridgeRequest`/`BridgeResult`, `Script` union extended by engine tag,
not by new spawn paths).

- Bidirectional serde, validated both ways: `serde_json` (Rust) ↔ `JSON.parse`
  (TS) with shared limits — max payload 100 000 bytes per value (matches
  `MAX_TOOL_INPUT_BYTES`), max depth (e.g. 32), unknown-field rejection on
  error/verdict shapes, `error.code` allowlisted against the 7 codes. Oversize /
  over-deep / unknown-shape payloads ⇒ `E-INPUT` (input side) or `E-CONTRACT`
  (output side) per §2, never silent truncation.
- Lifecycle hooks: `init` (load + hash-verify `.wasm`, pin toolchain version,
  create runtime once), `invoke` (per-call: mint capability token, marshal argv +
  input files, run with deadline, unmarshal exit-code/JSON union), `shutdown`
  (drop instances, drain channels, release pool), `health` (artifact present +
  hash ok + runtime alive + last-invoke latency; failure ⇒ `E-DEP`).
- Retries idempotent-only: only read-only paths (`orch_status` reads, `swarm_tool`
  catalog, `E-DEP`/`E-CAPACITY` on side-effect-free calls) retry, with the
  `ErrorHandler` backoff (1 s base + jitter, max 5). Mutating calls never auto-retry;
  caller re-checks with `swarm_status` first (D-07). `RetryPolicy.retryable_error_codes`
  in `gsd.proto` carries exactly the retryable column of §2.
- Timeouts: per-call `timeoutMs` (default 600 s; QA path 1860 s) enforced
  host-side; guest fuel is a backstop, not the authority. Timeout ⇒ instance drop
  + `E-TIMEOUT` + checkpoint/lease handling per `standard_handling`.
- Ordering/errors preserved: concurrent invokes resolve in completion order but
  each result keeps its caller's `toolCallId`/correlation; error shape is the
  frozen `SwarmToolError` union including `E-POLICY` scrubbing; group-kill
  (`killInflight`) cancels all in-flight invokes of that session without emitting
  results for them.

## 7. Parity + perf harness

- Recorded fixtures (JSON pairs: argv + stdin-files + env-stripped context →
  frozen exit-code/stdout), run against Python (truth) and Rust/Wasmer (challenger),
  byte-compared on code + parsed-compared on JSON:
  1. `orch_plan` happy path (feature pattern, low risk).
  2. `orch_status --transition` legal + illegal transition (14-state ladder).
  3. Gate verdict pass + fail-major (signed; signature bytes must match).
  4. `swarm_tool` catalog list + 100 000-byte boundary (cap enforced both sides).
  5. Validation-error case (bad id with `..`, unknown pattern) ⇒ exit 2 / `E-INPUT`.
  6. Empty case (empty ingest file, empty findings list) ⇒ success-empty, not crash.
  7. Large case (max-size findings payload, deep task graph) ⇒ within timeout + memory cap.
  8. Malformed case (non-JSON input file, non-object stdout simulation) ⇒
     `E-INPUT` / `E-INTERNAL` per §1.2, never uncaught.
  9. Timeout case (deadline shorter than work) ⇒ `E-TIMEOUT`, no result recorded.
  10. Policy case (plan-mode mutation, foreign gate) ⇒ `E-POLICY` with scrubbed message.
  11. Classifier quadruple-mirror check (all three copies + `classify()` + fixture agree).
- Perf gate (same hardware, per-path p95 + throughput): Rust/Wasmer must be
  parity-or-better vs Python per path before each strangler step flips. Record
  Wasmer overhead metrics separately on every run: instance cold-start latency,
  warm-invoke latency, serde time (marshal + unmarshal) vs guest execution time,
  peak linear memory, fuel consumed. The gate fails if serde dominates without a
  recorded mitigation (below), even when wall-time parity holds.
- Serde-cost acknowledgment + mitigation: FULL-core sandboxing forces every call
  across the host↔guest boundary as serialized JSON (argv in, exit-code/JSON out,
  storage + omp-callback round-trips mid-call) — this is the highest-cost Wasmer
  mode by design and is accepted. Mitigations, in order: (a) keep payloads small
  by passing input-file paths + `SWARM_DIR`-scoped reads instead of inline blobs
  wherever the frozen contract allows; (b) reuse warm instances (WarmPool) so
  per-call cost is serde, not instantiation; (c) batch lane-verb reads into single
  invokes instead of one call per item; (d) schema-validate once at the boundary
  and use zero-copy/parsed forms inside; (e) track the serde budget as a
  first-class harness metric with its own alert threshold so regressions in
  boundary chatter fail loudly. What we do NOT do: bypass the sandbox with shared
  mutable memory, widen host imports to dodge serialization, or rewrite the TS
  plugin to a native ABI — all three would void the isolation premise.

## Open options left to n5 (no code yet)

- O1: one monolithic `gsd-core.wasm` vs per-lane component modules behind one
  capability-checked dispatcher (isolation granularity vs cold-start/duplication).
- O2: `wasm32-wasip2` component-model canonical ABI vs plain `wasm32-wasi` + JSON
  over a single `invoke` export (type-rich boundary vs simplest frozen-JSON boundary).
- O3: host-side vs guest-side JSON-schema validation for FINDING/verdict shapes
  (single owner vs defense in depth; either way the wire shape is frozen).
