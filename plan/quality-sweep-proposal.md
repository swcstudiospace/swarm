# Quality sweep proposal (draft for review, 2026-11-06)

Status: draft. Nothing here is scheduled. ROADMAP.md is not edited: per the 2026-10-23 answer (q1), you add sweep phases to ROADMAP.md and the run then executes them.

## Basis

- Q2 triage, `.planning/quality/TRIAGE.md`: 100 findings from 23 reviewer items. Categories: FIX 35, DECISION 34, PERF 15, OUT 14, REJECT 2. Scope: EDIT 85 (the quality edit set), OUT 15.
- Decisions, `.planning/quality/BACKLOG.md` section 4 (D-1 to D-15). On 2026-11-06 you approved the recommended answers for D-1 to D-10 and D-13 to D-15. D-11 has no recommended answer and stays open. D-12 stays held (gsd-nexus is outside the edit set).
- T29 scanner defects (outside v2.4), `.planning/todo-map.md`: items 1 to 7. Items 1 and 4 are fixed on branch `fix/scanner-scope-skip` (local, not pushed).
- Held items (frozen or outside the edit set): 23-F1, 21-B1, 19-F5, 20-L1-01, 20-L1-02, 20-L1-05, 22-SR-1, 22-SR-2.

## Scope limit

The triage covers the Q2 reviewer scope: the quality edit set plus the reviewer-selected OUT items. A whole-repository sweep (every crate, `swarm/`, `scripts/`, `omp/`, `web/`) needs a fresh read of the code outside that scope. This proposal does not claim that coverage.

## Proposed phases

Ordered by risk. Each phase is independently revertible.

| Phase | Scope | Acceptance | Blocked by |
|---|---|---|---|
| S1 Approved decisions (done: D-1, D-2, D-4, D-7, D-8, D-9, D-11, D-13; deferred: D-3, D-5, D-6, D-10; held: D-12; no code: D-14, D-15) | gsd-pool, gsd-scheduler, gsd-state, gsd-governor, gsd-python `chat.rs`, the chat section of `swarm/_native.pyi`, the cancel-grace comments in `swarm/tools/shared_runtime.py` and `crates/gsd-state/src/session_join.rs` | A red test before each behaviour change; `cargo test` for the four crates and gsd-python; parity cells; stub check | None inside the edit set |
| S2 FIX items (35) | the same edit set | Per item, a regression test that is red before the fix | None inside the edit set |
| S3 PERF items (15) | the same edit set | Measure first, on a quiet host; no rewrite without numbers | A quiet host for the measurements |
| S4 Python scanners, T29 items 2, 3, 5, 6, 7 | `scripts/rev_gate.py`, `scripts/sec_gate.py`, `swarm/script_base.py` | Diff parser tests (added `++ ` lines, deleted `-- ` lines, binary changes, quoted names); invalid `--diff-base` refused; `cargo-audit` name corrected; finding order stable; malformed pip-audit JSON exits 2 | None |
| S5 Held files | `crates/gsd-runtime`, `crates/gsd-sched-core`, `crates/gsd-python/src/sched*.rs`, `swarm/rust_scheduler.py`, `swarm/claims.py` (23-F1, 21-B1, 19-F5, 20-L1-*, 22-SR-*) | Explicit approval per file | Frozen scope; 14.4 is deferred (DEC-02) |
| S6 Gates and CI | rustfmt drift (two files applied, workspace clean); clippy; the parity CI step (review finding 3); the test-job `gsd_engine` and `swarm._native` build (committed locally as 8fd2638 on the PR branch, not pushed) | The CI-faithful chain green on the branch that is pushed | The repository variable `SCHEDULER_CONTINUOUS_SHA256` (set 2026-11-07); the guest pin |
| S7 Guest pin | `crates/gsd-sandbox` pin files and `build.rs` (CARGO_ENCODED_RUSTFLAGS); `web/generated` re-transpiled | A10 and A11 sign-off: A10 produced no verdict; A11 returned FAIL with two CI gaps. The transpile drift is a known gap, accepted for now (user, 2026-11-07). The write-pin step was added and then dropped (user ruling): it fails on the branch's pins, which do not match its build (default and write) | A10 retry; the pin change is held until A10 and A11 sign off (user ruling) |

## Decisions needed before S1 starts

1. D-9, cancel grace: resolved (user, 2026-11-07: two concepts). gsd-state's session-join window is fixed at 3 s (`CANCEL_GRACE`) and does not read `SWARM_CANCEL_GRACE_S`. The agent cancel wait is set by `SWARM_CANCEL_GRACE_S` (10 s default in `swarm/tools/shared_runtime.py` and `scripts/swarm_run.py`). Both are documented; no default changed.
2. D-11, implemented (user ruling 2026-11-06: the module-doc table is the spec). The pool path maps QuotaExceeded to E-CAPACITY and Unauthorized to E-POLICY; red-first tests in gsd-pool, gsd-python and gsd-scheduler pass.
3. D-13, done (user ruling 2026-11-06: release first, then park). A refused release parks nothing; a park refused after a successful release destroys the thread with the refund applied.
4. D-2, implemented (user, 2026-11-07: the BACKLOG values). Ids (thread, message and session) are at most 256 bytes; payloads are at most 1 MiB. The pool refuses with `TooLarge` (E-INPUT); the governor refuses an oversized id with `InvalidBudget` (E-INPUT). The two `MAX_ID_BYTES` constants must stay equal.
5. D-4, done (user, 2026-11-07: delete). `policy` and `set_policy` are removed from gsd-scheduler. The field was write-only, so no behaviour changed and no red test applies.
6. D-3, deferred (user, 2026-11-07). The retention change is large and cross-crate, and BACKLOG:49 leaves its per-map rules open.
7. CI gaps (user, 2026-11-07): the write-pin step was added to the PR worktree's ci.yml and then dropped, because it is red on the branch's pins (user ruling). The transpile drift is recorded as a known gap.
8. PR #44 (user, 2026-11-07: investigate on the PR head): the beside-key-file fallback and its expectations are committed locally on ci/github-hosted-runners as 7cc85b9, with the test-job change as 8fd2638. Nothing is pushed. The parity cell and the write-parity file pass on the branch. The rust job's default pin test still fails there (item 7 and S7). The PR base is feat/v2.4-rust-wasm-core, and the 77 unpushed local feat commits are not on the branch.

## Not in this proposal

- The guest pin (S7) and the 14.4 scope (deferred by DEC-02).
- D-12 (Nexus unsubscribe): outside the edit set.
