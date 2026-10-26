# Wasmer full-core spike report (Phase 8, P3)

Milestone v2.4 · branch `feat/v2.4-rust-wasm-core` · A-spike (`plan/execution-waves.md:71-83`) · Status: DECIDED-PROVISIONAL until A03 and A11 approve, and A10 signs off (section 11).

Citation keys: `R:` `plan/rust-wasmer-omp-options.md` · `E:` `plan/execution-waves.md` · `D:` `plan/decompose.md` · `M:` `plan/missing-facts.md` · `K:` `plan/risks-tradeoffs.md` · `S:` `.planning/STATE.md` · `G:` `crates/gsd-security/src/lib.rs` · `J:` `jsonwebtoken-9.3.1/src/validation.rs` (local cargo registry) · `W:` WASI primary sources (URLs inline) · `P-sizing:` `plan/spike-parts/host-import-sizing.md` · `P-policy:` `plan/spike-parts/fs-net-policy-and-wasi.md` · `P-dist:` `plan/spike-parts/distribution-and-pins.md` · `P-route:` `plan/spike-parts/route-decisions.md` · `[RUN]` measured by the orchestrator's spike runs (values exact as recorded). `[MEASURE: pending]` = not measured; not inferred.

---

## 1 Scope and verdict

**Scope.** The A-spike decides the sandbox target, the module boundary (O1 to O3), the WASI and FS/net policy inputs, and the sizing inputs for U5.1 to U5.4 (`E:71-83`; `D:97-101`). It was run for the FULL Rust core, not the pure core (`plan/inventory.md:16-18`; `D:92-95`; `R:150-152`). The spike guest is not the full core: the full-core module (U5.1) does not exist yet (`P-dist`, repo state).

**Verdict.**

1. **The pinned target `wasm32-wasip2` cannot run under Wasmer 7.5.0.** The release guest is a component, and this Wasmer cannot instantiate components [RUN]. Recommended (D-P3-1): pin the sandbox target to `wasm32-wasip1` core modules (WASI 0.1), with deny-by-default host shims for the four imports. This is **not applied** to `Cargo.toml` or `rust-toolchain.toml` in this phase; it needs A03 and A11 approval, then a pin change in its own commit.
2. **`wasm32-wasip1` runs** with deny-by-default shims. The round-trip check passes [RUN]. Warm `invoke` p50 is 0.990 µs and p95 is 7.111 µs over 200 warm round trips, with the leak caveat in section 3.
3. **`wasm32-unknown-unknown` did not build.** The core guest failed in getrandom 0.4.3, which needs the `wasm_js` feature [RUN]. Not measured further.
4. **O1: monolithic `gsd-core.<semver>.wasm`** (D-P3-2, `P-route` O1).
5. **O2: plain core JSON over `alloc`, `dealloc` and `invoke`, for every route** (D-P3-3). The evidence confirms it, because components cannot run here.
6. **O3: host-side single-owner schema validation** (D-P3-4, `P-route` O3).
7. **Q-8 (WASI version) resolves to 0.1 under D-P3-1.** WASI 0.2 and 0.3 are not runnable with this Wasmer [RUN]. A03 must record it.
8. **No value in this report is gate-ready.** The metrics carry a dealloc caveat (section 3), and most thresholds are `[MEASURE: pending]` (`P-route`, Summary).
9. **Security defects found outside the spike** (section 9) block C3. Owner: P5 / A10.

---

## 2 Versions and triples

### 2.1 Toolchain and Wasmer

| Item | Value | Source |
|---|---|---|
| rustc and cargo | 1.98.1 (48a229cea 2026-09-01) | [RUN]; pin `rust-toolchain.toml:2`, `Cargo.toml:155` (`P-dist`) |
| Host triple | `x86_64-unknown-linux-gnu` | [RUN] |
| Wasmer crate | `=7.5.0` (exact) | [RUN]; `Cargo.toml:152`, `:156` (`P-dist`) |
| Wasmer backend | default `sys-default` backend; `cranelift` compiler feature available | [RUN] |
| Compiler feature used in the measured run | `cranelift` (declared in the host `Cargo.toml`; the release build succeeded and the run used the cranelift backend) [RUN] | host `Cargo.toml` |
| Lock entry `name = "wasmer"` | [MEASURE: pending] (no entry in `Cargo.lock` today; `P-dist`) | `P-dist` |
| Target pin in repo | `wasm32-wasip2` at `Cargo.toml:157`; no `targets` entry in `rust-toolchain.toml` | `P-dist`; `P-policy` W-N1 |

### 2.2 Triples

| Triple | Artifact | Result | Source |
|---|---|---|---|
| `wasm32-wasip2` (pinned) | **Component**, 80,086 bytes, header `00 61 73 6d 0d 00 01 00` | **FAILS.** Wasmer rejects it: `Validation error: unknown binary version and encoding combination: 0xd and 0x1, note: encoded as a component but the WebAssembly component model feature is not enabled`. `wasmer-7.5.0/src` contains 0 files that mention "component". Conclusion: this Wasmer cannot instantiate components. | [RUN]; `R:154-157`; O2 (`P-route`) |
| `wasm32-wasip1` (recommended under D-P3-1) | **Core module**, 62,979 bytes, header `00 61 73 6d 01 00 00 00`. Imports exactly four WASI 0.1 functions from the std runtime: `environ_get`, `environ_sizes_get`, `fd_write`, `proc_exit`. Exports: `memory`, `alloc`, `dealloc`, `invoke`. | Without shims: `unknown import: wasi_snapshot_preview1.environ_get`. With deny-by-default shims (environ calls return ENOSYS 52; `fd_write` returns EBADF 8; `proc_exit` traps with the exit code): **the host runs the module and the round-trip check passes.** | [RUN] |
| `wasm32-unknown-unknown` | not produced | The core guest build failed in getrandom 0.4.3, which needs the `wasm_js` feature. Not measured. | [RUN] |

`module_kind` (required by `P-route` O2, `08-PLAN.md:6,16`): `component` for `wasm32-wasip2`; `core` for `wasm32-wasip1`. [RUN]

### 2.3 D-P3-1: target decision (provisional)

- **Decision (provisional):** pin the sandbox target to `wasm32-wasip1` core modules, WASI 0.1, with host-side deny-by-default shims for the four imports.
- **Justification:** the wasi-only-with-justification clause (`R:154-157`) is met by the component result in 2.2. The wasi-fallback justification that `P-dist` step 2 left as `[MEASURE: pending]` is now recorded.
- **Status:** not applied to `Cargo.toml:157` or `rust-toolchain.toml`. Needs A03 and A11 approval, then a pin change in its own commit.
- **Consequence:** W-row names in section 6 are WASI 0.2 names. Under this decision they must be restated against 0.1 (section 6.1, note W-N4).

---

## 3 Evidence: runs and metrics

### 3.1 Measured run

Wasip1 core module, 200 warm round trips, 1003-byte TaskId payload, host x86_64 [RUN].

| Metric | Value | Note |
|---|---|---|
| `compile_ms` | 205.584 | Module compile. Not the cold-start instantiate figure. |
| `instantiate_ms` | 0.055 | Instance creation. |
| `invoke_warm_p50_us` | 0.990 | Warm `invoke`, 200 round trips (meets the 50+ sample requirement, `K:275`; `M:151`). Metric name from `08-PLAN.md:6,16`; not defined in `plan/` (`P-route`, Options). |
| `invoke_warm_p95_us` | 7.111 | As above. |
| `linear_memory_pages` | 23 after 201 invocations | See the caveat in 3.2. |
| `module_bytes` | 62,979 | Spike guest, wasip1 core. |

Not measured in this run: hardware fingerprint for the p50 (CPU model, memory, kernel). [MEASURE: pending]. Fuel consumed per call [MEASURE: pending]. Peak linear memory under a real workload [MEASURE: pending].

### 3.2 Caveat (required before any gate uses these numbers)

**The spike host does not call `dealloc` on the buffers it allocates.** Part of the page growth (23 pages after 201 invocations) is therefore leaked guest buffers, not live memory. Re-measure with `dealloc` called on every buffer before any gate or threshold (O1 revert trigger, O2 revert trigger, O3 revert trigger) uses these figures (`P-route`, O1 and O2 Evidence).

### 3.3 Stub behaviour measured

The four wasip1 imports were stubbed with deny-by-default behaviour (ENOSYS 52 for environ, EBADF 8 for `fd_write`, trap with exit code for `proc_exit`) [RUN]. The run shows that stubs are effective for the four imports. How the host maps those errno values to `E-POLICY` is not measured [MEASURE: pending]. See Q-1 (section 6.3).

### 3.4 Metrics the spike did not produce

Listed in section 10 with owners. Among them: fuel-exhaustion error variant (S-02, S-03); memory-grow refusal variant (S-04); warm `invoke` with `dealloc` enabled; serde share; cold start for the monolith under the per-route budget A13 sets (`P-route` O1).

---

## 4 Compile and stub table

`cargo check --lib --target wasm32-wasip2`, per workspace member [RUN]. `gsd-ipc` is not in the table because it is excluded from the P1 workspace (`S:50`; `P-policy` b2).

| Member | Result | Blocking crate(s) | Stub status |
|---|---|---|---|
| gsd-proto | PASS | none | n/a |
| gsd-serde | PASS | none | n/a |
| gsd-failure | FAIL | tokio: only `sync`, `macros`, `io-util`, `rt` and `time` are supported on wasm | not measured |
| gsd-nexus | FAIL | tokio | not measured |
| gsd-runtime | FAIL | io-uring 0.6.4 and socket2 0.5.10 | not measured |
| gsd-governor | FAIL | socket2 | not measured |
| gsd-pool | FAIL | io-uring and tokio | not measured |
| gsd-observability | FAIL | openssl-sys build script; io-uring and socket2 through runtime | not measured |
| gsd-security | FAIL | openssl-sys; io-uring and socket2 | not measured |
| gsd-scheduler | FAIL | openssl-sys | not measured |

Reading of the table:

- Two of ten members compile for `wasm32-wasip2`: `gsd-proto` and `gsd-serde`.
- Every failure traces to a host-native dependency already named in `P-sizing` section 0 (tokio net/fs/process/signal, tokio-uring, nix, mio, socket2, memmap2). The sizing part's [INFERENCE] that these do not link into wasip2 unmodified is now confirmed [RUN].
- Stubs for the failing members are not measured. The table does not yet show whether a stub or a feature split clears each failure [MEASURE: pending].
- The wasip1 compile of each member is not measured [MEASURE: pending]. The table covers wasip2 only.

---

## 5 Host-import sizing (U5.1 to U5.4)

Source: `P-sizing`. Sized for the FULL core. Frequency classes: *per call* = one guest invoke for one thread call (`D:99`); *per thread* = once per ChatThread instance, pool max 30 (`E:154`); *per session* = once per session (`E:244-245`).

### 5.0 Bounds and rules

- **Allowlist of record:** five imports, `log`, `time`, `rand`, `storage`, `omp-callback` (`R:169-177`; `E:338-342`). The U5.4 surface is wider. Rows marked "NOT an import" or "DENIED" are not guest imports. Any new import is widening, which G-serde-budget forbids (`E:36`).
- **Mitigation order (G-serde-budget):** M1 input-file paths over inline blobs; M2 WarmPool reuse; M3 batched lane-verb reads; M4 validate-once-at-boundary; M5 serde budget as first-class metric (`E:36`; `R:241-247`).
- **Byte bounds in the plan:** the only explicit per-value cap is 100 000 bytes, max depth 32 (`R:190-192`; `omp/src/tools.ts:67`, enforced at `omp/src/tools.ts:496-498`). argv is kernel-capped at 128 KiB (`omp/src/tools.ts:66`). Where the plan gives no bound, the ceiling is 100 000 per value [INFERENCE from `R:191-192`]; the real figure is `[MEASURE: pending]`.
- **Serde level:** none / scalar / low / medium / high / highest are qualitative judgments [INFERENCE]. Quantitative serde share of wall time is `[MEASURE: pending]` against the host-Rust baseline (`E:331`).

### 5.1 Sizing table

| # | Host import | Source (U5.4 item) | Direction | Payload bound (bytes) | Frequency class | Serde-budget implication | Mitigations, in M-order | C3 status | Plan cite |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `wasi:clocks/*` pulled by the wasip2 target (not chosen by us) | clock (target default) | host to guest | scalar; import count [MEASURE: pending] | per call (link-time presence; calls [MEASURE: pending]) | none per call; unused imports still appear in the component import section [INFERENCE] | M5: record the full import list in metrics and `health` | Not allowlisted. Strip, or record justification as a WASI wish-list item | `E:76`; `E:36`; `R:154-155` |
| 2 | `time` (monotonic clock + deadline) | clock | host to guest | scalar, no byte bound needed | per call; reads per invoke [MEASURE: pending] | scalar, no marshal; serde none. Deadline stays host-authoritative, so guest reads cannot decide E-TIMEOUT | M5 (count reads per invoke) | Allowlisted | `R:170-171`; `R:206-208`; `E:338-339` |
| 3 | `sync` primitives (mutex, semaphore, channel) | sync | none (guest-internal) | 0 | per thread (guest instance) | none; no crossing. Semaphore acquire-or-E-CAPACITY stays host-side | none needed | NOT an import. Adding one is widening (forbidden) | `D:100`; `K:96`; `R:87` (acquire must fail fast) |
| 4 | Filesystem access (gsd-runtime `io`/tokio fs) as preopen | io | host to guest (script and input reads); guest to host (writes to results) | per file [MEASURE: pending]; per value cap 100 000 | per call | serde is the path string only when M1 applies; inline content adds a full marshal | M1 first (input-file path); M4 | Not an import as such. Only `<SWARM_DIR>/results` write plus needed script-read inputs. WASI FS needs recorded justification | `R:162-166`; `E:341-342`; `R:155` |
| 5 | Net, process spawn, signal (gsd-runtime tokio net/process/signal) | io | none | 0 | 0 | none | none; denied | DENIED. No sockets, no spawn, no signals | `R:166`; `R:178-179`; `E:344-345`; `crates/gsd-runtime/Cargo.toml:12` |
| 6 | UDS-gRPC control plane (gsd-ipc) | UDS-gRPC | host-only; guest-facing equivalent is `storage` or `omp-callback` | request [MEASURE: pending]; per value cap 100 000 | per call (control messages) | two marshal layers if exposed (gRPC/prost plus serde_json at boundary) [INFERENCE]: high | M1 (pass a reference id, not the message); M3 (batch); M4 | Host-only. A guest-visible gRPC import is widening (forbidden) | `crates/gsd-ipc/Cargo.toml:5`, `:14`; `R:190-195`; `E:36` |
| 7 | memfd shared memory (gsd-ipc, Nexus CRDT state) | memfd | host to guest if mapped (rejected); copy-only if carried | total state [MEASURE: pending]; per value cap 100 000 | per session | mapping has zero serde but is the forbidden shared-mutable-memory bypass. Copy path: full marshal per crossing | M1 (state handle id, never the blob) | DENIED as shared memory. Copy-only through `storage` at most | `crates/gsd-ipc/Cargo.toml:5`; `E:36`; `R:248-249` |
| 8 | `storage` read (TaskStore / state, capability-tokened) | storage | guest to host request; host to guest response | per value 100 000 (`R:191-192`); total per call [MEASURE: pending] | per call | marshal plus unmarshal per read; per-item reads dominate: high | M3 (batch lane-verb reads, critical); M1 where file-backed; M4 | Allowlisted | `R:173-174`; `E:340` |
| 9 | `storage` write (only mutating import) | storage | guest to host | per value 100 000; total per call [MEASURE: pending] | per call | one marshal per write. Mutating calls never auto-retry, so retries do not multiply serde: medium | M4 (validate once at boundary); M5 | Allowlisted | `R:173-174`; `R:201-204`; `E:340` |
| 10 | `log` (bounded batch, fixed schema) | log | guest to host | batch byte cap [MEASURE: pending]; per value 100 000 | per call; events per invoke [MEASURE: pending] | per-event crossings would dominate: high if unbatched | M3 (one batched crossing per invoke, not per event); M4 (schema checked natively first) | Allowlisted | `R:169`; `K:103` |
| 11 | `rand` (host-seeded bytes) | rand | host to guest | seed or request bytes [MEASURE: pending] | per thread (seed once per instance); per call only if requested per call | seed once per thread removes per-call crossings: low | M2 (reseed on WarmPool reassignment; determinism per thread [INFERENCE]); M3 | Allowlisted | `R:172`; `K:97` |
| 12 | `omp-callback` (structured JSON into TS bridge) | model/tool I/O (control callbacks) | bidirectional; guest to host to TS | per value 100 000; depth 32 (`R:190-192`); total per call [MEASURE: pending] | per call (mid-call round trips) | two hops (serde_json and JSON.parse), each marshal plus unmarshal: highest. Most likely row to fail G-serde-budget | M1 (path refs); M3 (batch round trips); M4 (validate both ways once); M5 | Allowlisted. Guest cannot spawn or reach network through it | `R:175-177`; `R:190-195`; `E:36` |
| 13 | Model I/O (LLM request and response) | model I/O | guest to host request; host to guest response | request ≤ 100 000 per value; response [MEASURE: pending] (no plan bound) | per call | low relative to model latency [INFERENCE]; ratio stays under gate only if payload stays under cap: medium | M1 (prompt as input file, pass path); M4; M5 | No dedicated import. Network leg is host-only, so carry the request as an `omp-callback` operation [INFERENCE]; needs decision (D4) | `R:175-177`; `E:338-342` |
| 14 | Tool I/O (`swarm_tool` call) | tool I/O | guest to host to TS bridge to swarm_tool; result back | input ≤ 100 000 bytes, measured as UTF-8 of JSON.stringify (`omp/src/tools.ts:496-498`); result [MEASURE: pending] | per call | largest single-value class; TS already rejects oversize before spawn, so the Rust boundary must reproduce the check (M4): high | M1 (input file path on the Wasmer path only); M4; M5 | Carried via `omp-callback`; no new import | `omp/src/tools.ts:67`, `:496-500`; `R:223` |
| 15 | `invoke` entry (export, not an import; context) | per-call crossing | host to guest (argv and input files); guest to host (exit code and JSON) | argv ≤ 128 KiB (`omp/src/tools.ts:66`); per value 100 000 | per call | marshal every call unless warm: high | M1 first; M2 (WarmPool reuse, critical) | Not an import (export). Shape confirmed by run (section 3) | `R:197-199`; `E:166` |
| 16 | `init` / instance creation (cold start; context) | runtime creation | host internal | module size: 62,979 bytes (wasip1 spike guest, [RUN]); full core [MEASURE: pending] | per session (runtime once); per thread instance via WarmPool | cold-start: instantiate 0.055 ms and compile 205.584 ms on the spike guest [RUN]; full-core figure [MEASURE: pending]. High if warm reuse is missing | M2 (reuse before any other mitigation); M5 | Not an import | `R:196-197`; `R:232-235`; `E:166` |

### 5.2 Reconciling U5.4 with the five-import allowlist

- `clock` maps to `time` (row 2). `io` maps to preopen-only FS (row 4) and to `storage` or `omp-callback` (rows 8, 9, 12). `sync` (row 3) needs no import.
- Four U5.4 items have no allowlisted import: sync, net/process, UDS-gRPC, memfd (rows 3, 5, 6, 7). The sizing recommends none be added. G-serde-budget forbids import widening (`E:36`), and `E:344-345` names the non-imports explicitly.
- Model and tool I/O (rows 13, 14) are carried by `omp-callback` (`R:175`). No import is added for them.
- Row 1 is a separate risk. Evidence update [RUN]: the wasip2 target is a component that this Wasmer cannot instantiate, so its import list is [MEASURE: pending]. The wasip1 core module, the recommended target under D-P3-1, imports exactly four WASI 0.1 functions: `environ_get`, `environ_sizes_get`, `fd_write`, `proc_exit`. None is in the allowlist. Under the recommended target they are shimmed to deny (section 3.3), so none is a capability. [INFERENCE] Their mapping to the policy rows: environ → W-10 and W-11; `fd_write` → W-07 and W-08; `proc_exit` → W-12.

### 5.3 Per-session budget formula

For each import i: `bytes_per_session(i) = calls_per_session(i) × mean_payload(i)`, with the mean over all calls. `calls_per_session(i) ≤ 30 × calls_per_thread(i)` [INFERENCE from `E:154`, `E:244-245`]. Both factors are `[MEASURE: pending]`. The serde gate fails when serde dominates wall time even at parity (`E:36`); the dominating share is `[MEASURE: pending]`.

### 5.4 Decisions needed before U5.4 lands (provisional)

- **D1.** No `sync` import. Confirm guest-internal sync is sufficient for wasip2 (row 3). Owner: A03. Under D-P3-1 the same applies to wasip1.
- **D2.** UDS-gRPC and memfd stay host-only. Guest-facing state moves through `storage` by reference (rows 6, 7). Owner: A03 with A10.
- **D3.** WASI import list is enforced at link time. Any import outside the allowlist fails the build (row 1). Owner: A11 with A03.
- **D4.** Model I/O is an `omp-callback` operation with a named schema, not a new import (row 13). Owner: A03.
- **D5.** argv-carried tool input (`omp/src/tools.ts:500`) is frozen for the Python path. The Wasmer path may use input-file paths (row 14), but any change to spawned-command bytes needs an omp-mirror review (`D:109`). Owner: A03 with A06.
- **D6.** Open n4 items O1 and O2 change how many imports exist and how they are typed (`R:254-257`; `E:81-82`). Rows here assume the allowlisted five. They must be re-checked after O1 and O2 are decided. Section 8 now decides both (provisional).

---

## 6 WASI wish-list and FS/net policy draft

Source: `P-policy`. Under D-P3-1 the target is WASI 0.1. The rows keep the 0.2 names as written, and note W-N4 covers the restatement.

### 6.0 Governing rules

- **D0 Default deny.** A capability is available only if its row in 6.1 says ALLOW. `R:164-166`, `E:340-342`.
- **D1 Host imports are the only guest-to-world channel.** The sanctioned set is five imports: `log`, `time`, `rand`, `storage`, `omp-callback`. `R:167-177`.
- **D2 No guest-chosen paths or endpoints.** The host resolves every path and endpoint per invocation. `R:45-48`, `R:181-182`.
- **D3 Denial is fail-closed.** Denial returns `E-POLICY` with a scrubbed message. `R:89`, `R:69-70`.
- **D4 No import widening, no shared-memory bypass, no TS native ABI.** `E:36`, `R:248-250`.
- **D5 Python is truth.** Any divergence is a Rust bug and the flag reverts. `R:118-119`, `E:37`.

### 6.1 (a) WASI and host capability wish-list

| ID | Capability (WASI 0.2 name or host import) | Decision | Reason | Citation |
|---|---|---|---|---|
| W-01 | `wasi:clocks/wall-clock` | DENY | Guest never reads wall time; timeouts stay host-authoritative. | `R:170-171`; `E:338-339`; `K:150-152` |
| W-02 | `wasi:clocks/monotonic-clock` (direct) | DENY | Same reason. Monotonic time is reached only through host import W-20. | `R:170-171`; `E:338-339` |
| W-03 | `wasi:clocks/timezone` (feature-gated in later 0.2.x patches) | DENY | Clock class, not in the five-import set. | `R:167-177`; `W: https://wasi.dev/releases/wasi-p2` |
| W-04 | `wasi:random/random` | DENY | Replay must come from the host-seeded `rand`, not ambient entropy. | `R:172`; `E:339-340`; `K:150` |
| W-05 | `wasi:random/insecure`, `insecure-seed` | DENY | Same reason; `insecure-seed` exposes seed material. [INFERENCE on the seed exposure.] | `R:172`; `W: https://wasi.dev/releases/wasi-p2` |
| W-06 | `wasi:cli/stdin` (+ `terminal-stdin`) | DENY | Bridge spawns with `stdin=ignore`. | `R:42-44` |
| W-07 | `wasi:cli/stdout` (+ `terminal-stdout`) | DENY by default. ALLOW only if O2 selects a command-model JSON result, capped at 100 000 bytes, with `E-CONTRACT` on overflow. | Plain JSON `invoke` needs no stdout. | `R:49-53`; `R:190-195`; `R:256-257` |
| W-08 | `wasi:cli/stderr` (+ `terminal-stderr`) | DENY | One diagnostics channel only: the bounded `log` import (W-22). | `R:169` |
| W-09 | `wasi:cli/terminal-input`, `terminal-output` | DENY | No TTY in the bridge contract. [INFERENCE: no PTY is stated in `R:42-44`.] | `R:42-44` |
| W-10 | `wasi:cli/environment` `get-environment` | DENY all. The `SWARM_DIR`-equivalent is delivered as a host-set input value. | Env is stripped; no passthrough except the `SWARM_DIR`-equivalent. | `R:43-44`; `R:178-179` |
| W-11 | `wasi:cli/environment` `get-arguments` | DENY. argv arrives as an `invoke` input (frozen vector). | argv is a frozen input, not an ambient read. | `R:42`; `R:100-105` |
| W-12 | `wasi:cli/exit` | DENY as an import. Status is returned through the export. A trap maps to `E-INTERNAL`. | Guest results go through the frozen union. | `R:88-90`; `W: https://wasi.dev/releases/wasi-p3` (0.3 changes exit) |
| W-13 | `wasi:cli/run` (command world) | DENY unless O2 selects the command model. | Plain `invoke` export is the default boundary. | `R:256-257`; `W: https://wasi.dev/releases/wasi-p2` |
| W-14 | `wasi:filesystem/preopens`, `wasi:filesystem/types` | DENY. No preopen by default. Exceptions only as E-01 to E-05 in 6.2. | No ambient FS. | `R:164-166`; `E:21`; `E:340-342` |
| W-15 | `wasi:sockets/tcp`, `tcp-create-socket` | DENY | Net default is none. | `R:165-166` |
| W-16 | `wasi:sockets/udp`, `udp-create-socket`, `ip-name-lookup`, `network`, `instance-network` | DENY | Net default is none. DNS is a network capability. | `R:165-166` |
| W-17 | `wasi:http/outgoing-handler`, `incoming-handler`, `types` | DENY | Not in the five-import set; net default is none. | `R:166`; `R:169-177`; `W: https://wasi.dev/releases/wasi-p2` |
| W-18 | `wasi:io/poll`, `streams`, `error` | DENY as a capability. Permitted only as a transitive dependency of an allowed import, via a denying stub. | Std build may link these without I/O use. [MEASURE: pending: link set of the std build with no I/O.] | `R:154-157`; `M:114-115`; `W: https://wasi.dev/roadmap` (0.3 removes `wasi:io`) |
| W-19 | Process spawn (no WASI 0.2 package; host `Bun.spawn` or `python3`) | DENY | Explicit non-imports. `omp-callback` cannot spawn. `runPy` stays the only spawner. | `R:178-179`; `R:176-177`; `R:186-187` |
| W-20 | Host import `time` (monotonic, host-owned deadlines) | ALLOW | Only time source for the guest; replay uses recorded deadlines. | `R:170-171`; `E:338-339`; `K:150-152` |
| W-21 | Host import `rand` (host-seeded bytes) | ALLOW | Deterministic replay under a fixed seed. | `R:172`; `E:339-340` |
| W-22 | Host import `log` (bounded batch, fixed schema) | ALLOW | Only diagnostics channel (replaces W-08). Overflow behaviour is open: Q-5. | `R:169`; `E:338` |
| W-23 | Host import `storage` (session-namespaced, tokened) | ALLOW | The only mutating import. Read path also serves input files (E-01 alternative). | `R:173-174`; `E:339-340` |
| W-24 | Host import `omp-callback` (schema-validated) | ALLOW | Structured callback into the TS bridge; cannot spawn or touch net. | `R:175-177`; `R:190-195` |
| W-25 | Host extra from `decompose` U5.4: UDS-gRPC/memfd IPC via `gsd-ipc` | DENY pending A10 (E-06) | Adds a socket-class capability beyond the five imports. `gsd-ipc` is excluded from the P1 workspace. | `D:100`; `R:167-177`; `S:50` |
| W-26 | Host extra from `decompose` U5.4: model/tool I/O | DENY. Not approvable by A10 alone (E-09). | Widens the five-import set, which G-serde-budget forbids. | `D:100`; `E:36`; `R:248-250` |
| W-27 | Shared linear memory across instances or wasm threads | DENY | Forbidden as a bypass of isolation. | `R:248-250`; `E:36` |

Notes:

- **W-N1 WASI version.** The pin is `wasm32-wasip2` (`Cargo.toml:157`; `R:154-157`), which means WASI 0.2 names. `wasi.dev` lists 0.2 as superseded by 0.3, released 2026-06-11, which removes `wasi:io` and changes `wasi:cli/exit` (`W: https://wasi.dev/releases`, `https://wasi.dev/roadmap`, `https://wasi.dev/releases/wasi-p3`). Most toolchains still target 0.2 (`W: https://wasi.dev/releases`). A03 MUST record the version before C1 (`E:310-318`). Rows W-12 and W-18 change names under 0.3. This is Q-8, resolved to 0.1 under D-P3-1 (section 2.3) and pending A03.
- **W-N2 O2 dependence.** W-07, W-11 and W-13 depend on O2 (`R:256-257`). Under the plain JSON `invoke` decision (section 8), all three stay DENY.
- **W-N3 Deny mechanism.** A denied WASI import is either absent at link (instantiation error, mapped to `E-DEP` by `R:86`) or stubbed to return a denial (use, mapped to `E-POLICY` by `R:89`). This is Q-1. The run used stubs (section 3.3).
- **W-N4 Restatement under D-P5-13 (supersedes D-P3-1).** The decisions above do not change. The sandbox target is `wasm32-unknown-unknown`, wrapped as a WebAssembly component and run by wasmtime 50.0.0 with the `component-model` feature (D-P5-1, D-P5-13; `Cargo.toml:158`). The guest has no WASI import surface, so no WASI 0.1 or 0.2 name is imported. Deny-by-default holds: an import the host does not provide is refused at instantiation with E-DEP (D-P5-14), and the empty linker is the allowlist (D-P5-17, D-P5-20). The four WASI 0.1 imports of section 2.2 do not apply to the decided target.

### 6.2 (b) FS and net deny-by-default policy draft

#### b1. Preopened paths

| Path | Guest access | Mechanism | Decision | Citation |
|---|---|---|---|---|
| `<SWARM_DIR>` root (TaskStore and state) | none | State goes through `storage` (W-23) | DENY preopen. E-03 only if `storage` cannot serve reads. | `R:173-174`; `M:169-173` |
| `<SWARM_DIR>/results/` | none (read or write) | Bridge writes input files and never overwrites (`R:46-48`). Guest reads by logical id via `storage.read`. | DENY preopen. E-01 (read) or E-02 (write) only if measured as needed. | `R:45-48`; `R:241-243` |
| Repo tree, cwd, git toplevel | none | `SWARM_DIR` is resolved host-side. cwd is not exposed. | DENY | `R:42`; `R:45` |
| `scripts/`, `omp/` | none | Python spawn stays host-side. | DENY. E-04 only after Q-6 rules on "script-read inputs". | `R:27-33`; `R:165`; `R:178-179` |
| Any other host path (home, `/tmp`, `/etc`) | none | none | DENY. No exception proposed. | `R:164-166` |

Preopen rules, applying only if an exception is approved. These are proposals, not plan text.

1. The host canonicalises each path with `realpath` and rejects any path that escapes its mount root.
2. Mounts are computed per invocation and never cached across sessions (`R:181-182`).
3. Mounts are read-only unless E-02 is approved.
4. Guest aliases are fixed by the host (for example `/swarm/results`). The guest never chooses them.
5. Input-file ids use the bridge sanitiser (`R:46-47`). Mismatch returns `E-INPUT` (`R:84`).
6. Every mount is written to the audit trail (`G:16`, `audit_log_path`). [INFERENCE: the audit writer is not shown in the reviewed `lib.rs` lines.]

#### b2. Net

- **Default: none.** No TCP, UDP, DNS or HTTP (W-15 to W-17; `R:165-166`).
- **Only outbound route:** `omp-callback` to the in-process TS bridge (`R:175-177`). It is IPC, not net, and the guest cannot choose its endpoint.
- **Only socket-class candidate:** UDS IPC via `gsd-ipc` (W-25; `D:100`). `gsd-ipc` is not in the P1 workspace (`S:50`), so it cannot be sized until it is re-added.

#### b3. Gating each host import with a `gsd-security` capability token

Facts from the code:

- Claims carry `sub`, `iss`, `aud`, `exp`, `iat`, `scope`, `resources` (`G:55-63`).
- `issue` sets `iss` and `aud` to `"gsd-core"` and `exp` to `now + hours × 3600` (`G:73-77`).
- `validate` calls `Validation::default()`, then decodes and returns the claims (`G:92-99`).
- The manager's `validate` adds only a revocation check (`G:128-134`).

Defects in the existing token code. These are not in the plan. They MUST be fixed before C3 (`E:346-347`).

- **T-01 Audience rejects every issued token.** `Validation::default()` sets `validate_aud = true` and `aud = None` (`J:125-129`). When the token carries an `aud` claim and the expected set is `None`, jsonwebtoken returns `InvalidAudience` (`J:306-321`). `issue` always writes `aud: "gsd-core"` (`G:76`), so `validate` rejects every token this module mints. `lib.rs` has no `#[test]` that exercises this path. Proposed fix: set expected `aud` and `iss` explicitly and require the claims (`J:24-28`). [MEASURE: pending: C3 test on the jsonwebtoken version that resolves; see the Cargo.lock conflict in section 10.]
- **T-02 `validate` does not check scope, resources or subject.** Those checks belong to the import gate below (`G:92-99`, `G:128-134`).
- **T-03 Hard-coded default secret.** `jwt_secret` defaults to `"gsd-core-secret-change-in-production"` (`G:25`) with `enable_sandbox: true` (`G:24`). HS256 with a shared secret (`G:83-86`) means any holder of the secret can mint tokens. Proposed: refuse init when the default value is in use (S-23).
- **T-04 Expiry is whole hours.** `capability_token_expiry_hours` is `u64` hours (`G:18`, `G:77`), default 1 (`G:29`). That is longer than the 600 s and 1860 s call deadlines (`R:206-208`). Proposed: per-call `exp` ≤ call deadline. This needs a seconds-granular field (E-15, Q-7).

Gate algorithm (proposed). It runs on each of the five host imports and on each approved exception.

1. The token is bound host-side to the invocation handle. The guest holds no bearer token. This tightens `R:180-182` (Q-7).
2. Verify signature and `exp` through `CapabilityToken::validate`, after the T-01 fix (`G:92-99`).
3. Assert `iss == "gsd-core"` and `aud == "gsd-core"` explicitly (T-01).
4. Assert the token is not revoked (`G:131-133`).
5. Assert `sub` equals the thread id bound to this invocation (`G:56`; session id per `R:180-181`).
6. Assert the import's scope string is in `scope` (`G:61`).
7. Assert the requested resource is in `resources` (`G:62`).
8. On any failure, return `E-POLICY` with the scrubbed message (`R:89`, `R:69-70`) and record the denial.

| Import | Scope string (proposed) | Resource binding | Gate notes | Citation |
|---|---|---|---|---|
| `log` | `log.emit` | session id | Bounded batch, fixed schema | `R:169` |
| `time` | `time.read` | invocation id | Monotonic, host-owned deadline | `R:170-171` |
| `rand` | `rand.read` | invocation seed | Host-seeded | `R:172` |
| `storage` | `storage.read`, `storage.write` | session namespace | Only mutating import | `R:173-174` |
| `omp-callback` | `omp-callback.call` | callback name | Schema-validated within serde limits | `R:175-177`; `R:190-195` |

#### b4. Exceptions that need A10 approval (one per line)

- E-01 Read-only preopen of `<SWARM_DIR>/results`, only if the A-spike shows `storage.read` cannot serve input files (`R:173-174`, `R:241-243`).
- E-02 Any write preopen, including `<SWARM_DIR>/results` (`R:164-165` names a write mount; `R:46-48` makes the bridge the writer).
- E-03 Read-only preopen of the state directory, only if `storage` cannot serve TaskStore reads (`R:173-174`, `M:169-173`).
- E-04 Read preopen of `scripts/` or `omp/`. `R:165` says "script-read inputs" and must be ruled on (Q-6).
- E-05 Any preopen outside `<SWARM_DIR>` (repo root, git toplevel, home, `/tmp`). None proposed.
- E-06 UDS IPC through `gsd-ipc` (`D:100`). Socket path under `<SWARM_DIR>`, host-created, UDS only, no TCP. Requires `gsd-ipc` re-added (`S:50`).
- E-07 TCP, UDP, DNS or `wasi:sockets/*` (W-15, W-16). None proposed (`R:166`).
- E-08 `wasi:http` inbound or outbound (W-17). None proposed.
- E-09 Model or tool I/O import (W-26; `D:100`). Not approvable by A10 alone. It widens the five-import set (`R:167-177`), which G-serde-budget forbids (`E:36`). Escalate to A03 and the planner.
- E-10 WASI wall clock or monotonic clock as a direct import (W-01, W-02). None proposed (`R:170-171`).
- E-11 WASI random as a direct import (W-04, W-05). None proposed (`R:172`).
- E-12 stdin read (W-06). None proposed (`R:42-44`).
- E-13 stdout or stderr, only if O2 selects a command-model result (W-07, W-08; `R:256-257`). Not selected (section 8).
- E-14 Environment passthrough beyond the `SWARM_DIR`-equivalent (W-10; `R:178-179`).
- E-15 Per-call token lifetime longer than the call deadline, or any token cache across sessions (T-04; `R:181-182`).
- E-16 Accepting the default JWT secret (T-03; `G:25`). None proposed.

### 6.3 (c) Fault-injection scope for A10 sign-off

`E:78-79` and `K:295-297` ask A10 to sign off this scope. Every fault below is for the spike harness. **None has been executed** (the faults are a sign-off list, not run results).

#### c1. Faults in scope (A10 sign-off list)

| ID | Fault | Injection | Expected typed outcome | Citation |
|---|---|---|---|---|
| S-01 | Guest trap | Export hits `unreachable` | `E-INTERNAL`. No partial result. Instance dropped. | `R:90`; `R:144-146` |
| S-02 | Fuel exhaustion, no deadline mapping | Fuel set below work | `E-INTERNAL` (Q-3) | `R:163-164`; `E:325-326` |
| S-03 | Fuel exhaustion, deadline mapping present | Same, with a deadline | `E-TIMEOUT` (requeue once per standard handling) (Q-3) | `R:85`; `R:163-164`; `E:325-326` |
| S-04 | Memory growth refused at the linear-memory cap | Guest requests grow past cap | `E-CAPACITY`, not a trap. [MEASURE: pending: whether Wasmer 7.5.0 reports a grow refusal as a grow failure or a trap.] | `R:162`; `R:87`; `E:323-325` |
| S-05 | Invalid module bytes (non-wasm, truncated) | Corrupt `.wasm` | Init refuses. `E-DEP`. | `R:86`; `E:316` |
| S-06 | Hash or version mismatch on `.wasm` | Flip one byte; wrong semver | Init refuses. `E-DEP`. | `E:316`; `R:158-159`; `R:196-197` |
| S-07 | Serde overflow, input over 100 000 bytes | 100 001-byte input | `E-INPUT` on input. Never truncated. | `R:190-195`; `R:84` |
| S-08 | Serde depth over 32 | Nesting depth 33 | `E-INPUT` on input, `E-CONTRACT` on output | `R:190-195` |
| S-09 | Unknown field in error or verdict shape | Extra key in output | `E-CONTRACT` | `R:190-195` |
| S-10 | Unknown `error.code` from guest | Code outside the seven | `E-CONTRACT` (output side) | `R:190-195`; `R:68` |
| S-11 | Host import called with no token | Token omitted | `E-POLICY`, scrubbed | `R:89`; `R:180-181`; `E:346-347` |
| S-12 | Host import called with wrong scope | Token lacks `storage.write`; call writes | `E-POLICY` | `G:61`; b3 step 6 |
| S-13 | Token expires mid-call | `exp` passes during the call | Next import returns `E-POLICY` | `G:59`; `R:180-181` |
| S-14 | Token revoked | Add to revoked set | `E-POLICY` | `G:131-133` |
| S-15 | Token signed with wrong key | Sign with another secret | `E-POLICY` | `G:92-99` |
| S-16 | Token with wrong `iss` or `aud` (T-01 regression) | Mint with `aud = "other"` | `E-POLICY`. Also proves T-01 is fixed. | `G:76`; `J:306-321` |
| S-17 | Denied WASI import used (fs, sockets, clocks, random, stdin, process) | Call each denied import | `E-POLICY` if stubbed, `E-DEP` if absent at link (Q-1) | `R:164-166`; `R:178-179`; `R:86`; `R:89` |
| S-18 | Host import backend unavailable (storage down, omp-callback fails) | Kill backend | `E-DEP`, with retry per class | `R:86`; `R:177`; `K:177`; `R:201-203` |
| S-19 | Host deadline exceeded mid-call | Deadline shorter than work | `E-TIMEOUT`. Instance dropped. No result recorded. | `R:206-208`; `R:229` |
| S-20 | Cancel mid-call | `cancel/<corr>` at random point | Instance dropped. No partial success. Stop within the bound named in Q-4. | `R:140-146`; `D:67`; `S:48` |
| S-21 | Sandbox fault mid-join | Trap in one join participant | Typed failover host-Rust then Python. Crashed participant marked. Idempotency key kept. Never partial-as-success. | `E:355-361`; `K:139-147`; `K:178` |
| S-22 | One worker panic in a multi-task session | Panic in one task | One task `FAILED` `E-INTERNAL`. Siblings `DONE`. Cannot pass until the defect in section 9 (F-07) is cleared. | `D:49`; `E:196-197`; `K:174`; `S:55` |
| S-23 | Default JWT secret present at init | Default `SecurityConfig` | Init refuses (T-03). Proposed; not in plan. | `G:21-31`; `G:25` |
| S-24 | `log` flood beyond the bounded batch | 10 000 events in one call | Outcome open: Q-5 | `R:169` |

Note on S-02 and S-03: `D:99` says fuel exhaustion maps to `E-CAPACITY`. That conflicts with `R:163-164` and `E:325-326`. This report follows `R:163-164`. Q-3, finding F-03.

#### c2. Out of scope (one reason each)

- X-01 Compiler miscompilation in Wasmer or Cranelift. Not injectable. Detected by byte-compare against Python truth (`R:215-231`, `E:37`, `R:118-119`).
- X-02 Host kill, host OOM, host reboot. Outside the sandbox. A01 requeue and checkpoint own it (`R:75`). Replay past cancel or timeout is undefined (`K:152-154`, `M:212-213`).
- X-03 TaskStore corruption or retention faults. State semantics are unanswered (`M:169-173`, `M:212-213`). Unavailability stays in scope (S-18).
- X-04 Network partition or remote latency. No net by default (`R:165-166`). Reopens only if E-06 or E-07 is approved.
- X-05 Model or LLM provider outage or rate limit. Not a sandbox fault. AIMD and backoff are scheduler behaviour (`E:190-197`, `D:49`). The model import is not in the set (E-09).
- X-06 Side-channel or timing attacks. Not in the plan's threat model. Route to A10's threat-model task, not to fault injection.
- X-07 Supply-chain tampering beyond hash mismatch (signed provenance, SLSA). Distribution is unresolved (`M:206-207`). S-06 covers hash mismatch. Provenance belongs to A18.
- X-08 Well-formed but semantically wrong guest output. Caught by parity and byte-identical signatures, not by faults (`R:222`, `K:134-138`).
- X-09 Disk full or write failure under `<SWARM_DIR>/results`. The bridge writes inputs, not the guest (`R:46-48`). Covered by A3 vectors (`E:115-127`), not the sandbox.
- X-10 Shared-memory and wasm-thread faults. Shared memory is forbidden, so the fault class does not exist (`R:248-250`, `E:36`).
- X-11 31st-thread admission and session-cap violations. Host-side (`E:157-158`, `E:196-197`).
- X-12 Guest wall-clock skew. The guest has no wall clock (`R:170-171`). Host clock skew is out.
- X-13 Signing-key custody and HSM faults. Key location is undecided and blocked on A10 input (`K:179`, `K:295-297`).

#### c3. Questions A10 must answer

- **Q-1** Denied WASI import: absent at link (`E-DEP`, per `R:86`) or stubbed (`E-POLICY`, per `R:89`)? Proposed: stub, so that denial is recorded as a policy event rather than host unavailability. **Status:** the run used stubs for the four wasip1 imports, with errno returns and a trap (section 3.3). The `E-POLICY` mapping of those returns is [MEASURE: pending]. A10 to confirm.
- **Q-2** Stdio channel (W-07, W-08): stdio is DENY if O2 is a plain JSON `invoke` (`R:256-257`). **Status:** O2 is plain JSON (section 8). A10 to confirm.
- **Q-3** Fuel mapping: `R:163-164` and `E:325-326` govern. `D:99` (fuel to `E-CAPACITY`) is stale. Confirm and correct `D:99`. Finding F-03.
- **Q-4** Cancel bound for S-20: three numbers are in the plan. `S:48` gives 500 ms with 50 ms slices. `D:67` gives 10 s cooperative within 15 s. `R:146` gives a 3 s force-drop. S-20 must assert one. Name it. Finding F-04.
- **Q-5** `log` overflow (S-24, W-22): drop with a count, or `E-CONTRACT`? The plan does not say (`R:169`).
- **Q-6** Meaning of "script-read inputs" in `R:165`. Does any guest path need `scripts/` or `omp/`? Proposed: no (E-04).
- **Q-7** Token holding (b3 step 1): bearer token in guest, or host-bound to the invocation? Proposed: host-bound. Also approve T-01 through T-04 before C3 (`E:346-347`).
- **Q-8** WASI 0.2 or 0.3 for the target (W-N1). **Resolved to WASI 0.1 under D-P3-1** (section 2.3). WASI 0.2 and 0.3 are not runnable with this Wasmer [RUN]. A03's decision, pending approval; every W-row name depends on it (W-N4).

---

## 7 Distribution and pins

Source: `P-dist`. Sources of the plan text: `R:150-161`, `R:196-200`; `D:97-101`; `E:49-83`; `M:107-131`.

### 7.1 Repo state at drafting (read only, no build run)

- `rust-toolchain.toml:2` pins `channel = "1.98.1"`. Lines 1 to 4 list components only (`rustfmt`, `clippy`); no `targets` entry, so the `wasm32-wasip2` std target is not declared there. [INFERENCE: target install is not automated by the toolchain file.]
- `Cargo.toml:151-152` pins `wasmer = "=7.5.0"` (exact). `Cargo.toml:154-157` records `[workspace.metadata.gsd-pins]`: `rustc = "1.98.1"`, `wasmer = "7.5.0"`, `wasm_target = "wasm32-wasip2"`. D-P3-1 proposes changing the last value; not applied.
- `Cargo.toml:64` declares `sha2 = "0.10"`. `Cargo.lock` carries two sha2 entries, 0.10.9 (`Cargo.lock:3212-3216`) and 0.11.1 (`:3223-3227`). The workspace requirement resolves to 0.10.9.
- `Cargo.lock` has no `name = "wasmer"` package entry. `crates/gsd-security/Cargo.toml` has no wasmer dependency. [INFERENCE: Cargo omits unused workspace dependencies from the lock, so the pin materialises when U5.1/U5.2 add the dependency.]
- `crates/gsd-security/src/` contains only `lib.rs`. The U5.1 and U5.2 owned files (`wasmer_module_build.rs`, `wasmer_module_load.rs`, `D:97-98`) do not exist yet.
- `.github/workflows/ci.yml:51-53` runs `cargo build --workspace --locked` and `cargo test --workspace --locked`. There is no wasm target install, no module build, and no digest check step.
- `plan/missing-facts.md:107-131` and `:192-195` describe the pre-A0 state. The pins in `Cargo.toml` and `rust-toolchain.toml` supersede those statements. Treat `missing-facts` §5 as the historical gap record.

### 7.2 Distribution pipeline

Stages run in order: build (U5.1), hash and publish (U5.1), verify before load (U5.2), fail closed (U5.5). Status after this spike is marked on each step.

1. **Pin the toolchain and Wasmer in one manifest.** Owner: A0 (A11 devops + A03 architect), consumed by U5.1. Citation: `R:154-157`; `E:60-62`, `:67-68`. Status: done in repo (`rust-toolchain.toml:2`, `Cargo.toml:152`, `:155-157`). Acceptance: `grep -rn "wasmer\|wasm32" Cargo.toml rust-toolchain.toml` shows the pin (`E:66`).
2. **Select the target.** `wasi` is allowed only with a recorded justification. Owner: U5.1 (`crates/gsd-security/src/wasmer_module_build.rs`). Citation: `R:154-155`; `D:97`. Status: **justification recorded** (section 2.3). The pinned target is `wasm32-wasip2` (`Cargo.toml:157`), not runnable; the recommended target is `wasm32-wasip1` under D-P3-1, not applied. O2 decided per route (section 8). Acceptance: the spike report names the triple and the O2 decision (done here, provisional).
3. **Install the target and set the profile.** Owner: U5.1 (profile and target install); A11 devops for the CI runner. Citation: `D:97`; `R:155-156`. Gap: `rust-toolchain.toml` has no `targets` entry; `Cargo.toml:159-160` defines only `profile.release` (`opt-level = 3`). Wasm profile settings: [MEASURE: pending]. Acceptance: a clean runner resolves the target from the repo files alone.
4. **Build reproducibly from locked inputs.** Use reproducible build flags and no unpinned nightly features. Every build resolves from `Cargo.lock`. Owner: U5.1; A11 devops (CI). Citation: `R:160`; `.github/workflows/ci.yml:51` (`--locked`). Flag values (for example `SOURCE_DATE_EPOCH`, linker and codegen flags): [MEASURE: pending]. Acceptance: two builds of the same commit on the same toolchain produce the same module bytes [MEASURE: pending].
5. **Emit the module and its digest atomically.** Name the artifact `gsd-core.<semver>.wasm` and write a `sha256:` digest file alongside it. Write both through temp file plus rename so a reader never sees one without the other [INFERENCE on the rename mechanism]. Owner: U5.1. Citation: `R:158-159`; `D:97`. Core semver: [MEASURE: pending]. The workspace declares no core version; `gsd-security` is `0.1.0` (`crates/gsd-security/Cargo.toml:3`), a crate version, not the core version. Acceptance: the module file and digest file exist together and the digest matches the module bytes.
6. **Hash with SHA-256 via the workspace `sha2`.** The digest is computed over the emitted module bytes. Owner: U5.1. Citation: `R:158-159`; `Cargo.toml:64`; `Cargo.lock:3212-3216`. Digest value per build: [MEASURE: pending].
7. **Store content-addressed.** Downloads and cache lookups address the module by digest (or name plus digest). Resolution never uses an implicit `latest`. Owner: U5.1 produces; U5.2 consumes; A11 devops owns the storage location (`M:127`). Citation: `R:160-161`. Storage location and cache path: [MEASURE: pending]; the plan leaves distribution undecided (`M:122-124`). Acceptance: no code path resolves a module by tag or `latest`.
8. **Verify the pin in CI by hash, not by tag.** CI rebuilds the module and compares its digest to the recorded pin. A tag match alone does not pass. Owner: A11 devops (A0 owner, `E:60-62`, `:67`). Citation: `R:156-157`; `E:60-62`. Gap: `.github/workflows/ci.yml:51-53` has no digest step. Open CI change. Acceptance: a CI job fails when the rebuilt digest differs from the recorded pin.
9. **Verify before load.** `init` loads the module, hash-verifies the bytes against the recorded digest, and pins the toolchain version before creating the runtime once. Owner: U5.2 (`crates/gsd-security/src/wasmer_module_load.rs`). Citation: `R:196-197`; `D:98`. Acceptance: a digest mismatch prevents instantiation; no Wasmer engine is created on mismatch.
10. **Check version pin against the core.** U5.2 confirms the module's `gsd-core.<semver>` identity matches the core version the host was built against before instantiation. Owner: U5.2 (`D:98`, "version pin vs core"). Citation: `R:158`, `:196-197`. Mechanism for embedding the version in the module: [MEASURE: pending]. Acceptance: a version mismatch is refused at load.
11. **Fail closed on any verification failure.** Hash mismatch, version mismatch, or missing file routes to the fallback. No host-side escape and no silent retry with an unverified module. Owner: U5.5 (`crates/gsd-security/src/wasmer_fallback.rs`). Citation: `D:101` (`PoolError::Sandbox` ≡ E-POLICY/E-INTERNAL per cause; no silent host-escape); `R:196-197`. Error-code mapping for a hash or version mismatch: not specified in the plan; choosing E-POLICY or E-INTERNAL per cause is [MEASURE: pending] and is recorded as open item O-07 (section 10). Acceptance: a mismatch produces the mapped error, and the session continues only if join semantics allow (`D:101`).
12. **Health check repeats the artifact check.** `health` reports artifact present, hash ok, runtime alive, and last-invoke latency. Failure maps to `E-DEP`. Owner: U5.2 (health hook); U5.5 (fallback path). Citation: `R:199-200`. Acceptance: `health` returns `E-DEP` when the artifact is missing or the hash fails.
13. **Rebuild and re-verify.** A reviewer or CI job rebuilds from the locked inputs and checks the result against the published digest (step 8). Divergence blocks the release. Owner: U5.1 (rebuild); A11 devops (CI). Release promotion stays with A12 and is out of scope here. Citation: `R:156-161`; `.github/workflows/ci.yml:51`. Acceptance: rebuilt digest equals the recorded digest on the CI runner [MEASURE: pending].

### 7.3 Pins the spike records

| Pin | Value | Source | Status |
|---|---|---|---|
| rustc | `1.98.1` (rustc and cargo 1.98.1, 48a229cea 2026-09-01) | `rust-toolchain.toml:2`; `Cargo.toml:155`; `E:60` | **Confirmed by run** [RUN]. |
| wasmer crate | `=7.5.0` (exact) | `Cargo.toml:152`, `:156` | Pinned in workspace and used in the run [RUN]. Lock entry: [MEASURE: pending] (no `name = "wasmer"` in `Cargo.lock` today). |
| wasmer backend and features | `sys-default` backend; `cranelift` compiler feature available [RUN]. Singlepass and LLVM: not evaluated. Compiler feature selected for the run: [MEASURE: pending]. | `M:122-123` | Partly recorded. |
| target triple | `wasm32-wasip2` pinned (`Cargo.toml:157`). Not runnable under 7.5.0 [RUN]. Recommended: `wasm32-wasip1` (D-P3-1), not applied. | `R:154`; `R:154-157` | Pinned default is unrunnable; change proposed, awaiting A03 and A11. |
| WASI version / interface set | WASI 0.1 recommended (D-P3-1, Q-8). WASI 0.2 and 0.3 not runnable [RUN]. | `M:122-124`; `R:154-155` | Recommended; A03 to record. |
| boundary ABI (O2) | Plain core JSON over `alloc`, `dealloc`, `invoke` (D-P3-3) | `R:256-257`; `E:80-82` | Decided, provisional. |
| module filename pattern | `gsd-core.<semver>.wasm` | `R:158` | Pattern pinned; core semver [MEASURE: pending]. |
| digest file | `sha256:` prefix, written alongside module | `R:159` | Pinned policy. |
| digest algorithm and library | SHA-256 via `sha2 = "0.10"` (resolves to 0.10.9) | `Cargo.toml:64`; `Cargo.lock:3212-3216` | Pinned in repo. |
| artifact SHA-256 value | [MEASURE: pending] (one per build; not a constant) | spike run output | Recorded in the digest file and the spike report. |
| verification policy | verify before load; mismatch fails closed; no `latest`; CI checks by hash | `R:156-161`, `:196-197`; `D:98`, `:101` | Policy pinned; CI check not yet present (`.github/workflows/ci.yml:51-53`). |
| reproducible build flags | [MEASURE: pending] | `R:160` | Undecided; spike records the exact flags. |
| lockfile mode | `--locked` | `.github/workflows/ci.yml:51-53` | In use for the workspace. Whether it covers the wasm build: [MEASURE: pending]. |
| wasm profile | [MEASURE: pending] | `Cargo.toml:159-160` (only `profile.release`) | Undecided. |

---

## 8 Route decisions O1 to O3

Source: `P-route`, updated by the evidence in section 3. All three are DECIDED-PROVISIONAL. Frozen (unchanged by every decision): the omp-facing wire. argv `--flag=value` + `--json`, exit-code/JSON union, the 7 error codes, FINDING and verdict JSON shapes, 100 000-byte cap (`R:42-64`, `R:66-75`, `R:190-195`). Every option changes only the host to guest boundary or where validation runs. None changes a byte the omp plugin or Python sees.

Thresholds: no numeric p50 budget exists in `plan/` (`M:145-146`, §6 q4 is an `[ASSUMPTION]`). Every threshold below is `[MEASURE: pending]` and must be set from the spike's own run-to-run spread before a result can flip a decision.

### 8.1 O1 (D-P3-2): monolithic `gsd-core.<semver>.wasm`

**Options.** (a) Monolith: one hashed, versioned `gsd-core.<semver>.wasm` containing the whole Rust core (scheduler, state, gates, registries, lifecycle). (b) Per-lane modules: one module per lane (read-only, plan/assign, gates, swarm_run) behind one host dispatcher that checks capabilities before routing.

**Trade-off, from the plan.**
- The packaging unit is a single module: "FULL core means the whole Rust core … compiles to one Wasm module" (`R:150-152`); "single hashed, versioned module … build emits module + digest file atomically" (`R:158-159`).
- Per-route pinning is possible in either shape: "module + digest per strangler step" (`K:98`). Per-route revert is available in either shape because routing is per invocation, not per module (`R:107-112`).
- Isolation granularity is at the import level, not the module level: per-call capability token, deny-by-default imports (`R:167-182`). A per-lane split adds no capability the import gate lacks.
- Cold-start is amortised by WarmPool instance reuse (`R:243`; U1.3 in `K:219`), so per-lane cold-start only matters on first use of each lane.
- [INFERENCE] Per-lane modules that share `gsd-proto` / `gsd-runtime` types either duplicate that code or link through component-model composition. The second path is the O2 component path. O1 and O2 are coupled: per-lane modules without components duplicate the core N times.
- The R-F analysis rejected one big-bang cutover, not the monolith artifact (`K:114-115`). The monolith is compatible with per-route flips (`K:107`; `R:113-117`).

**Decision (D-P3-2, provisional): (a) monolith.**

**Per route.** All four routes use the same `gsd-core.<semver>.wasm` digest. What differs per route is the entrypoint selector in the `invoke` payload and the capability-token op set minted for that route.
- read-only (`orch_status` reads, `swarm_tool` catalog): read-only token ops; no `storage` write.
- plan/assign: `storage` write scoped to the session.
- gates: `storage` write limited to verdict rows; `omp-callback` allowed.
- `swarm_run` scheduler policy: full op set, bounded by the per-session allocator.

Rationale for per-route tokens rather than per-route modules: `storage` is "the only state-mutating import" (`R:173-174`), and tokens are "minted host-side per dispatcher call" (`R:180-182`). Narrowing is done there.

**Rationale.** The plan's artifact rules, the import-level capability model, and WarmPool reuse all assume one module. Per-lane splitting adds N digests, N pools and N cold-starts with no isolation the import gate lacks.

**Evidence (updated).**
- Instantiate latency: 0.055 ms; compile: 205.584 ms; peak linear memory: 23 pages after 201 invocations, with the dealloc caveat (section 3.2). Measured on the spike guest, not the full core. Threshold [MEASURE: pending].
- Compile/stub table: the monolith compiles for `wasm32-wasip2` only for `gsd-proto` and `gsd-serde` (section 4). The full core does not yet compile for any target. Monolith compile for wasip1 [MEASURE: pending].
- Artifact size for the full core: [MEASURE: pending]. The 62,979-byte figure is the spike guest.
- Single forced guest trap: instance drop and pool re-instantiate [MEASURE: pending]. This is the residual risk of a monolith (`K:175`, F5).

**Revert trigger.** If peak linear memory or cold-start for the monolith exceeds the per-route budget that A13 sets, reopen O1 and evaluate per-lane modules together with O2's component linking. Do not split modules without a shared-types strategy.

### 8.2 O2 (D-P3-3): plain core JSON over `alloc`, `dealloc`, `invoke`

**Options.** (a) Component-model canonical ABI: WIT-typed records across the boundary; `module_kind = component`. (b) Plain core module: three exports `alloc`, `dealloc`, `invoke`; argv and JSON bytes in linear memory; `module_kind = core`.

**Trade-off, from the plan.**
- The default target is `wasm32-wasip2`; `wasi` needs recorded justification (`R:154-156`).
- [RUN] `module_kind` is measured: `wasm32-wasip2` builds a component (80,086 bytes), which this Wasmer cannot instantiate. `wasm32-wasip1` builds a core module (62,979 bytes). The `module_kind` check is complete for both (`08-PLAN.md:6,16`).
- The frozen wire is JSON in both directions, with `serde_json` and `JSON.parse`, 100 000 bytes per value, depth 32, unknown-field rejection (`R:190-195`). Component types would not replace that JSON on the omp side. They would only type the host to guest hop.
- The serde budget is measured on JSON marshal and unmarshal against guest execution (`R:235-237`; `E:36`). A JSON invoke makes the host to guest bytes identical to the Python truth bytes, so the budget is comparable per route (`R:217-219`; G-python-truth at `E:37`).
- Component-model adoption pulls in WIT generation and a component runtime path the plan never sized. The plan sizes only the import list (`R:167-177`) and the `init/invoke/shutdown/health` hooks (`R:196-200`), which are JSON.
- Mitigation (c) batches lane-verb reads into single invokes (`R:244-245`). That works with one `invoke` export and does not depend on the ABI choice.
- Whether Wasmer 7.5.0 instantiates a component artifact: **answered by the run: it does not** (section 2.2).

**Decision (D-P3-3, provisional): (b) plain core JSON over one `invoke` export, for every route.**
- Boundary: `alloc`/`dealloc`/`invoke`; `invoke` takes argv + input-file paths + deadline as JSON bytes and returns the exit-code/JSON union as JSON bytes.
- No WIT record types cross the boundary. The component option, whose only data export would be a `list<u8>` JSON pass-through, is not runnable with this Wasmer. It is ruled out for 7.5.0. [INFERENCE: this follows from the run result in section 2.2.]

**Per route.** Identical ABI for all routes. Routes differ only by the entrypoint name carried inside the JSON payload. A second ABI for any route would be a mixed boundary, the failure mode the join-affinity guard exists to prevent (`R:107-112`).

**Rationale.** It keeps the one frozen wire (JSON) at the one boundary without an extra type system. It keeps the serde budget directly comparable to Python. It avoids a component runtime dependency that cannot run under the pinned Wasmer.

**Evidence (updated).**
- `module_kind`: core for wasip1, component for wasip2 [RUN].
- `invoke_warm_p50_us` for the JSON `invoke`: 0.990 µs over 200 warm round trips (wasip1) [RUN]. p95 7.111 µs [RUN]. Requires the dealloc re-measure (section 3.2) and the hardware fingerprint [MEASURE: pending].
- The component comparison (`list<u8>` pass-through vs core `invoke` on the same body) [MEASURE: pending]. Not runnable under 7.5.0 [RUN].
- Serde marshal and unmarshal share of warm invoke: [MEASURE: pending]. Recorded, not gated, until a budget exists (`R:235`).

**Revert trigger.** Reopen O2 only when a Wasmer version that instantiates components is pinned and the component path measures `invoke_warm_p50_us` above the spread of the core path. Or, with the core path, serde dominates warm invoke even after mitigations (`R:236-237`). Under the revert trigger, reopen O2 with the component path measured as the primary. [INFERENCE: the original trigger, `module_kind = component` on the current pin, is now true but unmeasurable, so it is restated.]

### 8.3 O3 (D-P3-4): host-side single-owner schema validation

**Options.** (a) Host-side: the host validates FINDING and verdict JSON against the schema at the `invoke` boundary, in both directions. The guest does no schema validation. (b) Guest-side: the guest validates the shapes itself; the host only checks envelope bytes. Defence in depth = both.

**Trade-off, from the plan.**
- The FINDING schema has one source: `omp/src/tools.ts:46-60` (`additionalProperties: false`, `required: ["severity","summary"]`). The mirror is pinned as U6.2 (`D:110`). There is no verdict field: "severities decide" (`omp/src/tools.ts:45`); verdict is derived, never stated (`swarm/gates.py:24-27`; `omp/src/tools.ts:379`).
- Validate-once-at-boundary is a named mitigation (`R:245-246`), placed at the boundary the host owns.
- Unknown-field rejection on error and verdict shapes, and `error.code` allowlisting, are assigned to the boundary (`R:192-194`; F9 at `K:179`). Oversize and shape failures map to `E-INPUT` on the input side and `E-CONTRACT` on the output side (`R:193-195`; error row `R:88`).
- The host already owns signature checks: ed25519/hmac envelope handling stays host-side (`K:179`). Keeping shape checks host-side keeps one owner for "is this a verdict we accept".
- Guest-side defence in depth costs guest linear memory and artifact size if a JSON-schema engine is linked into the guest. That cost is [MEASURE: pending]. Artifact size is not among the spike's measured metrics beyond `module_bytes`.
- Gate verdicts must be byte-identical across engines (`K:136-137`). Host-side validation keeps one code path for that comparison.

**Decision (D-P3-4, provisional): (a) host-side single owner, JSON-Schema from the U6.2 mirror.** Guest-side: typed `serde` structs with unknown-field rejection, used for the guest's own internal parse only. No JSON-Schema engine in the guest.

**Per route.**
- read-only and plan/assign routes: no FINDING or verdict shape crosses the boundary. Only the argv/JSON envelope and the 100 000-byte cap are validated.
- gates route: `per_target_findings` (`{target: [FINDING]}`, `omp/src/tools.ts:394-395`) and the verdict payload are validated host-side on the way in and out. The verdict payload is host-signed.
- `swarm_run` route: FINDING shapes arrive via `omp-callback` and are validated host-side at that import (`R:175-177`).

**Rationale.** Single owner at the boundary, matching validate-once. Keeps the guest small. Keeps the schema constant in one place (U6.2). Keeps the verdict comparison code path identical across engines.

**Evidence still needed.**
- Fixture cases 3, 5 and 8 (`R:222`, `:224`, `:227`) run with host validation on and off. Outcome per case recorded. Expected: identical `E-INPUT`/`E-CONTRACT` mapping (`R:193-195`) [MEASURE: pending].
- `invoke_warm_p50_us` delta with host validation on vs off, same guest body [MEASURE: pending].
- If guest-side defence in depth is later proposed: guest artifact size and peak linear memory delta with a validator crate linked [MEASURE: pending].
- Guest typed parse with unknown-field rejection rejects the same fixture-8 payloads as the schema's `additionalProperties: false`. Gap check, not a metric [MEASURE: pending].

**Revert trigger.** Host-side validation is shown to disagree with the schema on any fixture, or validation share dominates warm invoke. Then add guest-side schema validation for the failing shape only, and record the artifact-size cost.

### 8.4 Summary

| Item | Decision | Status | Primary evidence still needed |
|---|---|---|---|
| O1 (D-P3-2) | Monolithic `gsd-core.<semver>.wasm`; per-route = entrypoint + token scope | DECIDED-PROVISIONAL | Full-core cold start, peak linear memory, artifact size, single-trap recovery (all [MEASURE: pending] for the full core). |
| O2 (D-P3-3) | Plain core, JSON over `alloc`/`dealloc`/`invoke`, same for all routes | DECIDED-PROVISIONAL | `module_kind` measured (done); warm p50 re-measure with dealloc; hardware fingerprint; serde share. |
| O3 (D-P3-4) | Host-side single-owner schema validation; guest typed parse only | DECIDED-PROVISIONAL | Fixtures 3/5/8 on-vs-off; validation cost in `invoke_warm_p50_us`; artifact size if guest validation is later added. |

All thresholds are `[MEASURE: pending]`. No threshold is invented here.

---

## 9 Findings outside scope, with owners

Found during the spike or the policy review. Recorded, not fixed. Each is a blocker for the named gate; none is fixed in this phase.

| ID | Finding | Location | Owner | Blocks |
|---|---|---|---|---|
| F-01 | `validate` rejects every token that `issue` mints, because no expected audience is set. No test covers the path. (= T-01) | `crates/gsd-security/src/lib.rs:92-99`; mint at `lib.rs:76`; `J:125-129`, `J:306-321` | Security phase (P5 / A10) | C3 |
| F-02 | Hard-coded default JWT secret `"gsd-core-secret-change-in-production"`, with `enable_sandbox: true`. Any holder of the secret can mint tokens. (= T-03) | `crates/gsd-security/src/lib.rs:25` (also `G:24`, `G:83-86`) | Security phase (P5 / A10) | C3 |
| F-03 | Fuel exhaustion maps to `E-CAPACITY` in `decompose.md:99`, but to `E-INTERNAL` / `E-TIMEOUT` in `rust-wasmer-omp-options.md:163-164`. Conflict for A10 to rule on (Q-3). | `D:99` vs `R:163-164` | A10 (rule); correct `D:99` | S-02, S-03 |
| F-04 | Cancel bounds differ: 500 ms with 50 ms slices (`S:48`); 10 s cooperative within 15 s (`D:67`); 3 s force-drop (`R:146`). A10 to name one (Q-4). | `.planning/STATE.md:48`, `decompose.md:67`, `rust-wasmer-omp-options.md:146` | A10 | S-20 |
| F-05 | T-02: `validate` does not check scope, resources or subject. Covered by the proposed import gate (b3), but the defect is in the existing code. | `G:92-99`, `G:128-134` | Security phase (P5 / A10) | C3 |
| F-06 | T-04: token expiry is whole hours (`capability_token_expiry_hours`, default 1), longer than the 600 s and 1860 s call deadlines. Needs seconds-granular field (E-15, Q-7). | `G:18`, `G:29`, `G:77`; `R:206-208` | Security phase (P5 / A10) | C3 |
| F-07 | Priority-semaphore slow path never wakes waiters (hang risk). Blocks S-22 until cleared. | `.planning/STATE.md:55` (`S:55`) | Owner not assigned in the shared context. A10 to route. | S-22 |

---

## 10 Open items

### 10.1 Measurements still pending

- Built-module import list for `wasm32-wasip2`. The component cannot be instantiated, so the list needs an extraction path that does not run it [MEASURE: pending] (sizing row 1; W-18).
- Wasip1 link set for the std build with no I/O (W-18; `P-policy` c4).
- Calls per invoke and per session, per import (sizing rows 2, 8 to 12).
- Payload bytes, p50 and p95, per import and direction (sizing rows 8 to 14). The 1003-byte TaskId payload is the only measured payload [RUN].
- Serde time per crossing (marshal and unmarshal) against guest-exec time (`R:232-237`).
- Warm `invoke` re-measure with `dealloc` on every buffer (section 3.2). Required before any gate uses p50 or p95.
- Hardware fingerprint for the warm-invoke numbers [MEASURE: pending].
- Fuel consumed per call; fuel-exhaustion error variant in Wasmer 7.5.0 (S-02, S-03).
- Memory-grow refusal variant in Wasmer 7.5.0 (S-04).
- Peak linear memory under a real workload (O1 revert trigger).
- Wasip1 compile of each workspace member (section 4).
- Stub or feature-split plan for each failing member (section 4).
- Full-core cold start, artifact size and single-trap recovery (O1).
- Serde share of warm invoke (O2); validation on/off comparison for fixtures 3, 5 and 8 (O3).
- Compiler feature selected for the run; Wasmer lock entry; reproducible build flags; wasm profile; core semver and its embedding mechanism; artifact SHA-256 per build; storage location; time for the CI digest step (`P-dist`, section 7.3).
- Error-code mapping for a hash or version mismatch (O-07 below; `P-dist` step 11).
- T-01 audience test on the jsonwebtoken version that resolves (`P-policy` c4).
- Host-side mapping of the errno returns from the wasip1 shims to `E-POLICY` (Q-1).
- Exact commands for the guest and host builds and runs (section 12).

### 10.2 Decisions needed

- **A03:** approve D-P3-1 (wasip1 target, WASI 0.1) and record the WASI version (Q-8). Approve D1 to D4 (`P-sizing` section 4).
- **A11:** approve the target pin change (D-P3-1) in its own commit; add the CI digest step (`P-dist` step 8); D3 link-time import list enforcement.
- **A10:** rule on Q-1 to Q-7; F-03; F-04; approve T-01 to T-04 before C3; approve the sign-off list S-01 to S-24 and the exceptions E-01 to E-16.
- **A03 with A06:** D5 (argv-carried tool input frozen for the Python path).
- **A03 with A10:** D2 (UDS-gRPC and memfd host-only).
- **A03 and planner:** E-09 (model or tool I/O import). Not approvable by A10 alone.
- **O-07 (error code for hash or version mismatch):** choose E-POLICY or E-INTERNAL per cause and record it (`P-dist` step 11).
- **O-08:** re-add `gsd-ipc` to the workspace before its sizing (`S:50`; E-06).

### 10.3 Inconsistency between parts to reconcile

- `Cargo.lock` exists at the repo root and is committed in 7686ba8 (Phase 6). The `P-policy` statement that there is no `Cargo.lock` is stale, from before Phase 6. The jsonwebtoken version that resolves in the committed `Cargo.lock` is [MEASURE: pending].

---

## 11 A10 review

Reviewer: a10-security, advisory only; the human owner decides. Overall verdict: CONDITIONAL. The fault-injection scope and the wasip1 direction are acceptable.

- S-items: S-01, S-02, S-03 and S-05 to S-15, S-18, S-19, S-21 and S-23 accept. S-04, S-16, S-17, S-20 and S-24 accept with condition. S-22 rejected until F-07 is cleared, because the priority-semaphore slow path never wakes waiters.
- F-03 and Q-3: fuel exhaustion without a deadline is E-INTERNAL, with a deadline E-TIMEOUT. decompose.md:99 must be corrected to match.
- F-04 and Q-4: one outer 3 s force-drop bound with 500 ms cooperative slices. The 10 s window in decompose.md:67 is rejected.
- F-07: routed to A05 (backend), with A10 as verifier.
- Q-1: denial is a stub, recorded as E-POLICY. The errno-to-E-POLICY mapping must be measured before S-17 is signed.
- Q-2: stdio stays DENY, since O2 is plain JSON invoke.
- Conditions: (1) F-01, F-02, F-05 and F-06 fixed, with S-16 and S-23 passing, before any C3 work. (2) The Q-1 errno mapping measured before S-17 is signed. (3) S-04 grow behaviour measured before E-CAPACITY is signed. (4) Every latency figure re-measured with dealloc called on every buffer, before any threshold uses it. (5) A03 records WASI 0.1 (Q-8) and approves D-P3-1.

Items A10 must review, from sections 6.2 to 6.3 and 9: exceptions E-01 to E-16; faults S-01 to S-24; questions Q-1 to Q-7; findings F-01 to F-07; the T-01 to T-04 fixes.

Second review (a general reviewer, same phase): also CONDITIONAL, with these rulings.

- S-10 is rejected as written. Python truth returns E-INTERNAL for an unknown error code at exit 2, so the expected code is E-INTERNAL. The planner amends R:193-194 in writing.
- S-08: the depth value 32 is only an example. Define depth and mirror it in omp/src/tools.ts before the test runs.
- S-16: add a positive control, so a correctly minted token passes, plus cases for missing aud, wrong iss and missing iss.
- S-13: the default leeway is 60 s, so the gate sets leeway 0 for per-call tokens.
- Before C3, 21 conditions apply, including: the T-01 to T-04 fixes; the revocation check on the gate (N-4); F-07 fixed or its type deleted; the secret kept out of Debug (N-2); invalid PII regexes refused (N-1); denial records fsynced or the tamper-evidence claim removed (N-3); enable_sandbox wired or deleted (N-5); and a test for every S-item needed for C3, because gsd-security has no tests (N-7).
- New findings N-1 to N-7 are recorded in the second review. Round-trip success is implied by exit 0, since the host prints no separate line for it.

---

## 12 Reproduction

**Host.** rustc and cargo 1.98.1 (48a229cea 2026-09-01) on `x86_64-unknown-linux-gnu`. The host crate depends on `wasmer = { version = "=7.5.0", features = ["cranelift"] }` [RUN].

**Guest crates.** Live outside the repo in `/tmp/gsd-wasmer-spike/guest` (package `gsd-wasmer-spike-guest`, depends on `crates/gsd-proto` by path and on `prost =0.13.5`) and `/tmp/gsd-wasmer-spike/host` (package `gsd-wasmer-spike-host`) [RUN]. Three guest builds were attempted: `wasm32-wasip2` (component, 80,086 bytes), `wasm32-wasip1` (core, 62,979 bytes) and `wasm32-unknown-unknown` (failed in getrandom 0.4.3, which needs the `wasm_js` feature).

**Commands (all run by the orchestrator).**

- Target install for the pinned toolchain: `rustup target add wasm32-wasip2`, `rustup target add wasm32-wasip1` and `rustup target add wasm32-unknown-unknown`, run from the repo root so the pin applies [RUN].
- Guest, component: `cd /tmp/gsd-wasmer-spike/guest && RUSTUP_TOOLCHAIN=1.98.1 cargo build --target wasm32-wasip2 --release` [RUN, 2m 40s].
- Guest, core: `cd /tmp/gsd-wasmer-spike/guest && RUSTUP_TOOLCHAIN=1.98.1 cargo build --target wasm32-wasip1 --release` [RUN, 4m 02s].
- Host: `cd /tmp/gsd-wasmer-spike/host && RUSTUP_TOOLCHAIN=1.98.1 cargo build --release` [RUN, 33m 06s on the loaded host].
- Component run: `/tmp/gsd-wasmer-spike/host/target/release/gsd-wasmer-spike-host /tmp/gsd-wasmer-spike/guest/target/wasm32-wasip2/release/gsd_wasmer_spike_guest.wasm` gives the validation error in section 2.2 and exit 1. Output saved to `/tmp/gsd-wasmer-spike/run-wasip2.txt` [RUN].
- Core run: `/tmp/gsd-wasmer-spike/host/target/release/gsd-wasmer-spike-host /tmp/gsd-wasmer-spike/guest/target/wasm32-wasip1/release/gsd_wasmer_spike_guest.wasm` gives the metrics in section 3.1 and exit 0. Output saved to `/tmp/gsd-wasmer-spike/run-wasip1.txt` [RUN]. Before the deny shims were added, the same command failed with `unknown import: wasi_snapshot_preview1.environ_get` [RUN].
- Compile and stub table: `cd /root/src/repos/agent-swarm && cargo check --workspace --lib --target wasm32-wasip2 --keep-going`, log at `/tmp/gsd-wasmer-spike/wasip2-check.log`. Then per member: `cargo check -p <member> --lib --target wasm32-wasip2` [RUN].
- Wasmer source check: `grep -rli component ~/.cargo/registry/src/*/wasmer-7.5.0/src | wc -l`, which returned 0 [RUN].
- Core guest for unknown: `cd /tmp/gsd-wasmer-spike/guest && RUSTUP_TOOLCHAIN=1.98.1 cargo build --target wasm32-unknown-unknown --release` fails in getrandom 0.4.3 [RUN].

These runs happened after the parts were drafted, and the orchestrator executed them. The spike code is not committed, per the report-only rule.

## 13 Amendment 2026-10-22: runtime change (user decision)

The user chose: keep the `wasm32-wasip2` target and change the runtime. Sections 1 to 12 describe the earlier Wasmer 7.5.0 route. Where they conflict with this section, this section governs.

### 13.1 Decision

- D-P3-5: the sandbox runtime is wasmtime 50.0.0, with the `component-model` feature (crate `wasmtime`) and the `p2` feature (crate `wasmtime-wasi`). Target: `wasm32-wasip2`, kept.
- Supersedes D-P3-1 (the wasip1 pin change) and O2 as recorded in section 8. O2 becomes: the wasip2 component-model canonical ABI carries the frozen JSON bytes as `list<u8>` through a WIT `invoke` export. The wire shape is unchanged.
- Wasmer 7.5.0 remains unable to run components (section 2.2). It is no longer the sandbox runtime.

### 13.2 Evidence (measured)

- The WIT guest (`gsd:spike@0.1.0`, world `spike`, export `invoke: func(input: list<u8>) -> list<u8>`) builds for `wasm32-wasip2` as a component of 80,066 bytes (header `00 61 73 6d 0d 00 01 00`).
- Host: wasmtime 50.0.0, deny-by-default WASI 0.2 (`WasiCtxBuilder::new().build()`, no environment, no preopens, no inherited stdio). Built with rustc and cargo 1.98.1 in 25 min 45 s.
- Run output (`/tmp/gsd-wasmer-spike/run-wasmtime-wasip2.txt`, exit 0): compile_ms 205.358; instantiate_ms 0.423; invoke_warm_p50_us 1.350; invoke_warm_p95_us 1.480; module_bytes 80066; module_kind component; wasi_policy deny-by-default. 200 timed round trips with a 1003-byte TaskId payload; each sample checks output equals input.
- The dealloc caveat in section 3.2 does not apply to this run: the canonical ABI owns the list buffers. The caveat still applies to the retired Wasmer harness.

### 13.3 What is still open

- S-17 and Q-1: the denial of WASI imports is now enforced by the WASI 0.2 context, not by host stubs. The errno and error-code mapping must be re-measured on this runtime before S-17 is signed.
- The fault-injection scope (section 6.3) must be re-checked against wasmtime traps and fuel. The earlier Wasmer mapping in section 6.3 is not carried over.
- The compile/stub table (section 4) is still valid for the target, because the target is unchanged.
- The pins are not yet changed in the repo. Cargo.toml still declares `wasmer = "=7.5.0"`. Changing it to wasmtime is a separate commit, after the component gate in Phase 10.
- A10 review of this runtime change is pending. The earlier A10 conditions (section 11) still apply where they name fuel, the trap mapping, and the import allowlist.

### 13.4 Reproduction

- Guest: `cd /tmp/gsd-wasmer-spike/guest && RUSTUP_TOOLCHAIN=1.98.1 cargo build --target wasm32-wasip2 --release` (2 min 23 s).
- Host: `cd /tmp/gsd-wasmer-spike/wasmtime-host && RUSTUP_TOOLCHAIN=1.98.1 cargo build --release` (25 min 45 s).
- Run: `/tmp/gsd-wasmer-spike/wasmtime-host/target/release/gsd-wasmer-spike-wasmtime /tmp/gsd-wasmer-spike/guest/target/wasm32-wasip2/release/gsd_wasmer_spike_guest.wasm` (exit 0).
