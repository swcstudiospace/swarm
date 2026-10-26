# Flip record: `orch_status` read routes

Phase 12, unit U12.10. Records the parity, fault and budget evidence for each read route, the state of its default, and its sign-off status. Operator steps are in [docs/operations-runbook.md](../../docs/operations-runbook.md), section "Select the engine for `orch_status` reads".

Evidence provenance: the case tables and test results below were recorded on 2026-10-25 from the lead's run output: the Python leg and the `--rust` leg of `tests/parity/harness.py`, and the `cargo test` results reported for U12.5 to U12.9. That run output is not retained in the repository, and this unit did not re-run it. The reproduction commands at the end regenerate it. The harness has since gained one static check (the selector's fallback set, in `classifier_quadruple_mirror`), so a current run shows that row one check higher than the tables below.

## Status

| Item | State |
|---|---|
| Default engine | `python` (`GSD_ENGINE` unset) |
| Rust engine | Opt-in: `GSD_ENGINE=rust` with `GSD_ENGINE_BIN` |
| Sandbox backend | Opt-in: `GSD_RUST_BACKEND=sandbox` |
| Default flip of any route | Not approved. Needs a quiet-host A2 baseline (the Phase 14 final-code run was contended, load up to 21.36; see `tests/perf/baseline.md`) and the open A10 and A11 sign-offs |

## One row per route

| Route | Parity | Faults | Budget | Default | Sign-off status |
|---|---|---|---|---|---|
| `orch_status` list (no route flag, for example `--correlation-id`) | Cases `happy_path`, `empty`, `large`, `validation_error` (`--task-id` leg). Python legs pass. Rust leg: 4 checks, all pass. | 11 tests run the list argv; all pass (recorded run). Detail: Fault detail. | Warm invoke median 7.193 ms (latest run) and 8.547 ms (earlier run); p95 9.742 ms and 14.516 ms (n=300), 500-task snapshot. Both runs on a busy host (load 10 to 14 on 8 logical CPUs), so not a quiet-host baseline. No threshold (G-budget). | `python` | A10 runtime sign-off open. A11 pin approval open. A2 baseline waived, not measured. Default flip not approved. |
| `orch_status --history TASK_ID` | Cases `gate_pass_fail_major` (both legs) and `validation_error` (unknown-task leg). Python legs pass. Rust leg: 3 checks, all pass. | 5 tests run the history argv; all pass (recorded run). Detail: Fault detail. | Not measured. The budget record covers the list snapshot only. | `python` | Same as the list route. |

Not a route in this record: `--transition` and `--ingest` (mutating). The binary refuses them with `E-DEP` and the script prints the Python answer. The Rust leg of `transition_legal_illegal` adds 2 checks, both pass. The `policy` case's transition is run by a direct call and has no Rust check.

## Parity detail

Python leg, `python3 tests/parity/harness.py` (recorded 2026-10-25; output not retained):

```
CASE                          ROUTE                       RESULT  CHECKS
happy_path                    orch_status + swarm_tool    pass    3/3
transition_legal_illegal      orch_status                 pass    2/2
gate_pass_fail_major          orch_status                 pass    2/2
boundary_100kb                swarm_tool                  pass    3/3
validation_error              orch_status + swarm_tool    pass    3/3
empty                         orch_status + swarm_tool    pass    2/2
large                         orch_status + swarm_tool    pass    2/2
malformed                     swarm_tool                  pass    3/3
timeout                       swarm_tool                  pass    1/1
policy                        orch_status + swarm_tool    pass    2/2
classifier_quadruple_mirror   errors + retry + omp bridge pass    17/17
11/11 cases pass
```

Rust leg, `GSD_ENGINE_BIN=<repo>/target/debug/gsd_engine python3 tests/parity/harness.py --rust` (recorded 2026-10-25; output not retained):

```
CASE                          ROUTE                       RESULT  CHECKS
happy_path                    orch_status + swarm_tool    pass    4/4
transition_legal_illegal      orch_status                 pass    4/4
gate_pass_fail_major          orch_status                 pass    4/4
boundary_100kb                swarm_tool                  pass    3/3
validation_error              orch_status + swarm_tool    pass    5/5
empty                         orch_status + swarm_tool    pass    3/3
large                         orch_status + swarm_tool    pass    3/3
malformed                     swarm_tool                  pass    3/3
timeout                       swarm_tool                  pass    1/1
policy                        orch_status + swarm_tool    pass    2/2
classifier_quadruple_mirror   errors + retry + omp bridge pass    17/17
11/11 cases pass
```

Rust checks per route, counted from the case bodies in `tests/parity/harness.py`. Each `orch_status` leg that the harness compares adds one check under `--rust`:

- List: `happy_path` (1), `empty` (1), `large` (1), `validation_error` `--task-id bad..id` (1). Total 4.
- History: `gate_pass_fail_major` (2), `validation_error` unknown task (1). Total 3.
- Transition, not a route: `transition_legal_illegal` (2).

The recorded tables give pass or fail per case, not per leg. Which legs the binary answered and which fell back to Python is in the check names. `python3 tests/parity/harness.py --rust --json` prints them, but that output is not retained. [INFERENCE: the check names carry the answer; not observed in this unit.]

## Fault detail

Tests that run a read route. Source files: `crates/gsd-adapter/tests/read_route_faults.rs` (integration, 4 tests) and `crates/gsd-adapter/src/sandbox_engine.rs` (`mod tests` and `mod read_route_faults`, 16 tests). Results are the recorded `cargo test -p gsd-adapter` run: all pass.

| Test | Argv | What it checks |
|---|---|---|
| `reads_match_host_rust_on_an_empty_store` | list, history | Empty store: list and `--history T-9999` each equal host Rust's answer; the list exits 0. The unknown-task outcome is compared with host Rust, not asserted as a refusal. |
| `a_spent_deadline_is_e_timeout` | list | Zero deadline gives `E-TIMEOUT`. |
| `error_messages_carry_no_path_or_secret` | `--transition`, `--history`, list, `--history` | Four failing calls: `--transition T-0000 --to DONE`, `--history T-9999`, a list with a zero deadline, and `--history ../escape`. No message or stdout carries the root path or the secret in the environment overlay. |
| `list_and_history_of_the_500_task_snapshot_match_host_rust` | list, history | 500-task snapshot: guest reply equals host Rust for both. |
| `empty_store_matches_host_rust` | list | Empty store parity; health passes. |
| `one_task_store_matches_host_rust` | list, history | One-task store parity. |
| `unknown_task_is_answered_by_the_guest_with_host_parity` | history | Unknown task: guest error equals host error. |
| `sandbox_fault_fails_over_to_host_rust_on_reads_only` | list (and transition) | Wrong pin: read answered by host Rust; transition refused. |
| `fuel_of_one_million_is_internal_without_a_deadline` | list | Fuel 1e6 with no deadline gives `E-INTERNAL`. |
| `fuel_of_one_million_is_timeout_with_a_deadline` | list | Fuel 1e6 with a deadline gives `E-TIMEOUT`. |
| `four_mib_cap_refuses_the_500_task_request_as_capacity` | list | 4 MiB cap refuses the 500-task request as `E-CAPACITY`. |
| `failover_answer_is_the_host_rust_answer` | list | No guest call. `orch_status_reply`, the in-process answer the failover path uses, equals `RustEngine::orch_status` on the list snapshot for the same argv. |

Mutating argv (not a route row): `a_mutating_argv_is_refused_before_any_read` (integration) and `mutating_argv_is_refused_with_dep` (sandbox). Both refuse with `E-DEP` before any read.

Route-independent: `malformed_input_is_the_guest_input_answer_and_is_not_retried` (raw wire bytes give `E-INPUT`, not retried), `truncated_reply_bytes_map_to_contract`, `pin_mismatch_fails_health_at_the_identity_check`, `verified_guest_passes_health`, `other_entrypoints_refuse_with_dep`, `faults_keep_their_code_and_message`.

## Budget detail

Source: `target/gsd-sandbox/read_route_budget.json`, written by `crates/gsd-sandbox/tests/read_route_budget.rs` (test `read_route_budget_record`). The record covers the `orch_status` list snapshot only.

| Measure | Recorded value |
|---|---|
| Route and size | `orch_status` list, 500 tasks; request 109,665 bytes, reply 238,845 bytes |
| Limits | fuel 1,000,000,000; memory 8 MiB; no deadline |
| Verify and compile | 118.4 ms (latest run); 227.2 ms (earlier run); once per engine |
| Cold instantiate | median 0.016 ms, p95 0.040 ms (n=20, latest run); earlier run median 0.024 ms, p95 0.042 ms |
| Host marshal | median 0.473 ms, p95 0.586 ms (n=300, latest run); earlier run median 0.534 ms, p95 0.655 ms |
| Warm invoke | median 7.193 ms, p95 9.742 ms (n=300, latest run); earlier run median 8.547 ms, p95 14.516 ms |
| Unmarshal | median 0.364 ms, p95 0.442 ms (n=300, latest run); earlier run median 0.389 ms, p95 0.538 ms |
| Fuel per call | 57,812,737 (both runs) |
| Reuse | 1000 calls on one instance at 8 MiB: 1000 of 1000 OK in both runs; total 7,101.9 ms (latest run), 11,943.3 ms (earlier run) |
| Host | AMD EPYC 9354P, x86_64, linux, 8 logical CPUs |
| Load (`/proc/loadavg`) | latest run 11.13 5.75 7.94 at start, 10.12 5.71 7.90 at end; earlier run 13.24 16.72 14.16 at start, 14.22 16.76 14.22 at end |

Load caveat: each run of the read-route budget test regenerates `target/gsd-sandbox/read_route_budget.json`, so the figures change with the load between runs. Both recorded runs were on a busy host (load 10 to 14 on 8 logical CPUs). These figures are not a quiet-host baseline and make no performance claim. G-budget has no threshold, so the gate fails on correctness only. Serde share and thresholds move to D5 after the A2 baseline.

Phase 14 budget run (`cargo test -p gsd-sandbox --test read_route_budget --locked`, 2026-10-25, load 4.64 at start and 4.46 at end): 500 tasks, request 109,665 bytes, reply 238,845 bytes. Warm invoke median 8.006 ms and p95 10.805 ms (n=300); host marshal median 0.444 ms; unmarshal median 0.357 ms; cold instantiate median 0.018 ms (n=20); verify and compile 116.7 ms; dealloc check 1000 of 1000 calls ok. Fuel per call is 69,059,119 on the final guest, against 57,812,737 in the earlier record. The guest changed in the review fixes (its digest pin changed); that is the likely cause, but it was not isolated [INFERENCE]. No threshold (G-budget).

The 109,665-byte request is `snapshot()` in `crates/gsd-sandbox/tests/read_route_budget.rs` (lines 212 to 221), serialised with `serde_json`: a binary-tree DAG of 500 tasks, every third in review. The 140,721-byte request in `.planning/phases/10-wasmer-sandbox-and-omp-validation/10-CONTEXT.md:36` (A10 condition 4, 2026-10-23) is a different 500-task snapshot, whose builder is not in the repository, so the two sizes are not comparable. Fuel per call is not a snapshot-only figure: the same 109,665-byte snapshot gave 57,812,737 in the earlier record and 69,059,119 in the Phase 14 run (see above), so the guest build also moves it [INFERENCE].

## Known differences

Recorded, not fixed in this phase:

- The binary's own answer is always JSON, because it appends `--json`. A fallback to the Python body prints text unless the caller passed `--json`.
- The Rust list path writes no run-log event. The Python path emits `script.orch_status`.
- The binary applies a 600 s per-call deadline (`crates/gsd-adapter/src/bin/gsd_engine.rs`). The selector sets no timeout on the subprocess.
- The binary reads stdin without a size cap. The 100 000-byte cap applies to `--input` only.
- A misspelt `GSD_RUST_BACKEND` fails closed with `E-INPUT` and exit 2, with no Python fallback, under `GSD_ENGINE=rust`. The variable is read only under `GSD_ENGINE=rust`.
- The default route is `python`. `parse_engine(None)` stays `Python`.

## Open items

- A10 runtime sign-off: open. `plan/wasmer-spike.md:654`. Kept as recorded debt (`.planning/phases/10-wasmer-sandbox-and-omp-validation/10-CONTEXT.md:135`).
- A11 approval of the pin change: open. `plan/wasmer-spike.md:55`, `:572`.
- A2 quiet-host performance baseline: not met. The Phase 14 final-code run ran under contention (1-minute load up to 21.36; `tests/perf/baseline.md`). A quiet-host run is required before any default flip.
- G-budget thresholds: none set. Serde share and thresholds wait for the A2 baseline.
- `GSD_RUST_BACKEND=sandbox`: opt-in until the A10 and A11 items are recorded.

## Reproduction

From the repository root:

```
python3 tests/parity/harness.py
cargo build -p gsd-adapter --bin gsd_engine
GSD_ENGINE_BIN="$PWD/target/debug/gsd_engine" python3 tests/parity/harness.py --rust
GSD_ENGINE_BIN="$PWD/target/debug/gsd_engine" python3 tests/parity/harness.py --rust --json
cargo test -p gsd-adapter
cargo test -p gsd-adapter --test read_route_faults
cargo test -p gsd-sandbox --test read_route_budget -- --nocapture
```

The last command writes `target/gsd-sandbox/read_route_budget.json`. Its exact form is inferred from the test file name [INFERENCE: not run by this unit].
