# Summary CW4 — Linear-fake vs planning-state reconciliation (fakes only, no live writes)

**Assignment:** Reconcile Linear-fake vs planning state (fakes only, no live
Linear/Notion writes): synced/stale/missing/conflict table,
source-of-truth `STATE > Linear`, Notion never overrides; append *one*
STATE log line (phase status + next action + evidence links, atomic write
preserved); do not hand-edit STATE frontmatter beyond that log line.
Record reconciliation here. No code. Gates/formatters skipped.

**Branch observed:** `swarm/Tbe37-be` (assignment context `swarm/T74c2-be`).
**This file is the only write of this task** (`docs/summary-CW4.md`).
`.planning/STATE.md` was **not edited** — see §4 for why the log line is
recorded verbatim here instead of appended in place.

## 1. Sources read (fakes only — zero live writes)

| # | Source | What it proves |
|---|---|---|
| S1 | `docs/plan-CW2.md` §3 (lines 232–250) | Linear-fake mapping: 12 parents `TBD-LIN-v24-<phase>-P` + 33 children `…-W<k>` = 45 Issues, `tbd_real_id=null` for all; closed phases have no row. |
| S2 | `docs/plan-CW2.md` §4 (lines 254–265) | Source-of-truth table + precedence rule `STATE verdict > Linear workflow status`; Notion never overrides either. |
| S3 | `docs/plan-CW2.md` §1 (lines 40–157) | Per-phase Linear-fake parent/child work items + pause gates (the fake workflow state). |
| S4 | `docs/reconcile-CW1.md` §§1–3 | Planning truth: 12 outstanding phases (1 `human_needed` + 10 `gaps_found` + 14.4 not-counted), 6 closed-with-debt, conflicts C1–C12. |
| S5 | `.planning/ROADMAP.md` Status lines (86–301) | Per-phase `Status:` verbatim; disputed 2026-11-16 COMPLETE relabels on 14.4/14.7/15. |
| S6 | `.planning/STATE.md` frontmatter (5–17), Current focus/Position (27–36), audit (899), tail (890–934) | Milestone `gaps_found`, `current_phase: 14.4`, `percent: 33` (6/18 phases, 29/53 plans), `done=false`; 2026-11-18 audit: 6 passed / 1 human_needed / 10 gaps_found. |
| S7 | `docs/quality-CW3.md` | Wave 3 scoped gates: PASS/FAIL recorded, 1 pytest FAIL (QF-CW3-01) — evidence that planning state moved after CW1/CW2 without any Linear-fake row closing. |
| S8 | `.planning/STATE.md` Notion research line (2026-11-17 tail) | No Notion page/database maps onto v2.4 phases by name; single context page `40abc1a0…b7c` (STATE:134) is read-only reference only. |

**No live write was made or attempted:** no Linear POST/PUT, no Notion
write, no network call to a tracker host. Every external ID below is a
deterministic fake `TBD-LIN-…` or `null` (plan R-P1/R-P2).

## 2. Source-of-truth rule (applied to every row)

> **STATE verdict > Linear workflow status** for phase completion
> (plan §4). Notion never overrides either. Linear-fake `done` for a
> STATE `gaps_found` phase = **conflict** (park for user ruling, never
> auto-close). Linear-fake open for a STATE `passed` phase = **drift**
> (auto-correctable in fake mode). STATE frontmatter is never hand-edited
> to chase tracker state.

Classes used below: `synced` (fake workflow matches STATE verdict),
`stale` (fake row exists but its evidence/wording predates STATE tail
progress), `missing` (STATE phase with no fake row, or fake row with no
phase), `conflict` (fake/ROADMAP `done` vs STATE `gaps_found`, or STATE
internal contradiction that makes any single fake status wrong).

## 3. Reconciliation table — every phase sourced

Linear-fake workflow state is read from plan §1 headers + §3 parents:
all 12 outstanding parents are **open** (pause-gated, none `done`); the
6 closed phases have **no row** by design (R-P5). STATE verdict is from
S5/S6 + reconcile-CW1 §1.

| Phase | Linear-fake (fake ID) | STATE / ROADMAP verdict | Class | Evidence | Next action (owner) |
|---|---|---|---|---|---|
| 6 — Compile-first pin | `TBD-LIN-v24-6-P` open | `human_needed` (18/20 truths) | **stale** | Fake WI-6.1 still "await current-tree run"; STATE tail: GE-1 runs started 2026-11-17, may satisfy the 2 truths (C9) | Verifier re-run, then flip fake `W02` to `passed` or keep `gaps_found` — never silent close. |
| 7 — Parity harness | — (no row, by design) | `passed` | **synced** (`closed_with_debt`, do not re-open) | ROADMAP Status `passed`; audit 6-passed includes 7; STATE:890 supersedes STATE:109 (211/211 replay) | None. Absence of a fake row is correct. |
| 8 — Wasmer spike | `TBD-LIN-v24-8-P` open | `gaps_found` (3 NOT MET) | **synced** | ROADMAP Status + audit gaps_found agree; fake W02 = A10 exception (L3) | Hold for A10/user exception decision. |
| 9 — Scheduler join | `TBD-LIN-v24-9-P` open | `gaps_found` (2026-11-02 count stale) | **stale** | Fake W01/W02 predate 2026-11-18 row work: rows 26 (`a760263`) + 37 (`61880a5`) already in history, VERIF rows still NOT MET/PARTIAL (C9) | Quiet-host verifier re-run; refresh fake W02 counts from re-run, not by hand-editing Status. |
| 10 — Wasmtime sandbox | — (no row, by design) | `passed` (debt recorded) | **synced** (`closed_with_debt`) | ROADMAP `passed`; audit passed | None. |
| 11 — PyO3 + wheel CI | `TBD-LIN-v24-11-P` open | `gaps_found` (no GitHub CI run) | **synced** | STATE:901 — gate-3 smoke runs locally, first CI run needs unauthorised push; fake W01 = push authorisation | User authorises push; fake stays open until first GitHub `wheel.yml` run. |
| 12 — Port units | `TBD-LIN-v24-12-P` open | `gaps_found` frontmatter vs `pass_with_recorded_debt` body | **conflict** (C6) | ROADMAP:142–148 internal disagreement; STATE:902 — VERIF row 4 not met until verifier re-runs | Park fake W02 in `blocked_hold`; verifier re-run decides. No auto-resolve. |
| 13 — Browser deliverable | `TBD-LIN-v24-13-P` open | `gaps_found` | **synced** | ROADMAP Status + audit agree (loader depth 128≠32, 100 kB cap, no browser gate) | Evidence work items W01–W03; sequence blocked by Phase 12 regardless. |
| 14 — Acceptance proof | `TBD-LIN-v24-14-P` open | `gaps_found` (D1 accepted, status unchanged) | **conflict** (C7) | STATE:271 D1 accepts contended A2 run, yet Status stays `gaps_found` | User ruling on fake W02 / WI-14.2: does D1 close the last criterion? Status stays `gaps_found` until ruled. |
| 14.1 — Hot-path fixes | `TBD-LIN-v24-14.1-P` open | `gaps_found` (criteria met, debt open) | **synced** (terminology debt) | ROADMAP convention counts "criteria met, debt open" as gaps_found; D-P8/D-P11 never closed in STATE | User reconfirmation (fake W01/W02); record accepted debt, do not close by relabel. |
| 14.1.1 — Rust redaction | — (no row, by design) | `passed` | **synced** (`closed_with_debt`) | ROADMAP `passed`; audit passed | None. |
| 14.2 — CI evidence | — (no row, by design) | `passed` | **synced** (`closed_with_debt`) | ROADMAP `passed`; audit passed | None. |
| 14.3 — Read routes | — (no row, by design) | `Closed as pass_with_recorded_debt`, no flip | **synced** (`closed_with_debt`) | STATE:900 debt "accepted; it is not open work" | None. Do not re-open without new user decision. |
| 14.4 — Scheduler runtime | `TBD-LIN-v24-14.4-P` open | `gaps_found/INCOMPLETE (held)` vs "Gaps closed" vs "complete as written but not counted" | **conflict** (BLOCKER C3/C4) | ROADMAP:218–254 three-way state + DISPUTED 2026-11-16 COMPLETE relabel; STATE frontmatter `current_phase: 14.4`, audit `done=false`. Any fake `done` here would be a conflict under §2. | User ruling (WI-14.4.1); code stays held by R2 gate. Fake stays open. |
| 14.5 — Write routes | — (no row, by design) | `Closed as pass_with_recorded_debt`, no flip | **synced** (`closed_with_debt`) | STATE:900 (kill-9 gap stays in record) | None. |
| 14.6 — Chat-thread runtime | `TBD-LIN-v24-14.6-P` open | `gaps_found` (GE-1 open by user choice) | **synced** (held) | STATE:105 + ROADMAP:273 — 6/6 plans done ≠ closed; blocked by held 14.4 | User choice on GE-1 (fake W01); cannot close while 14.4 held. |
| 14.7 — OMP plugin/MCP | `TBD-LIN-v24-14.7-P` open | `gaps_found`, no VERIFICATION.md, AC-14.7-01 NOT PASSED | **conflict** (BLOCKER C5) + **stale** | Official dep Phase 14 vs de-facto dep held 14.4 (Wave 2); DISPUTED COMPLETE (C4). Stale: STATE tail now records `14.7-CONTRACT.md` written + W2 renamed — fake W04 still says "no contract". | Owner ruling (WI-14.7.1): correct dep or split Wave 1/Wave 2. Schedule on de-facto edge meanwhile. |
| 15 — Audit/cleanup/ship | `TBD-LIN-v24-15-P` open | `INCOMPLETE`, blocked before audit, no VERIFICATION.md | **conflict** (BLOCKER C8) | ROADMAP "needs explicit user confirmation" + STATE "ship stays blocked" vs STATE Operator Next Steps "Start the next milestone" (STATE:140) | User confirmation token (WI-15.1) or strike next-milestone line. No audit/cleanup/ship run meanwhile (L3/L4). |
| Milestone v2.4 (aggregate) | (no milestone fake row exists) | Frontmatter `gaps_found`, 33% vs Current Position "complete", 39% | **conflict** (C1/C2) + **missing** | STATE:5–17 vs STATE:33–36,140; audit `done=false` supports frontmatter. No fake milestone tracker row exists to derive a workflow status from. | User rules which STATE line is authoritative; re-run GSD status for percent. Do not start next milestone or claim ship readiness. |

**Counts (checkable by hand):** 18 v2.4 phases = 12 outstanding rows
above (6 `synced`, 3 `stale`, 5 `conflict`, 1 aggregate `conflict` +
`missing` milestone row) + 6 closed-with-debt rows with no fake row by
design (`synced`). Fake Issues: 12 parents + 33 children = 45; `tbd_real_id`
= `null` on all 45 (plan §3). Orphan fake issues: **0** (no
`TBD-LIN-0099`-style orphan was seeded in this task; plan §5.5 T-ORPHANS
remains a future fake-test item). Orphan phases: **0** — every STATE
phase is either mapped (§1) or explicitly closed-with-debt (R-P5).

### Notion (never overrides)

Single read-only context page `https://app.notion.com/p/40abc1a0c7ae81b588b7e01197a8d77c`
(STATE:134) is cited identically for all rows (plan §3). STATE research
(2026-11-17) found **no** Notion page/database mapping onto v2.4 phases
by name, so Notion carries no per-phase status and cannot override STATE
or Linear-fake on any row. No Notion write was made.

## 4. STATE log line — ready to append verbatim (atomic write preserved)

`docs/summary-CW4.md` is the exclusive target, so `.planning/STATE.md`
was **not edited** in this task: frontmatter untouched, no hand-edit, no
`percent`/status change. The single log line below is recorded verbatim
for the STATE owner to append **once**, through the owning atomic append
path (single-line append to the Run Log / Orchestration tail, preserving
the file's existing atomic-write convention — never a frontmatter edit):

```text
- Summary CW4 reconciliation (Linear-fake vs planning state, fakes only, 2026-11-27): STATE verdicts stand over 12 Linear-fake parents (TBD-LIN-v24-*-P, tbd_real_id=null) — 6 synced, 3 stale (6, 9, 14.7), 5 conflict (12 C6, 14 C7, 14.4 C3/C4, 14.7 C5, 15 C8), 6 closed-with-debt with no row by design, 0 orphans; milestone aggregate conflict C1/C2 (frontmatter gaps_found 33% vs Current Position complete 39%, audit done=false) + no fake milestone row (missing); Notion read-only context only, never overrides. Next action: user rulings on C3/C4 (14.4), C5 (14.7 dep), C7 (D1), C8 (Phase 15 confirmation) + verifier re-runs for 6/9/12; code stays held. Evidence: docs/summary-CW4.md §§1–3, docs/plan-CW2.md §§3–4, docs/reconcile-CW1.md §§1–3, docs/quality-CW3.md, .planning/ROADMAP.md Status lines, .planning/STATE.md:5-17,27-36,899-934. No live Linear/Notion write.
```

Append check (for the STATE owner, not run here): exactly one line
added, `git diff --stat .planning/STATE.md` shows 1 insertion / 0
deletions, frontmatter (`status`, `percent`, `current_phase`) byte-identical.

## 5. Trace (every fact cited)

| Claim | Source |
|---|---|
| 12 outstanding + 6 closed-with-debt phases, C1–C12 | `docs/reconcile-CW1.md` §§1–3 |
| 45 fake Issues (12 + 33), `tbd_real_id=null`, closed = no row | `docs/plan-CW2.md` §3, R-P5 |
| `STATE verdict > Linear`, Notion never overrides | `docs/plan-CW2.md` §4 |
| Per-phase pause gates / work items | `docs/plan-CW2.md` §1 |
| Status lines, disputed COMPLETE relabels | `.planning/ROADMAP.md:86–301` |
| Frontmatter/audit/tail, D1, GE-1, 14.7 contract | `.planning/STATE.md:5–17,27–36,105,140,271,890–934` |
| Wave 3 scoped evidence (no phase closed) | `docs/quality-CW3.md` |
| Notion: no phase-mapping page | `.planning/STATE.md` 2026-11-17 research line |

*Summary only. Gates/formatters skipped per acceptance. No code changed,
no live Linear/Notion write attempted, STATE.md not edited.*
