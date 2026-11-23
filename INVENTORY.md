# Inventory — agent-swarm (Wave1 n1)

Source: read-only inventory of repo `agent-swarm`, branch `swarm/T74c2-be`
(source: `git branch --show-current`). Every fact cites its source path;
anything not directly observed is marked `UNVERIFIED`. No code was changed
for this inventory. Full test suites were **not** run (known timeout, per
assignment); see §7 for the cheap checks that were run.

## 1. Layout

| Path | What | Source |
|---|---|---|
| `Cargo.toml` | Rust workspace, resolver 2, 22 members (see below) | repo root listing |
| `crates/` | 25 crate dirs: `concurrency`, `gsd-adapter`, `gsd-cli` (excluded), `gsd-codes`, `gsd-envelope`, `gsd-failure`, `gsd-governor`, `gsd-guest`, `gsd-ipc` (excluded), `gsd-nexus`, `gsd-observability`, `gsd-pool`, `gsd-proto`, `gsd-python`, `gsd-redact`, `gsd-route`, `gsd-runtime`, `gsd-sandbox`, `gsd-sched-core`, `gsd-scheduler`, `gsd-sdk`, `gsd-security`, `gsd-serde`, `gsd-state`, `gsd-taskwriter` | `ls crates/` |
| `Cargo.toml` workspace | `members` = 22 `gsd-*` crates; `exclude = ["crates/gsd-ipc", "crates/gsd-cli"]` with comment "ipc is used by no member and has an incomplete module tree … cli has an invalid manifest" | `Cargo.toml` |
| `crates/concurrency/` | `Cargo.toml` + `src/execution.rs`, NOT in workspace `members` | `Cargo.toml`, `ls crates/concurrency/` |
| `rust/` | Separate standalone crate `gsd_orchestrator` (own `[workspace]`, comment: "Standalone so the workflow verification can build it independently"), `src/` (8 files incl. `decomposition.rs`, `pool.rs`, `pyo3_bindings.rs`), `tests/concurrency_tests.rs`, `Cargo.toml`, `Cargo.lock` | `rust/Cargo.toml`, `ls -R rust/` |
| `swarm/` | Python runtime package: 46 `.py` files, incl. `taskstore.py`, `graph_quality.py`, `handover.py`, `telemetry.py`, `redact.py`, `_native.pyi`, `_native_loader.py`, `rust_scheduler.py`, `graph_planner.py`, `signal_detector.py`; plus `tools/` (39 files), `schemas/`, `substrate_mcp.json`, `_native.abi3.so` (1.2 MB, prebuilt) | `ls swarm/`, `ls swarm/tools/` |
| `swarm/tools/` | 39 files: `a01_tools.py`–`a30_tools.py`, `batch_01_05.py`, `batch_06_10.py`, `batch_11_15.py`, `orch_registry_ping.py`, `shared_handover.py`, `shared_pool.py`, `shared_runtime.py`, `support.py` | `ls swarm/tools/` |
| `scripts/` | 54 `.py` files incl. `swarm_run.py` (150 KB), `build_agents.py` (75 KB), `orch_plan.py`, `orch_status.py`, `arch_adr.py`, `arch_contract_check.py`; 45 TS twins in `scripts/ts/` (same stems); shell gates `ci_build_wheel.sh`, `ci_cargo_deny.sh`, `ci_host_facts.sh`, `ci_parity_rust.sh`, `ci_rust_toolchain.sh`, `ci_scheduler_rust.sh` | `ls scripts/`, `ls scripts/ts/` |
| `omp/` | TypeScript host extension: `package.json` (`"omp": { "extensions": ["./src/index.ts"] }`), `src/` (14 files incl. `index.ts`, `hooks.ts`, `tools.ts`, `bridge.ts`), `test/` (22 test files + `fixtures/`), `agents/` (30 files), `skills/` (407 dirs) | `omp/package.json`, `ls omp/src omp/test`, `ls omp/agents \| wc -l`, `ls omp/skills \| wc -l` |
| `agents/` | 30 agent specs `A01-orchestrator.md` … `A30-custody-compliance.md` | `ls agents/` |
| `agents.json` | Manifest, 2.5 MB; top-level keys `schema` (`swarm.manifest.v1`), `version` `1.0.0`, `defaults`, `tool_contract`, `capability_policy`, `shared_baseline`, `shared_tools`, `agents` (array of 30) | `jq` on `agents.json` |
| `python/` | `gsd_orchestrator/`, `orchestrator/` (`pool.py`, `decomposer.py`), `tests/` | `ls -R python/` |
| `python_tools/` | `decomposition.py`, `orchestration.py`, `validation.py`, `states.py`, `gsd_integration.py` | `ls python_tools/` |
| `hooks/` | `autonomous_run.py`, `user_prompt_submit.py`, `on_a01_complete.py` | `ls hooks/` |
| `tests/` | 225 `test_*.py` at top level (`find tests -name 'test_*.py'` = 246 incl. subdirs), `ts/` (3 files), `parity/`, `typecheck/`, `perf/`, `fixtures/` (incl. `classifier_prompts.json`, `omp_plugin_harness.py`) | `ls tests/`, `find tests` |
| `web/` | `package.json` (`agent-swarm-web`, devDeps `@bytecodealliance/jco` 1.39.0 + `playwright-core` 1.64.1, no runtime deps), `src/` (`api.mjs`, `screens.mjs`, `stories.mjs`, `tokens.css`), `transpile.mjs`, `serve.mjs`, 10 `*.test.mjs` | `web/package.json`, `ls web/ web/src/` |
| `contracts/`, `migrations/`, `docs/`, `design/`, `plan/`, `issues/`, `registry/`, `skills/`, `grokbot/`, `.cursor/`, `.grok/` | Present; `contracts/` holds `sc-graph-engine.json`, `gsd-quality-gate.yaml`; `migrations/` holds `V0001/V0002` + `U0001/U0002` SQL; `issues/tissue.json` (20 B) | root listing, `ls contracts/ migrations/ issues/` |
| `.swarm/` | Local runtime state (gitignored): `events.jsonl` 119 MB, `tasks.db` 5.7 MB, `substrate-tee.db`, `verdicts/`, `results/`, `live.json` — not source | `ls .swarm/` |
| git remote | `origin https://github.com/swcstudiospace/swarm.git` | `git remote -v` |
| git status | Dirty at inventory time with pre-existing user changes (`M tests/ts/screens.test.ts`, `M web/src/*`, untracked `DECISIONS.md`, `contracts/`, `design/`, `docs/acceptance.md`, …); HEAD `53b42ce` | `git status --porcelain`, `git log --oneline -1` |

## 2. Versions and commands discovered

| Fact | Value | Source |
|---|---|---|
| Rust toolchain | `1.98.1` pinned (`channel = "1.98.1"`, components `rustfmt`, `clippy`, target `wasm32-unknown-unknown`) | `rust-toolchain.toml` |
| `cargo --version` | `cargo 1.98.1 (797e8a9bc 2026-08-05)` — ran with `env -u SWARM_*` | §7 cheap check |
| `rustc --version` | `rustc 1.98.1 (48a229cea 2026-09-01)` | §7 cheap check |
| `python3 --version` | `Python 3.12.3`; `pyproject.toml` requires `>=3.12`, `mypy.ini` `python_version = 3.12`, `tool.ruff.target-version = "py312"` | §7 cheap check; `pyproject.toml`, `mypy.ini` |
| `bun --version` | `1.4.0` | §7 cheap check |
| `ruff --version` | `ruff 0.15.18` | §7 cheap check |
| `maturin --version` | `maturin 1.15.0` (matches `maturin>=1.15,<2.0` in `pyproject.toml`) | §7 cheap check; `pyproject.toml` |
| `gh --version` | `gh 2.45.0` | §7 cheap check |
| `mypy --version` | UNVERIFIED (probe printed config output, not a version) | §7 |
| Python deps | `PyYAML>=6.0.3,<7.0.0` (runtime), dev `pytest>=8.0,<10.0.0` | `pyproject.toml` |
| Rust deps | `pyo3 0.30.1` (`abi3-py312`), `tokio 1.40`, `wasmtime =50.0.2` pinned, plus ~90 workspace deps total | `Cargo.toml` `[workspace.dependencies]` |
| `deny.toml` | `cargo-deny` config, 4.5 KB | repo root listing |

Build / test / fmt / lint commands (from `.github/workflows/ci.yml`,
`scripts/ci_*.sh`, `pyproject.toml`, `mypy.ini`, `package.json`,
`.pre-commit-config.yaml` — all observed by reading those files):

- Rust build: `cargo fetch --locked`; `cargo build --workspace --locked`
- Rust test: `cargo test --workspace --locked --no-fail-fast`; targeted
  `cargo test -p gsd-sched-core -p gsd-scheduler -p gsd-python --locked --no-fail-fast`
  (`scripts/ci_scheduler_rust.sh`); `cargo test -p gsd-adapter --test read_route_faults --locked` (`scripts/ci_parity_rust.sh`)
- Rust fmt/lint: `cargo fmt --all --check`; `cargo clippy --workspace --all-targets --locked -- -D warnings`
- Python test: `python3 -m pytest` (`testpaths = ["tests"]`, `addopts = "-q"` in `pyproject.toml`);
  scheduler legs run with `env -u SWARM_PURE_PYTHON` (`scripts/ci_scheduler_rust.sh`)
- Python lint/typecheck: `ruff check .` (`tool.ruff`: line-length 140, select `E,F,W`);
  `python3 -m mypy` (strict, `files = swarm`); `mypy --strict swarm/ tests/typecheck/test_scheduler_ports_typecheck.py`;
  `mypy.stubtest swarm._native` (`scripts/ci_scheduler_rust.sh`)
- TS test: `bun test tests/ts` (root `package.json` `test`); `bun run test` in `omp/` (`bun test --timeout 60000 ./test/extension.test.ts && bun test --timeout 60000 --path-ignore-patterns='**/extension.test.ts'`)
- Web: `node transpile.mjs`, `node serve.mjs` (`web/package.json` scripts)
- Registry/doc gates: `python3 -m swarm.tool_registry check|docs --check|gen-tests --check`;
  `python3 -m swarm.skill_registry check|docs --check`; `python3 scripts/_registry_intake.py --check`;
  `python3 scripts/build_agents.py --check`; `python3 scripts/_check_pins.py`
- Wheel: `scripts/ci_build_wheel.sh` (maturin; module `swarm._native` from
  `crates/gsd-python/Cargo.toml`, `python-source = "."`, `bindings = "pyo3"`)
- Toolchain install: `scripts/ci_rust_toolchain.sh` (rustup, reads channel from `rust-toolchain.toml`)
- Pre-commit hooks: the same registry checks listed above (`.pre-commit-config.yaml`)

## 3. The 30 subagents: tools, unique vs shared

Manifest: `agents.json` → `agents[]` (30 entries, ids `A01`…`A30`, slugs
`a01-orchestrator`…`a30-custody-compliance`, legs A01–A15 `delivery`/`control`
plus A16–A30 specialist lanes; per-agent `max_parallel` 2–8).

**OMP host tools** (`agents[].tools`, counted with `jq`):

| Tool | Agents |
|---|---|
| `Read`, `Grep`, `Glob`, `Bash` | all 30 |
| `Write` | 28 (missing A09, A10) |
| `Edit` | 25 (missing A09, A10, A12, A27) |
| `Agent` | 1 (A01 only) |

Per-agent counts: 6 tools for 25 agents; 5 for A12, A27; 4 for A09, A10
(total 174 refs). Source: `jq '.agents[] | [.id, (.tools\|length)]'` on
`agents.json`.

**Registry tools** (`agents[].registered_tools`): 12 tools per agent, 13 for
A15, A19, A22, A23, A26, A27, A28, A29, A30 — 369 entries total, and
`jq -r '.agents[] | .registered_tools[].name' | sort -u | wc -l` = 369, i.e.
**all 369 registry-tool names are unique across agents** (no name is shared
between two agents). Sources by `entrypoint`: `swarm/tools/aNN_tools.py`
(5–13 tools each), plus `swarm/tools/batch_01_05.py` (11), `batch_06_10.py`
(5), `batch_11_15.py` (6), `orch_registry_ping.py` (1) — total must equal
369 by construction; the per-file tally above sums to 369 only if every
entrypoint is counted once, which the `jq` uniq-count of names confirms.

**Shared baseline** (`agents.json` `shared_baseline`): 21 tool names
(`shared_handover_write`, `shared_handover_read`, `shared_handover_validate`,
`shared_handover_bundle`, `shared_handover_verify`, `shared_handover_receipt`,
`shared_artifact_digest`, `shared_artifact_check`, `shared_dod_check`,
`shared_open_findings`, `shared_attempt_history`, `shared_lease_state`,
`shared_open_questions`, `shared_status_report`, `shared_scratchpad_put`,
`shared_scratchpad_get`, `shared_escalate`, `shared_cancel_check`,
`shared_log_emit`, `shared_route_check`, `shared_escalation_owner`) and 12
skill names (`swarm-cooperative-cancel` … `swarm-status-reporting`) available
to all agents. `shared_tools[]` defines 25 shared tool schemas with
entrypoints in `swarm/tools/shared_handover.py` (13), `shared_pool.py` (4),
`shared_runtime.py` (8).

**Capability policy**: `agents.json` `capability_policy` = tools min 10,
max 15, enforce true; skills min 10, max 15, enforce true. Note the observed
per-agent `registered_tools` count (12–13) sits inside this range, while
`tools` (OMP host tools, 4–6) does not — UNVERIFIED whether the policy is
evaluated against `registered_tools` or `tools`.

**Scripts per agent** (sample): A01 → `scripts/orch_plan.py`,
`scripts/orch_status.py`, `scripts/swarm_run.py`; A03 →
`scripts/arch_adr.py`, `scripts/arch_contract_check.py` (source: `jq`
`agents[].scripts`).

**Generated artifacts**: `omp/agents/` (30 files) and `omp/skills/` (407
dirs) are generated from `agents.json`/`prompts/`/`skills/` by
`scripts/build_agents.py` (CI runs it with `--check`); `agents/*.md` and
`skills/*` are the human-readable inputs. Source: `.github/workflows/ci.yml`,
`scripts/build_agents.py` header.

## 4. Decomposition, concurrency, Rust↔Python call paths

Discovered symbols (all observed via `grep` on the named files):

- **Decomposition (Python)**: `python_tools/decomposition.py:14`
  `decompose_task(task)`; `swarm/graph_planner.py` `GraphPlanner`,
  `plan_graph`, `topological_waves`, `validate_graph`, `TaskNode`, `Plan`,
  `Assignment` (lines 53–322).
- **Concurrency (Python)**: `python_tools/orchestration.py`
  `SubagentPool` (47), `rust_dispatch(subtask, rust_pool, rust_mod)` (177),
  `gsd_submit_task` (204), `gsd_collect_results` (218);
  `python/orchestrator/pool.py` ("Port of swarm classifier for pool
  errors", line 343); `python/orchestrator/decomposer.py`.
- **Concurrency (Rust)**: `crates/concurrency/src/execution.rs` `ThreadPool`,
  `ConcurrencyConfig`, `SubTask`, `SubTicket<T>`, `CancelToken` (lines
  205–416); `crates/gsd-scheduler/src/` `scheduler.rs`, `dag.rs`,
  `ready_frontier.rs`, `force_parallel_dispatch.rs`, `matrix_join.rs`,
  `nexus_barrier_join.rs`; `crates/gsd-sched-core/src/` `aimd.rs`,
  `backoff.rs`, `claims.rs`, `drain.rs`, `pool.rs`, `ports.rs`, `select.rs`,
  `runtime.rs`, `panic_guard.rs`.
- **Rust→Python (PyO3 extension `swarm._native`)**: implemented in
  `crates/gsd-python/src/` — `lib.rs` (`ClaimTable`, `AimdLimiter`),
  `sched.rs` + `sched_run.rs` (`run_scheduler`, `NativeCleanupFence`),
  `chat.rs` (`Governor`, `ChatPool`, `ChatTicket`), `pyo3/module.rs`
  (`GraphReport`, graph family). Built by maturin per `pyproject.toml`
  (`[tool.maturin]`, `manifest-path = "crates/gsd-python/Cargo.toml"`,
  `module-name = "swarm._native"`). The compiled artifact
  `swarm/_native.abi3.so` (1.2 MB) is present in-tree.
- **Python→Rust (call surface)**: `swarm/_native_loader.py` import gate
  (`_MODULE = "swarm._native"`, raises actionable `ImportError`); typed
  surface in `swarm/_native.pyi` (error-code, retry, `orch_status`,
  redaction, `sched_*`, `ClaimTable`, `AimdLimiter`, `run_scheduler`,
  `graph_*`, `registry_lookup`, `error_from_py`); `swarm/rust_scheduler.py`
  wraps `swarm._native.run_scheduler` ("owns the decision loop, the claim
  table, AIMD, backoff and the worker pool"). Import probe (§7) listed 25
  public names: `AimdLimiter`, `ChatPool`, `ChatTicket`, `ClaimTable`,
  `Governor`, `GraphReport`, `NativeCleanupFence`, `code_from_wire`,
  `decode_reply_wire`, `error_from_py`, `graph_api_version`,
  `graph_schedule`, `graph_submit`, `graph_validate`,
  `orch_status_invoke`, `redact_spans`, `registry_lookup`, `retry_policy`,
  `run_scheduler`, `sched_backoff_s`, `sched_claims_overlap`,
  `sched_drain_plan`, `sched_normalize_claim`, `sched_order_ready`,
  `wire_codes`. UNVERIFIED: whether `.pyi` and the `.so` are in sync
  (stubtest was not run).
- **Parity gates**: `scripts/ci_parity_rust.sh` (Python truth leg vs Rust
  host/sandbox legs via `tests/parity/harness.py`, min 11 cases) and
  `scripts/ci_scheduler_rust.sh` (scheduler binding/threads/stubs/vectors,
  byte-identical workspace guarantee).

**Classifier copies** (per `crates/gsd-sdk/src/omp_classifier_rule.rs`
header, verified by `grep`): the SDLC prompt classifier exists in **4 copies**
— `omp/src/hooks.ts` (authority: `classifyPrompt`, `classifiedText`),
`swarm/signal_detector.py` (`classify`, `extract_unescaped_original`),
`hooks/user_prompt_submit.py` (`classify`), and
`crates/gsd-sdk/src/omp_classifier_rule.rs` (Rust mirror, "fourth copy").
Shared fixture: `tests/fixtures/classifier_prompts.json`
(`{positive, negative}`), read by `omp/test/hooks.test.ts`,
`tests/test_hook.py`, and the Rust module (`include_str!`). Assignment
brief states "classifier in 3 copies + fixture" — observed. Note
`swarm/retry.py` line 1 also self-describes as "the transient-error
classifier" (a different classifier, same word); `crates/gsd-redact/src/keys.rs`
mentions token "classifiers" for name-token walking — neither is the SDLC
prompt classifier UNVERIFIED beyond the header claim, but the 4-copy list
above is from the Rust module's own doc comment.

## 5. Plugin entry points

| Entry point | Source |
|---|---|
| OMP extension: `omp.extensions = ["./src/index.ts"]` | `omp/package.json` |
| OMP config: `extensions: [omp]` | `.omp/config.yml` |
| Swarm run: `scripts/swarm_run.py` (150 KB) | `scripts/swarm_run.py` |
| Autonomous hook: `hooks/autonomous_run.py` | `hooks/autonomous_run.py` |
| Agent generation: `scripts/build_agents.py` | `scripts/build_agents.py` |
| Tool registry CLIs: `python3 -m swarm.tool_registry`, `python3 -m swarm.skill_registry` | `.pre-commit-config.yaml`, `ci.yml` |
| Native module: `swarm._native` via maturin from `crates/gsd-python` | `pyproject.toml` `[tool.maturin]` |
| MCP servers: `{ "mcpServers": {} }` (empty) | `.mcp.json` |
| Web: `node transpile.mjs` / `node serve.mjs` | `web/package.json` |

## 6. Unknowns (working assumptions — NOT facts)

Per assignment, the following are recorded as unknowns. The "assumption"
column is the working hypothesis for downstream steps, never a verified fact.

| Unknown | Working assumption | Basis (not proof) |
|---|---|---|
| Omp | OMP is a TypeScript host extension, not OpenMP | `DECISIONS.md` D1 row (untracked, user-authored); `omp/package.json` entry exists |
| workflowz | Existing Swarm orchestration (DAG dispatch policy only), no new DSL/crates | `DECISIONS.md` D2 row; `swarm/graph_planner.py` + `scripts/swarm_run.py` exist |
| Perf target | Wall-clock only, no SLO | `DECISIONS.md` D3 row; UNVERIFIED whether any SLO config exists in repo (not searched exhaustively) |
| native-ext | Hold PyO3 expansion until inventory proves it; Python stays default | `DECISIONS.md` D4 row; `swarm/_native.abi3.so` present proves a build exists, not a policy |
| Idempotency | UNVERIFIED — `DECISIONS.md` D5 was truncated at "Per-tool idemp…" in read output, so the assumption is unknown | truncated read; needs a full re-read of `DECISIONS.md` D5 |

`DECISIONS.md` itself is untracked (`?? DECISIONS.md` in `git status`) and
was cited only as the source of these hypotheses. All five rows above must
be re-verified against a decision owner before being used as facts.

## 7. Baseline checks (cheap only — full suites NOT run)

All commands run with `env -u SWARM_ED25519_KEY -u SWARM_SIGNING_KEY
-u SWARM_ALLOW_INSECURE_DEV_KEY` (lesson: strip `SWARM_*` env before tests).

| Check | Result | Source |
|---|---|---|
| `cargo --version` | PASS (`cargo 1.98.1`) | direct run |
| `rustc --version` | PASS (`rustc 1.98.1`) | direct run |
| `python3 --version` | PASS (`Python 3.12.3`) | direct run |
| `bun --version` | PASS (`1.4.0`) | direct run |
| `python3 -c 'import swarm._native'` | PASS (25 public names listed in §4) | direct run |
| `cargo metadata --no-deps` | PASS (workspace resolves; first package `gsd-runtime` shown) | direct run |
| `cargo test --workspace --locked -- --list` | PASS as a *listing* (compiled; 1624 lines matching `: test$` — i.e. 1624 test entries discovered). Tests were **not executed**. | direct run |
| `python3 -m pytest --collect-only -q` | Ran without collection errors; total count UNVERIFIED (probe `grep -c '::'` returned 0 due to `-q` output format, so no count was captured) | direct run |
| `mypy --version` | UNVERIFIED (printed config text, not a version) | direct run |
| `ruff --version`, `maturin --version`, `gh --version` | PASS (versions in §2) | direct run |
| Full `cargo test --workspace`, full `python3 -m pytest`, `bun test`, web `*.test.mjs` execution | **UNVERIFIED — not run** (assignment: known timeout; do not run full suites) | n/a |

Test inventory by file count (source: `find`/`ls`): 246 Python test files
under `tests/`, 42 Rust test files under `crates/*/tests/`, 1 under
`rust/tests/`, 22 `omp/test/*.test.ts`, 3 `tests/ts/*.test.ts`, 10
`web/*.test.mjs`. Pass/fail of any of these is UNVERIFIED until a full run.
