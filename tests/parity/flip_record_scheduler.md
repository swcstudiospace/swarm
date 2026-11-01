# Flip record: native scheduler (phase 14.4)

**Status: NOT FLIPPED. Python is the default. Every criterion below is pending.** This file is the record template. It asserts no flip, no speed-up, and no passed S6 or S7. Fill a row only from its evidence file, and leave it pending until that file exists.

## State

| Item | State |
|---|---|
| Default engine | `python`. The Python `Scheduler` runs every swarm run. The selector that reads `GSD_ENGINE` in `scripts/swarm_run.py` is not wired yet (unit S3.2); the planned default constant is `SCHEDULER_DEFAULT_ENGINE` |
| Native runtime | `run_scheduler` in `swarm._native` (crates `gsd-sched-core` and `gsd-python`). Opt-in once the selector exists |
| Switch | `GSD_ENGINE`: `python` (default, oracle) or `rust` (native). The scheduler does not read `GSD_ENGINE_BIN` |
| Rollback | One switch: `GSD_ENGINE=python`. `SWARM_PURE_PYTHON=1` also forces Python and wins over `GSD_ENGINE` |
| Architecture | B. Rust owns the worker pool threads and the decision loop. Python keeps session execution, the Task Store, telemetry, leases, handoffs, signals and signing, reached through sixteen callbacks |
| Speed-up | None claimed. The flip needs a qualifying quiet-host S6 with paired p50 and p95 both strictly below 1.00 |
| Explicit approval | None recorded |

## Approved limits

| Limit | Answer | Rule |
|---|---|---|
| CI route | "Scoped feature-push CI" | Push trigger only after local S5 is green; only the literal branch `feat/v2.4-rust-wasm-core` is pushed, by A01/Main; main and PR triggers kept; no PR, protected push, merge or CI waiver |
| Shutdown | "Cooperative callback bound" | The budget starts at the Python-observed `CancelRequest`, not at kernel delivery. Blocked Python callbacks are not force-preempted. Callback and GIL delay count against the budget |
| Panic | "Scoped native panic guard" | The process hook applies only to marked native scheduler work; other panics reach the prior hook. A panic in marked work fails that task with `E-INTERNAL`. No debug API, no new dependency |
| Priority | Plan rule (D-P47) | Unknown or missing priority is P2 |
| Keys | Plan rule (A10) | Keys never enter Rust |

## Criteria

Numbering follows `.planning/phases/14.4-rust-scheduler-runtime-and-default-flip/14.4-CONTEXT.md`. Every status is pending. The tree-state column records what exists in the working tree; it is not evidence.

| # | Criterion | Evidence to attach | Tree state (not evidence) | Status |
|---|---|---|---|---|
| 1 | Core crate: `gsd-sched-core` builds; `cargo fmt --check` and clippy with `-D warnings` clean for `gsd-sched-core`, `gsd-scheduler` and `gsd-python` | fmt and clippy output with exit codes | | pending |
| 2 | Vector parity: every committed and seeded vector equals the Python oracle through the real extension, 0 skipped | `tests/parity/test_scheduler_parity.py` output | file present | pending |
| 3 | Binding: `swarm._native` exports the names with `.pyi` stubs; strict mypy; stubtest passes or the gap is recorded; GIL released | `tests/parity/test_scheduler_binding.py`, mypy and stubtest output | test file present, untracked | pending |
| 4 | Selector, off by default: unset or `python` runs Python; `rust` runs native; any other value is refused with `E-INPUT` before dispatch; `SWARM_PURE_PYTHON=1` wins; a stale extension warns once at selection; both engines log `scheduler_engine: <name>` | selector tests for `run()` (S3.2) | `scripts/swarm_run.py` does not read `GSD_ENGINE`; `tests/test_scheduler_engine_rig.py` only sets child environment variables | pending |
| 5 | Black-box parity: `tests/test_scheduler_continuous.py` under `SWARM_TEST_ENGINE=python` and `=rust`, 0 failures and 0 skips; the Rust leg proves native entry, not fallback; the 15 of 49 cases that do not exercise the native scheduler by selector are mapped in `14.4-INPROCESS.md` | both-leg run output, native-entry marker assertion, the mapping | rig selector present; no native leg yet | pending |
| 6 | Ports parity: telemetry, leases, handoffs, reconcile reruns, `--once`, `--max-rounds`, rounds mode, the internal-failure limit and `extension_down` give the same records on both engines, modulo timestamps and ids | `tests/test_scheduler_ports.py` output | file missing | pending |
| 7 | Cancel and signals: timeout, SIGINT, SIGTERM and force; exit status 128+signum; no orphan process; no flag left; budget respected under the cooperative bound; a stuck worker does not keep the process alive | cancel tests on the Rust leg; S5.4 stuck-worker, delayed-loop/GIL and idle-final-drain handshakes | `tests/parity/test_scheduler_threads.py` missing | pending |
| 8 | Robustness: a worker panic or Python exception fails only its task (`E-INTERNAL`); a loop-thread exception keeps its type and exit mapping; 8 to 32 workers under stress, no deadlock; no Rust lock held across a Python call | `tests/parity/test_scheduler_threads.py`; Rust fake-port tests | file missing | pending |
| 9 | Measured performance, no speed claim: paired Python/Rust pass latency (fake ports, 50 and 500 ready tasks; 12-task fixture) and quiet-host A2, host load recorded; both paired p50 and p95 strictly below 1.00 | S5.2 output; A2 paired records in `tests/perf/baseline.md` (new 14.4 section) and `.planning/a2-quiet-run/` | existing A2 runs are moderate-load (mean load 11.04 in the recorded run) and Python-only; they do not count | pending |
| 10 | Flip evidence (S7 only): quiet-host A2 with both legs; this record complete; user approval recorded in `.planning/STATE.md`; default changed in one place; after the flip `GSD_ENGINE=python` runs the black-box suite green and `SWARM_PURE_PYTHON=1` forces Python | S6 section, this file, STATE entry, post-flip run output | nothing recorded | pending |
| 11 | Existing gates: ruff, strict mypy, registries, `_check_pins.py` unchanged, full pytest, `bun test tests/ts`, `cd omp && bun run test`, parity harness on both legs, `cargo test --workspace --locked` | gate outputs with exit codes | not run for this record | pending |
| 12 | Docs: runbook section "Select the scheduler engine"; AGENTS.md names the `_native` scheduler functions; the limits are stated | runbook and AGENTS.md text, link check | text written; not verified | pending |

## Pipeline steps

| Step | Unit | Evidence | Status |
|---|---|---|---|
| S5.1 | Parity run: both legs of the black-box suite, harness and full gate list; triage and fix | `tests/parity/records/14.4/scheduler-s5.md` | pending |
| S5.2 | Bench: pass latency with fake ports (50 and 500 ready tasks) and the 12-task fake-omp fixture; paired `GSD_ENGINE` leg; `/proc/loadavg` at start and end | `tests/perf/scheduler_engines.py` and `tests/perf/run.sh` outputs in `scheduler-s5.md`. The existing `tests/perf/records/14.4/scheduler-engines-results.txt` is the historical D-P26 benchmark, not S5.2 | pending |
| S5.3 | Docs: runbook section, AGENTS.md, this record | files named in this record | pending |
| S5.4 | Native thread, GIL, stuck-worker, idle-drain and panic tests | `tests/parity/test_scheduler_threads.py` (missing) | pending |
| Local S5 | The parent records local S5 green | `tests/parity/records/14.4/scheduler-s5.md` | pending |
| CI base | `scripts/ci_scheduler_rust.sh`, `.github/workflows/ci.yml`, `tests/test_scheduler_ci.py`; no feature push trigger yet | files and CI run | pending |
| CI feature push | Trigger for `feat/v2.4-rust-wasm-core` only after local S5 green; actual Omega exact-head result and artifacts | `scheduler-s5.md` | pending |
| S6 | Quiet-host paired A2: path (a) and the 12-task fixture, 3 warm-ups and 50 samples per leg, load before, during and after; verdict against criterion 9 | `tests/perf/baseline.md` (new 14.4 section), `.planning/a2-quiet-run/` | pending; blocked on a quiet window (1-minute load below 1.5 for ten minutes) |
| S7 | Flip: change the one default constant; update selector tests, runbook and this record; verify rollback | this record, STATE entry, post-flip runs | pending; blocked on the S6 verdict and explicit user approval |

## Evidence files still to be produced

| File | Holds | State |
|---|---|---|
| `tests/parity/records/14.4/scheduler-s5.md` | S5 closure: parity, both legs, S5.2 bench output, S5.4 results, exact-head Omega result | not present |
| `tests/perf/baseline.md`, new 14.4 section | Paired quiet-host A2 verdict against criterion 9 | not present; the file holds only the 2026-10-25 moderate-load sections |
| `.planning/a2-quiet-run/` paired outputs | Alternating `GSD_ENGINE=python` and `rust` legs with hardware fingerprint and load before, during and after | not produced; the existing `quiet-run.txt` and `load-during.txt` are Python-only and moderate-load |
| `.planning/STATE.md` approval entry | The user's explicit flip approval, recorded by the orchestrator | not recorded |
| Rollback proof | Black-box suite under `GSD_ENGINE=python`; `SWARM_PURE_PYTHON=1` run after the flip | not run (S7) |

## Approval and sign-off

| Item | State |
|---|---|
| Explicit user approval to flip | none |
| Quiet-host window | not reached |
| Flip decision | not made; `python` remains the default |
| Speed-up claim | none (criterion 9 rule) |

## Completing this record

- Attach, for each row, the command, its exit code, the artifact path and its SHA-256.
- Do not mark a row passed from a contended run, a fake-port result or a historical benchmark.
- Keep a row pending until its evidence file exists. A row that fails its criterion is recorded as failed, with the reason.
