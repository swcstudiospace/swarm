# A10 security gate: default guest pin 91b0970e (D-P38)

**Verdict: SECURED**, scoped to the default guest pin `91b0970ebe43fb7a80e5c6df68abcf6516572a766260e9780fd6f3c199b9a175` (the read-only guest, `crates/gsd-sandbox/guest-module.sha256`).
This is not a clearance for flipping the write routes (U3.8c), for the `regex` edge added in `gsd-adapter` (finding F3), or for the D-P38 wording about `ffa7b28a` (finding F2). No open blocker. Majors F1 to F3 do not block the re-sign of this pin; see the findings table.

- Repo: `/root/src/repos/agent-swarm`, branch `feat/v2.4-rust-wasm-core`, HEAD `144a8d3`, range reviewed `dc46279..HEAD` (47 commits).
- Scratch: `/root/.cache/gsd-redact-work/a10-guest-91b0970e/` (logs, junit, clean trees, isolated target dirs).
- Tooling: rustc/cargo 1.98.1 (pinned by `rust-toolchain.toml`), `--locked --offline` throughout. No product code, pin, `web/` or `docs/` file was edited.

## 1. Scope

Change set: `crates/gsd-guest`, `crates/gsd-route`, `crates/gsd-sandbox`, `crates/gsd-adapter/src/sandbox_engine.rs`, `crates/gsd-adapter/src/write_route/`, `crates/gsd-adapter/src/sandbox_shadow.rs`, `crates/gsd-adapter/tests/write_route_sandbox_faults2*`, `crates/gsd-taskwriter/src`, `crates/gsd-adapter/tests/keys_never_in_guest.rs`, `tests/test_keys_never_in_guest.py`, the three pin files and `web/jco.pin.json`. Also read for the chain of custody: `crates/gsd-envelope/src/keys.rs`, `crates/gsd-envelope/src/envelope.rs`, `Cargo.lock`, `crates/gsd-route/Cargo.toml`, `crates/gsd-guest/Cargo.toml`, `crates/gsd-sandbox/build.rs`.

Default guest content: `crates/gsd-guest/src/lib.rs:20-23` compiles `orch_status_invoke` only when `write-routes` is off. `gsd-guest` depends on `gsd-route` with `default-features = false` (`crates/gsd-guest/Cargo.toml`), so the pinned default guest contains no transition, second-batch, approval, ingest, plan or catalog code.

## 2. Commands and exit codes

| # | Command (run in repo unless noted) | Exit | Result |
|---|---|---|---|
| C1 | `cargo test -p gsd-adapter --test keys_never_in_guest --locked --offline` | 0 | 28 passed, 0 failed |
| C2 | `cargo test -p gsd-sandbox --locked --offline` | 0 | lib 49, guest_imports 7, read_route_budget 1, write_guest 1: all passed |
| C3 | `cargo test -p gsd-route --locked --offline` | 0 | lib 139, core_invoke 3, transition_wire 12: all passed |
| C4 | `cargo test -p gsd-adapter --features sandbox-write --test write_route_sandbox_faults2 --locked --offline` | 0 | 56 passed, 0 failed |
| C5 | `cargo test -p gsd-sandbox --features write-routes --locked --offline` | 0 | lib 49, guest_imports 7, read_route_budget 1, write_guest 4: all passed. `guest_digest::built_guest_digest_matches_the_committed_pin` ok against the P2 pin `guest-module-write.sha256` (50b06310...) |
| C6 | `python3 -m pytest tests/test_keys_never_in_guest.py -o addopts= -p no:cacheprovider --junitxml=<scratch>/k2.xml`, on the default `target/debug/gsd_engine` | 0 | 233 passed, 17 skipped, 0 failed. junit `gsd_engine_build=default` |
| C7 | same pytest, on a `--features sandbox-write` `gsd_engine` | 0 | 249 passed, 1 skipped (`test_the_binary_is_present_when_CI`: CI unset). junit `gsd_engine_build=sandbox-write` |
| C8 | `cargo tree --locked --offline -p gsd-guest -e normal,build --all-features` (and `-p gsd-route`, `-i gsd-envelope`) | 0 | see check 4 |
| C9 | `git diff dc46279 HEAD -- Cargo.lock` | 0 | 3 insertions, 0 new `[[package]]` entries |
| C10 | `cargo check -p gsd-sandbox --locked --offline` in clean `git archive` trees of HEAD, dc46279, eb7a2b9, each with its own target dir | 0, 0, 0 | digests in check 6 |

Notes on the run:
- The first pytest attempt (exit 1, 10 failed) is not a code result. My own concurrent `--features sandbox-write` build relinked the shared `target/debug/gsd_engine` after the test's probe had recorded `default`; the failing legs then ran against the sandbox-write binary. Evidence: the junit property was `default` while the failures show sandbox-write behaviour (`ok` for a sandbox APPROVED transition, `E-POLICY` for the require-key leg). I rebuilt the default binary alone and reran (C6, exit 0), then ran C7 on the sandbox-write binary (exit 0), then restored the default binary.
- Two clean-tree builds (dc46279, eb7a2b9) first ran into one shared build-script `OUT_DIR`, because the package hash does not include the source path. Their first-pass digests were overwritten and are not used. The results below come from isolated target dirs (`tgt-dc`, `tgt-eb`).

## 3. Checks

### Check 1: keys never reach the guest (from code)

Result: holds for every guest-bound value I traced. PASS, with the coverage gap in F1.

Key reads happen only on the host:
- `crates/gsd-taskwriter/src/approval.rs:104` (`KeyMaterial::from_lookup` in the approval key set), `:155` (`row_sig_ok`, which yields a boolean).
- `crates/gsd-adapter/src/write_route/plan.rs:299` (`KeyMaterial::from_env` in `answer_call`); the kickoff is signed by `sign_kickoff` (`plan.rs:587`) inside the write transaction, before any write (`create()` at `plan.rs:513`, kickoff at `:520`, write at `:531`).
- `crates/gsd-adapter/src/write_route/assign.rs:348` (host-only assignment route; not a guest mode).
- `gates/*.rs` also read keys on the host (not guest modes).

Every field a guest-bound request carries is a boolean, a name, a number, or static text:
- Approval: `policy` = `require_key`, `real_key`, `dev_key` booleans; `rows[].sig_ok` boolean (`approval_wire` header, `crates/gsd-route/src/approval_wire.rs`). The guest decides from `sig_ok` only and verifies nothing.
- Plan: `PlanFacts.signing` = `agent_session: bool`, `real_key: bool`, `config_error: Option<String>` (`crates/gsd-route/src/plan_derive.rs:180-187`, filled in `signing_state`, `plan.rs:740-758`). `config_error` is always one of three static constants: `REQUIRE_WITHOUT_KEY` (`crates/gsd-envelope/src/keys.rs:28`), `ED25519_UNLOADABLE` and `AGENT_SESSION_REFERENCE` (both `&'static str`; the field type is `keys.rs:123`, the accessor `keys.rs:304`).
- Ingest and reconcile: `sig_ok` booleans and stored row fields.
- Read (default guest): `request_json` takes `mode`, `agent`, `script`, `correlation`, `db_path`, `swarm_dir`, `events`, `tasks`, `verdicts`, `history` only (`crates/gsd-adapter/src/strangler_seam.rs:916-935`). No environment value and no key field.
- `envelope_json` (approval, ingest, reconcile rows) is the stored verdict envelope text (`crates/gsd-taskwriter/src/store.rs:528`, `:742`). It holds `sig` (`ed25519:` or `hmac:` plus base64): a signature output, not key material. The `hmac:` tag is covered in F4.
- Environment forwarded to routes: `SWARM_TASK_ID`, `SWARM_CORRELATION_ID`, `SWARM_DIR`, `SWARM_AGENT_SESSION`, `SWARM_CHILD` only (grep of `write_route/`, `gsd-route/src/`). The key variables are not forwarded: `test_each_route_is_handed_exactly_the_keys_the_policy_allows` (C6, C7) checks the selector's per-route key policy.

Run log, stderr and error text:
- `failover_notice` carries the wire code only (`crates/gsd-adapter/src/sandbox_engine.rs`, `failover_notice`).
- Refusal texts are static `fail-closed:` strings (`write_route/assign.rs:358-361`, `approval_wire`, `gsd-taskwriter/src/approval.rs`). `KeyMaterial`'s `Debug` prints presence flags only (`keys.rs:332-345`).
- The guest's `invoke-error` message is dropped on the write path (`sandbox_engine.rs`, `fault_of`, `:434-443`; `WriteGuest::call` keeps the code only).

Field classification:
- Verification results (allowed, booleans or facts): `sig_ok`, `real_key`, `dev_key`, `require_key`, `agent_session`, `config_error` (static).
- Material: none reaches a guest-bound type. `KeyMaterial` has no `Clone`/`Serialize` check on the adapter side (that check is disabled, `keys_never_in_guest.rs:1732-1741`); the `Clone` half runs in `crates/gsd-taskwriter/tests/approval.rs`.

Test evidence: C1, C6, C7 (canary scans of stdout, stderr, files, WAL and SHM, run log, refusal `Debug`, for the transition and the APPROVED legs on both builds). Guest import surface: the empty-linker and no-imports tests in C2 (`wasmtime_guest_invoke` and `guest_imports`).

Gap (F1): the canary scan does not see the P2 request bytes. `transition_via` passes no approval verifier without `sandbox-write` (`crates/gsd-adapter/src/sandbox_engine.rs:606-608`), and the canary file has no feature gate. The approval, ingest, plan, reconcile and catalog request bytes are never canary-scanned, because they are not persisted. The check above therefore rests on the typed encoders, not on a test.

### Check 2: verified shadow (host recomputes, fails over, forged refusal cannot print)

Result: PASS.

- `SandboxDecider::decide` (`sandbox_engine.rs:556-584`) always computes `host = HostDecider.decide(...)` first. It returns `host` on `Ok` (guest bytes equal the host's encoded decision) and on a fault (`Err(code)` records the failover and returns `host`). Only `Timeout` returns an error, and that error is E-TIMEOUT with `write_state: Final`. A guest reply is never printed; the host decision is.
- `Shadow::verify` (`crates/gsd-adapter/src/sandbox_shadow.rs:90-118`) compares `bytes == expected` (`:115`). Equality is the only route to `Ok`. Mismatch is `Fault(Contract)` (`:117-118`).
- Per mode: `SandboxApproval::verify` (`write_route/shadow_approval.rs`) returns `Ok` for a fault and the host answer stands; `ShadowIngest::check` (`write_route/shadow_ingest.rs:75-83`) does the same and checks the reconcile stage on its own; `PlanShadow::check` (`write_route/shadow_plan.rs`) the same.
- Pinned by tests: `write_route_sandbox_faults.rs:853` (`forged_refusal`, first batch); `write_route_sandbox_faults2/approval.rs` `an_honest_guest_agrees_with_the_host`, `facts_that_differ_from_the_host_answer_are_a_contract_fault` (`:433`); `write_route_sandbox_faults2/plan.rs` `a_faulty_guest_leaves_the_host_answer_in_place_and_records_the_fault` (`:458`), `a_prepare_failure_answers_from_the_host_without_calling_the_guest` (`:485`).

### Check 3: failure paths and write ordering

Result: PASS for guest error, spent deadline, malformed reply and oversized reply in all four second-batch modes. Fuel and the guest-claimed E-TIMEOUT are covered for approval and plan, and by generic code for ingest and catalog.

| Path | Mechanism | Test that pins it (all pass, C4 unless noted) |
|---|---|---|
| Guest error (any code) | `sandbox_shadow.rs:109` `Fault(code)`; host answer used (`sandbox_engine.rs:580`) | `write_route_sandbox_faults2/approval.rs` `a_guest_error_is_a_fault_with_its_code` (`:358`) |
| Spent deadline | `sandbox_shadow.rs:100-101`, `:108` `Timeout` -> E-TIMEOUT, rollback, nothing written | `approval.rs` `a_spent_deadline_is_a_timeout_and_no_failover` (`:371`) and `a_spent_deadline_during_the_approval_check_rolls_back_and_writes_nothing` (`:573`); `ingest.rs` `a_spent_deadline_inside_the_writer_rolls_back_and_leaves_every_table_unchanged` (`:497`); `plan.rs` `a_spent_deadline_is_a_timeout_and_no_failover` (`:199`) and `a_spent_deadline_inside_the_transaction_writes_nothing` (`plan.rs:551`); `catalog.rs` `a_spent_deadline_is_a_timeout_with_no_failover` (`:95`) |
| Fuel exhaustion | `wasmtime_limits.rs:190` OutOfFuel -> Timeout when a deadline is set; `sandbox_shadow.rs:105-106`: before the guest deadline -> `Fault(Internal)`, i.e. failover | `approval.rs` `fuel_while_time_remains_is_the_guest_fault_internal` (`:385`); `plan.rs` `fuel_is_a_fault_of_the_guest_not_a_timeout` (`:213`). Not tested for ingest or catalog (F6) |
| Malformed reply | `Shadow::verify` equality -> `Contract` | `approval.rs` `garbage_and_mangled_replies_are_contract_faults` (`:395`); `ingest.rs` `garbage_and_a_mangled_reply_are_contract_faults` (`:212`); `plan.rs` `garbage_and_a_mangled_reply_are_contract_faults` (`:225`). Not tested for catalog (F6) |
| Oversized reply (>1 MiB, `sandbox_engine.rs:389`) | `sandbox_shadow.rs:112-113` -> `Capacity`, checked before equality | `approval.rs` `an_oversized_reply_is_a_capacity_fault` (`:408`); `catalog.rs` (`:132`); `ingest.rs` (`:225`); `plan.rs` (`:237`) |
| Guest claims E-TIMEOUT in its own error | `fault_of` `sandbox_engine.rs:434-443` maps it to `Internal` (a failover, never a final timeout) | `sandbox_engine.rs:1004` `a_guest_cannot_claim_the_deadline_in_an_error_of_its_own` (C2) |
| Guest cannot be prepared | `Shadow::new` stores `prepared`; `verify` returns `Fault` before calling the guest (`sandbox_shadow.rs`, `verify`, the `prepared` check) | `approval.rs` `a_guest_that_cannot_be_prepared_is_a_fault_and_is_never_called` (`:418`) |
| Reconcile stage (inside ingest) | second `verify` in `shadow_ingest.rs` | no dedicated fault test (F6) |

Write ordering (no path writes before the host decision is verified):
- Transition: `apply.rs:347-353` runs `verifier.verify` (APPROVED facts) before `decide` (`:364`); the decide step (`SandboxDecider`) runs its own guest check before any apply. Only `Timeout` refuses; the writer rolls back. The write is after `decide`/`validate`, inside `BEGIN IMMEDIATE`.
- Ingest: `IngestCheck::check` runs after planning and before the first write, inside the transaction (`gsd-taskwriter/src/ingest.rs:305-308`, `:382-383`).
- Plan: the check runs before the first write (`write_route/plan.rs:354` for dry run and refusal; `:472-474` for creation). The kickoff signing (`with_kickoff`, `:493` and `:520`) comes after the check and before `write_plan_ops` (`:531`), and a signing failure rolls back.
- Read route (default guest): not shadowed. A guest answer, including its error, is final (`sandbox_engine.rs` `failover_code`, `:262-272`). Read output integrity rests on the pin and the identity check. See F5.

### Check 4: dependency edges and new crates

Result: PASS.

- `cargo tree --locked --offline -p gsd-guest -e normal,build --all-features` (C8): the graph is `gsd-codes`, `gsd-route`, `gsd-guest`, `wit-bindgen` (pre-existing), serde, wasm-encoder/wasmparser, and other build-time crates. It contains none of `gsd-envelope`, `ed25519-dalek`, `curve25519-dalek`, `hmac`, `gsd-security`. `-p gsd-route` gives the same negative result.
- `cargo tree -i gsd-envelope -e normal,build` (C8): reverse dependencies are `gsd-adapter` and `gsd-taskwriter` only. `gsd-sandbox` does not reach it.
- Cargo.lock (C9): 3 insertions, all dependency edges between existing packages: `gsd-adapter -> gsd-envelope`, `gsd-adapter -> regex`, `gsd-taskwriter -> gsd-envelope`. Zero new `[[package]]` entries, so no crate was added in the range. `regex` was already in the lock (used by `gsd-security`).
- Manifests: the `gsd-adapter -> regex` edge is new (`crates/gsd-adapter/Cargo.toml`, commit `1a09bff`, `gates/sec_gate.rs`, 1742 lines). See F3.
- No unsafe-capable crate was added (no new package).

### Check 5: tests at HEAD

Result: PASS (C1 to C7). Details in section 2.

### Check 6: reproduction of the pins from clean trees

Result: the default pin `91b0970e` reproduces at HEAD and at dc46279 from clean trees at other paths. The claim about `ffa7b28a` is correct for dc46279 and HEAD, but wrong for its origin commit eb7a2b9 (F2).

| Source (clean `git archive`, scratch path, `--locked --offline`, pinned toolchain) | Digest of the built guest component | Matches |
|---|---|---|
| HEAD (`144a8d3`) | `91b0970ebe43fb7a80e5c6df68abcf6516572a766260e9780fd6f3c199b9a175` | `crates/gsd-sandbox/guest-module.sha256`, `web/jco.pin.json`, and the in-repo test |
| dc46279 (isolated target) | `91b0970ebe43fb7a80e5c6df68abcf6516572a766260e9780fd6f3c199b9a175` | yes |
| eb7a2b9 (isolated target) | `ffa7b28a7879da7ea99a20c83999d0b79e757bd084878909dcce0054fe66cade` | the old pin, which D-P21 signed off |

- Write pin: `guest-module-write.sha256` = `50b06310480fd0dbce7c285ce24191a58eb5cca9bbe86ca28a23e89ea5ba15d4`, confirmed by C5 in the repo tree (not in a clean tree).
- `web/jco.pin.json`: `component_sha256` = `91b0970e...`, `version` = `1.39.0` (not `latest`), `checked_on` = 2026-10-30.
- Path independence: HEAD built at a different path gives the committed digest. The eb7a2b9 build reproduces at a different path too. So the path is not the cause.
- Change point: between eb7a2b9 and dc46279 there are 34 commits; those touching guest-linked crates start at `f87dab7`, which STATE D-P25 names as the first guest-changing commit. I did not bisect.

### Check 7 (sub-check): read-route request keys

The read-route key-set test is disabled (`keys_never_in_guest.rs:1777`, `#[cfg(any())]`, pending a public seam). Code proof: check 1 (`strangler_seam.rs:916-935`). The default guest receives no key or environment value. This is the same gap class as F1 for the read path, and it sits on the pinned guest. See F1.

## 4. Findings

| ID | Severity | Location | Finding | Blocks re-sign of 91b0970e? |
|---|---|---|---|---|
| F1 | major | `crates/gsd-adapter/src/sandbox_engine.rs:606-608`; `crates/gsd-adapter/tests/keys_never_in_guest.rs:1777`, `:1732-1741` | The key canary scan does not cover the P2 request bytes (approval, ingest, plan, reconcile, catalog) or the read-route request. The approval leg is not even built without `sandbox-write`. The read-mode key-set test is disabled. The invariant holds by typed encoders (check 1), not by a test that would catch a regression. | No. The default guest carries only the read route, and the read request is code-proven clean. Yes for any write-route flip (U3.8c): close F1 first, with a canary test that captures the encoder output of each mode. |
| F2 | major | `.planning/STATE.md` D-P38 (line 398); D-P37 (line 395) | The D-P38 record says ffa7b28a's origin is "not reproducible from clean trees". Evidence: a clean `git archive` of eb7a2b9 builds `ffa7b28a...` (check 6). The implementer's claim (not reproducible at dc46279 or HEAD) is correct; the origin claim is not. | No. Correct the D-P38 wording before it is recorded. The D-P21 sign-off for ffa7b28a rests on a build that does reproduce. |
| F3 | major (question for the user) | `crates/gsd-adapter/Cargo.toml` (`regex` edge); `crates/gsd-adapter/src/gates/sec_gate.rs` (commit `1a09bff`, 1742 lines) | New host-only dependency edge `gsd-adapter -> regex` for the sec_gate host port. Q8 says the `regex` crate "would need your approval". D-P36 item (1) overrides D-P33 for the port but does not name `regex` [INFERENCE: approval not recorded]. Not in the guest graph (check 4). | No. Obtain the user's explicit approval of `regex` for the host route before 1a09bff ships. |
| F4 | minor | `crates/gsd-envelope/src/envelope.rs:74-75`; approval/ingest/reconcile request `envelope_json` | Guest-bound requests carry stored envelope text including `sig`. No key material, but an `hmac:` tag over a known body allows offline guessing of a weak HMAC secret. Same data is already in the verdicts table. | No. Informational. |
| F5 | minor | `crates/gsd-adapter/src/sandbox_engine.rs` `failover_code` `:262-272`, read path | Read-route answers from the default guest are final, with no host cross-check (GA3). A faulty default guest could alter read output. Integrity rests on the pin and the identity check. | No. By design; state it in the record, and treat the pin as the control. |
| F6 | minor | `crates/gsd-adapter/tests/write_route_sandbox_faults2/` | Fault coverage gaps: no direct fault test for the reconcile stage; no fuel test for ingest or catalog; no malformed-reply test for catalog. | No. Add tests before the write flip. |
| F7 | minor (carried, not from this range) | `crates/gsd-envelope/src/envelope.rs:74-75`, tests `:496-525` | F-5 of the U3.4 review: an `hmac:` envelope verifies beside an Ed25519 signer. Still a user decision. | No. Unchanged by this range. |
| F8 | info | repo root | Untracked `deny-report.txt` is not from this review. Left untouched. | No. |

No blocker was found. Majors F1 to F3 do not block the re-sign of `91b0970e`.

## 5. What I could not verify

- Bisect of eb7a2b9..dc46279 for the commit where ffa7b28a stops reproducing. I cited D-P25's `f87dab7`, not my own run.
- The A11 reproducibility checks, the web artifact build, and `scripts/_check_pins.py`. Out of scope for this record.
- Checks on platforms other than this linux x64 VPS. The Python `cargo metadata` test runs all platforms (C6, C7 passed), but I did not run wasm32-wasip1/wasip2 builds.
- Guest linear memory, guest contents beyond the module identity check (no decompile).
- The parity harness and the 696-cell sandbox leg. Not part of this gate.
- Timing-sensitive tests (C4 and C5 include deadline tests) were run once each on a host with other sessions possibly running. A flaky timing result would not be detected by one run.
- Runtime behaviour of approval and ingest with real `KEY_FILE` contents and a real signed verdict set beyond the test fixtures. Covered by C7 canaries, not by an independent audit.
- The write-route fault tests run the in-process guest (`InProcessGuest`), not the wasm guest. The wasm guest runs in C2 and C5 only through the pin test and `write_guest`.
