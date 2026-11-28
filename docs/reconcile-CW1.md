# Reconcile CW1 — Roadmap / STATE cross-reconciliation

**Assignment:** Cross-reconcile `.planning/ROADMAP.md` + root `ROADMAP.md` + `.planning/STATE.md` into a dependency-ordered outstanding-phase list with source + conflicts. Flag contradictions as blockers; never invent phases. No live writes — TBD/fake IDs only. Gates/formatters skipped per acceptance.
**Branch (assignment context):** `swarm/T74c2-be` — **observed:** `swarm/Tbe37-be` (see Conflict C11). Repo: `agent-swarm` (`/root/src/repos/agent-swarm`).
**Decisions (assignment context, not file-derived):** fakes only (no live Linear/Notion); learning disabled; approver = user; tool-unless-judgement. Issues: SPE-38338 (context only — see §5, not a phase).

## Sources read

| Source | Result |
|---|---|
| `.planning/ROADMAP.md` | Found, 305 lines. Milestones v2.0–v2.3 shipped; v2.4 (Phases 6–15 + inserted 14.1–14.7) in progress. The only roadmap source. |
| `ROADMAP.md` (repo root) | **MISSING** — `Path 'ROADMAP.md' not found`. Recorded as missing; not created (target says do not create). Any consumer expecting a root roadmap finds nothing. |
| `.planning/STATE.md` | Found, 934 lines. Read only — **not edited**, per target. Frontmatter + Current Position + 2026-11-17/18 audit tail (lines 889–934). |

Prior work exists at `docs/reconcile-SPE-38338.md` (same three sources, 933-line STATE). This CW1 file is a fresh reconciliation against the current 934-line STATE (new tail line 934, SPE-38338 graph baseline) and does not edit or supersede that file; where they agree, both cite the same source lines.

## 1. Phase table — every phase sourced

Shipped v2.0 (1–7), v2.1 (1–6), v2.2 (1–4), v2.3 (1–5) are `✅ shipped` in ROADMAP and not outstanding. v2.4 has 18 phases (6–15 = 10, plus inserted 14.1, 14.1.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7 = 8), matching STATE frontmatter `total_phases: 18` (STATE:11).

| Phase | Depends on (ROADMAP) | ROADMAP Status | STATE status | Source | Conflict |
|---|---|---|---|---|---|
| 6 — Compile-first pin and workspace build | Phase 5 | `human_needed (2026-11-12 per 06-VERIFICATION.md: 18/20 truths, 2 await current-tree build/test run)` | Audit `1 human_needed (6)` | ROADMAP:80–86; STATE:899 | C9 — HTIL 2026-11-17 says GE-1 runs started, may satisfy 2 truths; STATE never closes Phase 6. Needs verifier re-run, not an edit. |
| 7 — Parity harness and frozen contracts | Phase 6 | `passed (per 07-VERIFICATION.md 2026-11-08)` | Audit `6 passed (…, 7, …)` | ROADMAP:91–97; STATE:899 | C10 — STATE:109 says frozen replay failed 52/211 and "do not mark Phase 7 passed"; STATE:890 supersedes that (211/211 after repoints). Historical line, resolved in-file. |
| 8 — Wasmer full-core spike report | Phase 7 | `gaps_found (2026-11-02: 3 criteria NOT MET)` | Audit `10 gaps_found (…, 8, …)` | ROADMAP:102–108; STATE:899 | None. Outstanding. |
| 9 — Host-native scheduler join and strangler seam | Phase 8 | `gaps_found (2026-11-02: 21 MET, 6 PARTIAL, 10 NOT MET, 2 NOT RUN of 39)` | Audit gaps_found; STATE:904–933 add verified 2026-11-18 row work (rows 1, 2, 7, 16, 18, 22, 26, 37) without closing the phase | ROADMAP:110–116; STATE:899, 904–933 | C9 — verified rows vs stale 2026-11-02 Status (e.g. row 26 NOT MET already implemented at `a760263`, STATE:914; row 37 PARTIAL stale, STATE:922). Neither re-verified nor re-closed. |
| 10 — Wasmtime sandbox and omp validation | Phase 9 | `passed` (debt recorded) | Audit passed | ROADMAP:121–127; STATE:899 | None. Closed. |
| 11 — Required PyO3 extension and wheel CI | Phase 10 | `gaps_found (2026-11-02)` | Audit gaps_found; STATE:901 gate 3 smoke in wheel job locally, first CI run needs unauthorised push | ROADMAP:131–137; STATE:899, 901 | Evidence scope: local run vs never-executed `wheel.yml` on GitHub. Outstanding until first CI run. |
| 12 — Port units and strangler flips | Phase 11 | `gaps_found (frontmatter) … Body says pass_with_recorded_debt` | Audit gaps_found; STATE:902 U12.2 item 4 rewritten in PLAN, VERIF row 4 still not met until verifier re-runs | ROADMAP:142–148; STATE:899, 902 | C6 — ROADMAP frontmatter vs body disagree. Treated outstanding. |
| 13 — Browser deliverable with isolation headers | Phase 12 | `gaps_found (2026-11-02)` | Audit gaps_found | ROADMAP:153–159; STATE:899 | None. Outstanding. |
| 14 — Acceptance proof | Phase 13 | `gaps_found … only for the quiet-host A2 record under decision D1` | Audit gaps_found; STATE:271 D1 accepts contended A2 run | ROADMAP:164–170; STATE:899, 271 | C7 — D1 accepts the record, Status stays `gaps_found`. User ruling needed. |
| 14.1 — Measured hot-path fixes and Rust redaction decision | Phase 14 | `gaps_found (criteria met, debt open)` | Audit gaps_found | ROADMAP:175–181; STATE:899 | Terminology: "criteria met" counts as gaps_found; D-P8 reconfirmation never closed in STATE. |
| 14.1.1 — Rust redaction prototype | Phase 14.1 | `passed` | Audit passed | ROADMAP:186–192; STATE:899 | None. Closed (D-P14 + wall-clock debt). |
| 14.2 — CI evidence and runner decision | Phase 14.1.1 | `passed` | Audit passed | ROADMAP:197–203; STATE:899 | None. Closed (first Omega-pool CI run only after ship PR). |
| 14.3 — Read routes and sandbox default flip | Phase 14.2 | `Closed as pass_with_recorded_debt … no flip` | Audit passed; STATE:900 debt "accepted; it is not open work" | ROADMAP:208–214; STATE:899–900 | None. Closed with debt. |
| 14.4 — Rust scheduler runtime and default flip | Phase 14.2 | `gaps_found / INCOMPLETE (held)` + `DISPUTED: 2026-11-16 COMPLETE relabel` + `Gaps closed per updated VERIF (2026-11-16)` | Frontmatter `current_phase: 14.4`, `status: gaps_found`; audit `complete as written but not counted (its gate holds). done=false` | ROADMAP:218–254; STATE:5–17, 899 | **BLOCKER C3/C4** — three states at once; relabel disputed ("gaps_found is never relabelled to bypass assess"). Code held by R2 gate. |
| 14.5 — Write routes in the sandbox | Phase 14.3 | `Closed as pass_with_recorded_debt … no flip` | Audit passed; STATE:900 kill-9 gap "real and stays in the record" | ROADMAP:256–262; STATE:899–900 | None. Closed with debt. |
| 14.6 — Chat-thread runtime | Phase 14.4 (plan 05 needed 14.4-02) | "Implemented, gated … verifier verdict stays `gaps_found` (GE-1 open by the user's choice)" | Audit gaps_found; STATE:105 `GE-1 open by your choice` | ROADMAP:267–273; STATE:899, 105 | Gate vs implementation: 6/6 plans `[x]`, verifier `gaps_found`. Cannot close while 14.4 held and GE-1 open. |
| 14.7 — OMP plugin and local MCP integration | Phase 14 (officially) | `gaps_found, no VERIFICATION.md. DISPUTED COMPLETE. AC-14.7-01 NOT PASSED` | Audit gaps_found; STATE:898 verifier `0/18 fully met`; STATE:905, 910 Wave 2 (W2-01/W2-04) blocked on held 14.4 | ROADMAP:283–289; STATE:898–899, 905, 910 | **BLOCKER C5** — official dep Phase 14 vs de-facto dep held 14.4 for Wave 2. |
| 15 — Audit, cleanup and ship | Phase 14.7 | `INCOMPLETE, blocked before audit, no VERIFICATION.md. DISPUTED COMPLETE. Needs explicit user confirmation.` Plan `[ ]` open | Audit gaps_found (counted in the 10); STATE:105 "not to run unattended", "BLOCKED at AUDIT"; STATE:140 "Start the next milestone" | ROADMAP:295–304; STATE:105, 140, 899 | **BLOCKER C8** — "start next milestone" vs blocked ship. Plan checkbox still open. |

Closed with recorded debt (not outstanding): 7, 10, 14.1.1, 14.2, 14.3 (R9/R11/R12), 14.5 (U3.8a/U3.8c/rollback/kill-9) — STATE:900 "accepted; it is not open work". Do not re-open without a new user decision.

STATE counts check: frontmatter `completed_phases: 6`, `completed_plans: 29`, `percent: 33` (STATE:11–15) matches the audit's 6 passed (STATE:899). Outstanding = 1 human_needed + 10 gaps_found + 14.4-not-counted = **12 phases**.

## 2. Dependency-ordered outstanding-phase list

DAG validated with `arch_cycle_check` (see §4): 19 nodes, 18 edges, `has_cycle: false`, `cyclic_components: []`. Order below follows ROADMAP `Depends on` edges; the one fork (14.3 and 14.4 both depend on closed 14.2) and the 14.7 de-facto edge are flagged, not re-drawn.

1. **Phase 6** (`human_needed`; dep: 5, shipped) — verify 2 pending truths with current-tree build/test; close or keep gaps_found. Blocks the green-baseline claim for everything below.
2. **Phase 8** (`gaps_found`; dep: 7, closed) — A10 exception approval + full-core measurement. Blocks 9.
3. **Phase 9** (`gaps_found`; dep: 8) — verifier re-run covering 2026-11-18 row work. Blocks 14.4 facade.
4. **Phase 11** (`gaps_found`; dep: 10, closed) — first GitHub CI run of `wheel.yml`. Needs user-authorised push; no silent push. Blocks 12.
5. **Phase 12** (`gaps_found`, C6; dep: 11) — U12.2 item 4 verifier re-run. Blocks 13.
6. **Phase 13** (`gaps_found`; dep: 12) — loader depth, 100 kB cap, browser gate. Blocks 14.
7. **Phase 14** (`gaps_found`, C7; dep: 13) — D1 A2 ruling. Blocks 14.1 and (officially) 14.7.
8. **Phase 14.1** (`gaps_found` — criteria met, debt open; dep: 14) — D-P8/D-P11 debt. Does not empirically block 14.2 (already closed); recorded debt only.
9. **Phase 14.4** (`gaps_found/INCOMPLETE (held)` — **BLOCKER C3**; dep: 14.2, closed) — R2 gate: A03 R2 delta → parent materialisation → BOTH A09+A10 rechecks → inventory green. Then local S5 → feature-only Omega CI → quiet S6 (p50 AND p95 strictly < 1.00, D-P21) → separately approved S7. No flip, no ship meanwhile. Blocks 14.6 and 14.7-Wave-2 (de-facto).
10. **Phase 14.6** (`gaps_found`; dep: 14.4) — GE-1 open by user choice. Cannot close while 14.4 held.
11. **Phase 14.7** (`gaps_found`, no VERIFICATION.md, AC-14.7-01 NOT PASSED — **BLOCKER C5**; dep: 14 officially, 14.4 de-facto for W2-01/W2-04) — needs SUMMARIES/VERIFICATION (STATE:897) and a corrected dependency. Blocks 15.
12. **Phase 15** (`INCOMPLETE` — **BLOCKER C8**; dep: 14.7) — audit, cleanup and ship need explicit user confirmation; assess `done=false`. Last phase; nothing valid follows it until the user rules.

## 3. Conflicts / blockers — never auto-fixed

| ID | Conflict | Evidence | Decision needed |
|---|---|---|---|
| C1 | Milestone status: frontmatter + Current focus say v2.4 INCOMPLETE (`status: gaps_found`, `current_phase: 14.4`) vs Current Position says "Milestone v2.4 complete … archived 2026-11-16" | STATE:5–17, 27 vs 33–36 | User: which line is authoritative? Do not start a next milestone or claim ship readiness meanwhile. |
| C2 | Progress: `percent: 33` vs bar `39%` | STATE:15 vs 29 | Re-run GSD status; do not hand-edit STATE (read-only here). |
| C3 | **BLOCKER** Phase 14.4 three-way state: `gaps_found/INCOMPLETE (held)` vs `Gaps closed per updated VERIF (2026-11-16)` vs audit "complete as written but not counted" | ROADMAP:218–254; STATE:899 | User: does updated VERIF close it, or does the R2 gate still hold? Code stays held. |
| C4 | **BLOCKER** Disputed 2026-11-16 COMPLETE relabels (14.4, 14.7, 15) using Python dispatch layers; audit rule: "gaps_found is never relabelled to bypass assess" | ROADMAP:224, 289, 301 | User: evidential standard for COMPLETE before any relabel stands. |
| C5 | **BLOCKER** Phase 14.7 dependency: ROADMAP `Depends on: Phase 14` vs STATE de-facto dep on held 14.4 for Wave 2 (STATE:910) | ROADMAP:287; STATE:910 | Owner: correct ROADMAP dep or split 14.7 into Wave 1 (independent) / Wave 2 (14.4-gated). Schedule on de-facto edge meanwhile. |
| C6 | Phase 12 frontmatter `gaps_found` vs body `pass_with_recorded_debt` | ROADMAP:142–148 | Verifier re-run decides (STATE:902). |
| C7 | Phase 14: D1 accepts contended A2 run, Status stays `gaps_found` | ROADMAP:164–170; STATE:271 | User: does D1 close the last criterion? | 
| C8 | **BLOCKER** Phase 15: ROADMAP "needs explicit user confirmation", STATE Blockers "ship stays blocked" vs STATE Operator Next Steps "Start the next milestone" | ROADMAP:301; STATE:101, 140 | User: authorise Phase 15, or strike the next-milestone line. |
| C9 | Stale per-phase Status vs 2026-11-17/18 progress (Phase 6 GE-1 runs started; Phase 9 rows verified/committed) — phases neither re-verified nor re-closed | ROADMAP:86, 116; STATE:890–933 | Verifier re-runs; do not edit Status by hand. |
| C10 | Phase 7: STATE:109 "do not mark passed" (52/211 replay fail) vs STATE:890 correction (211/211, supersedes) | STATE:109 vs 890 | Resolved in-file; keep 890 as current. Noted so no reader trusts line 109. |
| C11 | Branch: assignment context `swarm/T74c2-be` vs observed `swarm/Tbe37-be` | git branch --show-current | Orchestrator: confirm which branch this reconciliation was assigned to; no branch change made here. |
| C12 | Root `ROADMAP.md` missing (target listed it as a source; only `.planning/ROADMAP.md` exists) | read `ROADMAP.md` → not found | If a root copy is wanted it must come from GSD tooling; not hand-copied here. |

## 4. Tool evidence

- `arch_cycle_check` on the 18 ROADMAP dependency edges (+ node 5): `state: SUCCESS, has_cycle: false, node_count: 19, cyclic_node_count: 0, cyclic_components: []` — the §2 order is a valid topological order of the declared DAG. The STATE de-facto 14.7→14.4 edge (C5) is not in the declared DAG and would also be acyclic if added.
- No `api.contract`, ADR, or blueprint change was made, so no `arch_contract_check` / `arch_contract_ops` run applies (no contract document drafted or revised). No gates or formatters run, per acceptance.

## 5. Notes for the next agent

- **SPE-38338 (STATE:934):** a validated dependency-graph baseline (code/schema only; `docs/graphcheck-SPE-38338.md`). It is context, not a phase — never invent a phase from it. Its findings (tool requirements unexpressible in `TaskNode`; Rust failed-dependency deadlock; greedy-frontier starvation) are recorded there, not re-litigated here.
- **No live writes:** no Linear/Notion rows created or updated. Any tracker reference for CW1 must use TBD/fake IDs. The existing Notion task (STATE:134) is referenced read-only.
- **Learning disabled until policy:** HTIL 2026-11-17 choices (sonic inline, `crates/` deliverable, GitHub-hosted CI, Pool Resize `dynamic_within_cap`) are user-recorded in STATE/ROADMAP, not ratified policy.
- **Approver = user via pause:** every BLOCKER above (C3, C4, C5, C8) is a pause point. Do not schedule phases, release held code, or ship on inferred answers.
- **STATE was not edited.** This file is the only write of this task (`docs/reconcile-CW1.md`).

## 6. Reconciliation trace

Every substantive claim above traces to: ROADMAP milestones/phases lines 5–304 (`.planning/ROADMAP.md`), STATE frontmatter lines 5–17, Current focus/Position lines 27–36, Blockers line 101, D1 line 271, GE-1/14.7 lines 890–898, audit line 899, debt line 900, Phase 9/14.7 lines 901–933, graph baseline line 934 (`.planning/STATE.md`), and `arch_cycle_check` output (§4).
