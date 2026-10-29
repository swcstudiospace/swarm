# Flip record: `orch_status` read routes

Phase 12, unit U12.10, refreshed in Phase 14.3, unit R13 (2026-10-29). Records the parity, fault and budget evidence for each read route, the state of its default, the decision taken on each recorded divergence, and the sign-off status. **This document describes the state before the flip: every default below is still Python (engine) and host Rust (backend).** Operator steps and the rollback drill are in [docs/operations-runbook.md](../../docs/operations-runbook.md), section "Select the engine for `orch_status` reads".

Evidence provenance:

- The parity counts below come from the retained **interim** record in `tests/parity/records/14.3/interim-64f5759/` (written by `tests/parity/record_parity.sh`; five legs, each ending `11/11 cases pass`). It is interim because it was taken on 2026-10-28 (23:23 UTC) with a binary built from commit `64f5759`, before the guest-affecting `gsd-route` commits (decision D-P25) and before the flip build, and on a host with a 1-minute load between 25 and 53 on 8 CPUs during the legs (71 at the end of the cargo runs). Parity is a byte comparison and does not depend on load. The evidence for the flip is the same script run on the flip build.
- The tables of the earlier version of this file (recorded 2026-10-25 from run output that was not retained) are replaced. Fault and budget figures that this unit did not re-measure are marked with their date.
- This unit ran no cargo command and no harness leg; it read the retained files and the code and reports what they say.

## Status

| Item | State |
|---|---|
| Default engine | `python`. `DEFAULT_ENGINE = "python"` in `scripts/orch_status.py`; `ORCH_STATUS_READ_DEFAULT = Engine::Python` in `crates/gsd-adapter/src/route_table.rs`. An unset or empty `GSD_ENGINE` resolves through that constant |
| Default backend of the binary | `host`. `DEFAULT_BACKEND = Backend::Host` in `crates/gsd-adapter/src/bin/gsd_engine.rs`. An unset or empty `GSD_RUST_BACKEND` resolves to it |
| Rust engine | Opt-in: `GSD_ENGINE=rust` (binary from `GSD_ENGINE_BIN`, else `<repo>/target/release/gsd_engine`) |
| Sandbox backend | Opt-in: `GSD_RUST_BACKEND=sandbox` |
| Rollback | One switch: `GSD_ENGINE=python`. `SWARM_PURE_PYTHON=1` also forces pure Python and wins over `GSD_ENGINE` |
| Default flip of any route | **Not done and not approved.** Blocked on (1) a quiet-host A2 baseline (1-minute load below 1.5 for ten minutes; the Phase 14 final-code run was contended, load up to 21.36, see `tests/perf/baseline.md`) and (2) the user's explicit approval at the flip. Parity evidence on the flip build is also still to be retained |

## One row per route

Counts are per check, taken from `tests/parity/records/14.3/interim-64f5759/rust-host.json` (identical `served_by` per check in `rust-sandbox.json` and `rust-host-wal.json`). See "Parity evidence" for what a check is and what `rust`, `fallback:<CODE>` and `python` mean.

| Route | Parity (interim record) | Faults | Budget | Default | Sign-off status |
|---|---|---|---|---|---|
| `orch_status` list (no route flag, for example `--correlation-id`) | 4 list argv in the Rust legs: `rust` 3 (`happy_path`, `empty`, `large`), `fallback:E-INPUT` 1 (`--task-id bad..id`). All four print Python's bytes. The default selector answers each of the four with `python` (binary not started) | `read_route_faults` 4 of 4 passed in the interim run (dev profile); the other tests below were recorded earlier. Detail: Fault detail | Warm invoke median 7.193 ms (latest 2026-10-25 run) and 8.547 ms (earlier), p95 9.742 ms and 14.516 ms (n=300), 500-task snapshot, busy host, no threshold (G-budget). Detail: Budget detail | `python` | A10 runtime and A11 pin: signed off by the user (see Sign-off record). A2 baseline not measured. Flip not approved |
| `orch_status --history TASK_ID` | 3 history argv in the Rust legs: `rust` 0, `fallback:E-DEP` 2 (`T-gate-pass`, `T-gate-fail`: signed verdict rows, divergence D2), `fallback:E-INPUT` 1 (`T-parity-missing`: unknown task). All three print Python's bytes through the fallback. **No retained check had Rust answer a history call.** The default selector answers each with `python` | 5 tests run the history argv (earlier recorded run) | Not measured. The budget record covers the list snapshot only | `python` | Same as the list route |

Not a route in this record: `--transition` and `--ingest` (mutating). In the Rust legs `transition_legal_illegal` sends two `--transition` argv: both `fallback:E-DEP`, both print Python's bytes (the one legal transition masks the wall-clock `updated_at` on both sides and nothing else). Under a default flip the selector's pre-filter (D5) keeps these calls from starting the binary at all.

## Parity evidence

Legs, as `tests/parity/record_parity.sh` runs them (one binary, `GSD_ENGINE_BIN`). Every leg ends `11/11 cases pass`:

| Leg | Command (harness) | Checks | `served_by` counts |
|---|---|---|---|
| `python` | `python3 tests/parity/harness.py` | 42 | `python` 42 |
| `rust-host` | `python3 tests/parity/harness.py --rust --backend host` | 81 | `rust` 10, `fallback:E-DEP` 17, `fallback:E-INPUT` 3, `python` 51 |
| `rust-sandbox` | `python3 tests/parity/harness.py --rust --backend sandbox` | 81 | `rust` 10, `fallback:E-DEP` 17, `fallback:E-INPUT` 3, `python` 51 |
| `python-wal` | the `python` leg with `SWARM_TASKSTORE_WAL=1` | 42 | `python` 42 |
| `rust-host-wal` | the `rust-host` leg with `SWARM_TASKSTORE_WAL=1` | 81 | `rust` 10, `fallback:E-DEP` 17, `fallback:E-INPUT` 3, `python` 51 |

The `python` and `python-wal` text tables are byte-identical, and so are `rust-host`, `rust-sandbox` and `rust-host-wal`. In the sandbox leg every check carries the same `served_by` as in the host leg, and a sandbox failover would show as `fallback:<CODE>` (the harness reads the binary's failover notice from stderr); none appears, so no failover took place on those checks.

What a check and a `served_by` value are:

- The counts are **per check, not per route call**. One `orch_status` argv in a Rust leg produces two checks: the binary's answer (or, where the binary refuses with a fallback code, the answer of the script with `GSD_ENGINE=rust`) and the default selector (`GSD_ENGINE` unset, `GSD_ENGINE_BIN` set), each compared with Python's bytes and exit code. The truth leg's own checks (the same 42 as the `python` leg) and the static and mirror checks of `classifier_quadruple_mirror` are counted too. For the 81 checks of a Rust leg: 42 truth-leg checks (`python`), 9 answer checks (`rust` 3, `fallback:E-DEP` 4, `fallback:E-INPUT` 2), 9 default-selector checks (`python`), 21 mirror probes (`rust` 7, `fallback:E-DEP` 13, `fallback:E-INPUT` 1).
- `rust`: the `gsd_engine` binary answered and its bytes and exit code are Python's.
- `fallback:<CODE>`: the binary refused with that fallback code (or the sandbox backend failed over and said so on stderr), and the printed answer came from the fallback, which is the Python body. Python's bytes were compared.
- `python`: the Python script answered. This covers every check of the truth leg, the static checks, and the default-selector checks **while the default is Python** (the harness's recording wrapper confirms the selector never started the binary; the check fails if the default were Rust and the call were served by Python, or the reverse).
- The 21 mirror probes are the argv corpus of the `rust_serves` pre-filter check (D5): the selector's `rust_serves` must agree with the binary's own routing for each. 8 argv are served (`rust` 7, plus `--history T-1 --json`, which the binary serves and then refuses as an unknown task with `E-INPUT`), 13 are refused with `E-DEP`.

Python leg table (`python.txt`):

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
classifier_quadruple_mirror   errors + retry + omp bridge pass    19/19
11/11 cases pass
```

Rust leg table (`rust-host.txt`; `rust-sandbox.txt` and `rust-host-wal.txt` are byte-identical):

```
CASE                          ROUTE                       RESULT  CHECKS
happy_path                    orch_status + swarm_tool    pass    5/5
transition_legal_illegal      orch_status                 pass    6/6
gate_pass_fail_major          orch_status                 pass    6/6
boundary_100kb                swarm_tool                  pass    3/3
validation_error              orch_status + swarm_tool    pass    7/7
empty                         orch_status + swarm_tool    pass    4/4
large                         orch_status + swarm_tool    pass    4/4
malformed                     swarm_tool                  pass    3/3
timeout                       swarm_tool                  pass    1/1
policy                        orch_status + swarm_tool    pass    2/2
classifier_quadruple_mirror   errors + retry + omp bridge pass    40/40
11/11 cases pass
```

Facts of the interim run (`facts.txt`): binary `/root/.cache/gsd-redact-work/target-rel/release/gsd_engine`, sha256 `10238b571383886703ebf8b874eca697227dde9d7f7dd67530e638c41866db9f`, 18,459,528 bytes, built from commit `64f5759` in a clean worktree (`cargo build -p gsd-adapter --bin gsd_engine --release --locked`, 65 min 16 s on the loaded host); the legs ran from the working tree at `53df964` plus 33 uncommitted paths, which the binary does not contain; guest digest `ffa7b28a7879da7ea99a20c83999d0b79e757bd084878909dcce0054fe66cade`; Python 3.12.3; Linux 6.8.0-147-generic x86_64, 8 CPUs; 1-minute load at the start and end of the legs between 25.27 and 53.27.

## Divergence decisions

The five divergences recorded in the Phase 9 summary (section 3.7) and the roadmap as D1 to D5. Not to be confused with the budget gate "D5" under Budget detail. Each decision is the phase's default; the user can overrule it at the flip approval. "Status" is what the code and the retained records show today, checked while writing this section.

| # | Divergence | Decision | Evidence | Status today |
|---|---|---|---|---|
| D1 | The Rust read wrote no `events.jsonl` record. Python's `AgentScript.main` emits `script.orch_status` for every result; the substrate tee forwards `script.*` as `tool.call` and `scripts/receipt_export.py` lists them | **Fix** in the Python selector: after a Rust answer it writes the same record through the same `emit` path, so the run log, the tee and receipts are unchanged. Reason: the record is an observable side effect of Python, and other consumers read it | `swarm/script_base.py` `AgentScript.record_external_reply` (called from `run_rust` in `scripts/orch_status.py`, before the answer is printed). `tests/test_script_base_record_reply.py` (27 tests): `test_external_reply_record_equals_the_python_path_record` (list, ids carried, dry run, error reply: identical records with `ts` and `msg_id` masked; tee count doubles, dry run stays local), `test_dry_run_is_not_teed_and_is_still_written_locally`, `test_a_write_failure_propagates_and_leaves_the_existing_log_alone`. `tests/test_orch_status_default_engine.py`: `test_rust_answer_writes_the_run_log_record_the_python_path_writes` (list, ids carried, dry run), `test_rust_answer_is_printed_verbatim_and_recorded_once` (exit 0 and 1 give `script.orch_status`, exit 2 gives `script.orch_status.error`), `test_rust_answer_whose_record_cannot_be_written_is_left_to_the_python_body`. `tests/test_orch_status_rollback.py`: one record per call after a served call and after a rollback | **Fixed**, selector side. Limits: those tests answer with a fake binary that replays Python's bytes, so they prove record equality, not Rust's output; the retained parity records contain no run-log check. The binary itself still writes no record: only a call that goes through `scripts/orch_status.py` is recorded. The selector's own refusals (`E-TIMEOUT` on a wedged binary, `E-DEP` on an explicit `rust` with no startable binary, `E-INPUT` on a bad `GSD_ENGINE` or `GSD_RUST_BACKEND`) print an error reply and write no record |
| D2 | A verdict row with an `envelope_json` is refused `E-DEP` by the Rust engine; the selector then falls back to Python, which verifies the signature | **Accept.** Rust must not guess a verification. `verify_envelope` in the binary needs the key-custody decision (X-13, answered in D-P24) and belongs to Phase 14.5. The cost is recorded: such calls pay one failed Rust attempt before Python answers; the A2 baseline is to measure that path | `crates/gsd-route/src/lib.rs` `missing_reasons`: any non-empty, non-null `envelope_json` returns `E-DEP`, message "envelope signature verification is not implemented in the rust engine; use GSD_ENGINE=python". It is called by `history_fields` for every `--history` call and, in the list, only for tasks in `IN_REVIEW` (`status_row`: `if task.state == "IN_REVIEW"`). Record: `rust-host.json`, `gate_pass_fail_major`, `--history T-gate-pass` and `--history T-gate-fail`, both `fallback:E-DEP`, both print Python's bytes. `tests/test_orch_status_rollback.py` builds a store with the same two signed review verdicts and runs a `--history T-gate-fail` call through every opt-out. Rust test: `signed_verdict_rows_are_refused_not_guessed` (`crates/gsd-adapter/src/strangler_seam.rs`) | **Accepted, in force.** Every `--history` on a task with a signed gate verdict is answered by Python even after the flip. The list is answered by Rust unless an `IN_REVIEW` task carries a signed row [INFERENCE: read from `status_row`; no retained record has an `IN_REVIEW` task with a signed row] |
| D3 | The Rust error envelope carries `details: {}` and no `trace`. Python's typed `SwarmError` also prints `details` (empty on the read paths); `trace` exists only for untyped exceptions, as a Python traceback with file paths | **Accept.** A traceback has no Rust equivalent and leaks paths. Codes, messages, `handling` and exit codes are the contract | `crates/gsd-route/src/lib.rs` `SeamError::to_json` renders `code`, `message`, `details`, `handling` and nothing else; `details` is `{}` unless `with_details` is called (no read path calls it). Unit tests `seam_error_renders_the_error_object` and `seam_error_details_follow_swarm_error_keyword_arguments`. Python side: `swarm/script_base.py` `main`, the untyped `except Exception` branch, adds `trace` | **Accepted, in force.** Note on the comparison: in the retained records every Rust error on a read was a fallback code, so Python printed the bytes; the harness compares the binary's error **code** with the fallback set and Python's bytes with the script's, not Rust's error message and `handling` byte for byte. Rust's messages are compared with host Rust's and checked for paths and secrets in `error_messages_carry_no_path_or_secret` |
| D4 | "A timeout SIGKILLs without SIGTERM grace" (a `child.kill()` in `strangler_seam.rs`) | **Closed as fixed; the remaining case is accepted.** `PythonEngine` runs through `gsd-sdk`, whose runner sends group SIGTERM and then SIGKILL after `KILL_GRACE_MS` (3000). The selector's outer `subprocess.run(timeout=660)` SIGKILLs the `gsd_engine` binary, which spawns no child and answers a read, so there is nothing to clean up | No `.kill()` remains in `crates/gsd-adapter/src/strangler_seam.rs`; `run_through_sdk` there calls the SDK runner. `KILL_GRACE_MS = 3_000` (`crates/gsd-sdk/src/omp_constants_mirror.rs`). Tests (not run by this unit): `python_engine_timeout_stops_the_whole_process_group` (`strangler_seam.rs`), `child_that_ignores_sigterm_is_killed_after_the_grace_period` (`crates/gsd-sdk/src/omp_bridge_runner.rs`). Selector side: `tests/test_orch_status_review_fixes.py::test_wedged_rust_binary_reports_e_timeout_and_never_falls_back`, `tests/test_orch_status_default_engine.py::test_wedged_binary_is_e_timeout_and_never_falls_back` and `::test_call_deadline_is_660_seconds_unless_a_positive_integer_is_set`; the rollback drill's `hang` binary | **Closed.** The Phase 9 summary line and the scout map are stale on this item. `PythonEngine` is not reachable from the binary on the `rust` path. That the store DDL is idempotent and transactional, so a SIGKILL of a read leaves nothing, is [INFERENCE] |
| D5 | Non-JSON and non-read argv are refused `E-DEP` under Rust. Under a default flip every `--transition`, `--ingest` and text-mode call would start the binary, be refused, and then run Python | **Accept the refusal; add a selector pre-filter.** A default-resolved call reaches the binary only when its argv is a read route with `--json`; everything else runs Python directly. Rust text mode is not built. Explicit `GSD_ENGINE=rust` still sends every call to the binary | `scripts/orch_status.py` `rust_serves`. The binary's side: `read_args` in `strangler_seam.rs` and `is_read_only_route` in `route_table.rs`. `tests/test_orch_status_default_engine.py`: `test_rust_serves_accepts_the_read_json_grammar`, `test_rust_serves_refuses_everything_else`, `test_default_call_never_starts_the_binary_for_an_argv_it_does_not_serve`. Record: the 21 mirror probes of `classifier_quadruple_mirror` (8 served, 13 refused `E-DEP`/`E-INPUT` as `rust_serves` predicts) and the `--transition` checks (`fallback:E-DEP` under explicit `rust`) | **Implemented, and dormant until the flip:** `select_engine` returns before it looks for a binary while `DEFAULT_ENGINE` is `"python"`, so no default call reaches `rust_serves` today. The unit tests set `DEFAULT_ENGINE` to `"rust"` to exercise it. The harness derives what the default-selector check expects from `DEFAULT_ENGINE` and `rust_serves`, so the same check holds before and after the flip |

## Fault detail

Tests that run a read route. Source files: `crates/gsd-adapter/tests/read_route_faults.rs` (integration, 4 tests), `crates/gsd-adapter/src/sandbox_engine.rs` (29 `#[test]` functions in this tree, counted, not run by this unit; `mod tests` and `mod read_route_faults`), `crates/gsd-adapter/src/bin/gsd_engine.rs` (8) and `crates/gsd-adapter/src/route_table.rs` (11). Results of the rows recorded before 2026-10-28 are the 2026-10-25 `cargo test -p gsd-adapter` run (all pass). `read_route_faults` was re-run in the interim record (`cargo-read-route.txt`, dev profile, HEAD `53df964` plus uncommitted files): 4 passed, 0 failed.

| Test | Argv | What it checks |
|---|---|---|
| `reads_match_host_rust_on_an_empty_store` | list, history | Empty store: list and `--history T-9999` each equal host Rust's answer; the list exits 0. The unknown-task outcome is compared with host Rust, not asserted as a refusal. |
| `a_spent_deadline_is_e_timeout` | list | Zero deadline gives `E-TIMEOUT`. |
| `error_messages_carry_no_path_or_secret` | `--transition`, `--history`, list, `--history` | Four failing calls: `--transition T-0000 --to DONE`, `--history T-9999`, a list with a zero deadline, and `--history ../escape`. No message or stdout carries the root path or the secret in the environment overlay. |
| `a_mutating_argv_is_refused_before_any_read` | `--transition` | Refused `E-DEP` before any read. |
| `list_and_history_of_the_500_task_snapshot_match_host_rust` | list, history | 500-task snapshot: guest reply equals host Rust for both. |
| `empty_store_matches_host_rust` | list | Empty store parity; health passes. |
| `one_task_store_matches_host_rust` | list, history | One-task store parity. |
| `unknown_task_is_answered_by_the_guest_with_host_parity` | history | Unknown task: guest error equals host error. |
| `sandbox_fault_fails_over_to_host_rust_on_reads_only` | list (and transition) | Wrong pin: read answered by host Rust; transition refused. |
| `fuel_of_one_million_is_internal_without_a_deadline` | list | Fuel 1e6 with no deadline gives `E-INTERNAL`. |
| `fuel_of_one_million_is_timeout_with_a_deadline` | list | Fuel 1e6 with a deadline gives `E-TIMEOUT`. |
| `four_mib_cap_refuses_the_500_task_request_as_capacity` | list | 4 MiB cap refuses the 500-task request as `E-CAPACITY`. |
| `a_pin_mismatch_fails_over_to_the_host_answer`, `a_capacity_fault_on_a_read_fails_over_to_the_host_answer`, `fuel_without_a_deadline_fails_over_to_the_host_answer` | list | Each sandbox fault (wrong pin, 4 MiB cap, fuel with no deadline) is answered by host Rust with the host's own reply. |
| `fuel_with_a_deadline_is_e_timeout_and_does_not_fail_over`, `a_guest_invoke_error_is_the_answer_and_not_a_host_answer` | list | `E-TIMEOUT` does not fail over; an error the guest itself reports is the answer. |

Failover notice (Phase 14.3 R3b). When the sandbox backend fails over to host Rust, `gsd_engine` writes exactly one line to stderr, `gsd_engine: sandbox fault <CODE>; answered by host Rust`, built by `failover_notice` in `sandbox_engine.rs` from the wire code only (no path, message or environment value). Stdout and the exit code do not change. A guest answer (an error it reports included) and `E-TIMEOUT` are final and print no notice. Tests: `failover_notice_text_is_fixed_for_every_code`, `failover_code_is_the_fault_code_and_none_for_final_outcomes`, `a_failover_is_recorded_with_its_code_and_the_answer_is_the_host_answer`, `a_guest_answer_records_no_failover`, `a_later_refused_call_clears_the_failover_record`, `a_timeout_records_no_failover`, `a_guest_answer_under_the_production_limits_records_no_failover` (`sandbox_engine.rs`); `a_failover_prints_one_fixed_line_to_stderr_and_leaves_stdout_alone`, `exit_codes_are_the_replys_with_or_without_a_notice`, `the_host_backend_carries_no_notice_and_a_notice_is_passed_through` (`gsd_engine.rs`).

Default tables (Phase 14.3 R3a, R3b), each pinned by a test that the flip unit edits and no other: `read_route_default_is_python_until_the_flip` (`route_table.rs`) and `default_backend_is_host_until_the_flip` (`gsd_engine.rs`). Also in `route_table.rs`: `unset_and_empty_engine_default_to_python`, `unset_and_empty_resolve_through_the_route_table`, `unset_and_empty_on_a_non_read_route_resolve_to_python`, `every_non_read_route_defaults_to_python_whatever_the_read_row_says`.

Mutating argv (not a route row): `a_mutating_argv_is_refused_before_any_read` (integration) and `mutating_argv_is_refused_with_dep` (sandbox). Both refuse with `E-DEP` before any read.

Route-independent: `malformed_input_is_the_guest_input_answer_and_is_not_retried` (raw wire bytes give `E-INPUT`, not retried), `truncated_reply_bytes_map_to_contract`, `pin_mismatch_fails_health_at_the_identity_check`, `verified_guest_passes_health`, `other_entrypoints_refuse_with_dep`, `faults_keep_their_code_and_message`.

## Budget detail

Source: `target/gsd-sandbox/read_route_budget.json`, written by `crates/gsd-sandbox/tests/read_route_budget.rs` (test `read_route_budget_record`). The record covers the `orch_status` list snapshot only. The figures below are the 2026-10-25 runs. The interim record re-ran the test (`cargo-read-route.txt`: 1 passed, 119.19 s, dev profile, 1-minute load 71.38 at the end); its figures were not retained, and the file in `target/` is overwritten by every run, so no figure from 2026-10-28 is quoted.

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

Load caveat: each run of the read-route budget test regenerates `target/gsd-sandbox/read_route_budget.json`, so the figures change with the load between runs. Both recorded runs were on a busy host (load 10 to 14 on 8 logical CPUs). These figures are not a quiet-host baseline and make no performance claim. G-budget has no threshold, so the gate fails on correctness only. Serde share and thresholds move to the budget gate "D5" after the A2 baseline.

Phase 14 budget run (`cargo test -p gsd-sandbox --test read_route_budget --locked`, 2026-10-25, load 4.64 at start and 4.46 at end): 500 tasks, request 109,665 bytes, reply 238,845 bytes. Warm invoke median 8.006 ms and p95 10.805 ms (n=300); host marshal median 0.444 ms; unmarshal median 0.357 ms; cold instantiate median 0.018 ms (n=20); verify and compile 116.7 ms; dealloc check 1000 of 1000 calls ok. Fuel per call is 69,059,119 on the final guest, against 57,812,737 in the earlier record. The guest changed in the review fixes (its digest pin changed); that is the likely cause, but it was not isolated [INFERENCE]. No threshold (G-budget).

The 109,665-byte request is `snapshot()` in `crates/gsd-sandbox/tests/read_route_budget.rs`, serialised with `serde_json`: a binary-tree DAG of 500 tasks, every third in review. The 140,721-byte request in `.planning/phases/10-wasmer-sandbox-and-omp-validation/10-CONTEXT.md:36` (A10 condition 4, 2026-10-23) is a different 500-task snapshot, whose builder is not in the repository, so the two sizes are not comparable. Fuel per call is not a snapshot-only figure: the same 109,665-byte snapshot gave 57,812,737 in the earlier record and 69,059,119 in the Phase 14 run, so the guest build also moves it [INFERENCE].

The guest is verified and compiled in every `gsd_engine` process (118 to 227 ms above; no compile cache exists in `gsd-sandbox`, and `answer()` builds a new `SandboxEngine` per call [INFERENCE: not measured per process]). Path (c) of the A2 baseline (`orch_status` list through the script) is about 131 ms in Python today. Whether the sandbox should become the default backend if it is slower is the user's question Q5 at the flip gate.

## Known differences

Corrected in Phase 14.3 R13 against the code. Recorded, not fixed:

- The binary does **not** append `--json`. `crates/gsd-adapter/src/bin/gsd_engine.rs` passes `argv` through unchanged and serves only an argv that contains `--json` (`read_args` in `strangler_seam.rs`, `is_read_only_route` in `route_table.rs`); any other argv is refused `E-DEP`. The SDK bridge and the harness' direct probe append `--json` themselves, and the selector forwards the caller's argv. The earlier statements in this file, in the runbook and in the harness docstring that the binary appends `--json` were wrong.
- A fallback to the Python body runs `scripts/orch_status.py` with the caller's argv, so it prints text unless the caller passed `--json`. This can only happen under explicit `GSD_ENGINE=rust`: a default-resolved call without `--json` never starts the binary (D5).
- The Rust list path used to write no run-log event (the Python path emits `script.orch_status`). For calls through `scripts/orch_status.py` it now does: the selector records every Rust answer (D1). The binary run directly still writes none.
- The selector **does** set a timeout on the subprocess: 660 s (`RUST_CALL_TIMEOUT_S` in `scripts/orch_status.py`), 60 s above the binary's own 600 s per-call deadline, which covers neither its start-up nor its stdin read. `GSD_RUST_TIMEOUT_S`, a positive ASCII integer number of seconds, overrides it; any other value is ignored. A timeout prints `E-TIMEOUT`, exit 2, and never falls back. The earlier statement "the selector sets no timeout" was wrong.
- The binary reads stdin without a size cap. The 100 000-byte cap applies to `--input` only.
- A misspelt `GSD_RUST_BACKEND` fails closed with `E-INPUT` and exit 2, with no Python fallback, whenever the selector is about to use the binary (explicit `rust`, or a default-resolved call that reaches it). It is ignored when Python answers without the binary (`GSD_ENGINE=python`, `SWARM_PURE_PYTHON=1`, a default call that does not reach the binary). Under explicit `rust` the backend is checked before a missing binary is reported.
- The binary is found through `GSD_ENGINE_BIN`, else `<repo>/target/release/gsd_engine` when that file is executable. The earlier statement that `GSD_ENGINE_BIN` is required under `rust` is out of date: without it and without a release binary, explicit `rust` exits 2 with `E-DEP`. The wheel does not ship the binary.
- The default route is `python`, in three places that the flip changes together: `DEFAULT_ENGINE`, `ORCH_STATUS_READ_DEFAULT` and `DEFAULT_BACKEND`. `parse_engine(None)` stays `Python`; the per-route table (`default_engine`) decides for unset or empty `GSD_ENGINE`, and every route other than the `orch_status` read route is Python.

## Sign-off record

- Signer: the user. Date: 2026-10-26, in chat ("A10 runtime sign-off and A11 pin approval accepted by the user for ship"); an agent does not sign for either role. Scope as recorded on 2026-10-28 (the user's answer "Covers digest and runtime"): the wasmtime runtime (A10) and the guest digest `ffa7b28a7879da7ea99a20c83999d0b79e757bd084878909dcce0054fe66cade`, `crates/gsd-sandbox/guest-module.sha256` (A11).
- Two exceptions accepted by the user on 2026-10-28: the pin landed inside the combined commit `eb7a2b9` instead of its own commit, and `.github/workflows/ci.yml` has no guest-digest step (it runs `scripts/_check_pins.py`; the guest digest is asserted by a Rust test in `crates/gsd-sandbox/src/lib.rs`).
- The sign-off is valid only while `guest-module.sha256` equals `ffa7b28a…`. This record's interim binary was built before the guest changed (see Known open item). Whether the sign-off also covers making the sandbox the default backend is the flip approval's question; the user's approval of the flip itself has not been given.

## Known open item

Decision D-P25 (2026-10-28): the committed default-guest pin test `built_guest_digest_matches_the_committed_pin` (`crates/gsd-sandbox/src/lib.rs`) has been red since commit `f87dab7`. The guest links `gsd-route`; the `py_float` halfway-rounding fix and `SeamError::with_details` in that commit changed the guest bytes, so a clean build of the tree gives digest `7daf6274710afa9076c35de1705c1ce670dd85a87b4bfef0f96d48ea5a841cda` against the approved `ffa7b28a…cade`. No pin file was changed. The re-pin is folded into the P1 pin of Phase 14.5 (one re-pin per batch; it needs its own A11 approval). Until then the test stays red and is listed as a known failure in every gate report, and the ship does not open while it is red. The parity evidence for the flip must be taken on the flip build, and its guest digest recorded with it. This unit did not run the test.

## Open items

- A2 quiet-host performance baseline: not met. The Phase 14 final-code run ran under contention (1-minute load up to 21.36; `tests/perf/baseline.md`). A quiet-host run (load below 1.5 for ten minutes) is required before any default flip; the A2 harness legs for it exist (`tests/perf/run.sh`, `tests/perf/watch_quiet.sh`) and have not been run on a quiet host.
- The user's approval of the flip, with the open questions of the phase (binary discovery, D1, quiet window, sign-off scope, sandbox speed, misspelt backend, failover notice, empty `GSD_ENGINE`).
- Parity evidence on the flip build: the five legs of `record_parity.sh`, retained under `tests/parity/records/14.3/`.
- G-budget thresholds: none set. Serde share and thresholds wait for the A2 baseline.
- `GSD_RUST_BACKEND=sandbox` stays opt-in until the flip approves the default; the A10 runtime and A11 pin sign-offs are recorded (see above) and no longer the blocker.
- The D-P25 re-pin (see Known open item).

## Reproduction

From the repository root. The harness legs need `import swarm._native` (`maturin develop`) because the selector legs run the Python body without `SWARM_PURE_PYTHON`.

```
python3 tests/parity/harness.py
cargo build -p gsd-adapter --bin gsd_engine --release --locked
GSD_ENGINE_BIN="$PWD/target/release/gsd_engine" python3 tests/parity/harness.py --rust --backend host
GSD_ENGINE_BIN="$PWD/target/release/gsd_engine" python3 tests/parity/harness.py --rust --backend sandbox --json-out out.json
tests/parity/record_parity.sh "$PWD/target/release/gsd_engine" tests/parity/records/14.3/<name>
cargo test -p gsd-adapter --locked
cargo test -p gsd-adapter --test read_route_faults --locked
cargo test -p gsd-sandbox --test read_route_budget --locked -- --nocapture
python3 -m pytest tests/test_orch_status_default_engine.py tests/test_orch_status_rollback.py tests/test_script_base_record_reply.py tests/test_orch_status_review_fixes.py -q
```

`record_parity.sh` runs the five legs of the table above and writes `facts.txt`, `<leg>.txt` and `<leg>.json` into the output directory; `GSD_BINARY_BUILT_FROM` names the commit the binary was built from when it is not this tree's HEAD. The last `cargo test` command writes `target/gsd-sandbox/read_route_budget.json`.
