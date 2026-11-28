# Reconcile SPE-38338 — Roadmap / STATE cross-reconciliation

**Issue:** SPE-38338 (TBD IDs — fakes only, no live Linear/Notion writes)
**Branch:** swarm/T74c2-be (`agent-swarm`, repo `swcstudiospace/swarm`)
**Sources read:**
- `ROADMAP.md` (repo root) — **NOT FOUND** (read: `Path 'ROADMAP.md' not found`; glob `**/ROADMAP.md` returns no matches). No second roadmap source exists at root.
- `.planning/ROADMAP.md` (305 lines) — the only roadmap source. Milestones v2.0–v2.3 shipped; v2.4 in progress.
- `.planning/STATE.md` (933 lines) — read frontmatter + Current Position + 2026-11-18 audit tail (lines 899–933). Not edited, per target.
**Decisions (assignment context, not file-derived):** fakes only (no live Linear/Notion); learning disabled until policy; approver = user via pause; tool-unless-judgement.
**Rule:** No invented phases. Every phase below is quoted from `.planning/ROADMAP.md`; every state is quoted from ROADMAP Status lines or STATE frontmatter/audit. Contradictions are flagged as blockers, not resolved by guessing.

## 1. Full phase table (source + state + conflicts)

Shipped milestones v2.0 (Phases 1–7), v2.1 (1–6), v2.2 (1–4), v2.3 (1–5) are all `✅ shipped` in ROADMAP Milestones and `completed` in the milestone detail blocks — **not outstanding, not repeated here** (they are shipped history, not work). v2.4 (Phases 6–15 + inserted 14.1–14.7) is the outstanding scope.

| Phase | Depends on (ROADMAP) | ROADMAP Status (verbatim) | STATE status | Source | Conflict |
|---|---|---|---|---|---|
| 6 — Compile-first pin and workspace build | Phase 5 | `human_needed (2026-11-12 per 06-VERIFICATION.md: 18/20 truths, 2 await current-tree build/test run)` | Frontmatter `status: gaps_found`, `current_phase: 14.4`; 2026-11-18 audit: `1 human_needed (6)` | ROADMAP:80–86; STATE:5–17, 899 | **CONFLICT (stale):** HTIL 2026-11-17 `stopped_at` says GE-1 workspace/pytest runs started and `rust/ suite green at 28fc396`, which may satisfy the 2 pending truths — but STATE never closes Phase 6. Unresolved; needs verifier re-run, not an edit. |
| 7 — Parity harness and frozen contracts | Phase 6 | `passed (per 07-VERIFICATION.md 2026-11-08)` | Audit: `6 passed (7, …)` | ROADMAP:91–97; STATE:899 | None. Closed. |
| 8 — Wasmer full-core spike report | Phase 7 | `gaps_found (per 08-VERIFICATION.md 2026-11-02: 3 criteria NOT MET …)` | Audit: `10 gaps_found (8, …)` | ROADMAP:102–108; STATE:899 | None. Outstanding. |
| 9 — Host-native scheduler join and strangler seam | Phase 8 | `gaps_found (per 09-VERIFICATION.md 2026-11-02: 21 MET, 6 PARTIAL, 10 NOT MET, 2 NOT RUN of 39 …)` | Audit: gaps_found; STATE:904–924 add 2026-11-18 row work (rows 1, 2, 7, 16, 18, 22, 26, 37) verified/committed without closing the phase | ROADMAP:110–116; STATE:899, 904–924 | **CONFLICT (partial progress):** 2026-11-18 notes verify individual rows and commit them, but ROADMAP Status remains the 2026-11-02 `gaps_found` with stale rows (e.g. row 26 NOT MET recorded stale, row 37 PARTIAL stale). Phase is neither re-verified nor re-closed. |
| 10 — Wasmtime sandbox and omp validation | Phase 9 | `passed (per 10-VERIFICATION.md; 16/17 or 17/17 …)` | Audit: passed | ROADMAP:121–127; STATE:899 | None. Closed (debt recorded in VERIFICATION). |
| 11 — Required PyO3 extension and wheel CI | Phase 10 | `gaps_found (per 11-VERIFICATION.md 2026-11-02)` | Audit: gaps_found; STATE:901 notes gate 3 smoke runs locally, first CI run needs push (not authorised) | ROADMAP:131–137; STATE:899, 901 | **CONFLICT (evidence scope):** ROADMAP says U5/wheel CI lack CI step/run; STATE says the smoke gate is in the wheel job locally. CI evidence, not local run, is the gap. Outstanding until first CI run. |
| 12 — Port units and strangler flips | Phase 11 | `gaps_found (frontmatter per 12-VERIFICATION.md 2026-10-25). Body of VERIF says pass_with_recorded_debt` | Audit: gaps_found; STATE:902 notes U12.2 item 4 rewritten in PLAN, VERIFICATION row 4 still not met until verifier re-runs | ROADMAP:142–148; STATE:899, 902 | **CONFLICT (internal):** ROADMAP frontmatter (`gaps_found`) vs body (`pass_with_recorded_debt`) disagree. Treat as outstanding (audit counts gaps_found). |
| 13 — Browser deliverable with isolation headers | Phase 12 | `gaps_found (per 13-VERIFICATION.md 2026-11-02)` | Audit: gaps_found | ROADMAP:153–159; STATE:899 | None. Outstanding. |
| 14 — Acceptance proof | Phase 13 | `gaps_found. … Phase 14 stays open only for the quiet-host A2 record under decision D1.` | Audit: gaps_found; STATE D1 (line 271) accepts contended A2 run as the Phase 14 A2 record | ROADMAP:164–170; STATE:899, 271 | **CONFLICT (decision vs status):** D1 accepts the A2 record, yet Status stays `gaps_found`. Either D1 closes the last criterion or it does not — STATE never says. Outstanding until user rules. |
| 14.1 — Measured hot-path fixes and Rust redaction decision | Phase 14 | `gaps_found (convention: criteria met, debt open per 14.1-VERIFICATION.md 2026-11-10)` | Audit: gaps_found | ROADMAP:175–181; STATE:899 | **CONFLICT (terminology):** "criteria met, debt open" counts as `gaps_found` in the audit. Outstanding only if debt (D-P8 reconfirmation, D-P11 opt-in WAL confirm) is still owed — STATE never closes D-P8. |
| 14.1.1 — Rust redaction prototype | Phase 14.1 | `passed (per 14.1.1-VERIFICATION.md)` | Audit: passed | ROADMAP:186–192; STATE:899 | None. Closed (recorded debt D-P14 + wall-clock tests). |
| 14.2 — CI evidence and runner decision | Phase 14.1.1 | `passed (per 14.2-VERIFICATION.md)` | Audit: passed | ROADMAP:197–203; STATE:899 | None. Closed (debt: first Omega-pool CI run only after ship PR). |
| 14.3 — Read routes and sandbox default flip | Phase 14.2 | `Closed as pass_with_recorded_debt (2026-10-30, D-P43)` — "no flip" | Audit: passed; STATE:900: debt "accepted; it is not open work" | ROADMAP:208–214; STATE:899–900 | None. Closed with debt. ROADMAP `Plans` checkbox is `[x]`. |
| 14.4 — Rust scheduler runtime and default flip | Phase 14.2 | `gaps_found / INCOMPLETE (held)` + `DISPUTED: a 2026-11-16 run relabelled this phase COMPLETE` + later `Gaps closed per updated VERIF (2026-11-16)` paragraph | Frontmatter: `current_phase: 14.4`, `status: gaps_found`; audit: `14.4 complete as written but not counted (its gate holds). done=false` | ROADMAP:218–254; STATE:5–17, 899 | **BLOCKER — direct contradiction:** Three states at once — ROADMAP `gaps_found/INCOMPLETE (held)` vs ROADMAP `Gaps closed per updated VERIF (2026-11-16)` vs STATE audit `complete as written but not counted`. The 2026-11-16 COMPLETE relabel is explicitly disputed in-roadmap ("gaps_found is never relabelled to bypass assess"). Cannot start Phase 14.4 code or claim completion without a user ruling. |
| 14.5 — Write routes in the sandbox | Phase 14.3 | `Closed as pass_with_recorded_debt (2026-10-30, D-P43, D-P44). Every write route stays opt-in (default python).` | Audit: passed; STATE:900: debt accepted, kill -9 gap "real and stays in the record" | ROADMAP:256–262; STATE:899–900 | None. Closed with debt. |
| 14.6 — Chat-thread runtime | Phase 14.4 (plan 05 needed 14.4-02) | `Implemented, gated and plan-checked through round 3… The verifier verdict stays gaps_found (GE-1, cargo test --workspace, is open by the user's choice).` | Audit: gaps_found; STATE:105: `Phase 14.6 is gaps_found (2 major, 4 minor), with GE-1 open by your choice` | ROADMAP:267–273; STATE:899, 105 | **CONFLICT (gate vs implementation):** All 6 plans `[x]`, "Implemented, gated", yet verifier `gaps_found`. STATE says GE-1 open "by the user's choice" — so the phase cannot close without that choice being reversed. Outstanding. |
| 14.7 — OMP plugin and local MCP integration | Phase 14 (officially) | `gaps_found, no VERIFICATION.md. DISPUTED: a 2026-11-16 run marked this phase COMPLETE… AC-14.7-01 (wave 0) was NOT PASSED` | Audit: gaps_found; STATE:905, 910: Wave 1 only, no Wave 2 plan/contract on disk (contract later written), W2-01/W2-04 blocked on held Phase 14.4 | ROADMAP:283–289; STATE:899, 905, 910 | **BLOCKER — dependency conflict:** Official `Depends on: Phase 14`, but STATE records Wave 2 blocked on the held Phase 14.4. The 2026-11-16 COMPLETE relabel is disputed and AC-14.7-01 NOT PASSED. Outstanding; dependency must be corrected before scheduling. |
| 15 — Audit, cleanup and ship | Phase 14.7 | `INCOMPLETE, blocked before audit, no VERIFICATION.md. DISPUTED: a 2026-11-16 run marked this COMPLETE. Cleanup and ship need explicit user confirmation.` Plan `[ ]` open. | Audit: gaps_found; STATE:105: `Phase 15 is not to run unattended…`, `BLOCKED at AUDIT`; STATE:140: Operator Next Steps = "Start the next milestone with /gsd-new-milestone" | ROADMAP:295–304; STATE:899, 105, 140 | **BLOCKER — instruction conflict:** ROADMAP says audit/cleanup/ship needs explicit user confirmation and is not run unattended; STATE Operator Next Steps says start the next milestone. Both cannot be true. Phase 15 Plan checkbox is still open. Outstanding. |

### Shipped / closed phases (for completeness, not outstanding)
- **Shipped:** v2.0 Phases 1–7, v2.1 Phases 1–6, v2.2 Phases 1–4, v2.3 Phases 1–5 (all `[x]` in ROADMAP detail blocks, ship dates 2026-09-26 to 2026-10-22).
- **v2.4 closed:** Phase 7 (`passed`), Phase 10 (`passed`), Phase 14.1.1 (`passed`), Phase 14.2 (`passed`), Phase 14.3 (`pass_with_recorded_debt`, "no flip"), Phase 14.5 (`pass_with_recorded_debt`, "no flip"). STATE audit carries the same six as `passed`.

## 2. Dependency-ordered outstanding-phase list

Order follows ROADMAP `Depends on` edges. Branches are shown where the DAG forks (14.4 and 14.3 both depend on 14.2; 14.7 officially depends on 14 but de-facto on 14.4 — flagged, not re-drawn).

1. **Phase 6 — Compile-first pin and workspace build** (`human_needed`; depends: Phase 5, shipped). Verify the 2 pending truths with a current-tree build/test run; close `human_needed` or keep gaps_found. Blocks everything below that claims a green baseline.
2. **Phase 8 — Wasmer full-core spike report** (`gaps_found`; depends: 7, closed). 3 criteria NOT MET (A10 exception approval, full-core measurement). Blocks 9.
3. **Phase 9 — Host-native scheduler join and strangler seam** (`gaps_found`; depends: 8). 10 NOT MET + 6 PARTIAL + 2 NOT RUN (2026-11-02 VERIF) plus 2026-11-18 row work awaiting a verifier re-run on a quiet host. Blocks 10's parity claim and 14.4's facade.
4. **Phase 11 — Required PyO3 extension and wheel CI** (`gaps_found`; depends: 10, closed). U5 quiet-host baseline + U2 smoke gate lack a GitHub CI step/run; `wheel.yml` never executed on GitHub. Blocks 12. A push/CI run is needed — user authorisation required (no silent push).
5. **Phase 12 — Port units and strangler flips** (`gaps_found` frontmatter vs `pass_with_recorded_debt` body; depends: 11). U12.2 item 4 replan/verification open. Blocks 13.
6. **Phase 13 — Browser deliverable with isolation headers** (`gaps_found`; depends: 12). Loader depth 128-not-32, 100 kB cap, browser gate not run. Blocks 14.
7. **Phase 14 — Acceptance proof** (`gaps_found`; depends: 13). Last criterion: quiet-host A2 record under D1 (STATE D1 accepts contended run; Status still gaps_found — user ruling needed). Blocks 14.1 and 14.7 (official).
8. **Phase 14.1 — Measured hot-path fixes and Rust redaction decision** (`gaps_found` — criteria met, debt open; depends: 14). D-P8 (Rust redaction reconfirmation) + D-P11 (WAL opt-in) debt. Blocks 14.1.1 is already closed, so 14.1 debt does not block 14.2 empirically — but ROADMAP DAG says 14.1.1→14.2. Treat debt as recorded, not blocking downstream close-out.
9. **Phase 14.4 — Rust scheduler runtime and default flip** (`gaps_found/INCOMPLETE (held)` — BLOCKER, see §1; depends: 14.2, closed). Held by R2 independent-review gate (A09 + A10 rechecks, inventory green). Local S5 → feature-only push → actual Omega CI → quiet-host S6 (both p50 and p95 strictly < 1.00) → separately approved S7. No default flip, no ship. Blocks 14.6 and 14.7-Wave-2 (de-facto).
10. **Phase 14.6 — Chat-thread runtime** (`gaps_found`; depends: 14.4). GE-1 workspace test open "by the user's choice". Cannot close while 14.4 is held and GE-1 is open.
11. **Phase 14.7 — OMP plugin and local MCP integration** (`gaps_found`, no VERIFICATION.md, AC-14.7-01 NOT PASSED; depends: 14 officially, 14.4 de-facto for Wave 2). Wave 2 (14.7-W2-01–W2-04) blocked on held 14.4. Blocks 15.
12. **Phase 15 — Audit, cleanup and ship** (`INCOMPLETE`, blocked before audit; depends: 14.7). No VERIFICATION.md. Audit + cleanup + ship need explicit user confirmation; ship assessment `done=false` (STATE audit). Last phase; nothing downstream except STATE's contradictory "start next milestone" line — that line must be struck or the phase must be confirmed complete by the user before a new milestone opens.

**Closed with recorded debt (not outstanding):** 14.3 (R9 quiet window deferred, R11/R12 user approval — all routes stay opt-in, default `python`), 14.5 (U3.8a quiet window, U3.8c per-route flip approvals, rollback drills, kill -9 gap — all routes opt-in, default `python`). STATE:900: debt "accepted; it is not open work". Do not re-open without a new user decision.

## 3. Blockers — contradictions requiring a user ruling (never auto-fix)

1. **B1 — Milestone v2.4 status:** STATE frontmatter (`status: gaps_found`, `current_phase: 14.4`, 33%) + Current focus ("Milestone v2.4 remains INCOMPLETE") vs STATE Current Position ("Phase: Milestone v2.4 complete … Last activity: 2026-11-16 — Milestone v2.4 completed and archived", progress bar 39%) and Operator Next Steps ("Start the next milestone with /gsd-new-milestone"). Three positions, one file. The 2026-11-18 audit tail (`done=false`) supports the frontmatter. **User must rule: which line is authoritative?** Until then, do not start a next milestone and do not claim ship readiness.
2. **B2 — Phase 14.4 three-way state** (held `gaps_found/INCOMPLETE` vs `Gaps closed per updated VERIF` vs audit "complete as written but not counted"). The 2026-11-16 COMPLETE relabel is disputed in-roadmap. **User ruling required** on whether the updated VERIF closes the phase or the gate still holds. Phase 14.4 code stays held in the interim (STATE: held by R2 gate).
3. **B3 — Disputed 2026-11-16 COMPLETE relabels:** Phases 14.4, 14.7, 15 were each "marked COMPLETE" by a 2026-11-16 run using Python dispatch layers as evidence, and each is DISPUTED in-roadmap with the milestone-audit rule ("gaps_found is never relabelled to bypass assess"). **User ruling required** on the evidential standard for COMPLETE before any relabel stands.
4. **B4 — Phase 14.7 dependency mismatch:** ROADMAP `Depends on: Phase 14` vs STATE de-facto dependency on held Phase 14.4 for Wave 2. Scheduling must use the de-facto edge until the user corrects the roadmap. **User/owner must correct** ROADMAP dependency or explicitly split 14.7 into Wave 1 (independent) and Wave 2 (14.4-gated).
5. **B5 — Phase 15 "start next milestone" conflict:** STATE Operator Next Steps says start the next milestone; ROADMAP says Phase 15 needs explicit user confirmation and STATE Blockers say "Ship stays blocked until actual acceptance and assess return done". **User must confirm** whether Phase 15's audit/cleanup/ship is authorised or the next-milestone line is stale.

## 4. Notes for the next agent

- **`ROADMAP.md` (repo root) is missing** — the assignment listed it plus `.planning/ROADMAP.md` as the two sources. Only the `.planning/` copy exists. Any consumer expecting a root roadmap will find nothing. If a root copy is wanted, it must be generated by the GSD tooling (`gsd` writes `.planning/ROADMAP.md`); do not hand-copy.
- **STATE Progress numbers disagree:** frontmatter `percent: 33` (6/18 phases, 29/53 plans) vs Current Position bar `39%`. Neither is updated after the 2026-11-18 audit. Reconcile by re-running the GSD status command, not by editing STATE by hand (STATE is read-only for this task).
- **No live Linear/Notion writes** were made in this reconciliation — Linear rows were never created (free issue limit, per STATE Run Log) and the existing Notion task is referenced read-only. SPE-38338 has no Linear/Notion ID here; any tracker row must be created as a fake/TBD per "fakes only".
- **Learning disabled until policy:** the 2026-11-17 HTIL decisions (Pool Resize = `dynamic_within_cap`, sonic inline, `crates/` deliverable, GitHub-hosted CI) are recorded in STATE/ROADMAP as HTIL notes, but no policy artefact is cited. Treat as user-recorded choices, not ratified policy.
- **Approver = user via pause:** every B1–B5 blocker above is a pause point. Do not proceed with phase scheduling, code release, or ship on inferred answers.

## 5. Reconciliation trace (every fact cited)

| # | Fact | Source (file:line) |
|---|---|---|
| 1 | v2.4 phases 6–15 + inserted 14.1–14.7, started 2026-10-22 | `.planning/ROADMAP.md:9` |
| 2 | 2026-10-31 checkpoint: INCOMPLETE, code held | `.planning/ROADMAP.md:65` |
| 3 | Phase 6 human_needed, 18/20 truths | `.planning/ROADMAP.md:80–86` |
| 4 | Phase 7 passed | `.planning/ROADMAP.md:91–97` |
| 5 | Phase 8 gaps_found, 3 criteria NOT MET | `.planning/ROADMAP.md:102–108` |
| 6 | Phase 9 gaps_found, 21 MET / 6 PARTIAL / 10 NOT MET / 2 NOT RUN | `.planning/ROADMAP.md:110–116` |
| 7 | Phase 10 passed | `.planning/ROADMAP.md:121–127` |
| 8 | Phase 11 gaps_found | `.planning/ROADMAP.md:131–137` |
| 9 | Phase 12 frontmatter gaps_found vs body pass_with_recorded_debt | `.planning/ROADMAP.md:142–148` |
| 10 | Phase 13 gaps_found | `.planning/ROADMAP.md:153–159` |
| 11 | Phase 14 gaps_found, open only for quiet-host A2 under D1 | `.planning/ROADMAP.md:164–170` |
| 12 | Phase 14.1 gaps_found, criteria met debt open | `.planning/ROADMAP.md:175–181` |
| 13 | Phase 14.1.1 passed | `.planning/ROADMAP.md:186–192` |
| 14 | Phase 14.2 passed | `.planning/ROADMAP.md:197–203` |
| 15 | Phase 14.3 closed pass_with_recorded_debt, no flip | `.planning/ROADMAP.md:208–214` |
| 16 | Phase 14.4 gaps_found/INCOMPLETE held + DISPUTED COMPLETE + Gaps closed paragraph | `.planning/ROADMAP.md:218–254` |
| 17 | Phase 14.5 closed pass_with_recorded_debt, no flip | `.planning/ROADMAP.md:256–262` |
| 18 | Phase 14.6 implemented/gated but verifier gaps_found (GE-1 open by user choice) | `.planning/ROADMAP.md:267–273` |
| 19 | Phase 14.7 gaps_found, no VERIFICATION.md, AC-14.7-01 NOT PASSED, DISPUTED COMPLETE | `.planning/ROADMAP.md:283–289` |
| 20 | Phase 15 INCOMPLETE, blocked, DISPUTED COMPLETE, needs user confirmation | `.planning/ROADMAP.md:295–304` |
| 21 | STATE frontmatter: gaps_found, current_phase 14.4, 18 phases / 6 completed, 33% | `.planning/STATE.md:5–17` |
| 22 | STATE Current focus: v2.4 remains INCOMPLETE; Current Position: v2.4 complete/archived 2026-11-16, 39%; Next step: start next milestone | `.planning/STATE.md:27, 33–36, 140` |
| 23 | Audit 2026-11-18: 6 passed, 1 human_needed, 10 gaps_found, 14.4 complete as written but not counted, done=false | `.planning/STATE.md:899` |
| 24 | 14.3/14.5 debt accepted, not open work | `.planning/STATE.md:900` |
| 25 | Phase 15 not to run unattended | `.planning/STATE.md:105` |
| 26 | D1 accepts contended A2 run (2026-10-25) | `.planning/STATE.md:271` |
| 27 | 14.7 Wave 2 blocked on held 14.4 | `.planning/STATE.md:905, 910` |
| 28 | Root ROADMAP.md missing | read `ROADMAP.md` → not found; glob `**/ROADMAP.md` → no matches |
