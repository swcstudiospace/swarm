# Rust core

Scope: the original source-reading snapshot on branch `feat/v2.4-rust-wasm-core` at HEAD `5b76090` is retained below; it is not a fresh audit of all current WIP. The 2026-10-31 quality/parity checkpoint (`Tcheckpoint-checkpoint`, correlation `ut-mvvownjp-ff01b275`) updates the specific pin, concurrency, source-root and evidence notes identified here from the signed parent assignment, ingested results and named source sections. Every engine default remains Python; no guest/web pin, backend or route default change is claimed. Parent Main reports scoped checks, not a final-tree or release-parity pass. A15 ran no tests, builds, gates, generation or publication for this edit-only checkpoint.

## 1. Crates

Workspace members (`Cargo.toml`, `[workspace] members`, 22 crates). Excluded: `crates/gsd-ipc` (no member uses it; incomplete module tree) and `crates/gsd-cli` (invalid manifest), per `[workspace] exclude`.

| Crate | Role (from its `Cargo.toml` `description`, unless noted) |
|---|---|
| `gsd-adapter` | Legacy adapter. Hosts the `gsd_engine` binary (`src/bin/gsd_engine.rs`), the engine routing (`src/route_table.rs`), the write routes (`src/write_route/`) and the gate routes (`src/gates/`). Feature `sandbox-write`. |
| `gsd-route` | Pure route logic: Python-shaped JSON and Task Store row shaping, argv grammars, no database access. Features `write-routes`, `write-routes-2`, `host-routes`. |
| `gsd-taskwriter` | Host-only Task Store writer: `BEGIN IMMEDIATE` transactions, conditional state updates, append-only audit rows over the Python-owned `tasks.db` schema. |
| `gsd-envelope` | Swarm envelope canonical form, signing and verification (HMAC-SHA256 and Ed25519). The only GSD-Core crate that reads signing keys. |
| `gsd-sandbox` | Wasmtime host for the guest: module identity, loading, limits, typed invoke boundary. Holds the guest pins (section 7). |
| `gsd-guest` | Sandboxed guest: the `gsd:core` WIT world exporting `invoke` over the pure route logic. `cdylib`, `publish = false`, built for `wasm32-unknown-unknown` by `gsd-sandbox/build.rs`. |
| `gsd-python` | PyO3 extension `swarm._native` (section 2). |
| `gsd-redact` | Secret-span detection for run-log text; a port of `swarm/redact.py`, which is the parity oracle. |
| `gsd-sched-core` | Scheduler decision core: AIMD limiter, claim table, ordering, backoff, pass loop. std only. |
| `gsd-codes` | The seven shared error codes (per the `gsd-python` crate docs; its manifest has no description). |
| `gsd-failure` | Circuit breakers, retries, dead-letter queues. Source of the default retry policy (`gsd-python/src/lib.rs`, `retry_policy`). |
| `gsd-sdk` | Rust mirror of the omp host contract: bridge argv, exit mapping, process-group runner, constants and classifier pins. |
| `gsd-proto` | Protobuf definitions for internal messages and types. |
| `gsd-serde` | prost-reflect and bincode persistence for checkpoints and artifacts. |
| `gsd-runtime` | Deterministic clock, async primitives, I/O utilities. |
| `gsd-pool` | Thread pool manager for chat-thread subagents. |
| `gsd-scheduler` | DAG-aware, priority-driven concurrent executor. |
| `gsd-governor` | Token budgets, rate limits, concurrency caps, cost controls. |
| `gsd-observability` | Structured logging, tracing, metrics, replay. |
| `gsd-security` | Thread isolation, capability tokens, audit logging, PII redaction. |
| `gsd-nexus` | Pub/sub, request/response, shared-memory CRDT, broadcast. |
| `gsd-state` | Versioned, branchable, mergeable session state. |

The `gsd_engine` binary is built from `crates/gsd-adapter/src/bin/gsd_engine.rs` (no `[[bin]]` table; auto-discovered). Build: `cargo build -p gsd-adapter --bin gsd_engine --release --locked` (`docs/operations-runbook.md`, "Build and run"). It reads one JSON object on stdin (`{agent, script, argv, root, env}`, plus `effects_path` for a write), writes the reply on stdout, and exits with the reply's exit code (`gsd_engine.rs`, module doc).

### 1.1 Governor release accounting

`crates/gsd-governor/src/lib.rs` keeps outstanding amounts by the exact session/thread pair, under the global accounting lock. A release returns at most that pair's outstanding tokens, wall time, tool calls and cost. It clamps local used counters at zero and refunds only the corresponding capacity. Unknown or mismatched pairs and repeats after a pair is fully returned do not refund another pair's reservations. Reservation and release take local locks in the same order (session, thread, global); no public signature or default is changed.

Global cost remains integer USD micros, rounded separately for each reservation. Partial cost refunds cannot exceed the micros still charged to the pair; returning all of its cost refunds exactly those remaining micros. Local USD counters still use floating point. This does not introduce exact-decimal pricing, configurable global-cap enforcement, rate limiting or scheduler integration: the existing global check is still hard-coded to 1,000,000 tokens and USD 100, and the rate-limit hook still returns true. The amount-based API has no reservation identity, so it cannot distinguish a delayed duplicate from a legitimate release after new reservations on the same pair.

Behavioral regressions are authored in `crates/gsd-governor/tests/release_accounting.rs`: repeated and oversized returns, unknown and mismatched pairs, shared sessions/threads, partial multi-resource returns, and separately rounded cost charges. They use the public reserve/release API, snapshots, and subsequent grant/refusal decisions. The existing in-module `release_without_matching_reservation_frees_no_global_usage` regression is retained. Neither the governor edit-only lane nor this documentation lane ran verification. Main subsequently reports a scoped workspace run (section 11); that is not a final-tree gate or proof that the amount-based duplicate ambiguity above is solved.

### 1.2 AsyncBarrier generation correction

The recovery edit in `crates/gsd-runtime/src/sync.rs` replaces independent atomic count/reset/notify state with `Arc<tokio::sync::Barrier>`. It preserves `Clone`, `new`, `wait`, `wait_timeout`, the nonzero-count assertion and timeout wrapper. Six public generation-invariant tests in `crates/gsd-runtime/tests/barrier_generations.rs` cover cohort/leader behavior through manual polling; they are not a deterministic failing-before proof of the old private reset/notify interleaving. Cancellation-unsafety remains: no arrival rollback, poison/reset semantics or cancellation-safe API is introduced. This primitive correction does not implement or start the native scheduler binding/facade.

## 2. Python binding: `swarm._native`

**Build.** `pyproject.toml` `[tool.maturin]`: `manifest-path = "crates/gsd-python/Cargo.toml"`, `module-name = "swarm._native"`, `bindings = "pyo3"`, `features = ["extension-module"]`, `python-source = "."`. `requires-python >= 3.12`. The PyO3 build is `abi3-py312` (`Cargo.toml` workspace dependency `pyo3`). maturin is `>=1.15,<2.0` in `pyproject.toml`; CI installs `maturin==1.15.0` (`.github/workflows/ci.yml`, `rust` job; `.github/workflows/wheel.yml`, job `wheel`). `pip install .` builds the extension (`swarm/_native_loader.py`, `_INSTALL_HINT`). The output `swarm/_native*.so` is gitignored (`.gitignore`, line 10).

**Exports** (`crates/gsd-python/src/lib.rs`, `fn _native`): functions `code_from_wire`, `wire_codes`, `retry_policy`, `orch_status_invoke`, `decode_reply_wire`, `redact_spans`, `sched_order_ready`, `sched_normalize_claim`, `sched_claims_overlap`, `sched_backoff_s`, `sched_drain_plan`; classes `ClaimTable` and `AimdLimiter`. Type stub: `swarm/_native.pyi`, checked by `mypy.stubtest` in `wheel.yml`.

**Runtime callers in Python** (grep of `swarm/` and `scripts/`):

- `swarm/__init__.py`: calls `load_native()` unless `SWARM_PURE_PYTHON=1` (the import gate).
- `swarm/redact.py` `redact_spans`: calls `swarm._native.redact_spans` only when `SWARM_REDACT_ENGINE=rust`.
- `swarm/claims.py` `make_claim_table`: builds `NativeClaimTable` over `swarm._native.ClaimTable` only when `SWARM_CLAIMS_ENGINE=rust`.
- The other exports are referenced only by `tests/parity/` and `tests/typecheck/`.

**Missing or stale extension.**

- Without `SWARM_PURE_PYTHON=1`, `import swarm` raises `ImportError` with the install step (`swarm/__init__.py`; `_native_loader.load_native`). The package does not fall back silently.
- `[INFERENCE]` From reading the code: because that import fails first, the "missing extension" branches in `redact.py` and `claims.py` are not reached through a normal import. The stale-extension branch (a build without `redact_spans` or `ClaimTable`) is reachable.
- With `SWARM_PURE_PYTHON=1` the import is skipped. `SWARM_REDACT_ENGINE=rust` and `SWARM_CLAIMS_ENGINE=rust` each warn once (reason `pure-python`) and use Python (`redact._rust_engine_selected`, `claims._rust_engine_selected`).
- Stale extension: redaction uses Python for that call; the claim table is built as Python at construction. One warning per reason per process (`redact._native_spans`, `claims.make_claim_table`).
- A native exception: redaction uses Python for that call (`redact._native_spans`); a claim table falls back to a Python `ClaimTable` that holds the claims so far (`claims.NativeClaimTable._fall_back`).

## 3. Engine switches

| Variable | Default (unset or empty) | Accepted values | Read in | Opt-out or rollback |
|---|---|---|---|---|
| `GSD_ENGINE` | `python` on every route: `DEFAULT_ENGINE`, `DEFAULT_ENGINE_WRITE`, `DEFAULT_ENGINE_INGEST` (`scripts/orch_status.py`, lines 169-181); `default=lambda: "python"` in `qa_gate.py`, `rel_plan.py`, `rev_gate.py`, `sec_gate.py`, `orch_plan.py`, `orch_assign.py` | `python`, `rust`. Any other value: exit 2, `E-INPUT`, value not echoed | `swarm/engine_select.py` `select_engine` | `GSD_ENGINE=python` |
| `SWARM_PURE_PYTHON` | not set | exactly `1` | `engine_select.select_engine`; `_native_loader.pure_python_requested`; `swarm/__init__.py` | Global rollback: forces Python on every route and skips the extension import |
| `GSD_ENGINE_BIN` | not set | a path | `engine_select.select_engine` | When set it is the only candidate; a non-executable value is not replaced by the release binary. Its digest must match `GSD_ENGINE_BIN_SHA256` for a keyed child (3.2) |
| `GSD_ENGINE_BIN_SHA256` | not set | 64 lowercase hex digits | `engine_select.identity_refusal` | Needed only when a child holds a signing key (3.2) |
| (release binary) | `target/release/gsd_engine` under the repo root, if executable | | `engine_select.RELEASE_BINARY` | Only used when `GSD_ENGINE_BIN` is unset or empty |
| `GSD_RUST_BACKEND` | `host`: the binary's `DEFAULT_BACKEND = Backend::Host` (`gsd_engine.rs`) | `host`, `sandbox`. Other values: `E-INPUT` | `gsd_engine.rs` `backend_named`; `engine_select.select_engine` checks it only when the binary is about to be used | Unset means host. `sandbox` is opt-in (section 6) |
| `GSD_RUST_TIMEOUT_S` | 660 s (`engine_select.RUST_CALL_TIMEOUT_S`) | positive ASCII integer; other values ignored | `engine_select.rust_timeout_s` | The binary's own deadline is 600 s (`DEADLINE` in `gsd_engine.rs`); it does not cover start-up or the stdin read |
| `SWARM_REDACT_ENGINE` | `python` | `python`, `rust`. Other values warn once and use Python | `swarm/redact.py` `_rust_engine_selected` | Unset or `python` |
| `SWARM_CLAIMS_ENGINE` | `python` | `python`, `rust`. Other values warn once and use Python | `swarm/claims.py` `_rust_engine_selected`, `make_claim_table` | Unset or `python` |
| `SWARM_TASKSTORE_WAL` | off | `1` turns on WAL with `synchronous=NORMAL` at each open. Any other value does nothing | `swarm/taskstore.py` `_tune_journal` | Unset. To revert the stored mode, see `docs/operations-runbook.md`, "Task Store journal mode" |
| `SWARM_GSD_STATE_FILE` | unset: no `.planning/STATE.md` mirror | a path to a file. Unset or empty writes nothing to `STATE.md` | `swarm/state_store.py` `gsd_state_path` (line 30), `append_gsd_line` | Unset or empty. It does not gate the `.claude/swarm-state.json` writes (section 10, L4) |

`GSD_ENGINE`, `GSD_ENGINE_BIN`, `GSD_RUST_BACKEND` and `GSD_RUST_TIMEOUT_S` apply only to scripts that use the selector (section 4). `SWARM_REDACT_ENGINE` and `SWARM_CLAIMS_ENGINE` are separate switches; they do not read `GSD_ENGINE`.

### 3.1 Resolution order

Resolution for one call (`swarm/engine_select.py` `select_engine`):

1. `SWARM_PURE_PYTHON=1`: Python.
2. `GSD_ENGINE=python`: Python.
3. Any value other than unset, empty or `rust`: `E-INPUT`, exit 2.
4. `GSD_ENGINE=rust` (explicit): a call the route claims goes to the binary. Without a binary, `E-DEP`, exit 2. An argv no route claims goes to Python once the binary is confirmed startable. An identity refusal (3.2) exits 2 with `E-POLICY`; Python does not run.
5. Unset or empty: the route's default row answers. Only a row set to `rust`, with a route that claims the argv, a grammar that parses it and an executable binary, reaches the binary. Otherwise Python answers silently. In the code today every row is `python`.
6. `GSD_RUST_BACKEND` is validated just before the binary is used.

The read, write and ingest outcome tables are in `docs/operations-runbook.md`, sections "Fallback table" and "Write routes". Both are implemented in `engine_select.run_read` and `engine_select.run_write`. `RUST_FALLBACK_CODES` is `E-INTERNAL`, `E-DEP`, `E-CAPACITY`, `E-CONTRACT` and `E-INPUT` (`engine_select.py`).

### 3.2 Children that hold a signing key

Read as: `swarm/engine_select.py` (`KEY_ENV_NAMES`, `carries_key`, `identity_refusal`, `no_core_dumps`, `child_guard`), called from `run_read`, `_answer_write` (both before the start of the child). Read, not exercised.

- **Which children.** A child is keyed when its environment holds one of `SWARM_SIGNING_KEY`, `SWARM_ED25519_KEY` or `SWARM_ED25519_KEY_FILE` (`KEY_ENV_NAMES`). Each script's `_binary_env` grant lists the key names, and it copies only those that are set in the parent (`os.environ` filter). The grants that include key names are: `qa_gate`, `rel_plan`, `rev_gate`, `sec_gate`, `orch_plan`, `orch_assign` (their `_binary_env`), and `orch_status`'s `transition-approved` and `ingest` (`ROUTE_KEY_ENV`, `scripts/orch_status.py`, per its comment: "a transition to APPROVED and every ingest verify signatures").
- **Identity check.** For a keyed child, the binary is started only when `GSD_ENGINE_BIN_SHA256` is set to 64 lowercase hex digits and equals the SHA-256 of the binary file (`binary_sha256`, a streamed read of the path). Otherwise `identity_refusal` returns a message and the selector writes one line to stderr (`<script>: E-POLICY: ...`); the binary is not started. Under explicit `GSD_ENGINE=rust` the call then exits 2 with an `E-POLICY` reply and Python does not run (`identity_refused`, `explicit=True`). Under the default engine `run_read` and `run_write` return None, and the Python body answers. This is not a reply from the binary. The check happens before the start, not at exec (section 10, L2).
- **Core dumps.** A keyed child is started with `preexec_fn = no_core_dumps`: `RLIMIT_CORE` 0, soft and hard. `child_guard` uses it in place of the route's own hook, for reads and for writes (`_answer_write`, where the route hook is `route.write.preexec(env_route)` and is replaced for a keyed child). The gate scripts' hooks are `no_core_dumps` anyway.
- **Linux process inspection (F-6).** Before reading stdin, `gsd_engine` checks the same three key names for presence, including an empty-present value, and uses the safe rustix API to set `PR_SET_DUMPABLE` to zero. A kernel refusal returns `E-DEP` with `write_state: not_started` before an invocation is read; the diagnostic contains no key value. Unkeyed processes are unchanged. This is defence in depth against unprivileged process inspection, not per-agent UID/container isolation or protection from a privileged debugger; AR-04 key custody remains open.
- **What the check keys on.** The identity check and the core-dump guard test only `KEY_ENV_NAMES`, three names. The isolated HOME (`child_home`) is triggered by those three plus `SWARM_REQUIRE_KEY`, `SWARM_ALLOW_INSECURE_DEV_KEY`, `SWARM_AGENT_SESSION` and `XDG_CONFIG_HOME` (`HOME_ISOLATING_ENV_NAMES`; section 10, L1). A child that gets only those non-key names is HOME-isolated but neither identity-checked nor core-dump guarded.

### 3.3 Scoped core scheduler panic output

`gsd-sched-core::panic_guard::NativeSchedulerScope::enter()` is an explicit, thread-bound Rust opt-in. Its first use installs one hook; importing the module, creating an ordinary pool, or submitting ordinary work installs none. Native-marked work suppresses panic-hook payload output, while unmarked work calls the captured prior hook unchanged. Pool submissions capture the mark per job, including queued work; a guard stays alive through catch/unwind and caught-payload disposal.

`with_host_callback` suspends and restores the native mark around arbitrary host/Python execution. This is not a Python debug API or a claim to censor callback-owned output. The embedding host must finish hook setup before first entry and must not replace the hook during supported use. Later replacement, unavailable thread-local storage, `panic=abort`, and output outside the hook are explicit limits; the implementation does not fight a host replacement.

Core child-process tests and a standalone public-API consumer exercise native worker isolation, sibling completion, and prior-hook behavior. These are core proofs, not evidence that the pending Python runtime binding or scheduler default flip is complete. All defaults remain unchanged.

## 4. Routes

| Route | Selector (script) | Argv the selector claims | Selector class | Default row | Rust route (`crates/gsd-adapter/src/route_table.rs`) |
|---|---|---|---|---|---|
| `orch_status` read (`list`, `--history`) | `scripts/orch_status.py` `READ_ROUTE` | every argv; `rust_serves` admits `--json` with read flags only | read: `run_read` | `DEFAULT_ENGINE` = `python` | `ORCH_STATUS_READ_DEFAULT = Python` (line 90) |
| `orch_status --transition` | `WRITE_ROUTE` | any argv with `--transition` | write: `run_write` | `DEFAULT_ENGINE_WRITE` = `python` | `ORCH_STATUS_TRANSITION_DEFAULT = Python` (line 96) |
| `orch_status --ingest` | `INGEST_ROUTE` | any argv with `--ingest` | write: `run_write` | `DEFAULT_ENGINE_INGEST` = `python` | `ORCH_STATUS_INGEST_DEFAULT = Python` (line 101) |
| `qa_gate` | `scripts/qa_gate.py` `__main__` | `--json`, without `--dry-run`, `--help` or `-h` (`_claims_qa`) | write: `run_write` (section 5.1) | `python` | `gates::qa_gate` |
| `rel_plan` | `scripts/rel_plan.py` `__main__` | same rule (`_claims_rel`) | write: `run_write` (5.1) | `python` | `gates::rel_plan` |
| `rev_gate` | `scripts/rev_gate.py` `__main__` | same rule (`_claims_rev`) | write: `run_write` (5.1) | `python` | `gates::rev_gate` |
| `sec_gate` | `scripts/sec_gate.py` `__main__` | same rule (`_claims_sec`) | write: `run_write` (5.1) | `python` | `gates::sec_gate` |
| `orch_plan` | `scripts/orch_plan.py` `__main__` | the flags in `_PLAN_NO_VALUE` and `_PLAN_VALUE`, each scalar once (`_plan_options`) | write: `run_write` | `python` | `write_route::plan` |
| `orch_assign` | `scripts/orch_assign.py` `__main__` | `--json` with `--dry-run`, `--root`, `--task-id`, `--correlation-id` (`_claims_assign`) | write: `run_write` (D3) | `python` | `write_route::assign` |

`route_for_call` in `route_table.rs` accepts `rust` for exactly these nine route classes and refuses every other entrypoint with `E-DEP`, `write_state = "not_started"` (`before_any_write`). Its refusal text names only the three `orch_status` routes (D2).

- `swarm_tool` has no Rust route. `scripts/swarm_tool.py` has no selector and never reads `GSD_ENGINE`.
- The catalog exists only as a guest mode (`crates/gsd-route/src/catalog.rs`). No route in `route_table.rs` names it.
- `scripts/swarm_run.py` has no selector.

## 5. Write routes (opt-in)

Each write route runs on Rust only when a call sets `GSD_ENGINE=rust`. Python is the default row for each one (table in section 4). Eight routes have a write flow (`engine_select.WriteFlow`, served by `run_write`): `orch_status --transition`, `orch_status --ingest`, `orch_plan`, `orch_assign`, and the four gate routes `qa_gate`, `rel_plan`, `rev_gate` and `sec_gate`.

A write is never run twice. `run_write` answers from a table: Python answers only when the binary says nothing was written (`write_state = "not_started"`). Any other doubtful outcome is refused with a typed `E-INTERNAL`, `write_state = "unknown"`, with no Python run (`engine_select.run_write`, `unknown_outcome`). A failure the binary marks `final` is printed as the binary's answer, and Python does not run it again.

Per-route declines (what Python answers instead), the APPROVED transition rules and the known gaps are in `docs/operations-runbook.md`, section "Write routes". The rollback is `GSD_ENGINE=python`. The runbook records that the rollback drill has not been run.

### 5.1 Gate verdicts: final and unknown

The rules below are in the Rust gate routes (`crates/gsd-adapter/src/gates/`) and in the selector's settlement (`engine_select._answer_write`). Read, not exercised.

- **Final after a commit.** A failure of a verdict-file or plan-file write after the Task Store commit is `write_state = "final"` (`after_commit`, `crates/gsd-adapter/src/strangler_seam.rs`). The sites are `write_plan_file` in `qa_gate.rs`, `rev_gate.rs` and `sec_gate.rs`; `write_files` in `rel_plan.rs`; and `committed_verdict` in `rev_gate.rs`, which runs when the committed envelope carries no verdict. The verdict is recorded and is not run again.
- **Not started before a commit.** The same writers answer `before_any_write` (`not_started`) when nothing was committed. That covers the no-task path, which derives the plan without a store and commits nothing (`committed()` in each gate file), and an `issue_gate` failure that the writer reports as `NotStarted`.
- **Unknown from `issue_gate`, rel_plan.** `rel_plan.rs` `issue_gate_refusal` maps a `WriterError` whose state is `Final` to `outcome_unknown` (`E-INTERNAL`, `write_state = "unknown"`). The writer marks a failure `Final` when it happens at or after the first write statement, including the commit (`crates/gsd-taskwriter/src/store.rs` `failure`, `commit`).
- **qa_gate, rev_gate, sec_gate: no tag.** These three pass an `issue_gate` failure through as `error.to_seam_error()` with no `write_state` (for example `qa_gate.rs`, around line 596). The selector then decides from the code: `E-INTERNAL` with no `write_state` is `unknown` (`unknown_outcome`); any other code with no `write_state` is printed as the binary's refusal. Python does not run in either case. The outcome is right, but only by the selector's rule. Tagging these three as rel_plan does is not done (D1).

### 5.2 Source roots and native-served evidence

The current source has deliberate self-scan boundaries, not a blanket requirement to run a gate from one special directory:

- Python `scripts/sec_gate.py` passes resolved `__file__` to `scan_files`; it skips the exact resolved executing script, not every file of that name.
- Python `scripts/rev_gate.py` passes resolved `__file__` to `mechanical_checks`; its own resolved script is exempt from selected TODO/debug/secret checks, not every mechanical check.
- Native `crates/gsd-adapter/src/gates/sec_gate.rs` conservatively declines when a scanned resolved file is named `sec_gate.py`. Native `rev_gate.rs` declines a changed file named `rev_gate.py` or a symlink in the changed set because it cannot prove Python-equivalent self handling.

A typed decline can hand the call to Python. Matching output from such a fallback is not actual native-served parity. A detached minimal root avoids a self-scan decline only where it still represents the scenario under test; it is not a waiver of the scanner's real source-root behavior. Main's actual current-binary sec/rev/qa/rel served-parity run remains pending at this checkpoint.

## 6. Sandbox backend (default off)

- The binary's default backend is `host` (`gsd_engine.rs`, `DEFAULT_BACKEND`). Only `GSD_RUST_BACKEND=sandbox` runs the Wasm guest (`gsd-sandbox`, wasmtime) first.
- A guest fault other than a timeout fails over to host Rust inside the binary. The binary prints one line on stderr, `gsd_engine: sandbox fault <CODE>; answered by host Rust`, and stdout and the exit code are unchanged (`gsd_engine.rs` module doc; runbook, "Failover notice").
- A write on the sandbox backend is served only by a binary built with `--features sandbox-write` (`crates/gsd-adapter/Cargo.toml`: `sandbox-write = ["gsd-sandbox/write-routes"]`). Any other build refuses it with `E-DEP`, `write_state = "not_started"` (`gsd_engine.rs` module doc). CI builds that binary with `cargo build -p gsd-adapter --bin gsd_engine --features sandbox-write --locked` (`ci.yml`, `rust` job).
- The guest is built by `crates/gsd-sandbox/build.rs`: `gsd-guest` for `wasm32-unknown-unknown` with `--locked --release`, wrapped as a component (`wit-component`), its SHA-256 written to `OUT_DIR/guest-module.sha256`. With the `write-routes` feature the nested build uses the `write-routes-2` feature and pins `guest-module-write.sha256`.
- The runbook records the sign-off status of the default guest pin, `91b0970e...` (`docs/operations-runbook.md`, "Open sign-offs and blockers"). The write guest has its security and reproduction records, `A10-write-guest-50b06310.md` and `A11-write-guest-50b06310.md` in `tests/parity/records/14.5/`.

## 7. Pins and how they are checked

| Pin | Value | Where | Checked by | Run by CI? |
|---|---|---|---|---|
| Rust toolchain | channel `1.98.1`, components `rustfmt`, `clippy`, target `wasm32-unknown-unknown` | `rust-toolchain.toml` | `scripts/_check_pins.py` (raw bytes of the file) | `test` job runs `_check_pins.py`; `rust`, `clippy`, `parity-rust`, `supply-chain` install the channel with `scripts/ci_rust_toolchain.sh` |
| Workspace Rust pins | `rustc 1.98.1`, `wasmtime 50.0.1`, `wit-bindgen 0.57.1`, `wasm_target` | `Cargo.toml`, `[workspace.metadata.gsd-pins]` | `scripts/_check_pins.py` (canonical JSON of the table), against `scripts/pins.sha256`; Main reports regenerated digest and 13 passing pin tests below | `test` job |
| Default guest | `91b0970e...9a175` | `crates/gsd-sandbox/guest-module.sha256` | `guest_digest::built_guest_digest_matches_the_committed_pin` (`crates/gsd-sandbox/src/lib.rs`) compares the build's digest with this pin | `rust` job, `cargo test --workspace --locked --no-fail-fast`. `[INFERENCE]` that the workspace run builds gsd-sandbox without `write-routes` |
| Write guest | `50b06310...15d4` | `crates/gsd-sandbox/guest-module-write.sha256` | `crates/gsd-sandbox/tests/guest_imports.rs`, `the_embedded_guest_is_the_flavour_the_feature_selects`, under `--features write-routes` | `rust` job, step "guest imports (write guest)" |
| jco | `@bytecodealliance/jco` `1.39.0`; `component_sha256` equals the default guest pin | `web/jco.pin.json` | `web/transpile.mjs` (refuses another jco version and another component digest) | No. No CI job or gate script runs it (section 9, D5) |

`scripts/_check_pins.py` covers only the toolchain file and the `gsd-pins` table. It does not check the guest pins or `web/jco.pin.json`.

**Wasmtime security patch (scoped parent measurements, not release sign-off).** The user approved the exact workspace dependency `=50.0.1` and matching `gsd-pins` metadata `50.0.1`, retaining `component-model` and unrelated pins. The earlier cargo-deny run against 50.0.0 found RUSTSEC-2026-0422, RUSTSEC-2026-0423 and RUSTSEC-2026-0424. `tests/test_pin_hash.py` now asserts live exact dependency/metadata equality and the retained feature; its synthetic 50.0.0 fixture is historical input, not the live pin.

Main performed the precise targeted `Cargo.lock` update and canonical pin generation without changing the pin-hash algorithm. Parent-reported `scripts/pins.sha256` digest: `ab46999ec7d08c473a9b7329c326662825e33453d4968d3563b0787855ea966c`; **13 pin tests passed**. This digest covers the canonical toolchain/metadata inputs, not a guest or executable identity.

Main reports postpatch cargo-deny **advisories, bans, licenses and sources passed**, with the **unchanged six approved ignores and deny policy**. Preserve the original repository `deny-report.txt` and failed `/root/.cache/gsd-redact-work/resume-gates/deny-report.txt`; the passing report is `resume-gates/deny-after-wasmtime.txt` in the same cache directory. No advisory waiver or new ignore is claimed.

Main also reports Wasmtime 50.0.1 workspace **1247 passed, 2 ignored, 74 suites, 0 failed**. Current served gate parity, release harness/build parity, reproducible guest identity and any required new sign-offs are not established by those scoped results. No guest/web pin or backend/default change is authorized or claimed by this host-runtime patch.

**Pin rule.** The rule is in the D-P25 entry of `.planning/STATE.md` (2026-10-28): a commit touching `gsd-route`, `gsd-codes` or `gsd-guest` runs the sandbox pin tests. It is written only there. No workflow, runbook or script encodes it, and `ci.yml` and `wheel.yml` have no `paths:` filter (their `on:` is `push` to `main` and `pull_request`). The `rust` job runs the pin tests on every push and pull request, so the rule is covered by the workspace run, not by a path filter (section 9, D6).

## 8. CI jobs and local equivalents

`.github/workflows/ci.yml`: every job runs on `[self-hosted, linux, x64, omega-ci]` and is skipped for fork pull requests. Each starts with `scripts/ci_host_facts.sh`. The local equivalents are in `docs/operations-runbook.md`, section "CI gates".

| Job | What it runs (`ci.yml`) | Local equivalent (runbook) |
|---|---|---|
| `test` | `SWARM_PURE_PYTHON=1`; `ruff check .`; `python3 -m pytest`; `build_agents.py --check`; `_check_pins.py`; registry checks; `bun test tests/ts`; `cd omp && bun run test` | `SWARM_PURE_PYTHON=1 python3 -m pytest`, plus the other steps in order |
| `rust` | `cargo build --workspace --locked`; key tests; parity cells; `cargo test -p gsd-sandbox --features write-routes --test guest_imports --locked`; sandbox-write build; `cargo test --workspace --locked --no-fail-fast`; fault-injection suites; write fault suite | `cargo build --workspace --locked`, then `cargo test --workspace --locked --no-fail-fast`. The other steps have no runbook row; see `ci.yml` lines 69-163 |
| `clippy` | `cargo fmt --all --check`; `cargo clippy --workspace --all-targets --locked -- -D warnings` | the same two commands |
| `parity-rust` | `scripts/ci_parity_rust.sh parity-out` | `scripts/ci_parity_rust.sh <out dir>` |
| `browser` | `npm ci --ignore-scripts` in `web/`; `python3 scripts/_ci_browser_gate.py`. Skipped when the repository variable `BROWSER_GATE` is `skip` | `cd web && npm ci --ignore-scripts`, then `python3 scripts/_ci_browser_gate.py` |
| `supply-chain` | `scripts/ci_cargo_deny.sh deny-report.txt` | `bash scripts/ci_cargo_deny.sh [report]` |

`.github/workflows/wheel.yml` runs on `ubuntu-latest`. This is the one exception to the Omega pool (decision D-P22; `docs/operations-runbook.md`, "The wheel exception").

| Job | What it runs | Local equivalent (runbook) |
|---|---|---|
| `wheel` | `mypy` (strict, repo config); `scripts/ci_build_wheel.sh dist` (abi3-py312, manylinux_2_28, x86_64) | `scripts/ci_build_wheel.sh dist`, after `mypy` |
| `install-and-gate` | installs the wheel in `python:3.12-slim-bookworm`; `python -c "import swarm._native"`; `mypy.stubtest`; the parity tests run against the installed wheel | the commands in the runbook row for `install-and-gate` |

## 9. Known discrepancies

These are not fixed by this document. Each names the code that disagrees, or says what was checked at HEAD.

- **D1. Gate routes: the selector treats them as writes (fixed in the selector, one gap left).** Commit `08d903b` gave `qa_gate.py` (line 169), `rel_plan.py` (205), `rev_gate.py` (228) and `sec_gate.py` (251) a `WriteFlow`, so they run through `run_write`. The runbook's write-route table (rows for the four gates) already lists them as writes. The gap: qa, rev and sec do not tag an `issue_gate` failure (section 5.1). Their outcome is still `unknown` for `E-INTERNAL` and never a Python re-run, but it depends on the selector's rule, not on an explicit `write_state`.
- **D2. Stale refusal text.** `route_for_call` in `crates/gsd-adapter/src/route_table.rs` (line 144) says only the three `orch_status` routes are served. The same function serves nine route classes (section 4).
- **D3. `orch_assign`: three texts, two behaviours.** The Python selector gives it a `WriteFlow` (`scripts/orch_assign.py`, `__main__`), so the selector treats it as a write. The Rust route opens the Task Store read-only and runs one `SELECT` (`crates/gsd-adapter/src/write_route/assign.rs`, `read_task`); it has no Task Store writer and returns only the signed reply. Its module doc calls it "the host `orch_assign` verify route", which matches the runbook's "(verify)" label. The `orch_assign.py` docstring says it "never claims a task, starts a runner, or records task/gate/log state". On the rust path the selector calls `record_external_reply` (`swarm/script_base.py`, line 144), which writes the run-log record that `main` also writes for each call. The docstring's "log state" therefore needs a qualifier. No task, gate or store row is written.
- **D4. Runbook row for `swarm_tool`.** `docs/operations-runbook.md`, "Write routes" (line 455), says `GSD_ENGINE=rust` gives `E-DEP` for every `swarm_tool` call, citing `RustEngine::swarm_tool` in `strangler_seam.rs`. The citation's function does return `E-DEP` `not_started` (`strangler_seam.rs`, line 655, in `impl CoreBackend for RustEngine`), and the binary refuses first in `route_for_call` (`route_table.rs`, line 142). The row is correct only for the binary. `scripts/swarm_tool.py` has no selector and never reads `GSD_ENGINE`, so `GSD_ENGINE=rust python3 scripts/swarm_tool.py` runs Python normally. The refusal exists only for an entrypoint named `swarm_tool` sent to the binary.
- **D5. `web/jco.pin.json` is checked by nothing that runs in CI.** `web/transpile.mjs` checks it. `web/package.json` names it as the `transpile` script. No file in `.github/` or `scripts/` references it, and the `browser` job runs only `tests/acceptance/browser_headers.mjs` and `browser_sab.mjs` (`scripts/_ci_browser_gate.py`, line 4). The only recorded run is a manual one in `tests/parity/records/14.5/A11-guest-pin-reproduce.md` (line 42).
- **D6. No CI path filter for the pin rule.** `ci.yml` and `wheel.yml` trigger on `push` to `main` and on `pull_request` with no `paths:` filter. The pin rule's only written source is the D-P25 entry of `.planning/STATE.md` (section 7).
- **D7. This document's HEAD line was stale.** The previous revision named `2d4b1b4`. Eight commits since then changed the gate routes, the commit states, the identity check and the state mirror (`git log 2d4b1b4..HEAD`). They are covered above.

## 10. Open limits

These stay open, except that commit `9cd5af1` narrows L1 for children with a key-policy name and closes L3 for explicit `rust`. Each open entry has a decision or a missing step.

- **L1. The binary can consult the conventional signing-key file for a child with no key variable.** `crates/gsd-envelope/src/keys.rs`: `Environment::conventional_reference` (lines 167-174) gives `${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex`. `from_lookup` (lines 245-253) reaches it only when no key variable is set, and classifies it by `lstat` without opening it; a present file makes the loader decline (`Declined::ConventionalKeyFile`). Done: a child with one of `HOME_ISOLATING_ENV_NAMES` gets an empty HOME and XDG_CONFIG_HOME for the call (`child_home`, `swarm/engine_select.py`), so the path is absent for it. Remains: a child with none of those names keeps the caller's HOME (`HOME` is in `BINARY_ENV_NAMES`, `swarm/engine_select.py` line 60), and the identity check and core-dump guard do not run for it. `XDG_CONFIG_HOME` is in the gate grants. Decision pending with the user.
- **L2. Time-of-check to time-of-use.** `binary_sha256` reads the binary path, and `subprocess.run([binary])` then starts that path. The file can be replaced between the two. Nothing closes this window (for example by exec from a verified descriptor).
- **L3. Closed for explicit `GSD_ENGINE=rust` (commit `9cd5af1`).** An identity refusal under explicit `rust` exits 2 with `E-POLICY` (`identity_refused`, `explicit=True`), and Python does not run. A default call still hands the refused call to the Python body, and the binary is not started. Decision 2 (2026-10-30) keeps that default fallback.
- **L4. The `.claude/swarm-state.json` writes are not gated by `SWARM_GSD_STATE_FILE`.** `swarm/state_store.py` `gsd_state_path` (line 30) gates only the `STATE.md` mirror (commit `225212e`). The `.claude` file is written by `StateStore` (`swarm/state_store.py`, around lines 99-110) and by `swarm/observability.py` (around line 106). Open item.
- **L5. Sandbox default off; flips and quiet-window evidence are pending.** The earlier runbook's “after ship” quiet-window ordering is not current authority for reopened scheduler Phase 14.4: D-P47 requires S5 parity, then quiet-host S6 before ship, followed by explicit S7 approval/rollback evidence. The scheduler binding/facade remains absent; only the one-instance composition producer plan is prepared. No scheduler start or default flip is delivered by the quality/parity checkpoint.
- **L6. Guest pins and their records.** The default guest pin is `91b0970e...`. The P2 write guest is `50b06310...`. A10 and A11 records exist for both in `tests/parity/records/14.5/` (`A10-guest-pin-91b0970e.md`, `A11-guest-pin-reproduce.md`, `A10-write-guest-50b06310.md`, `A11-write-guest-50b06310.md`). The runbook records the default-pin sign-off (D-P38 item 1). Those records are not a flip approval.

## 11. Evidence checkpoint and not verified

Task `Tcheckpoint-checkpoint`, correlation `ut-mvvownjp-ff01b275`, records Main's authoritative observations from its signed assignment, not checks executed by A15. Internal detailed evidence and integration footprint are in `.planning/quality/POSTRUN.md`; `.planning/` remains excluded and uncommitted. This document is an internal review edit, not an external publication or release record.

- Parent scoped measurements: focused Python initial **115 total / 114 passed / 1 failed** (`KeyError: 'runs'`), then **112 passed / 0 failed**; affected Python **1367 passed / 0 failed**; OMP **14 isolated + 2701 main passed / 0 failed**; root Bun **13 passed / 0 failed**; workspace/pin/cargo-deny results in section 7. Skip/collected totals not separately supplied remain unknown; suite counts, passes, ignored/skipped cases and elapsed time are distinct.
- The focused count fell by three because two unauthorized NEW P4-03 blocking cases and one NEW source-only hook case were removed, preserving old black-box assertions and accepted T29/T30. Main replaced old native review-prefix expectations with single-match/full-value-plus-prefix non-disclosure checks; only redundant new Python evidence wording and native security summary/evidence wording were removed. Byte parity, fail-major checks, native-served probes and raw secret-prefix absence remain; their retention is not a passing current parity run.
- Historical FULL3 is **19632 total = 18748 passed + 884 skipped**, zero failures/errors, not a current full-pytest pass. No current full pytest result is asserted.
- Main observed three clippy errors: `Scan::add` eight arguments, `dep_audits` return complexity and release-test `err_expect`. Parent A01ResumeRecovery subsequently reports A05 `Tgatefix3-clippy`'s edit result ingested **IN_REVIEW**, not checked: borrowed `(rule, severity)` grouping at three callers, private `DependencyAuditResults`, and `expect_err` using existing `WriteStore: Debug`. Parent clippy rerun is pending, not fixed/green.
- Actual current-binary sec/rev/qa/rel served parity and the release harness remain pending. Main's empty-root release CLI actually returned **exit 1 / fail**, payload review `skipped:no-task`, and wrote an artifact without supplied signing keys; artifact creation does not prove gate approval or signer availability.
- Parent guard-export smoke allowed benign input and denied malformed AST/deep-shell input. Parent LSP results were OK for Python `results.py` and TS `swarm_bar.test.ts`; unchanged `guard.ts:10:23` TS2307 `node:path` remains a configured-project typing prerequisite, not suppressed or fixed through new packages. Neither is a complete security/typecheck sign-off.
- All 326 candidate findings/dispositions remain without a severity cutoff; T29/T30 accepted behavior is unchanged. No IPC deletion/revert, changelog or enterprise-ready claim.
- Scheduler S3–S7, real-extension callback/thread/black-box parity, S5 → quiet-host S6, explicit S7 approval/rollback, final-tree postrun/CI evidence, required sign-offs, audit, completion/cleanup previews with user confirmation and successful ship assessment remain outstanding. Preserve **INCOMPLETE** and `gaps_found`; no phase completion or ship is claimed.

The original source-reading statements above about fallback/failover, identity/core-dump handling, final/unknown settlement and missing-extension behavior were not all freshly exercised by this checkpoint. Earlier guest pin/sign-off records are historical scope evidence, not proof of reproducible current patch-build identity or a default flip approval.
