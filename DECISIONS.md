# Decisions — Graph Engineering standalone upgrade

Source: spec n2 (`docs/requirements/spec-T74c2-req.md`, VALIDATED for downstream
architecture) plus `docs/inventory.md`, `docs/assumptions.md`, `docs/GOAL.md`.
Context: Wave1 n1 inventory, repo agent-swarm branch `swarm/T74c2-be`
(prior T74c2 wave, `crates/gsd-*` present). Standalone upgrade — no milestone
phases, per user. GitHub repo swcstudiospace/swarm. Issues SPE-37334/SPE-37337.

| # | Decision | Default | Owner | Gate / what it blocks |
|---|---|---|---|---|
| D1 | Omp | TS host extension, no OpenMP | A06 | Blocks any OpenMP/native-thread Omp rewrite |
| D2 | workflowz | Existing Swarm orchestration (DAG dispatch policy only) | A01 | Blocks new workflowz language/DSL/crates |
| D3 | Perf target | Wall-clock only, no SLO | A08 | Blocks p95/SLO-based perf gates |
| D4 | Host / Rust / Python / native-ext | Evidence-gated; hold PyO3 expansion until n1 proves; Python stays default | A05 | Blocks PyO3 expansion and engine default flip |
| D5 | Per-tool idempotency / side-effects | No auto-retry until per-tool inventory | A05 | Blocks enabling automatic retry on any tool |
| D6 | gsd-autonomous scope | Out of scope (standalone per user) | A01 | Blocks gsd-autonomous milestone-phase work |

## D1 — Omp = TypeScript host extension (no OpenMP)

- **Default:** Omp is the TypeScript host extension. The frozen TS surface
  (`omp/src/bridge.ts` spawning `python3 scripts/<script>.py --json`, plus
  `tools.ts`, `guard.ts`, `hooks.ts`, `context.ts`, `commands.ts`) is the
  authority. No OpenMP usage, no native-thread Omp rewrite.
- **Owner:** A06 (frontend / client-side TS host).
- **Blocks / gate:** Blocks any proposal that treats Omp as OpenMP or rewrites
  the TS plugin. Gate: TS twin parity — `bun scripts/ts/<script>.ts --root
  <repo> --json` must mirror the Python twin exit/JSON contract; mismatch is
  a defect, not a reason to change the TS surface.

## D2 — workflowz = existing Swarm orchestration

- **Default:** `workflowz` is a DAG dispatch policy over the existing Swarm
  orchestration (30 agents A01–A30, cap ≤ 30 workers/session, Python
  scheduler in `scripts/swarm_run.py` as oracle). No workflowz DSL, no
  session-script format, no new `workflowz-core` / `workflowz-py` crates
  (also recorded: `docs/GOAL.md` fallback crate names discarded).
- **Owner:** A01 (orchestrator — owns decomposition and dispatch policy).
- **Blocks / gate:** Blocks adding a workflowz language, a second orchestrator
  product, or new workflowz crates. Gate: graph validation (zero cycles, zero
  dangling links) and cap enforcement already in spec n2 (US-20, AC-39/AC-40)
  must stay green; a new workflowz crate would need a new approved spec first.

## D3 — Perf target = wall-clock, no SLO

- **Default:** Performance is judged on wall-clock only. No p95, throughput,
  or latency SLO is committed. (Repo evidence is consistent: `docs/inventory.md`
  q4 records "parity or better" as an assumption, "not SLO"; `docs/assumptions.md`
  repeats this. A full pytest run previously timed out at 900 s on the host, so
  verification stays scoped per-path.)
- **Owner:** A08 (QA — owns measurement and load evidence).
- **Blocks / gate:** Blocks any gate or acceptance criterion that requires a
  numeric SLO (for example inventing a p95 < 300 ms gate). Existing measured
  targets (e.g. NFR-P3 ready-order check) remain evidence, not SLOs; new SLO
  values need stakeholder-approved spec change first.

## D4 — Host / Rust / Python / native extension = evidence-gated

- **Default:** No new native-extension work until n1 (Wave1 inventory) proves
  the need. Hold PyO3 expansion beyond the existing 18 `swarm._native` exports
  (12 functions + 6 classes, `crates/gsd-python`, abi3-py312, PyO3 0.30.1).
  Python remains the default engine on every route (`GSD_ENGINE=python`);
  Rust is per-route opt-in only. Python is the parity oracle.
- **Owner:** A05 (backend — owns Rust/Python engine boundary and PyO3 surface).
- **Blocks / gate:** Blocks PyO3 expansion, a Rust default flip, and any
  engine switch without parity evidence. Gate: per-route parity harness —
  0 divergence cases on identical outcomes, error codes, and timing spread
  (US-22, AC-43/AC-44); any mismatch means Rust-side bug and revert, not a
  Python change.

## D5 — Per-tool idempotency / side-effects: no auto-retry until inventoried

- **Default:** No automatic retry on any tool until a per-tool idempotency and
  side-effect inventory exists for that tool. Idempotency keys
  (`task-id, attempt` / `stage-id, join-seq`) are preserved, but retry itself
  stays off by default. This holds even for tools exposed through the PyO3
  bridge.
- **Owner:** A05 (backend — owns retry policy and tool call semantics).
- **Blocks / gate:** Blocks enabling auto-retry (including any bounded-backoff
  retry policy surface) on an uninventoried tool. Gate: the tool appears in
  the per-tool idempotency/side-effects inventory with its own retry
  eligibility recorded; without a row, retry remains disabled.

## D6 — gsd-autonomous = out of scope (standalone per user)

- **Default:** `gsd-autonomous` is out of scope for this upgrade. The upgrade
  is standalone — no milestone phases, no GSD phase work (in particular no
  phase-15-style closeout and no milestone phase decomposition), per user.
  Existing `gsd-autonomous` command-path evidence in the repo is left
  untouched; nothing in this tranche extends it.
- **Owner:** A01 (orchestrator — owns scope cuts and phase decomposition).
- **Blocks / gate:** Blocks gsd-autonomous features, milestone-phase planning,
  and phase closeout tasks in this tranche. Gate: a new stakeholder-approved
  spec (requirements.change with impact estimate) would be required before any
  gsd-autonomous work re-enters scope.
