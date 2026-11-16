# Product goal: non-inline 30-slot subagent orchestration

Path choice: `docs/GOAL.md` is the decision record for this request. It is not a GSD schema file. The planner's fallback `docs/PRODUCT_GOAL.md` is discarded. The fallback crate names `workflowz-core` and `workflowz-py` are also discarded: workspace discovery found an existing layout, and the spec says to follow it rather than add a second product beside the orchestrator.

## Discovery (found, not invented)

Searched this checkout before any new crate.

| Looked for | Found |
|---|---|
| Workspace | `Cargo.toml`, `pyproject.toml`, `rust-toolchain.toml` |
| PyO3 | `crates/gsd-python` builds `swarm._native`; Python calls Rust |
| Pool and scheduler | `crates/gsd-sched-core` (std threads, no tokio in the scheduler core); Python `Scheduler` / `WorkerPool` in `scripts/swarm_run.py` |
| Cap | `--max-parallel` default 4, ceiling 32; per-agent `max_parallel` in `agents.json`; 30 agent roles A01–A30 |
| CLI agents this runner starts | `claude`, `grok`, `omp` (`scripts/swarm_run.py`, `--runtime`) |
| GSD-Core | workflows under `$HOME/.agents/gsd-core`; this repo is driven by those workflows, not a fork of them |
| Official GSD-Core CLI-agent catalog | not found as a file in this checkout. Live adapters beyond claude, grok, and omp are not claimed |

`tokio` and `rayon` appear elsewhere in the workspace. The scheduler core does not use them. That pin is kept: std threads for the pool, not a new async runtime.

## Problem

A parent session that programs a declared workspace must be able to decompose SDLC work and supervise a bounded pool of subagents without being forced to work in-line. The operator is a GSD-Core session or an engineer driving the Python tools in this repo.

## What "program our codebases" means here

In scope: accept structured tasks, decompose them under a fail-closed dependency rule, schedule them onto pooled workers, and collect status. A quality or pattern task runs only as an explicit agent task inside a declared workspace root. The orchestrator checks terminal status. It does not start a linter, rewrite unrelated trees, or grow a second product.

## Pool of 30

30 is the maximum simultaneous pooled workers, not 30 invented role personas. The 30 named roles already exist (A01–A30). A 31st in-flight worker is a bug. Past the configured cap, work queues or is refused; it is not spawned unbounded. The global ceiling in this repo is 32 (`SWARM_MAX_PARALLEL_CEILING`), which is above 30. The per-run cap defaults to 4. Neither number is a new default from this document.

## Non-inline submit versus wait

Submit (plan plus dispatch) returns control after accept or reject. It is not completed-success. Waiting for an agent is a separate call (`swarm_run.py` without a barrier the caller did not ask for; the continuous scheduler refills slots). A caller that wants in-line behavior waits explicitly. Default submit does not block on agent completion.

## Ownership

Rust owns the pool threads, the decision loop, timeouts, and cancel polling (`gsd-sched-core`, bound as `swarm._native.run_scheduler`). Python owns session execution, the Task Store, telemetry, leases, and handoffs, and calls into Rust. PyO3 direction is Python calls Rust. The GIL is released around the native run (`py.detach` on the outer call; callbacks reattach). A panic at the boundary is `E-INTERNAL`, not an interpreter abort.

Concurrency is overlapping agent waits across slots. Parallelism is the separate Rust worker set inside the scheduler. The two widths are not the same number.

## Failure, cancel, retry

Fail-closed. A recorded fail is a missing gate. A dependency that fails does not let dependents proceed as success. Illegal state transitions raise `SwarmError`. Timeout, cancel, missing runtime binary (`E-DEP`), and saturation are distinct. Retry is bounded (`notes.retry_after`, capped backoff). Two attempts of the same leased task do not run at once. Shutdown is cooperative then kill, and a second signal skips the wait. Secrets are redacted before result files are written. Agent stdout is untrusted and capped.

## Security

Every run has a workspace root. Gate and orch scripts are not a general shell. Task text is data until an adapter maps it to a configured runtime (`claude`, `grok`, or `omp`). Paths under `.swarm/`, `.omp/`, and the runtime root are protected in swarm sessions. Signing keys do not enter child sessions.

## Quality bar

This repo is written to the bar it applies: typed errors, no silent failure, bounded slots, tested concurrency. That bar is not a new linter product.

## Unknowns (labeled)

- Official GSD-Core CLI-agent catalog: unknown in this checkout. Not invented.
- Cross-restart batch retention beyond the Task Store: the store is SQLite and survives process restart; an in-process-only 1800s window was a planner default and is not adopted.
- Queue depth 32 as a new constant: not adopted. This repo's ceiling is the existing `SWARM_MAX_PARALLEL_CEILING`.
- S7 default flip of `GSD_ENGINE` to rust: not approved. Python remains the default.

## What this goal does not authorize

New crates named `workflowz-core` or `workflowz-py`. A second Python package. A live adapter for an agent not in `{claude, grok, omp}`. A fork of GSD-Core. A dashboard, a general shell, or a rewrite of unrelated repositories.

## Acceptance mapping

| Check | Where it already lives |
|---|---|
| Cap, no 31st slot | `SWARM_MAX_PARALLEL_CEILING` 32; per-agent `max_parallel`; scheduler select |
| Invalid graph rejected | `orch_plan.py` custom-plan schema; `TaskStore` dependency checks |
| Child waits on parent | dependency satisfied only at IN_REVIEW, APPROVED, or DONE |
| Submit does not block | continuous scheduler; `--dry-run` returns without a model |
| Overlapped agents | `WorkerPool` / `run_scheduler` |
| PyO3 panic becomes an error | `guard_panic` / `E-INTERNAL: run_scheduler failed: internal panic` |
| Adapters only when known | runtimes claude, grok, omp; anything else is `E-INPUT` |
| Distinct failures | `E-INPUT`, `E-TIMEOUT`, `E-DEP`, `E-CAPACITY`, `E-CONTRACT`, `E-POLICY` |

Refs SPE-32820.
