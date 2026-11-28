# Critique SPE-38339 — Ambiguity / blocker critique from Wave-1 reconcile + graphcheck

**Issue:** SPE-38339 (TBD IDs — fakes only, no live Linear/Notion writes)
**Branch:** `swarm/T74c2-be` (`agent-swarm`, repo `swcstudiospace/swarm`)
**Sources read (Wave-1, consumed not re-derived):**
- `docs/reconcile-SPE-38338.md` — 12 outstanding phases, 5 blockers B1–B5 (§3), 12 closed/shipped phases excluded.
- `docs/graphcheck-SPE-38338.md` — canonical 10-node order, 0 cycles, `TaskNode` no-tool-field gap, frontier starvation, sequential fallback.
**Own checks attempted for this critique (read-only, no gates/formatters per assignment):**
- `read .planning/STATE.md:1-45` — frontmatter `status: gaps_found`, `current_phase: 14.4`, 33% vs Current Position 39%/complete.
- `read .planning/STATE.md:890-933` — 2026-11-18 audit tail (`done=false`), Notion-no-mapping note (line 895), 14.7-W2 blocked on held 14.4 (line 905/910).
- `read .planning/ROADMAP.md:218-305` — 14.4 `gaps_found/INCOMPLETE (held)` + DISPUTED COMPLETE + later "Gaps closed" paragraph; 14.7 `Depends on: Phase 14`; 15 needs user confirmation.
- `read swarm/graph_planner.py:73-82` — `TaskNode` fields exactly `(id, files, dependencies, complexity, priority, duration)`; no tool field.
- `read agents.json:10-13` — `capability_policy.tools {min:10, max:15, enforce:true}`; skills same range.
- `grep "Linear|Notion.*authoritative|fakes only"` over `docs;INVENTORY.md;.planning/STATE.md` — Linear free-limit refusals, Notion rows exist, no authority statement found in tracker docs.
- `grep "learning disabled|session.*policy"` over `.planning/STATE.md;docs;agents.json` — no policy artefact; only hit is assignment context in `docs/reconcile-SPE-38338.md:9`.
- `grep "tool-unless-judgement"` over `.planning/STATE.md;docs;agents.json` — no file-derived source; only `docs/reconcile-SPE-38338.md:9` assignment context.
- `bash git branch --show-current` → `swarm/T74c2-be`; `ls docs/critique-SPE-38339.md` → not found before this write.

**Assignment decisions (context, not file-derived — flagged wherever used):** fakes only (no live Linear/Notion writes); learning disabled until policy; approver = user via pause; tool-unless-judgement.
**Rule:** No invented phases, topologies, thresholds, or policies. Every traced fact cites `path:line` or `(grep pattern)`. Where a check returned *not found*, that absence is recorded — not filled by guess. Contradictions are paused, never auto-fixed.

## How to read this critique

- **BLOCKING** = downstream work (scheduling, code, ship, tracker sync, graph optimisation) must pause until the user rules on the exact question in the pause decision. Skipping the pause would fabricate an answer.
- **SAFE-ASSUMPTION** = a bounded assumption that holds the pause without guessing: e.g. "keep learning disabled", "keep sequential fallback as degraded mode", "use frontmatter status as operative until user rules". The assumption is named, its blast radius is stated, and it is reversible on user ruling.
- **Exact pause decision** = the single question put to the user, with options, impact of each option, and preserved state (what stays untouched while paused).

## Roadmap conflicts (B1–B5) — decision log per blocker

### D-B1 — Milestone v2.4 status (three positions, one file)

| Field | Detail |
|---|---|
| Ambiguity | Which line is authoritative for v2.4 status: frontmatter (`gaps_found`, `current_phase: 14.4`, 33%) or Current Position ("Milestone v2.4 complete", 39%, "Awaiting next milestone")? |
| File check attempted | `read .planning/STATE.md:5-17` → frontmatter as above; `read .planning/STATE.md:27-36` → "Milestone v2.4 remains **INCOMPLETE**" vs "Milestone v2.4 complete / 2026-11-16 completed and archived"; `read .planning/STATE.md:899` → 2026-11-18 audit `done=false`. Reconcile §6 trace rows 21–22 agree. |
| Verdict | **BLOCKING.** Scheduling a next milestone or claiming ship readiness on either line alone contradicts the other two. The audit (`done=false`) supports frontmatter but does not delete the other lines. |
| Exact pause decision | *User rules: which single line is authoritative — (A) frontmatter `gaps_found/INCOMPLETE`, (B) Current Position `complete/archived 2026-11-16`, or (C) neither until GSD status re-run reconciles them?* |
| Options / impact | (A) Preserve paused hold: no next milestone, no ship claim; 12 outstanding phases stay outstanding. (B) Ship/next-milestone path opens only after B2+B3+B5 also rule released — B1 alone cannot authorise it. (C) Re-run GSD status command (reconcile §4 note) and re-issue STATE; until then treat as (A). |
| Preserved state | `.planning/STATE.md` not edited; `.planning/ROADMAP.md` phase statuses unchanged; no next-milestone branch opened; no ship/PR opened. |
| Safe-assumption (interim) | Operative status = frontmatter `gaps_found` + audit `done=false`. Reversible on ruling. |

### D-B2 — Phase 14.4 three-way state

| Field | Detail |
|---|---|
| Ambiguity | ROADMAP Status holds three states at once: `gaps_found/INCOMPLETE (held)` vs "Gaps closed per updated VERIF (2026-11-16)" vs STATE audit "complete as written but not counted (its gate holds)". |
| File check attempted | `read .planning/ROADMAP.md:224-226` → held + DISPUTED + S5→S6→S7 gate; `read .planning/ROADMAP.md:251` → "Gaps closed per updated VERIF (2026-11-16)"; `read .planning/STATE.md:899` → audit "complete as written but not counted". Reconcile §1 Phase 14.4 row verbatim. |
| Verdict | **BLOCKING.** Phase 14.4 code stays held; starting code, claiming close, or flipping default on any one of the three states would bypass the R2 gate. |
| Exact pause decision | *User rules: does the 2026-11-16 updated VERIF close Phase 14.4, or does the R2 independent-review gate (A09+A10 rechecks, inventory green) still hold?* |
| Options / impact | (A) Gate holds — code stays held, next is S5 local → feature-only Omega CI → quiet-host S6 (p50 and p95 < 1.00) → separately approved S7 (ROADMAP:226). (B) VERIF closes phase — gate is lifted by name, but S5/S6/S7 evidence still required before any default flip/ship (ROADMAP:226 "No default flip/PR/ship"). (C) Partial — close written criteria but keep flip gated. Each requires naming the recheck owner. |
| Preserved state | 14.4 held; Python default; `GSD_ENGINE=python`; no flip, no ship, no PR. Uncommitted pin-gated files stay uncommitted (STATE:907). |
| Safe-assumption (interim) | Held. No 14.4 code, no relabel. |

### D-B3 — Disputed 2026-11-16 COMPLETE relabels (14.4, 14.7, 15)

| Field | Detail |
|---|---|
| Ambiguity | A 2026-11-16 run marked 14.4/14.7/15 COMPLETE using Python dispatch layers / Python layers as evidence; each is DISPUTED in-roadmap under "gaps_found is never relabelled to bypass assess". What evidential standard makes a phase COMPLETE? |
| File check attempted | `read .planning/ROADMAP.md:224` → 14.4 DISPUTED; `.planning/ROADMAP.md:289` → 14.7 DISPUTED + AC-14.7-01 NOT PASSED; `.planning/ROADMAP.md:301` → 15 DISPUTED. STATE:896–898 → 14.7 verifier `gaps_found` (0/18 met, 5 partial, 13 unmet), matrix discovery criterion not yet evidenced. |
| Verdict | **BLOCKING.** No relabel stands until the standard is ruled; otherwise any run can relabel gaps_found → COMPLETE. |
| Exact pause decision | *User rules: evidential standard for COMPLETE — (A) verifier PASS + audit `done=true` required (no Python-layer proxy), (B) Python-layer evidence acceptable with named verifier waiver, or (C) per-phase standard?* |
| Options / impact | (A) 14.4/14.7/15 stay open; 2026-11-16 COMPLETE ignored. (B) Opens path to close on proxy evidence but must record waiver per phase and re-run assess. (C) Needs phase-by-phase ruling; slowest but explicit. |
| Preserved state | All three phases remain as recorded; no VERIFICATION.md written for 14.7/15; assess `done=false`. |
| Safe-assumption (interim) | Existing VERIFICATION + audit verdicts govern; Python layers are not completion evidence. |

### D-B4 — Phase 14.7 dependency mismatch

| Field | Detail |
|---|---|
| Ambiguity | ROADMAP `Depends on: Phase 14` vs STATE de-facto dependency on held 14.4 for Wave 2 (14.7-W2-01–W2-04). Scheduling on the official edge would dispatch Wave 2 into a held phase. |
| File check attempted | `read .planning/ROADMAP.md:287` → `Depends on: Phase 14`; `read .planning/STATE.md:905` → "Wave 2 depends on the held Phase 14.4"; `read .planning/STATE.md:910` → "W2-01 and W2-04 are blocked on Phase 14.4", contract renamed Wave 2 to W2-01–W2-04. |
| Verdict | **BLOCKING for Wave 2 scheduling; safe to continue Wave 1 read-only.** Wave 1 (14.7-01/02) is complete; Wave 2 cannot be scheduled until edge corrected. |
| Exact pause decision | *User/owner rules: (A) correct ROADMAP to `Depends on: Phase 14.4`, (B) split 14.7 into Wave 1 (independent, done) / Wave 2 (14.4-gated), or (C) keep official edge and treat STATE note as stale?* |
| Options / impact | (A) or (B) — Wave 2 waits on B2 release; Wave 1 stays closed. (C) risks dispatching W2-01/W2-04 against held 14.4 contracts — rejected as unsafe. |
| Preserved state | `14.7-CONTRACT.md` on disk; no Wave 2 plan/code dispatched on the official edge; AC-14.7-01 stays NOT PASSED. |
| Safe-assumption (interim) | De-facto edge (14.4) governs scheduling; official edge is treated as stale. |

### D-B5 — Phase 15 "start next milestone" vs user confirmation

| Field | Detail |
|---|---|
| Ambiguity | STATE Operator Next Steps ("Start the next milestone") vs ROADMAP Phase 15 ("needs explicit user confirmation", INCOMPLETE, blocked before audit) + STATE "Phase 15 is not to run unattended" / "Ship stays blocked until actual acceptance and assess return done". |
| File check attempted | `read .planning/ROADMAP.md:295-304` → 15 INCOMPLETE, plan `[ ]`; `read .planning/STATE.md:27` → "Ship stays blocked…"; reconcile §1 Phase 15 + §3 B5 trace (STATE:105, 140). `read .planning/STATE.md:899` → audit `done=false`. |
| Verdict | **BLOCKING.** Both cannot be true; authorising audit/cleanup/ship on the next-milestone line would run Phase 15 unattended. |
| Exact pause decision | *User rules: (A) strike the next-milestone line, Phase 15 audit/cleanup/ship requires separate explicit confirmation, or (B) next-milestone line stands and Phase 15 is deemed complete by user confirmation named here?* |
| Options / impact | (A) No new milestone, no audit/cleanup/ship until confirmation; preserves fail-closed. (B) Only valid if user names confirmation verbally (date + scope) and assess `done=true` is re-established — not inferable from the line alone. |
| Preserved state | 15-PLAN `[ ]`; no audit/cleanup/ship commands run; no `.planning/milestones/` new-milestone artefact created. |
| Safe-assumption (interim) | No new milestone, no ship. |

## Cross-cutting topics (required coverage)

### Roadmap conflicts

- **Traced:** D-B1–D-B5 above; plus stale-vs-current sub-conflicts in reconcile §1 (Phase 6 2 pending truths may be satisfied by HTIL `rust/ suite green at 28fc396` per STATE:6; Phase 9 2026-11-18 row work vs stale 2026-11-02 VERIF rows 26/37; Phase 11 local smoke vs missing CI run; Phase 12 frontmatter vs body; Phase 14 D1 accepts contended A2 run (STATE:271) vs Status `gaps_found`).
- **Check attempted:** `read .planning/STATE.md:6` (HTIL stopped_at), `read .planning/STATE.md:899-902` (audit + 14.3/14.5 debt + Phase 11 smoke), `read .planning/STATE.md:271` via reconcile trace row 26.
- **Verdict:** **BLOCKING** for each named phase close; **SAFE-ASSUMPTION** for listing them as outstanding (reconcile §2 order is retained unchanged; no re-drawn DAG).
- **Pause:** each phase close needs verifier re-run (quiet-host build/test) + user ruling where D1/D-P8/D-P11 debt is cited; debt 14.3/14.5 is accepted-not-open (STATE:900) and does not re-open without a new user decision.
- **Preserved:** phase list, order, and `Depends on` edges exactly as quoted; no hand-edit of STATE/ROADMAP by this lane.

### Linear / Notion authority

| Ambiguity | "Linear mirror" vs "Notion page" vs ".planning/STATE+ROADMAP" vs "Task Store" — which is the single source of truth for phase status? |
|---|---|
| File check attempted | `grep "Linear|Notion"` → `docs/acceptance.md:5-6,60-61,91-92` and `docs/architecture/blueprint.md:19,211` assert mirror invariants (12 Linear issues / 12 Notion pages, 0 orphans) but state no authority rule; `read .planning/STATE.md:26` → "Task Store state is authoritative, gates are fail-closed"; `grep "free issue limit"` → STATE:96, 134, 144–147, 189, 203, 344, 437, 696, 368 — Linear rows repeatedly *not created* (limit reached), Notion rows exist / `login required` / no v2.4-mapping page found (STATE:895, 344); `read .planning/STATE.md:895` → "no page or database that maps onto the v2.4 phases by name", SPE-34261/SPE-34263 ID overlap only. |
| Verdict | **BLOCKING for tracker sync; SAFE-ASSUMPTION for phase status.** No tracker can be authoritative today: Linear rows do not exist for v2.4, Notion has no phase-mapping page. Do not treat either as source of truth for milestone status. |
| Exact pause decision | *User rules: authority ranking — (A) Task Store (STATE:26) for runtime state + .planning/ROADMAP/STATE (file) for milestone status, trackers are mirrors only; (B) Linear re-created as mirror once limit is raised, with fakes until then; (C) Notion mapping page to be created first?* |
| Options / impact | (A) Preserves current fail-closed posture; reconcile stays file-based. (B) Requires limit raise + mirror creation (live writes — barred by assignment "fakes only"); until then fakes/TBD IDs. (C) Single organised place but still a live write; must not overwrite file status. |
| Preserved state | **No live Linear/Notion writes made or attempted** (grep proves read-only; reconcile §4 same). SPE-38338/SPE-38339 stay TBD/fake. No existing Notion task status changed. |
| Safe-assumption (interim) | Fakes only. `docs/reconcile-SPE-38338.md:72` (assignment context) is the only "fakes only" source — flagged as assignment-derived, consistent with STATE free-limit facts above, not a new policy. |

### Optimisation priority

| Ambiguity | Baseline vs optimisation: is sequential fallback the floor, or should the frontier / wave order already be optimised? Who decides priority order? |
|---|---|
| File check attempted | `grep "optimis|sequential.*fallback|baseline"` → `docs/graphcheck-SPE-38338.md:4` "No optimisation: this is the baseline record, not a plan"; `:109-116` sequential fallback *noted, not optimised* — single-wave `FileConflictError`, fallback = run wave order one node at a time; `read swarm/graph_planner.py:13-14,196-197` via graphcheck §2 → within-wave priority sort is planner-only; `read crates/gsd-scheduler/src/ready_frontier.rs:49-50` via graphcheck §5 → greedy workflow-order admission, `free_slots=0` starvation, no aging rule; `read swarm/graph_quality.py:85-86` via graphcheck §5 → cap 30/ceiling 32, over-cap = `429 E-CAPACITY` refusal only. `INVENTORY.md:138-181,236-247` cited in graphcheck §8. |
| Verdict | **BLOCKING for optimisation; SAFE-ASSUMPTION for baseline.** Optimisation (priority promotion, retry/rebalancing, aging) is out of scope until the baseline in graphcheck §2 (order `start, extra1, n1, n2, n3, n4, m1, m2, m3, last`, makespan 4.0) is accepted by the design trio. The baseline degraded mode is safe to keep. |
| Exact pause decision | *User/design-trio rules: (A) accept baseline + sequential fallback as floor, optimise only after Wave-2 (GraphModelDesign) ships; (B) optimise frontier starvation (aging/priority) before Wave-2; (C) optimise makespan (parallelism) before correctness?* |
| Options / impact | (A) Preserves correctness-first; starvation/deadlock stay recorded risks (graphcheck §5–§6). (B) Adds scheduler-side retry/drain outside DAG (graphcheck §5: "Recovery must come from outside the DAG") — must be designed, not assumed. (C) Rejected: graphcheck places sequential fallback as *baseline degraded mode*, not target. |
| Preserved state | No topology, wave, or priority change; no `graph_edges` rows written (still 0); no code changed. |
| Safe-assumption (interim) | Baseline accepted as record; sequential fallback is the only sanctioned degraded mode. |

### Tool / agent threshold + approver

| Ambiguity | Agent tool-count threshold and who approves a new/changed tool; is `tool-unless-judgement` a file rule or assignment-only? |
|---|---|
| File check attempted | `read agents.json:10-13` → `capability_policy.tools {min:10, max:15, enforce:true}`, skills same; `read swarm/graph_planner.py:73-82` → `TaskNode` has **no tool field** (graphcheck §4: tool requirement "cannot be expressed"); `grep "tool-unless-judgement"` → **no file-derived source** (only `docs/reconcile-SPE-38338.md:9` assignment context); `grep "capability_policy"` in STATE context → live `registered_tools` per agent min 12 / max 13 (graphcheck §4, inside 10–15). |
| Verdict | **Threshold: file-derived and BLOCKING only for per-node gating; Approver + tool-unless-judgement: assignment-only and BLOCKING if treated as policy.** Per-agent counts are enforceable today; per-graph-node tool requirements pass silently (graphcheck §4 risk). |
| Exact pause decision | *User rules: (A) threshold stays per-agent 10–15 enforced by `agents.json`, tool requirements remain per-agent (not per-node) until a `TaskNode` schema change is approved; approver for any new tool = user via pause (assignment). (B) Add per-node tool field to `TaskNode` + `contracts/sc-graph-engine.json` + migration — needs A03 design + user approval. (C) Treat `tool-unless-judgement` as ratified policy — needs user to ratify it as a file, not assignment context.* |
| Options / impact | (A) Safe today: 12–13 tools/agent inside 10–15; no silent per-node gating permitted. (B) Correct long-term fix for the graphcheck §4 gap but is a schema change (L3 scope, user-approved). (C) Cannot be assumed — grep proves no policy artefact exists. |
| Preserved state | `agents.json` untouched; no new tools registered; no `TaskNode` schema change; no tool added/removed. |
| Safe-assumption (interim) | Per-agent 10–15 governs; per-node tool gates are *recorded gaps, not refusals* (graphcheck §4). Assign `tool-unless-judgement` as assignment decision only — never cited as file policy. |

### Session policy (learning disabled until policy — confirmed)

| Ambiguity | Is "learning disabled until policy" a confirmed session policy or an assignment preference? |
|---|---|
| File check attempted | `grep "learning disabled|session.*policy"` → no file artefact; only `docs/reconcile-SPE-38338.md:9` assignment context lists it. `grep "learning"` in STATE/docs → only `docs/requirements/brief-T6dc4-req.md:12` "Self Improving / self retooling" as *future* scope (evidence: 394 inventory entries, 10–15 tool range), not a ratified policy. `read .planning/STATE.md:6` → HTIL 2026-11-17 decisions (sonic inline, `crates/` deliverable, GitHub-hosted CI) recorded as *HTIL notes*, reconcile §4 line 73: "no policy artefact is cited". |
| Verdict | **Confirmed as SAFE-ASSUMPTION (keep disabled); BLOCKING to enable.** "Learning disabled until policy" is *confirmed as the operating assumption*, sourced from assignment context + absence of any policy artefact. Enabling learning would be a new policy decision (L3), not a default. |
| Exact pause decision | *User rules: (A) keep learning disabled until a user-ratified policy artefact exists (file, versioned); (B) ratify HTIL notes (sonic inline, `crates/`, GitHub-hosted CI, Pool Resize `dynamic_within_cap`) as policy now; (C) enable learning with named limits?* |
| Options / impact | (A) Preserves current posture; self-improvement scope stays proposal-only. (B) Ratifies recorded choices — must be written as a file to take effect. (C) Requires policy + approver + session evidence rules before any agent acts on session learnings. |
| Preserved state | Learning remains disabled; no session-to-proposal writes; no policy file created by this lane. |
| Safe-assumption | Disabled until (A) is replaced by a ratified file. Reversible only by explicit user ratification. |

## Graphcheck gaps — traced (no new topology)

| Gap (from graphcheck) | File check | Verdict | Pause / preserved state |
|---|---|---|---|
| No persisted topology (`graph_edges` 0 rows) | `migrations/V0002__graph_engine.sql:25-30` + live `.swarm/tasks.db` 242 tasks, 0 edges (graphcheck §1, independently confirmed in STATE:934) | **SAFE-ASSUMPTION** to baseline on fixtures; **BLOCKING** to claim live order | Pause: none needed for baseline record; loading a real topology needs GraphModelDesign output (Wave-2). Preserved: no `graph_edges` rows written. |
| `TaskNode` no tool field | `read swarm/graph_planner.py:73-82` (this critique) | **BLOCKING** for tool-gated nodes; see tool/agent row | Same pause as tool/agent. Preserved: no schema change. |
| Rust failed-dependency deadlock (`dag.rs:68-70,140-141`) | `read crates/gsd-scheduler/src/dag.rs:1-211` via graphcheck §5; `mark_failed` leaves dependents blocked, `remove_task` refuses with dependents | **BLOCKING** for dispatch of failed-dependency work; **SAFE-ASSUMPTION** to record risk | Recovery = scheduler retry/drain outside DAG (not graph). Preserved: no DAG edit. |
| Greedy frontier starvation (`ready_frontier.rs:49-50`) | `read crates/gsd-scheduler/src/ready_frontier.rs:1-110` via graphcheck §5; `max_parallel=0` coerced to 1 | **BLOCKING** for optimisation; see optimisation row | Pause: optimisation priority ruling. Preserved: greedy admission unchanged. |
| No starvation defence (cap-refusal only) | `swarm/graph_quality.py:85-86` cap 30/ceiling 32; over-cap `429 E-CAPACITY` (graphcheck §5) | **SAFE-ASSUMPTION** that refusal is acceptable baseline; **BLOCKING** to schedule beyond cap | Preserved: cap unchanged. |
| Cycle / dangling / duplicate / file-conflict probes | `swarm/graph_planner.py:206-209,218-240` (CycleError path `a -> c -> b -> a`); probes in graphcheck §3 | **SAFE-ASSUMPTION** — planner refuses correctly | No topology assumed. Preserved: no code changed. |

## Preserved-state inventory (untouched by this critique)

- `.planning/STATE.md`, `.planning/ROADMAP.md` — read-only; no hand-edit (reconcile §4 explicitly: re-run GSD status, do not edit STATE by hand).
- `TaskNode`, `agents.json`, `graph_edges`, Rust scheduler — read-only; graphcheck §1 live counts (10-node fixture, 0 edges) retained as record.
- Linear (free-limit refusals: STATE:96, 437, 696) and Notion (STATE:895 no mapping page) — **no live writes attempted**; SPE-38338/SPE-38339 remain TBD/fake.
- Learning — stays disabled; no policy artefact created.
- No gates, formatters, suites, or code run (per assignment: SKIP gates/formatters, no code).

## What must not happen until user rules

1. No next milestone, no ship/PR, no Phase 15 audit/cleanup (B1+B5).
2. No Phase 14.4 code, no flip, no relabel of 14.4/14.7/15 (B2+B3).
3. No Wave 2 scheduling on the official 14.7 edge (B4).
4. No optimisation (frontier aging, priority promotion, retry/rebalancing, makespan work) before baseline acceptance.
5. No per-node tool gating, no new tool registration, no `agents.json` change.
6. No learning enablement, no HTIL-to-policy promotion.
7. No tracker sync (Linear/Notion) — fakes only, file + Task Store remain the record.

## Reconciliation trace (own checks)

| # | Fact | Source (file:line / grep result) |
|---|---|---|
| 1 | STATE frontmatter gaps_found / 14.4 / 33% | `.planning/STATE.md:5-17` read |
| 2 | STATE 39% + "Milestone v2.4 complete" vs "INCOMPLETE" | `.planning/STATE.md:27-36` read |
| 3 | Audit 2026-11-18 done=false | `.planning/STATE.md:899` read |
| 4 | 14.4 held vs Gaps-closed paragraph | `.planning/ROADMAP.md:224-226` + `:251` read |
| 5 | 14.7 Depends on Phase 14; DISPUTED | `.planning/ROADMAP.md:287-289` read |
| 6 | 15 INCOMPLETE needs user confirmation | `.planning/ROADMAP.md:295-304` read |
| 7 | Wave 2 blocked on held 14.4 | `.planning/STATE.md:905,910` read |
| 8 | Notion no v2.4-mapping page | `.planning/STATE.md:895` read |
| 9 | Linear free-limit rows not created | grep STATE:96,134,437,696 (read output) |
| 10 | Task Store authoritative | `.planning/STATE.md:26` read |
| 11 | `TaskNode` no tool field | `swarm/graph_planner.py:73-82` read |
| 12 | Tool threshold 10–15 enforced | `agents.json:10-13` read |
| 13 | `tool-unless-judgement` assignment-only | grep → no file source; `docs/reconcile-SPE-38338.md:9` only |
| 14 | Learning policy absent | grep → no policy artefact; brief-T6dc4-req.md:12 future-scope only |
| 15 | Baseline + sequential fallback, no optimisation | `docs/graphcheck-SPE-38338.md:4,109-116` + grep |
| 16 | Branch `swarm/T74c2-be`, target did not exist pre-write | `bash git branch --show-current` + `ls` (exit 2, not found) |
| 17 | Graph baseline in STATE already appended | `.planning/STATE.md:934` read |

---
