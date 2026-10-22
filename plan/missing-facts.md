# Missing facts / ambiguity triage · Refs SPE-18065

Scope: critique what is still unknown after `plan/inventory.md`; gate later work on discovery.
Rule: every repo claim cites a file read. Unverifiable = OPEN with owner + cheapest discovery step.
Assumptions adopted as defaults are labeled `[ASSUMPTION]`.

Settled inputs (do not relitigate without new evidence):
- q1/q2 settled by repo evidence (see inventory + §1/§2 below).
- q3 = full core sandbox (user decision 2026-10-20).
- q4 = parity-or-better ([ASSUMPTION], default; see §6).

---

## 1. workflowz (DAG dispatch policy, not a language)

- **Status:** settled-by-evidence.
- **Evidence:**
  - `plan/inventory.md:8-10` — `workflowz` is a command-path token
    (`/gsd-autonomous workflowz ultrathink orchestrate`), with pointers to
    `AGENTS.md`, `swarm/hook_trigger.py`, `tests/fixtures/classifier_prompts.json`.
  - Verified this run: `tests/fixtures/classifier_prompts.json` exists
    (`ls tests/fixtures/` output); inventory's claim of "no DSL parser or
    session-script format" stands — no file under `swarm/` or `scripts/` implementing
    a workflowz parser was found in inventory, and none is cited anywhere as one.
- **Blocks:** n6/n7 scheduler port must implement the `swarm_run.py` force-parallel
  policy (priority/DAG-depth/creation order, `--max-parallel` default 4 / ceiling 32),
  not a new workflow language. Any proposal introducing a workflowz DSL is out of scope.
- **Owner:** n/a (settled).
- **Discovery:** none. If challenged, cheapest check: `grep -rn workflowz AGENTS.md
  swarm/ omp/src/ | head`.

## 2. matrix-vs-nexus (flat fan-out vs staged join)

- **Status:** settled-by-evidence.
- **Evidence:**
  - `plan/inventory.md:11-15` — `gsd-scheduler` = "DAG-aware, priority-driven,
    dependency-respecting concurrent executor"; `gsd-nexus` = "pub/sub,
    request/response, shared memory CRDT, broadcast" (crate descriptions in
    workspace `Cargo.toml:3-19` confirm both crates exist: `crates/gsd-scheduler`,
    `crates/gsd-nexus`).
  - `crates/gsd-proto/src/gsd.proto` (grep this run) — `NexusMessage`,
    `Subscription`/`SubscribeResponse`, `recipients` ("empty = broadcast") confirm
    the pub/sub + broadcast nexus half; `WorkflowDef`, `SessionState`,
    `Budget` confirm the staged-definition half.
  - Matrix = flat fan-out execution; nexus = staged fan-out/fan-in with barrier join
    coordinated over nexus. No repo fact contradicts this.
- **Blocks:** n6 must keep scheduler (matrix) and bus (nexus) as separate seams;
  joining them into one abstraction would erase the barrier-join coordination point.
- **Owner:** n/a (settled).
- **Discovery:** none. Cheapest re-verify: read `crates/gsd-scheduler/src/*.rs` +
  `crates/gsd-nexus/src/*.rs` heads when n6 starts (interface drift check only).

## 3. omp meaning / plugin API + versioning story

- **Status:** settled-by-evidence on meaning and surface; OPEN on versioning (explicitly verified absent).
- **Evidence (meaning/API):**
  - `omp/src/omp-api.ts:236-254` (read this run) — `ExtensionAPI` =
    `registerTool`, `on("tool_call" | "before_agent_start" | string)`,
    optional `events` bus, `getActiveTools()`, `registerCommand`, `sendUserMessage`;
    `ExtensionFactory = (pi: ExtensionAPI) => void`. omp = the host extension/plugin
    contract; "omp" is the extension API + bridge layer, not a protocol version.
  - `omp/src/index.ts:37,39` — registration wiring: `pi.registerTool(tool)` per
    built tool, `pi.registerCommand("swarm", …)`.
  - `plan/inventory.md:42-46` — `omp/src/bridge.ts` spawns
    `python3 scripts/<script>.py --json`; `omp/src/tools.ts` mirrors Python
    (ID_PATTERN, 14 STATES, 7 PATTERNS, 100KB cap). Frozen authority, no rewrite.
- **Evidence (versioning absent — explicitly verified):**
  - `omp/package.json:1-11` (read this run) — `name: agent-swarm-otp… "agent-swarm-omp"`,
    `private: true`, NO `version` field, no semver range, no `engines`/`peerDependencies`.
  - `omp/src/omp-api.ts` full-text grep for `version|semver|apiVersion|compatibility`
    (this run) — zero API-versioning hits; only prose ("A plain JSON Schema object").
  - No `omp/omp.json`, `omp/manifest*` found (`ls omp/` → `agents/ skills/ src/ test/
    package.json` only).
- **Blocks:** Any Rust↔omp or TS-SDK contract change has no negotiated-version escape
  hatch today; a breaking `ExtensionAPI` change would be a flag-day. Wave 3+ must treat
  the TS surface as frozen unless a versioning story is designed first.
- **Owner (versioning design):** A03 architect (contract) + A06 frontend (consumer).
- **Cheapest discovery:** 30 min — confirm with omp host owners whether `ExtensionAPI`
  versioning is intentionally absent (host pins by commit) or an omission; record answer
  here before n-design touches `omp-api.ts`.

## 4. Python scope / fixtures (correctness baseline)

- **Status:** settled-by-evidence on scope; OPEN on fixture completeness for parity testing.
- **Evidence:**
  - `plan/inventory.md:23-38` — entry points (`scripts/swarm_run.py` 2452 lines,
    `orch_plan.py`, `orch_status.py`, `orch_assign.py`, gate scripts, `swarm_tool.py`
    100KB cap), libraries (`swarm/errors.py` 7 codes, `swarm/taskstore.py` 14 states,
    `tool_registry.py`, `skill_registry.py`, `envelope.py`, `telemetry.py`,
    `handover.py`, `lifecycle.py`).
  - `swarm/taskstore.py:22-36` (read this run) — 14 states verified:
    CREATED, VALIDATED, PLANNED, CLAIMED, IN_PROGRESS, IN_REVIEW,
    CHANGES_REQUESTED, APPROVED, DONE, BLOCKED, FAILED, RETRY, ESCALATED, CANCELLED;
    `LEGAL_TRANSITIONS:40-55`, `MAX_REWORK_LOOPS = 2`, `GATES_BY_RISK` low→high.
  - `tests/fixtures/` (listed this run) — `classifier_prompts.json`,
    `elapsed_cases.json`, `live_runner_sample.json`, `omp_agent_end.jsonl`,
    `redact_cases.json`, `redact_generated.json` (+3 filtered). Fixtures exist but are
    lane/feature-scoped, not a scheduler-parity corpus.
- **Blocks:** Rust port's error model MUST map the 7 `swarm/errors.py` codes and the 14
  task states + legal transitions; without a parity fixture corpus, "parity" is
  unverifiable per path.
- **Owner:** A02 requirements (parity corpus definition) + A08 QA (harness).
- **Cheapest discovery:** 1–2 h — enumerate `scripts/<lane>_<verb>.py` count and which
  lanes lack fixture coverage (`ls scripts/ | wc -l` + cross-ref `tests/`); emit the
  uncovered-lane list as the parity-corpus backlog for n8.

## 5. Wasmer target / capabilities (toolchain absent — explicitly verified)

- **Status:** OPEN (no repo facts; design required).
- **Evidence (absence verified this run):**
  - Workspace `Cargo.toml:21-175` (read) — zero hits for `wasmer|wasmtime|wasm`
    in `[workspace.dependencies]`; confirmed by `grep -i wasmer|wasmtime Cargo.toml
    Cargo.lock crates/*/Cargo.toml` → no output.
  - `Cargo.lock` does not exist in repo root (`ls Cargo.lock` → No such file) —
    dependency closure is unresolved; Wasmer version selection is fully open.
  - No `rust-toolchain*`, no `.cargo/` config, no `[build-target] wasm*` or
    `wasm32-wasip2` target stanza (workspace `Cargo.toml:189-195` only configures
    `x86_64/aarch64-unknown-linux-gnu` with clang/lld). `rustc --print target-list`
    shows stock wasm targets available in the toolchain, but the repo pins none.
  - `plan/inventory.md:65-67` already records: "ZERO hits for wasmer/wasmtime/wasm
    deps. No wasm target, profile, or artifact pipeline."
- **Blocks:** n4 (sandbox spike) cannot size the build: module format
  (wasm32-wasip1 vs wasip2), Wasmer version + features (Singlepass/Cranelift/LLVM,
  WASI enablement), host-import surface, FS/net policy, and artifact distribution
  are all undecided — and q3 (full core, user-decided) makes these load-bearing
  (serde/host-call budget scales with full-core surface).
- **Owner:** A03 architect (target + host-import policy) with A11 devops (artifact pipeline).
- **Cheapest discovery:** half-day spike (n4) — compile one `gsd-*` crate to
  `wasm32-wasip2` under Wasmer latest, report: (a) what compiles vs what needs
  stubbing (tokio/net/fs), (b) candidate host imports, (c) WASI capability wish-list.
  Record exact Wasmer version + target triple here before n6/n7 proceed.

## 6. Perf target / harness

- **Status:** target = user-default `[ASSUMPTION]`; harness = OPEN.
- **Evidence:**
  - `plan/inventory.md:19,69-70` — q4 "parity or better (default): hold p95 +
    throughput on same hardware" is stated as a default, not a measured requirement;
    no SLO file backs it. Full pytest timed out at 900 s / ~31% (bg_77, exit 124) —
    suite-level slow; per-path p95 harness still needed (n4).
  - `tests/` listing (this run) — lane/parity/audit tests present
    (`test_ingest_parity.py`, `test_tools_a*.py`, …); `grep -l bench|criterion|p95|perf
    tests/ scripts/` returned only lane-test filenames tangled with the pattern —
    no dedicated criterion/bench harness or p95 SLO file was identified.
- **[ASSUMPTION] adopted:** q4 parity-or-better holds p95 latency + throughput on same
  hardware vs the Python baseline. This is a default assumption, NOT a measured SLO.
- **Blocks:** Without a per-path baseline (scheduler dispatch, gate verdict, bridge
  round-trip) and a criterion-style harness, parity claims are uncheckable and the
  full-core serde budget (q3) cannot be sized.
- **Owner:** A13 observability (SLO + harness) with A08 QA (acceptance runs).
- **Cheapest discovery:** 2–4 h — time the three hot paths (`swarm_run` dispatch tick,
  one gate script, one `bridge.ts` round-trip) on this host, 50+ samples each, record
  p50/p95 + hardware fingerprint here;هن n4 turns them into the regression gate.

## 7. Thread context / persistence (sessions/threads in proto; runtime story partial)

- **Status:** partially settled-by-evidence (model exists); OPEN on persistence/resume semantics.
- **Evidence (model exists):**
  - `crates/gsd-proto/src/gsd.proto` via grep (this run) — `ChatThreadId`,
    `ThreadState` enum (`IDLE`, `INITIALIZING`, `RUNNING`, …), `ThreadState` message
    (`id`, `subagent_type`, `capability_manifest`, `messages: Any`, `context_window`
    map), `SessionState` (`version`, `session_id`, `threads` map, `branches`,
    `checkpoints`, `workflow`), `Checkpoint.thread_state_snapshot`
    (bincode-serialized `ThreadState`), `GetSessionResponse{session}`.
  - `docs/` (listed this run) — `swarm-bar.md` documents per-session filtering
    (bar reads its own session id before `live.json` restore, ignores other sessions);
    `substrate-memory.md` documents `verified_persisted` write semantics for memory —
    neither defines GSD-Core thread resume/checkpoint-restore semantics.
- **What is unknown:** who may snapshot/restore a `ThreadState`, when `context_window`
  vs `Checkpoint` is authoritative, resume-after-crash vs resume-after-cancel rules,
  retention/GC of threads/checkpoints, and whether `live.json`-style restore carries
  over to the Rust core or is replaced by `SessionState` RPCs. No `gsd-state` persistence
  backend choice is recorded in inventory.
- **Blocks:** n6/n7 (scheduler + state) cannot implement crash recovery, rework loops
  (`MAX_REWORK_LOOPS = 2` interplay), or session resume without these semantics; wrong
  guesses fork the state model.
- **Owner:** A03 architect (semantics) + A07 data (backends/retention).
- **Cheapest discovery:** 1 h — read `crates/gsd-state/src/*.rs` + `crates/gsd-pool/src/*.rs`
  heads (ChatThreadHandle lifecycle, WarmPool reuse vs fresh, checkpoint write path);
  record the snapshot-authority + resume-rule answers here before n7 designs recovery.

---

## SDK subdirs: explicitly verified absent

- `find crates/gsd-sdk -type f` (this run) → `Cargo.toml` only.
- `ls crates/gsd-sdk/{python,typescript,go}` → all three "No such file or directory".
- `crates/gsd-sdk/Cargo.toml:11-22` (read) — comments promise
  `gsd-sdk/python|typescript|go` subdirs + workspace members, but the directories do
  not exist: the SDK bridge is a stub. Wave 3+ MUST NOT assume any SDK client exists.

## Wasmer toolchain: explicitly verified absent (summary)

- No `wasmer`/`wasmtime`/`wasm` in workspace `Cargo.toml` deps; no `Cargo.lock`;
  no `rust-toolchain*`; no `.cargo/`; no wasm build-target stanza. See §5.

## omp versioning: explicitly verified absent (summary)

- `omp/package.json` has no `version`; `omp-api.ts` has no `apiVersion`/negotiation;
  no manifest file. TS contract is unversioned — treat as frozen. See §3.

---

## What Wave 3+ may NOT assume

1. No Wasmer version, wasm target triple, WASI version, or host-import list — §5 open.
2. No FS/net sandbox policy, no module distribution story — §5 open.
3. No `gsd-sdk/python|typescript|go` clients — verified absent; bridge is a stub.
4. No `ExtensionAPI` versioning/negotiation — verified absent; TS surface frozen.
5. No per-path p95 baseline, no perf harness, no SLO numbers — §6 open; q4 is an
   `[ASSUMPTION]` (parity-or-better on same hardware), not a measured target.
6. No thread snapshot-authority, resume, retention, or crash-recovery semantics —
   proto model exists but runtime rules are §7 open.
7. No parity fixture corpus covering all lanes — fixtures exist piecemeal (§4); full
   scheduler/gate parity corpus is backlog for A02/A08.
8. No `Cargo.lock` — dependency closure (including any future Wasmer version) is unpinned.
9. No workflowz language or session-script format — settled absent (§1); do not invent one.
10. No merged matrix/nexus abstraction — settled as separate seams (§2); keep them split.
