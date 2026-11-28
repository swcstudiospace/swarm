# Audit CW5 — 12 outstanding phases vs discuss/plan/verify evidence

**Assignment:** Audit all 12 phases vs artifacts (discuss/plan/verify evidence):
for each phase mark complete/blocked/pending with evidence link + B-ruling
needed. Milestone stays open (5 conflicts parked) — convert each failure to
tracked follow-up (TBD-LIN IDs, fakes). No cleanup yet, no live writes.
**Branch observed:** `swarm/Tbe37-be` (assignment context `swarm/T74c2-be`).
**This file is the only write of this task** (`docs/audit-CW5.md`).
`.planning/STATE.md` and `.planning/ROADMAP.md` were **read only, not edited**.
Gates/formatters skipped per acceptance. No code. No live Linear/Notion writes —
every tracked ID below is a deterministic fake `TBD-LIN-…` or `null`
(plan R-P1/R-P2, `docs/plan-CW2.md` §3).

**Outcome: milestone stays open.** 12/12 phases audited: **0 complete,
1 pending, 11 blocked**. The 6 closed-with-debt phases (7, 10, 14.1.1, 14.2,
14.3, 14.5) are not in the 12 and are not re-opened. The 5 parked conflicts
(C3, C4, C5, C7, C8 per `docs/summary-CW4.md` §3) are what keeps the milestone
open; each is converted below to a tracked fake follow-up, never auto-closed.

## 1. Sources read

| # | Source | What it proves |
|---|---|---|
| A1 | `.planning/ROADMAP.md` Status lines 86–304 | Per-phase `Status:` verbatim; disputed 2026-11-16 COMPLETE relabels on 14.4/14.7/15 |
| A2 | `.planning/STATE.md` frontmatter 5–17, audit 899, tail 890–934 | Milestone `gaps_found`, 6 passed / 1 human_needed / 10 gaps_found / 14.4 not-counted, `done=false` |
| A3 | `.planning/MILESTONE-AUDIT.md` §3 | Per-phase VERIFICATION verdicts + counting rule (passed only if VERIFICATION `passed` with no open gap) |
| A4 | `.planning/milestones/v2.4-phases/*/` per-phase files | CONTEXT/PLAN/SUMMARY/VERIFICATION presence per phase (verified by listing, see §2) |
| A5 | `docs/reconcile-CW1.md` §§1–3 | 12 outstanding phases, dependency order, conflicts C1–C12 |
| A6 | `docs/plan-CW2.md` §3 (lines 232–250) | Fake mapping: 12 parents `TBD-LIN-v24-<phase>-P` + 33 children `…-W<k>` = 45 Issues, `tbd_real_id=null` |
| A7 | `docs/summary-CW4.md` §§2–3 | 6 synced / 3 stale / 5 conflict (C3/C4/C5/C7/C8 parked), source-of-truth `STATE > Linear`, Notion read-only |
| A8 | `docs/quality-CW3.md` (QF-CW3-01) | Wave 3 scoped gate evidence: 1 pytest FAIL, owner A05/A07 — context, not a phase verdict |

Notes: (a) All 18 phases have a VERIFICATION file (MILESTONE-AUDIT §3), so no
phase is `pending` for want of a verdict file; `pending` below means
`human_needed` awaiting a run, `blocked` means a ruled gap/held gate.
(b) Root `ROADMAP.md` is missing (CW1 C12); only `.planning/ROADMAP.md` exists.
(c) (d) `discuss` evidence = CONTEXT file, `plan` evidence = PLAN file(s),
`verify` evidence = VERIFICATION file; all paths under
`.planning/milestones/v2.4-phases/<phase-dir>/`.

## 2. Audit table — 12/12

State key: `complete` = VERIFICATION `passed` counted by MILESTONE-AUDIT rule;
`pending` = `human_needed`, awaiting a current-tree run; `blocked` = ruled gap
or held gate needing a user/owner ruling (B-ruling). `tbd_real_id=null` on all
45 fakes. Owner of each follow-up: fake workflow owner per plan §1 (user/approver).

| Phase | Verdict | Discuss/Plan/Verify evidence | B-ruling needed (approver: user) | Tracked follow-up (fakes only) |
|---|---|---|---|---|
| 6 — Compile-first pin | **pending** (`human_needed`, 18/20 truths, 2 await current-tree build/test run) | Discuss: `06-CONTEXT.md` · Plan: `06-PLAN.md` (+`06-GAP-01/02-PLAN.md`) · Summary: `06-SUMMARY.md` · Verify: `06-VERIFICATION.md` (2026-11-12, `human_needed`) · ROADMAP:86 | B-ruling: none procedural — needs a **current-tree verifier re-run** (GE-1 runs started 2026-11-17, STATE tail). Verifier records verdict transition; never a silent close. (C9) | Parent `TBD-LIN-v24-6-P` + children `TBD-LIN-v24-6-W01` (WI-6.1 run), `W02` (WI-6.2 verdict transition), `W03` (WI-6.3 baseline write-up). `tbd_real_id=null`. |
| 8 — Wasmer full-core spike | **blocked** (`gaps_found`, 3 NOT MET) | Discuss: `08-CONTEXT.md` · Plan: `08-PLAN.md` (+`08-GAP-01-PLAN.md`) · Summary: `08-SUMMARY.md` · Verify: `08-VERIFICATION.md` (2026-11-02, 3 NOT MET: no A10 FS/net exception approval E-01–E-16, no full-core measurement, no A03 D-P3-1/WASI approval) · ROADMAP:108 | B-ruling: **A10 exception decision** (approve E-01–E-16 or carry as gap) + A03 approval on D-P3-1/WASI version. (WI-8.2) | Parent `TBD-LIN-v24-8-P` + `W01` (WI-8.1 full-core re-measurement), `W02` (WI-8.2 A10 exception — L3, fake stays open), `W03` (WI-8.3 gap-list refresh). `tbd_real_id=null`. |
| 9 — Host-native scheduler join | **blocked** (`gaps_found`, 2026-11-02: 21 MET / 6 PARTIAL / 10 NOT MET / 2 NOT RUN) | Discuss: `09-CONTEXT.md` · Plan: `09-PLAN.md` (+`09-GAP-01-PLAN.md`) · Summary: `09-SUMMARY.md` · Verify: `09-VERIFICATION.md` (2026-11-02) · STATE:904–933 (2026-11-18: rows 1, 2, 7, 16, 18, 22, 26, 37 verified/committed, not re-closed) · ROADMAP:116 | B-ruling: **quiet-host verifier re-run** decides true counts; rows 26 (`a760263`) + 37 (`61880a5`) already in history, VERIF rows still stale. (C9) Blocks 14.4 facade. | Parent `TBD-LIN-v24-9-P` + `W01` (WI-9.1 re-verify 2026-11-18 rows), `W02` (WI-9.2 true MET/PARTIAL/NOT MET count), `W03` (WI-9.3 seam/entrypoint parity). `tbd_real_id=null`. |
| 11 — PyO3 + wheel CI | **blocked** (`gaps_found`: no GitHub CI run of `wheel.yml`) | Discuss: `11-CONTEXT.md` · Plan: `11-PLAN.md` (+`11-GAP-01-PLAN.md`) · Summary: `11-SUMMARY.md` · Verify: `11-VERIFICATION.md` (2026-11-02: U2 gate-3 smoke has no CI step/run; U5 quiet-host A2 baseline contended/moderate-load only; `wheel.yml` never run on GitHub) · STATE:901 (gate-3 smoke passes locally 13/13) · ROADMAP:137 | B-ruling: **user authorises push** for first GitHub `wheel.yml` run (no silent push). Fake stays open until that run. Blocks 12. | Parent `TBD-LIN-v24-11-P` + `W01` (WI-11.1 wheel CI step/run), `W02` (WI-11.2 gate-3 smoke local-vs-CI). `tbd_real_id=null`. |
| 12 — Port units + strangler flips | **blocked** (`gaps_found` frontmatter vs `pass_with_recorded_debt` body, C6) | Discuss: `12-CONTEXT.md` · Plan: `12-PLAN.md` (+`12-GAP-01-PLAN.md`) · Summary: `12-SUMMARY.md` · Verify: `12-VERIFICATION.md` (2026-10-25, frontmatter `gaps_found`, body `pass_with_recorded_debt`; U12.2 item 4 replay-after-failover test missing) · STATE:902 (item 4 rewritten in PLAN, VERIF row 4 still not met until verifier re-runs) · ROADMAP:142–148 | B-ruling: **verifier re-run** decides (not a user relabel). Fake `W02` parked `blocked_hold` meanwhile. (C6) Blocks 13. | Parent `TBD-LIN-v24-12-P` + `W01` (WI-12.1 U12.2 item 4), `W02` (WI-12.2 C6 conflict park), `W03` (WI-12.3 10-units-in-5-waves parity roll-up). `tbd_real_id=null`. |
| 13 — Browser deliverable | **blocked** (`gaps_found`: loader depth 128≠32 row 10; 100 kB cap not in browser row 20; browser gate not run) | Discuss: `13-CONTEXT.md` · Plan: `13-PLAN.md` (+`13-GAP-01-PLAN.md`; `13-PLAN.superseded.md` kept) · Summary: `13-SUMMARY.md` · Verify: `13-VERIFICATION.md` (2026-11-02, 2 NOT MET, 5 NOT RUN/PARTIAL) · ROADMAP:159 | B-ruling: none procedural beyond evidence work items W01–W03; sequence blocked by Phase 12 regardless. | Parent `TBD-LIN-v24-13-P` + `W01` (WI-13.1 loader depth), `W02` (WI-13.2 100 kB cap), `W03` (WI-13.3 browser gate run). `tbd_real_id=null`. |
| 14 — Acceptance proof | **blocked** (`gaps_found`; D1 accepted yet Status unchanged, C7) | Discuss: `14-CONTEXT.md` · Plan: `14-PLAN.md` (+`14-GAP-01-PLAN.md`) · Summary: `14-SUMMARY.md` · Verify: `14-VERIFICATION.md` (2026-11-02; quiet-host A2 criterion NOT MET, contended record loadavg 4.42→15.05) · STATE:271 (D1 accepts contended A2 run) · ROADMAP:164–170 | B-ruling: **user rules** whether D1 closes the last criterion (fake W02 / WI-14.2). Status stays `gaps_found` until ruled. (C7 — one of the 5 parked) | Parent `TBD-LIN-v24-14-P` + `W01` (WI-14.1 suite gate), `W02` (WI-14.2 D1 ruling). `tbd_real_id=null`. |
| 14.1 — Hot-path fixes | **blocked** (`gaps_found` by convention: criteria met, debt open) | Discuss: `14.1-CONTEXT.md` · Plan: `14.1-PLAN.md` (+`14.1-GAP-01-PLAN.md`) · Summary: `14.1-SUMMARY.md` · Verify: `14.1-VERIFICATION.md` (2026-11-10, `gaps_found`, C1–C6 met, debt recorded) · ROADMAP:175–181 | B-ruling: **user reconfirmation** of D-P8 (superseded by D-P12 in 14.1.1) and D-P11 (SQLite WAL opt-in `SWARM_TASKSTORE_WAL=1`). Record accepted debt; never close by relabel. (WI-14.1.1/2) | Parent `TBD-LIN-v24-14.1-P` + `W01` (WI-14.1.1 D-P8), `W02` (WI-14.1.2 D-P11). `tbd_real_id=null`. |
| 14.4 — Scheduler runtime + flip | **blocked** (`gaps_found/INCOMPLETE (held)`; VERIFICATION `complete` as written, not counted) | Discuss: `14.4-CONTEXT.md` · Plan: `14.4-PLAN.md` + sub-plans `14.4-02…21-PLAN.md` · Summary: `14.4-SUMMARY.md` (+02/04/15/19) · Verify: `14.4-VERIFICATION.md` (2026-11-16, `complete`, self-described as manually maintained, not independent) · MILESTONE-AUDIT §3/§5.1 (R2 gate held: A03 R2 delta → parent install → BOTH A09+A10 rechecks → inventory green) · ROADMAP:218–254 | B-rulings: **user rules** (a) does updated VERIF close it or does the R2 gate still hold? (b) evidential standard for COMPLETE before the disputed 2026-11-16 relabel stands? Code stays held; no flip, no ship. (C3/C4 — two of the 5 parked) | Parent `TBD-LIN-v24-14.4-P` + `W01` (WI-14.4.1 C3 ruling), `W02` (WI-14.4.2 C4 standard), `W03` (WI-14.4.3 R2-gate release). `tbd_real_id=null`. |
| 14.6 — Chat-thread runtime | **blocked** (`gaps_found`: GE-1 open by user choice; 6/6 plans done ≠ closed) | Discuss: `14.6-CONTEXT.md` · Plan: `14.6-01…06-PLAN.md` (+`14.6-GAP-01-PLAN.md`) · Summary: `14.6-01…06-SUMMARY.md` · Verify: `14.6-VERIFICATION.md` (`gaps_found` at `e7b922b`; 0 blockers, 2 major, 4 minor; GE-1 `cargo test --workspace` + full pytest not run) · STATE:105 · ROADMAP:267–273 | B-ruling: **user choice on GE-1** (fake W01). Cannot close while 14.4 held (dep). | Parent `TBD-LIN-v24-14.6-P` + `W01` (WI-14.6.1 GE-1), `W02` (WI-14.6.2 held-dep release). `tbd_real_id=null`. |
| 14.7 — OMP plugin / local MCP | **blocked** (`gaps_found`; VERIFICATION exists, AC-14.7-01 NOT PASSED, 0/18 met) | Discuss: `14.7-CONTEXT.md` + `14.7-CONTRACT.md` (98 lines, STATE:910) · Plan: `14.7-01-PLAN.md`, `14.7-02-PLAN.md` (Wave 1; W2 renamed `14.7-W2-01…04`) · Summary: none listed at phase level · Verify: `14.7-VERIFICATION.md` (2026-11-17, `gaps_found`, 0/18 met, 5 partial, 13 unmet) · STATE:898, 905, 910 · ROADMAP:283–289 (stale "no VERIFICATION.md") | B-ruling: **owner rules** correct dep or split Wave 1 (independent) / Wave 2 (14.4-gated, W2-01/W2-04 blocked on held 14.4). Schedule on de-facto edge meanwhile. (C5 — one of the 5 parked) | Parent `TBD-LIN-v24-14.7-P` + `W01` (WI-14.7.1 C5 dep ruling), `W02` (WI-14.7.2 AC-14.7-01 evidence), `W03` (WI-14.7.3 Wave-2 plans 14.7-W2-01…04), `W04` (WI-14.7.4 contract review). `tbd_real_id=null`. |
| 15 — Audit / cleanup / ship | **blocked** (`INCOMPLETE`, blocked before audit; VERIFICATION `gaps_found` withdraws 2026-11-16 closure) | Discuss: `15-CONTEXT.md` · Plan: `15-PLAN.md` · Summary: `15-SUMMARY.md` · Verify: `15-VERIFICATION.md` (2026-11-16, corrected 2026-11-17 to `NOT CLOSED`) · STATE:105 ("not to run unattended"), 140 ("Start the next milestone") · ROADMAP:295–304 | B-ruling: **user confirmation token** (WI-15.1) for cleanup/ship (L3/L4 — no run meanwhile), or strike the next-milestone line. (C8 — one of the 5 parked) | Parent `TBD-LIN-v24-15-P` + `W01` (WI-15.1 C8 confirmation), `W02` (WI-15.2 cleanup list approval), `W03` (WI-15.3 ship/assess `done=false` → `done=true`). `tbd_real_id=null`. |

**Counts: 12/12 audited — 0 complete, 1 pending (6), 11 blocked.**
Per-phase evidence files checked by listing each phase dir: every one of the
12 has CONTEXT + PLAN(s) + VERIFICATION, and a SUMMARY except 14.7 (phase-level
SUMMARY absent; sub-plan SUMMARIES exist for 14.4/14.6). Milestone audit
confirms all 18 phases have a VERIFICATION file.

## 3. The 5 parked conflicts (why the milestone stays open)

| Parked conflict | Phases | What would close it | Parked fake |
|---|---|---|---|
| C3 — 14.4 three-way state (`held` vs `Gaps closed` vs `complete as written`) | 14.4 | User ruling (WI-14.4.1); code held by R2 gate | `TBD-LIN-v24-14.4-W01` |
| C4 — disputed 2026-11-16 COMPLETE relabels (14.4/14.7/15) | 14.4, 14.7, 15 | User evidential standard for COMPLETE (WI-14.4.2) | `TBD-LIN-v24-14.4-W02` |
| C5 — 14.7 dep: ROADMAP `Phase 14` vs de-facto held 14.4 (Wave 2) | 14.7 | Correct dep or split Wave 1/Wave 2 (WI-14.7.1) | `TBD-LIN-v24-14.7-W01` |
| C7 — Phase 14 D1 accepted, Status stays `gaps_found` | 14 | User ruling on D1 (WI-14.2) | `TBD-LIN-v24-14-W02` |
| C8 — Phase 15 needs user confirmation vs STATE "start next milestone" | 15 | Confirmation token or strike line (WI-15.1) | `TBD-LIN-v24-15-W01` |

Aggregate conflicts C1/C2 (milestone frontmatter 33% vs Current Position 39%;
`gaps_found` vs "complete") are recorded in CW1/CW4 and also park the
milestone; they are not among the 12 phase rows but gate any ship claim.
Under the source-of-truth rule (`STATE verdict > Linear workflow status`,
Notion never overrides), no fake `done` can close a `gaps_found` phase —
conflicts park for user ruling, never auto-close.

## 4. What stays open / next action

- **No cleanup attempted** (Phase 15 cleanup needs explicit user confirmation).
- **No live writes** (no Linear/Notion rows; all IDs `TBD-LIN-…`, `tbd_real_id=null`).
- **Next action:** user rulings on the 5 parked conflicts above + verifier
  re-runs for phases 6/9/12 (C9 stale rows). Approver = user via pause.
- Evidence kept read-only: STATE/ROADMAP not edited; per-phase evidence paths
  in §2 are the citations for every row.

## 5. Trace (every fact cited)

| Claim | Source |
|---|---|
| 12 outstanding + 6 closed-with-debt, 0/1/11 split | This table (A1–A4) |
| Per-phase VERIFICATION verdicts/dates | `MILESTONE-AUDIT.md` §3; per-phase `*-VERIFICATION.md` frontmatter |
| 45 fake Issues (12 + 33), `tbd_real_id=null`, closed = no row | `plan-CW2.md` §3 |
| 5 parked conflicts C3/C4/C5/C7/C8; `STATE > Linear` | `summary-CW4.md` §§2–3 (CW1 C3–C8) |
| State frontmatter, D1, GE-1, 14.7 contract | `STATE.md:5–17, 105, 140, 271, 898–910` |
| Notion read-only context only | `STATE.md:134`; CW4 §3 Notion note |

*Audit only. Gates/formatters skipped per acceptance. No code changed.*
