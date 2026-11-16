Verdict: SECURED, scoped to the P2 write guest `50b06310480fd0dbce7c285ce24191a58eb5cca9bbe86ca28a23e89ea5ba15d4` (`guest-module-write.sha256`): key isolation, verified shadow, failure paths, dependency edges and pin identity. NOT a clearance for any write-route flip (U3.8c), and it does not cover the read-route request, the signed-envelope contract in P2 requests, or the catalog mode, all listed below.

# A10 security gate: write guest 50b06310 (P2, U3.8b gap)

- Repo `/root/src/repos/agent-swarm`, branch `feat/v2.4-rust-wasm-core`, HEAD `d8a354e` (observed at start; the clean export below was taken from `HEAD` at the same point). Range reviewed `dc46279..HEAD` (40 files changed in the assignment paths, 20474 insertions, 188 deletions).
- Read-only on product code, pins, `web/`, `docs/`, `.planning/`. The only file created in the repo is this record. Nothing committed.
- Scratch: `/root/.cache/gsd-redact-work/A10-write/` (clean `git archive` export of HEAD and its own `CARGO_TARGET_DIR`, about 3 GB). The shell guard refused its removal (`rm -rf`), so the orchestrator must delete it.
- Tooling: `rustc`/`cargo` 1.98.1 from `rust-toolchain.toml`, `--locked --offline` throughout.
- The three re-runs of check 5 used the shared `target/` (sequential, one job). The pin reproduction used the scratch target, because the shared target can hold two guest digests under one hash directory (A11, D-P39).

## 1. Commands and exit codes

| # | Command | Exit | Result |
|---|---|---|---|
| R1 | `cargo test -p gsd-adapter --test keys_never_in_guest --features sandbox-write --locked --offline` | 0 | 33 passed, 0 failed (includes the `p2_writes` module: approval, ingest with reconcile, plan) |
| R2 | `cargo test -p gsd-adapter --test write_route_sandbox_faults2 --features sandbox-write --locked --offline` | 0 | 56 passed, 0 failed |
| R3 | `cargo test -p gsd-route --features write-routes-2 --locked --offline` | 0 | 154 passed in 4 suites, 0 failed |
| R4 | `cargo test -p gsd-adapter --test write_route_sandbox_faults2 --features sandbox-write --locked --offline -- --list` | 0 | 56 tests: approval 16, catalog 11, ingest 13, plan 16 |
| R5 | `git archive HEAD \| tar -x -C <scratch>/tree` then `CARGO_TARGET_DIR=<scratch>/target cargo test -p gsd-sandbox --features write-routes --lib built_guest_digest --locked --offline -- --nocapture` (in the export) | 0 | 1 passed. The build wrote `OUT_DIR/guest-module.sha256` = `50b06310480fd0dbce7c285ce24191a58eb5cca9bbe86ca28a23e89ea5ba15d4` (read back with `read`) |
| R6 | `cargo tree --locked --offline -p gsd-guest -e normal,build --all-features --prefix none` | completed | full guest graph read (see check 4) |
| R7 | `cargo tree --locked --offline -p gsd-route -e normal,build --all-features` | completed | `gsd-route` depends only on `gsd-codes` |
| R8 | `cargo tree --locked --offline -e all -i <gsd-envelope \| regex \| ed25519-dalek \| hmac> --prefix none` | completed (pipeline exit not captured) | reverse edges read (see check 4) |
| R9 | `cargo tree -p gsd-sandbox -e normal,build --prefix none -i ed25519-dalek` | completed | `ed25519-dalek` reached via `gsd-security` |
| R10 | `git diff dc46279..HEAD -- Cargo.lock`; `git diff --stat dc46279..HEAD -- '*Cargo.toml' Cargo.lock` | completed | 3 insertions, no new `[[package]]` |
| R11 | `git grep -n "SELECT \* FROM verdicts" dc46279 HEAD -- crates/gsd-adapter/src/strangler_seam.rs` | 0 | line 808 at `dc46279`, line 826 at HEAD (same query before and after the range) |
| R12 | `grep` of `50b06310` in `web crates scripts tests docs .planning/STATE.md` | completed | hits only in `guest-module-write.sha256`, two records, `flip_record.md`, `docs/operations-runbook.md`; no `web/` pin for the write component |

Not re-run here: the Python canary `tests/test_keys_never_in_guest.py`, `gsd-sandbox` `guest_imports` (7) and `write_guest` (4), the parity harness and the 696-cell sandbox leg. Their results are from the 91b0970e record and are not restated as results of this run.

## 2. Check 1: keys never reach the guest

Result: PASS for every field I traced. No P2 request carries key bytes, a seed, an HMAC secret, a key-file path or a key-file content. The canary scan (R1) found no canary in any captured request, reply, effects manifest, store file or refusal text across approval, ingest with reconcile, and plan.

Field classification (results are booleans or fixed texts; "signed text" is data the guest must parse):

| Mode | Field | Carries | Class |
|---|---|---|---|
| approval | `policy.require_key`, `real_key`, `dev_key` | key-policy booleans from `KeyMaterial` | result |
| approval | `task.{task_id, correlation_id, risk_class, notes}` | task row | data |
| approval | `rows[].sig_ok` | host's signature verdict, boolean | result |
| approval | `rows[].envelope_json` | stored signed envelope text, including top-level `sig` and any `hmac:` tag | signed text (F1) |
| approval | `rows[].{id, task_id, gate, verdict, ts}`, `issuers[]`, `now` | verdict row columns, gate-task states, clock | data |
| ingest | `facts.reconcile.key_policy` | booleans | result |
| ingest | `facts.reconcile.verdicts[].sig_ok` | boolean | result |
| ingest | `facts.reconcile.verdicts[].row.envelope_json` | signed envelope text | signed text (F1) |
| ingest | `facts.reconcile.verdicts[].envelope` | payload parsed by the host (`gate`, `task_id`, `verdict`, `issued_at`, `expires_s`, `dry_run`, `gate_task`) | parsed signed fields |
| ingest | `facts.{result, meta, digest, advisory, claims, caller, task_id, agent_id}` | the agent's result JSON and metadata | data (user text, not keys) |
| reconcile | `key_policy`, `sig_ok`, `envelope_json`, `envelope` | as in ingest | as above |
| plan | `facts.signing.agent_session`, `real_key` | booleans; `agent_session` from `SWARM_AGENT_SESSION` and `SWARM_CHILD` (`plan.rs:740-757`) | result |
| plan | `facts.signing.config_error` | `&'static str` from `KeyMaterial::config_error` (`keys.rs:304-306`), a fixed message | result (fixed text) |
| plan | `request.brief_text`, `plan_file`, `plan_files[].text` | brief and plan file contents | data |
| catalog | `args`, `registry`, `skills` | agent and tool metadata, no key field | data |
| transition (P1, reference) | `facts.approval` (five keys) | booleans and gate names only; checked by `shape_drift` (`keys_never_in_guest.rs:1324-1325`) | result |

Where key material is read (all host-side): `crates/gsd-taskwriter/src/approval.rs:104` (`KeyMaterial::from_lookup`), `crates/gsd-adapter/src/write_route/plan.rs:299` (`KeyMaterial::from_env` in `answer_call`), `crates/gsd-adapter/src/write_route/assign.rs:348`. None is inside a guest-bound encoder.

Type guards: `KeyMaterial` (`crates/gsd-envelope/src/keys.rs:120`) has no `Clone` (compile-time check, `crates/gsd-taskwriter/tests/approval.rs:1277-1286`) and no `Serialize` (no impl in `keys.rs`); its `Debug` prints presence flags only (`keys.rs:333-336`). The `Serialize` half of the check is disabled (`keys_never_in_guest.rs:2109`, R11), so a future `Serialize` impl would not be caught by a test (F8).

Teeth: the canary tests detect a planted canary in a request, a reply and an extra key at each level (`the_p2_scan_finds_a_canary_planted_in_a_captured_request_or_reply`, `keys_never_in_guest.rs:1893`).

## 3. Check 2: verified shadow

Result: PASS. In every P2 mode the host computes its own answer first, the guest's bytes are compared for equality only, and the host answer stands on every fault. Only a spent deadline changes the outcome, and it is E-TIMEOUT with nothing written.

| Mode | Host decision | Shadow | Write ordering (checked in code) | Pinned by (R1/R2) |
|---|---|---|---|---|
| approval | `approval_wire::answer` on the facts read inside `BEGIN IMMEDIATE`; `witness` is `None` only on a key-policy refusal (`taskwriter/src/approval.rs:299, 335, 440`) | `SandboxApproval::verify` (`write_route/shadow_approval.rs`) over `Shadow::verify` (`sandbox_shadow.rs:90-118`; equality at `:115`) | `taskwriter/src/apply.rs:351-353` verifies before `decider.decide` and before `write_validated` in the same function | `an_honest_guest_agrees_with_the_host`, `facts_that_differ_from_the_host_answer_are_a_contract_fault`, `without_a_verifier_the_host_path_is_unchanged` |
| ingest | `plan_ingest` (host) and, for an ingest, `plan_reconcile` (host) | `ShadowIngest::check` (`write_route/shadow_ingest.rs`) checks the plan, then the reconcile stage only if the plan agreed | `taskwriter/src/ingest.rs:410` checks before `resolve_and_write` in `run` | `an_honest_guest_verifies_the_plan_and_the_reconcile_stage_and_the_host_answer_stands`, `a_faulty_reconcile_stage_alone_fails_over_and_the_host_answer_stands` (the `ReconcileLie` guest, `ingest.rs:479`) |
| plan | `plan_derive` (host) | `PlanShadow::check` (`write_route/shadow_plan.rs`) | `write_route/plan.rs:474` checks inside the transaction, before `write_plan_ops` at `:531`; the probe-phase check at `:354` covers the non-created outcomes (refusals and dry runs) | `a_faulty_guest_leaves_the_host_answer_in_place_and_records_the_fault`, `a_prepare_failure_answers_from_the_host_without_calling_the_guest`, `the_sandbox_route_prints_the_host_bytes_and_checks_the_creation_once` |
| catalog | `catalog_wire::answer` | none: no host route serves `swarm_tool` (`catalog_wire.rs` header) | no write; nothing to order | `catalog.rs` (11 tests) exercise the shadow over the function, not a route (F3) |
| read (`list`, `history`) | `orch_status_invoke` answers in the guest; no host cross-check | none (GA3, unchanged from the default pin) | no write | `gsd-route/tests/core_invoke.rs:42` asserts `core_invoke` equals `orch_status_invoke` on the read cases |

Dispatch in the guest: `core_invoke` (`gsd-route/src/invoke.rs`) sends a request to a P2 entry only when its `mode` string matches, and everything else to `orch_status_invoke`; the guest entry is `gsd-guest/src/lib.rs:17-20`.

Limitation (F4): the P2 host answers come from the same Rust functions the guest runs. The shadow therefore checks the wire, the codec, the guest identity and the guest's execution. It does not check the decision logic independently. Logic correctness rests on the committed Python oracle vectors (approval 253 cases in `approval.rs:191-193`, plan, catalog, ingest) and the parity harness, which this record does not re-run.

## 4. Check 3: failure paths

Result: PASS. The 56 tests cover every required path in every mode that can reach the guest, and the gaps the 91b0970e record listed as F6 are closed by named tests. Counts come from R4; the cell is the test that pins it, all in `write_route_sandbox_faults2/<mode>.rs`.

| Path | approval (16) | catalog (11) | ingest (13) | plan (16) |
|---|---|---|---|---|
| guest error (`Fail`) | `a_guest_error_is_a_fault_with_its_code` | `a_guest_error_is_a_fault_with_its_code_and_the_host_answers` | `a_guest_error_is_a_fault_with_its_code` | `a_guest_error_is_the_fault_it_reports_and_is_recorded` |
| spent deadline (`Spent`) | `a_spent_deadline_is_a_timeout_and_no_failover`; `a_spent_deadline_during_the_approval_check_rolls_back_and_writes_nothing` | `a_spent_deadline_is_a_timeout_with_no_failover` | `a_spent_deadline_is_a_timeout_and_is_not_recorded`; `a_spent_deadline_inside_the_writer_rolls_back_and_leaves_every_table_unchanged`; `a_spent_deadline_on_the_route_is_e_timeout_with_the_store_and_the_manifest_untouched` | `a_spent_deadline_is_a_timeout_and_no_failover`; `a_spent_deadline_inside_the_transaction_writes_nothing` |
| fuel (`Fuel`) | `fuel_while_time_remains_is_the_guest_fault_internal` | `fuel_before_the_budget_is_spent_is_internal` | `a_stop_before_the_budget_is_spent_is_fuel_and_a_fault` | `fuel_is_a_fault_of_the_guest_not_a_timeout` |
| malformed (`Garbage`) and one-byte change (`Mangled`) | `garbage_and_mangled_replies_are_contract_faults` | `bytes_that_are_not_a_reply_are_a_contract_fault` | `garbage_and_a_mangled_reply_are_contract_faults`; `a_mangled_plan_fails_over_on_the_host_answer_and_records_the_code` | `garbage_and_a_mangled_reply_are_contract_faults` |
| oversized (`Oversized`) | `an_oversized_reply_is_a_capacity_fault` | `an_oversized_reply_is_a_capacity_fault`; `a_reply_above_the_stdout_cap_is_the_same_refusal_on_both_sides` | `an_oversized_reply_is_a_capacity_fault` | `an_oversized_reply_is_a_capacity_fault` |
| facts mismatch (honest guest, other facts) | `facts_that_differ_from_the_host_answer_are_a_contract_fault` | `facts_the_guest_does_not_get_from_the_request_are_a_contract_fault` | `host_facts_that_differ_from_the_encoded_facts_are_a_contract_fault` | `host_facts_that_differ_from_the_encoded_facts_are_a_contract_fault` |
| identity failure (`Prepare`), guest never called | `a_guest_that_cannot_be_prepared_is_a_fault_and_is_never_called` | `a_guest_that_fails_the_identity_check_is_never_asked` | `a_guest_that_fails_its_identity_check_is_never_called` | `an_identity_failure_is_a_fault_and_the_guest_is_never_called` |
| reconcile stage faulted alone | n/a | n/a | `a_faulty_reconcile_stage_alone_fails_over_and_the_host_answer_stands` | n/a |

Write ordering on failure: a spent deadline, and only that, rolls the writer back with no row written (`ingest.rs` spent-deadline tests check every table; `plan.rs` checks the transaction). Every other fault records the code for the notice and the host answer is written, as the shadow contract says.

Residual gaps: the reconcile stage has one fault case (a mangled reply), not a guest error, spent deadline or fuel case of its own. Those go through the same `Shadow::verify`, so the code path is shared, but no test names them (F6, minor).

## 5. Check 4: dependency edges

Result: PASS. The range adds no edge into `gsd-route`, `gsd-guest` or the guest module.

- `Cargo.lock` range diff (R10): 3 insertions, no new package. Edges added: `gsd-adapter -> gsd-envelope`, `gsd-adapter -> regex`, `gsd-taskwriter -> gsd-envelope`.
- `gsd-route/Cargo.toml` and `gsd-codes`: unchanged in range; `gsd-route` depends only on `gsd-codes` (R7).
- `gsd-guest/Cargo.toml` (range diff +3, features only): depends on `gsd-codes`, `gsd-route` (`default-features = false`) and `wit-bindgen`. Its normal+build graph (R6) has no `gsd-envelope`, `regex`, `ed25519-dalek`, `hmac` or `sha2`.
- Reverse edges (R8, `-e all`): `gsd-envelope` has dependents `gsd-adapter` and `gsd-taskwriter` only. `regex` reaches `gsd-adapter` (approved by D-P40), `gsd-governor`, `gsd-observability`, `gsd-pool`, `gsd-proto` and others, none of them guest-bound. `hmac` is reached by `gsd-adapter`, `gsd-envelope`, `gsd-state`, `gsd-taskwriter`, `hkdf`, `sqlx`, not by the guest.
- `ed25519-dalek` reaches `gsd-sandbox` through `gsd-security` (R9). `gsd-security` is unchanged in range and `gsd-sandbox`'s range diff is features only, so the edge predates the range. `gsd-sandbox` is the host loader; the guest module is built from `gsd-guest` alone (`gsd-sandbox/build.rs`), so the edge does not enter the guest.
- `unsafe`: no `unsafe` token in `crates/gsd-route/src`, `crates/gsd-guest/src` or `crates/gsd-codes/src`. Neither crate sets `#![forbid(unsafe_code)]`, and the workspace `Cargo.toml` has no `[workspace.lints]` table (F9, info). The guest's build-time proc macros and `wit-bindgen` generated glue are pre-existing.
- `regex`: not in `gsd-route` or `gsd-guest` (R7, R8). Its internal unsafe use stays the ship-gate open item of D-P40.

Build wiring (checked): `gsd-sandbox/build.rs` builds the nested guest with `--features write-routes-2` when its own `write-routes` feature is on (`build.rs` around line 62) and selects `guest-module-write.sha256` (`WRITE_PIN_FILE`, line 103). `gsd-guest` implies `write-routes` from `write-routes-2`.

## 6. Check 5: pin identity and the re-run

Result: PASS. The P2 write pin reproduces from a clean export of HEAD.

- R5: `CARGO_TARGET_DIR` under scratch, the export of HEAD, `built_guest_digest_matches_the_committed_pin` passes, and the built component digest is `50b06310480fd0dbce7c285ce24191a58eb5cca9bbe86ca28a23e89ea5ba15d4`.
- R12: no `web/` pin covers the write component. `web/jco.pin.json` covers the default component only (`91b0970e...`), so the write guest has no transpile pin. This is recorded as a gap, not a finding against the Rust pin.
- R1, R2, R3: exit codes in section 1. Timing-sensitive tests (the spent-deadline cases) ran once.

## 7. Findings

| ID | Severity | Location | Finding | Blocks a P2 write flip? |
|---|---|---|---|---|
| F1 | minor | `crates/gsd-route/src/approval_wire.rs:6-7` (doc), `:15-16` and `:226`; `ingest_wire.rs:519`; `reconcile_wire.rs:16`, `:547-548`, `:571-573`; parsed at `gate_facts.rs:214-216`, gated at `:537` | P2 requests carry the stored signed envelope text, including top-level `sig` and any `hmac:` tag, in `approval`, `ingest` (through reconcile) and `reconcile`. The doc at `approval_wire.rs:6-7` says nothing here reads a signature, and the canary test asserts absence only for the P1 transition request (`keys_never_in_guest.rs:1344`, reached only through `is_transition_request`). The text is needed: the guest parses it and the answer depends on it (`gate_facts.rs:537`), so stripping it changes the decision logic. No key bytes are present (R1). No exfiltration path seen in code: `gsd-sandbox/tests/guest_imports.rs:89-93` asserts that the embedded guest imports only the allowlisted interfaces (not re-run here), and the reply carries gate names and fixed refusal texts, not the envelope text. | No on security grounds. Before any citation of "approval facts carry booleans only": correct the doc, and either extend the test to P2 or accept the text in writing. Stripping it is a design change, not a fix. |
| F2 | minor | `crates/gsd-adapter/src/strangler_seam.rs:826` (`SELECT * FROM verdicts`), `:908-911` (`tasks`, `verdicts` fields of `OrchStatusRequest`), `:916` (`request_json`); the same query at `dc46279:808` | The read request embeds whole verdict rows (`agent_id`, `findings`, `envelope_json`, `sig`) and task rows. This predates the range and is in the default pin `91b0970e` too. The D-P41 statement that `request_json` "takes mode, agent, script, correlation, paths and counts only" is wrong. The disabled test that would cover it (`keys_never_in_guest.rs:2138-2145`) documents this exact shape. | No for P2. D-P41 must be corrected. The read-route flip (R12) stays blocked until the read request has a shape test and a canary scan. |
| F3 | minor | `crates/gsd-adapter/src/write_route/` (no `swarm_tool` route); `crates/gsd-route/src/catalog_wire.rs` header | The `catalog` mode is in the pinned guest, reachable by request, and has no host route and no shadow. It writes nothing. | No for write routes. Any catalog route must add its shadow first (the 11 catalog tests show the wiring). |
| F4 | minor | `crates/gsd-adapter/src/write_route/shadow_*.rs`; `crates/gsd-route/src/approval_wire.rs`, `ingest_plan.rs`, `plan_derive.rs` | The shadow compares the guest with the host running the same Rust code, so a logic error common to both passes. Correctness of the decision logic rests on the Python oracle vectors and the parity harness, neither re-run here. | No, if the flip evidence includes the parity harness 696-cell sandbox leg (`flip_record.md`). |
| F5 | minor (pre-existing, unchanged) | read route, `failover_code` in `sandbox_engine.rs` | Read answers from the guest are final, with no host cross-check (GA3). | No. Covered by the pin and the identity check. |
| F6 | minor | `crates/gsd-adapter/tests/write_route_sandbox_faults2/ingest.rs:479` | The reconcile stage has one fault case (mangled). Guest error, spent deadline and fuel for that stage have no named test. | No. Add cases before flipping ingest. |
| F7 | info | `crates/gsd-adapter/tests/write_route_sandbox_faults2/` and `gsd-route/tests/core_invoke.rs` | The 56 cases run the in-process guest (`InProcessGuest`), which calls `core_invoke`; they do not run the embedded wasm module. Fuel, deadline and output-size behaviour of the pinned module for the P2 modes are not exercised here. | No. Evidence gap to name in the flip record. |
| F8 | minor | `crates/gsd-adapter/tests/keys_never_in_guest.rs:2099-2110` (disabled), `crates/gsd-taskwriter/tests/approval.rs:1277-1286` | The `Clone` half of the `KeyMaterial` check runs. The `Serialize` half is disabled (R11). Today `KeyMaterial` has no `Serialize` impl, so this is a regression guard that is absent, not a current leak. | No. |
| F9 | info | repo root | Untracked `deny-report.txt` is still present (from the 91b0970e review; not touched). No `[workspace.lints]`, so `unsafe_code` is not enforced workspace-wide. | No. |
| F10 | info | `ingest` and `plan` requests | These carry the agent's result JSON, brief text and plan file text. That is user data the host already stores, not signing material. Not canary-relevant, but it does reach the guest. | No. |

Closed since the 91b0970e record: F1 (P2 bytes in the canary scan) by `d0617a5`, verified by the `p2_writes` module and `p2_catalog_request_and_reply_hold_no_canary`; F6 fault gaps for ingest fuel (`ingest.rs:202`), catalog fuel (`catalog.rs:110`) and catalog malformed replies (`catalog.rs:120`), and the reconcile stage (`ingest.rs:479`), now have named tests (F6 residual above). F3 (regex) by D-P40.

No blocker. No major finding. Minor findings are listed with their fix.

## 8. Corrections needed in other records (not edited here)

1. `STATE.md` D-P41: the claim that the read request carries "counts only" is wrong (F2). The read request embeds verdict rows with `sig` and `envelope_json`.
2. `crates/gsd-route/src/approval_wire.rs:6-7`: "Nothing here reads a key, a signature ..." should say the request carries the signed envelope text, which the guest parses (F1).
3. `tests/parity/records/14.5/flip_record.md` row for the write guest: the "A10 and A11 records for the write guest" row is still "Missing"; this record fills the A10 half. The A11 half (reproduction of `50b06310`) remains open.

## 9. Verdict reasoning

- Keys: no P2 field carries key material. The fields that carry verification results are booleans and fixed texts. The one carried text that involves signatures (F1) is signed data, with no exfiltration path to the guest.
- Shadow: the host decides in every mode, the guest is an equality check, and the host answer stands on every fault. Writes come after the check in each route.
- Failure paths: every required path is pinned by a named test in every mode that reaches the guest.
- Dependencies: no new edge into the guest or `gsd-route`. The pin reproduces from a clean HEAD export.

This supports SECURED for the scope named in the header. It does not clear a flip: the flip needs U3.8c approval, the read-route debt (F2), the catalog shadow before any catalog route (F3), the evidence named in F4 and F7, and the corrections in section 8.

## 10. What I could not verify

- The Python canary test, the `guest_imports` and `write_guest` suites, and the parity harness were not re-run at HEAD in this review.
- The embedded wasm module's behaviour under fuel, deadline and oversize for the P2 modes (F7). No test here runs it.
- Timing: the spent-deadline cases ran once each on a shared host.
- The read route's request content beyond the code read (F2). The disabled test that would cover it is not run.
- The decision logic itself, independently of the shared Rust code (F4). Only the Python oracle vectors cover it, and they were not re-run.
- `regex`'s internal unsafe use (D-P40, ship gate).
- Platforms other than linux x64. No wasm32 target build was run beyond the clean export in R5.
- The `cargo metadata --format-version 1` JSON requested in check 4. I used `cargo tree` (built from the same resolve); the JSON was not captured.
