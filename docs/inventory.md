# GSD-Core inventory — Wave 1 synthesis

Synthesizes the Wave 1 discovery inventory (`plan/inventory.md`, Refs
SPE-18064 / SPE-33068) with the repository as it stands on branch
`swarm/T2484-be`. Where Wave 1 and the repo disagree, the repo wins and the
delta is noted.

## Settled decisions (do not relitigate without new evidence)

- **q1 `workflowz` = DAG dispatch policy.** No DSL or session-script format
  exists; no new language. The repo evidence is a command-path token
  (`/gsd-autonomous workflowz ultrathink orchestrate`).
- **q2 matrix vs nexus = flat vs staged join.** `gsd-scheduler` is the
  DAG-aware, priority-driven, dependency-respecting executor with
  configurable fan-out/fan-in; `gsd-nexus` is pub/sub, request/response,
  shared-memory CRDT and broadcast. Matrix = flat fan-out; nexus = staged
  fan-out/fan-in with a barrier join coordinated over nexus.
- **q3 Wasmer mode = FULL CORE sandbox** (user override 2026-10-20). The
  entire Rust core runs in the sandbox; host imports and the serde budget are
  sized for the full core, not a pure core. In the repo this is realized
  with **wasmtime `=50.0.2`** (`component-model` feature), the guest being a
  `wasm32-unknown-unknown` core module wrapped as a component
  (`gsd-guest` exports `invoke` over the pure route logic; `gsd-sandbox` is
  the host). Wave 1 recorded "zero wasmtime hits" — that gap is now closed.
- **q4 perf target = parity or better** on the same hardware (p95 +
  throughput). A full pytest run previously timed out at 900 s on this host,
  so verification is scoped per-path, never suite-wide.

## Rust workspace: 22 member crates

Cargo workspace members (root `Cargo.toml`). Descriptions are each crate's
`Cargo.toml` description (gsd-codes has none; its lib doc stands in).

| # | Crate | Role |
|---|-------|------|
| 1 | gsd-runtime | Core runtime primitives: deterministic clock, async primitives, I/O utilities |
| 2 | gsd-proto | Protobuf definitions for internal messages and types (`gsd.proto`: ChatThreadId, ThreadState IDLE..TERMINATED, SessionState, WorkflowDef, Budget, NexusMessage) |
| 3 | gsd-serde | Serialization layer: prost-reflect + bincode for checkpoint/artifact persistence |
| 4 | gsd-pool | Thread Pool Manager: lifecycle, pooling, reuse, isolation for chat-thread subagents |
| 5 | gsd-scheduler | Matrix Scheduler: DAG-aware, priority-driven, dependency-respecting concurrent executor |
| 6 | gsd-governor | Resource Governor: token budgets, rate limits, concurrency caps, cost controls |
| 7 | gsd-observability | Observability stack: structured logging, distributed tracing, metrics, replay/debugging |
| 8 | gsd-security | Security sandbox: thread isolation, capability tokens, audit logging, PII redaction |
| 9 | gsd-nexus | Nexus coordinator: pub/sub, request/response, shared memory CRDT, broadcast |
| 10 | gsd-failure | Failure domain isolation: circuit breakers, retries, dead-letter queues, graceful degradation |
| 11 | gsd-state | State store: versioned, branchable, mergeable session state with conflict resolution |
| 12 | gsd-adapter | Legacy adapter: zero-code migration layer for existing subagents |
| 13 | gsd-sandbox | Wasmtime sandbox host: module identity, loading, limits, typed invoke boundary |
| 14 | gsd-route | Pure route logic: Python-shaped JSON and Task Store row shaping, no database access |
| 15 | gsd-codes | Shared failure codes, mirroring the seven codes in `swarm/errors.py` (leaf crate, builds for `wasm32-unknown-unknown`) |
| 16 | gsd-guest | Sandboxed guest: the `gsd:core` WIT world exporting `invoke` over the pure route logic |
| 17 | gsd-sdk | Rust mirror of the omp host contract: bridge argv, exit mapping, process-group runner, constants, classifier pins |
| 18 | gsd-python | PyO3 extension module `swarm._native` (see 18 exports below) |
| 19 | gsd-redact | Secret-span detection for run-log text; port of `swarm/redact.py`, Python module is the parity oracle |
| 20 | gsd-sched-core | Scheduler decision core: AIMD limiter, claim table, ordering, backoff, pass loop behind a ports trait — std only. Now also `graph`: task-graph model, validation, node FSM |
| 21 | gsd-envelope | Host-side swarm envelope canonical form, signing and verification (HMAC-SHA256 / Ed25519); the only crate that reads signing keys |
| 22 | gsd-taskwriter | Host-only Task Store writer: `BEGIN IMMEDIATE` transactions, conditional state updates, append-only audit rows over the Python-owned `tasks.db` schema |

Excluded from the workspace (root `Cargo.toml` `exclude`): `crates/gsd-ipc`
(used by no member; incomplete module tree — its owning phase re-adds it)
and `crates/gsd-cli` (invalid manifest). `crates/concurrency` exists on
disk but is not a workspace member.

## Python surface (correctness baseline = Python truth)

Entry points (`scripts/`, all `--dry-run --json`, exit 0/1/2 via
`swarm/script_base.py`): `swarm_run.py` (continuous scheduler — this is the
force-parallel policy to port), `orch_plan.py`, `orch_status.py`
(`--ingest`/`--transition`), `orch_assign.py`; gate scripts `qa_gate.py`,
`rev_gate.py`, `sec_gate.py`, `rel_plan.py` (signed verdicts); `swarm_tool.py`
(registry call bridge, 100_000-byte input cap); 30+ lane scripts
`<lane>_<verb>.py` plus `scripts/ts/` Bun twins.

Libraries (`swarm/`): `errors.py` (seven codes — the Rust error model MUST
map these), `taskstore.py` (14 states, transitions, gates),
`tool_registry.py`, `skill_registry.py`, `envelope.py` (ed25519/hmac
signing), `telemetry.py`, `handover.py`, `lifecycle.py`.

Error taxonomy (`swarm/errors.py`, mirrored in `gsd-codes`): E-INPUT,
E-TIMEOUT, E-DEP, E-CAPACITY, E-CONTRACT, E-POLICY (fail-closed),
E-INTERNAL, plus `classify()` and exp-backoff with jitter.

## `swarm._native` exports: 18 (additive-only)

`crates/gsd-python` (PyO3, `module = "swarm._native"`). 12 functions and
6 classes = 18 exports. No breaking changes: legacy fallback paths stay,
guarded per family.

| # | Export | Kind | Source |
|---|--------|------|--------|
| 1 | `code_from_wire` | function | `lib.rs` |
| 2 | `wire_codes` | function | `lib.rs` |
| 3 | `retry_policy` | function | `lib.rs` |
| 4 | `orch_status_invoke` | function | `lib.rs` |
| 5 | `decode_reply_wire` | function | `lib.rs` |
| 6 | `redact_spans` | function | `lib.rs` |
| 7 | `sched_order_ready` | function | `lib.rs` |
| 8 | `sched_normalize_claim` | function | `lib.rs` |
| 9 | `sched_claims_overlap` | function | `lib.rs` |
| 10 | `sched_backoff_s` | function | `lib.rs` |
| 11 | `sched_drain_plan` | function | `lib.rs` |
| 12 | `run_scheduler` | function | `sched_run.rs` (`run_scheduler_function`) |
| 13 | `NativeCleanupFence` | class | `sched_run.rs` |
| 14 | `ClaimTable` | class | `lib.rs` |
| 15 | `AimdLimiter` | class | `lib.rs` |
| 16 | `Governor` | class | `chat.rs` |
| 17 | `ChatPool` | class | `chat.rs` |
| 18 | `ChatTicket` | class | `chat.rs` |

## TypeScript omp surface (frozen authority, NO rewrite)

- `omp/src/bridge.ts` — single bridge: spawns
  `python3 scripts/<script>.py --json`, exit-contract mapping, input-file
  writer, session inflight group-kill.
- `omp/src/tools.ts` — mirrors Python: ID_PATTERN, 14 STATES, 7 PATTERNS,
  GATE_SCRIPTS, SEVERITIES (BLOCKING=major), FINDING schema, 100KB cap,
  QA_GATE_TIMEOUT_MS.
- `omp/src/guard.ts`, `hooks.ts`, `context.ts`, `commands.ts` (`/swarm`),
  `swarm_bar.ts`.
- Classifier lesson: the prompt classifier lives in THREE mirrored copies —
  `swarm/signal_detector.py`, `hooks/user_prompt_submit.py` (fallback),
  `omp/src/hooks.ts` — plus `hooks/autonomous_run.py` `classify()`; all
  pinned by `tests/fixtures/classifier_prompts.json`. Change all + fixture
  together.

## Toolchain commands

| Tool | Version / pin | Command |
|------|---------------|---------|
| rustc / cargo | 1.98.1 (`rust-toolchain.toml`, components rustfmt+clippy, target `wasm32-unknown-unknown`) | `cargo test -p gsd-sched-core` · `cargo test -p <crate>` · `cargo build --workspace` |
| pyo3 | `0.30.1`, `abi3-py312`, mixed layout | `maturin develop` (build backend `maturin>=1.15,<2.0`; manifest `crates/gsd-python/Cargo.toml`, `module-name = "swarm._native"`, features `["extension-module"]`) |
| maturin | 1.15.0 | `maturin develop` |
| python | 3.12 (`requires-python = ">=3.12"`, engine default `GSD_ENGINE=python`) | `python3 -m pytest tests/<path> -q` (scoped; full suite times out) |
| bun | 1.4.0 | `bun scripts/ts/<script>.ts --root <repo> --json` |
| wasmtime | `=50.0.2` (component-model) | via `cargo build -p gsd-sandbox -p gsd-guest` |

Rust engine is the opt-in (`GSD_ENGINE=rust` per `gsd-adapter`'s
`gsd_engine` route table); the Python scheduler (`scripts/swarm_run.py`)
stays the oracle.

## Graph module (this unit)

`crates/gsd-sched-core/src/graph/` — remapped from the provisional
`src/graph/*` sketch to fit this crate:

- `graph/model.rs` — `NodeId(String)` newtype, `Node` (inputs, deps,
  capability, priority, cost, timeout, retry, idempotency_key, required),
  `Contract`, `Graph`.
- `graph/validation.rs` — pure `validate`/`validate_with_limits` returning
  typed `ValidationError`s: size, duplicate ids, dangling deps, missing
  contracts, fan-out, cycles, depth, unreachable required nodes.
- `graph/state.rs` — single FSM (queued/ready/running/paused/succeeded/
  failed/cancelled/timed_out/skipped) with `LEGAL_TRANSITIONS`; illegal
  transitions are rejected. Deterministic ready order: priority, then
  `NodeId`.

## Out of scope (binding)

No 31st agent or new personalities; no TS plugin rewrite; no UI redesign;
no unrelated automation. Concurrency cap ≤ 30 threads/session unless a load
test proves a lower cap safer.
