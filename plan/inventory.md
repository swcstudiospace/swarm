# Wave 1 (n1) — Discovery inventory · Refs SPE-18064

End state: 30 existing subagents (A01–A30) as isolated Chat Threads in forced-parallel
matrix/nexus per GSD-Core session; Rust replaces Python behind a parity-gated boundary;
full Rust core executes inside Wasmer sandbox; TypeScript omp plugin contract frozen.

Settled decisions (do not relitigate without new evidence):
- q1 workflowz = DAG dispatch policy. Repo evidence: `workflowz` is a command-path token
  (`/gsd-autonomous workflowz ultrathink orchestrate`, AGENTS.md, hook_trigger.py,
  classifier_prompts.json). No DSL parser or session-script format exists. No new language.
- q2 matrix vs nexus = flat vs staged join. Repo evidence: `gsd-scheduler` = "DAG-aware,
  priority-driven, dependency-respecting concurrent executor with configurable
  fan-out/fan-in"; `gsd-nexus` = "pub/sub, request/response, shared memory CRDT,
  broadcast". Matrix = flat fan-out execution; nexus = staged fan-out/fan-in with
  barrier join coordinated over nexus.
- q3 Wasmer mode = FULL CORE sandbox (user override 2026-10-20, over recommended
  pure-core-only). Entire Rust core in Wasmer; strongest isolation, highest serde cost.
  n6/n7 must size host imports and serde budget for full core, not pure core.
- q4 perf target = parity or better (default): hold p95 + throughput on same hardware.

## Python surface in scope (correctness baseline = Python truth)

Entry points (scripts/, all `--dry-run --json`, exit 0/1/2 via swarm/script_base.py):
- `swarm_run.py` (2452 lines) — continuous scheduler: persistent worker pool refilled on
  every completion; order priority/DAG-depth/creation; cap --max-parallel (default 4,
  ceiling 32); per-agent max_parallel; AIMD degrade on rate-limit/fail-streak; cancel
  via cancel/<corr> + process-group kill. THIS IS the force-parallel policy to port.
- `orch_plan.py`, `orch_status.py` (--ingest/--transition), `orch_assign.py`
- Gate scripts: `qa_gate.py`, `rev_gate.py`, `sec_gate.py`, `rel_plan.py` (signed verdicts)
- `swarm_tool.py` (registry call bridge, 100_000-byte input cap)
- 30+ lane scripts `<lane>_<verb>.py` + `scripts/ts/` Bun twins (36 passthrough, 5 native)

Libraries (swarm/): `errors.py` (7 codes below — Rust error model MUST map these),
`taskstore.py` (14 states, transitions, gates), `tool_registry.py`, `skill_registry.py`,
`envelope.py` (ed25519/hmac signing), `telemetry.py`, `handover.py`, `lifecycle.py`.

Error taxonomy (swarm/errors.py): E-INPUT, E-TIMEOUT, E-DEP, E-CAPACITY, E-CONTRACT,
E-POLICY (fail-closed), E-INTERNAL. Plus `classify()` mapping and exp-backoff+jitter.

## TypeScript omp surface (frozen authority, NO rewrite)

- `omp/src/bridge.ts` — single bridge: spawns `python3 scripts/<script>.py --json`,
  exit-contract mapping, input-file writer, session inflight group-kill.
- `omp/src/tools.ts` — mirrors Python: ID_PATTERN, 14 STATES, 7 PATTERNS, GATE_SCRIPTS,
  SEVERITIES (BLOCKING=major), FINDING schema, 100KB cap, QA_GATE_TIMEOUT_MS.
- `omp/src/guard.ts`, `hooks.ts`, `context.ts`, `commands.ts` (/swarm), `swarm_bar.ts`.
- Classifier lesson (applies to any omp-surface work): prompt classifier lives in THREE
  mirrored copies — swarm/signal_detector.py, hooks/user_prompt_submit.py (fallback),
  omp/src/hooks.ts — plus hooks/autonomous_run.py classify(); all pinned by
  tests/fixtures/classifier_prompts.json. Change all + fixture together; verify with
  `python3 -m pytest tests/test_hook.py -q` and a bun classifyPrompt check.

## Rust starting point (15 crates, workspace Cargo.toml)

gsd-runtime (clock/sync/io/sim), gsd-proto (gsd.proto: ChatThreadId, ThreadState
IDLE..TERMINATED, SessionState, WorkflowDef, Budget, NexusMessage — ALREADY the thread
model), gsd-serde, gsd-ipc (UDS gRPC + memfd shmem + channels), gsd-pool (ThreadPool,
ChatThreadHandle, WarmPool, lifecycle hooks), gsd-scheduler (MatrixScheduler, dag,
policy, semaphore, dynamic, hitl), gsd-state, gsd-governor (budgets/rate-limit),
gsd-observability, gsd-security (capability tokens), gsd-cli, gsd-adapter (legacy
migration layer), gsd-nexus, gsd-failure, gsd-sdk.

## Greenfield gaps (no repo facts — design required)

- Wasmer: ZERO hits for wasmer/wasmtime/wasm deps. No wasm target, profile, or artifact
  pipeline. Toolchain, module format (wasm32-wasip2 vs wasi), host imports, FS/net
  policy, distribution all open → n4/n5.
- gsd-sdk: Cargo.toml only; python/typescript/go subdirs DO NOT EXIST. Bridge is stub.
- Perf baseline: full pytest timed out at 900 s / ~31% (bg_77, exit 124) on this host —
  suite-levelähnlich slow; per-path p95 harness still needed (n4).

## Out of scope (binding)

No 31st agent or new personalities; no TS plugin rewrite; no UI redesign; no unrelated
automation. Concurrency cap ≤ 30 threads/session unless load test proves lower safer.
