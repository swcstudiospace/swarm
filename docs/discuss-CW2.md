# Discuss CW2 — CONTEXT record for the 12 outstanding phases

**Assignment:** From Wave1 docs, write the discuss record per phase for all 12 outstanding phases: decisions captured, grey areas, pause needs (especially the 4 BLOCKERs from reconcile + the tool-field gap), constraints. No code, no live writes. CONTEXT-shaped — decisions and pause points, not a plan.

**Branch (assignment context):** `swarm/T74c2-be` — **observed:** `swarm/Tbe37-be` ( Conflict C11 in `docs/reconcile-CW1.md`; no branch change made here). Repo: `agent-swarm` (`/root/src/repos/agent-swarm`).

**Wave1 sources used (only sources cited below):**
- `docs/reconcile-CW1.md` (CW1 reconciliation: ROADMAP + STATE cross-check, 12 outstanding phases, conflicts C1–C12)
- `docs/graphvalidate-CW1.md` (CW1 graph validation from code/schema only: planner, TaskStore, Rust scheduler, tool-field gap)

**Decisions (assignment context, not file-derived):** fakes only (no live Linear/Notion writes); learning disabled; approver = user via pause; tool-unless-judgement. Issues: SPE-38340 (context only — see §5, not a phase).

**This file is the only write of this task** (`docs/discuss-CW2.md`). Gates/formatters skipped per acceptance. STATE (`.planning/STATE.md`) was not edited — read-only.

---

## 1. Global decisions captured (apply to every phase below)

| # | Decision | Source |
|---|---|---|
| D-G1 | Fakes only. No live tracker writes. Any tracker reference must use TBD/fake IDs. | Reconcile §5; graphvalidate §8 "No code changed" |
| D-G2 | Learning disabled until policy. HTIL 2026-11-17 choices (sonic inline, `crates/` deliverable, GitHub-hosted CI, Pool Resize `dynamic_within_cap`) are user-recorded, not ratified policy. | Reconcile §5 |
| D-G3 | Approver = user via pause. Every BLOCKER (C3, C4, C5, C8 + tool-field gap below) is a pause point: do not schedule, release held code, flip defaults, or ship on inferred answers. | Reconcile §5; §3 below |
| D-G4 | Closed phases stay closed. Phases 7, 10, 14.1.1, 14.2, 14.3, 14.5 are closed with recorded debt and are **not** outstanding; do not re-open without a new user decision. | Reconcile §1 / §2 note |
| D-G5 | Sequential fallback is the baseline when parallel dispatch is unsafe. No optimisation, priority promotion, retry or rebalancing is proposed in this discuss record. | Graphvalidate §7 |
| D-G6 | STATE was not edited; per-phase `Status` lines that conflict with 2026-11-17/18 progress (C9) are left for verifier re-runs, never hand-edited here. | Reconcile §3 C9 |

---

## 2. Per-phase discuss record (12 outstanding, dependency order)

Order follows Reconcile §2 (which follows ROADMAP `Depends on` edges). Each entry is CONTEXT only: decisions, grey areas, pause needs, constraints. No plan steps, no code.

### Phase 6 — Compile-first pin and workspace build (`human_needed`; dep: Phase 5, shipped)

| Discuss item | Content |
|---|---|
| **Decisions captured** | Status `human_needed (2026-11-12 per 06-VERIFICATION.md: 18/20 truths, 2 await current-tree build/test run)`. Audit count `1 human_needed (6)`. Blocks the green-baseline claim for everything downstream. (Reconcile §1–§2 row 1) |
| **Grey areas** | Whether the 2026-11-17 HTIL note "GE-1 runs started, may satisfy 2 truths" (C9) actually closes the 2 pending truths, or whether a fresh current-tree build/test run is still required. STATE never closes Phase 6 — the truth count in STATE is unverified. |
| **Pause needs** | **Not a C-BLOCKER, but a baseline pause.** Verifier re-run on current tree; do not treat the green baseline as established until then. No user ruling needed if the re-run passes; user ruling needed only if the 2 truths still fail. |
| **Constraints** | Do not hand-edit STATE Status (C2/C9). Fakes only. |

### Phase 8 — Wasmer full-core spike report (`gaps_found`; dep: Phase 7, closed)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found (2026-11-02: 3 criteria NOT MET)`. Audit `10 gaps_found (…, 8, …)`. Blocks Phase 9. (Reconcile §1 row 8) |
| **Grey areas** | The A10 exception approval + full-core measurement need has no recorded outcome in ROADMAP/STATE — grey area is whether the 3 NOT MET criteria are still NOT MET or were superseded by later spikes. No evidence either way in Wave1 sources. |
| **Pause needs** | A10 exception decision (security exception approval) is an L3 security acceptance — **pause for A10/user**, do not silently accept residual risk. No C-BLOCKER label, but the exception dependency makes Phase 8 a pause point in practice. |
| **Constraints** | A10 owns security-risk acceptance (A03 guardrail). Not to be waived here. |

### Phase 9 — Host-native scheduler join and strangler seam (`gaps_found`; dep: Phase 8)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found (2026-11-02: 21 MET, 6 PARTIAL, 10 NOT MET, 2 NOT RUN of 39)`. STATE:904–933 add verified 2026-11-18 row work (rows 1, 2, 7, 16, 18, 22, 26, 37) without closing the phase. Blocks the 14.4 facade. (Reconcile §1 row 9) |
| **Grey areas** | **C9 staleness:** verified rows vs stale 2026-11-02 Status — e.g. row 26 NOT MET already implemented at `a760263` (STATE:914); row 37 PARTIAL stale (STATE:922). Neither re-verified nor re-closed. Grey area is the true current MET/PARTIAL/NOT MET count. |
| **Pause needs** | **Verifier re-run** covering the 2026-11-18 row work. Do not close Phase 9, and do not release the 14.4 facade on the stale 2026-11-02 counts. Not a C-BLOCKER, but a hard verification pause. |
| **Constraints** | Do not edit Status by hand (C9). Sequencing with Phase 8 (A10 exception) unresolved — graphvalidate §5 Rust deadlock/starvation notes apply to any scheduler rework here (see §4). |

### Phase 11 — Required PyO3 extension and wheel CI (`gaps_found`; dep: Phase 10, closed)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found (2026-11-02)`. STATE:901: gate 3 smoke in wheel job locally, first CI run needs unauthorised push. Blocks Phase 12. (Reconcile §1 row 11) |
| **Grey areas** | Evidence scope: local smoke run exists; `wheel.yml` has never executed on GitHub. Grey area is whether local gate-3 smoke is evidentially sufficient, or whether first CI run is the only acceptable evidence. |
| **Pause needs** | **User-authorised push** — no silent push. This is an explicit operational pause: the phase cannot close until the user authorises the GitHub CI run. Not a C-BLOCKER, but a human-authorisation gate. |
| **Constraints** | No unauthorised push. Fakes only. |

### Phase 12 — Port units and strangler flips (`gaps_found`, C6; dep: Phase 11)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found (frontmatter) … Body says pass_with_recorded_debt`. Audit `gaps_found`. STATE:902: U12.2 item 4 rewritten in PLAN, VERIF row 4 still not met until verifier re-runs. Blocks Phase 13. (Reconcile §1 row 12) |
| **Grey areas** | **C6 contradiction:** frontmatter `gaps_found` vs body `pass_with_recorded_debt`. Grey area is which statement describes the phase: debt-only, or one genuinely unmet criterion. |
| **Pause needs** | **Verifier re-run of U12.2 item 4 / VERIF row 4 decides** (per STATE:902). Treated outstanding meanwhile. This is a verification pause, not a user ruling — unless the re-run contradicts the rewrite, which would escalate. |
| **Constraints** | Do not resolve C6 by editing ROADMAP frontmatter/body here. |

### Phase 13 — Browser deliverable with isolation headers (`gaps_found`; dep: Phase 12)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found (2026-11-02)`. Audit `gaps_found`. Blocks Phase 14. (Reconcile §1 row 13) |
| **Grey areas** | Loader depth, 100 kB cap, browser gate — all three named as outstanding with no recorded progress in Wave1 sources. Grey area is which of the three is the true blocker vs already met. No per-criterion evidence in ROADMAP/STATE. |
| **Pause needs** | None identified in Wave1 sources (no C-BLOCKER, no user ruling named). Grey areas above must be resolved by evidence before Phase 13 can be planned, but they are not flagged as pause points for the user on current sources. |
| **Constraints** | Sequence blocked by Phase 12 (C6) regardless of its own state. |

### Phase 14 — Acceptance proof (`gaps_found`, C7; dep: Phase 13)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found … only for the quiet-host A2 record under decision D1`. STATE:271 D1 accepts contended A2 run. Blocks 14.1 and (officially) 14.7. (Reconcile §1 row 14) |
| **Grey areas** | **C7:** D1 accepts the record, but Status stays `gaps_found`. Grey area is evidential: does the contended quiet-host A2 run count as acceptance proof, or is a fresh quiet-host run required? STATE holds both lines simultaneously. |
| **Pause needs** | **User ruling — does D1 close the last criterion?** (Reconcile §3 C7). Pause point: do not claim acceptance or start 14.1/14.7 on an assumed answer. Not one of the 4 C-BLOCKERs, but a named user-decision pause. |
| **Constraints** | L3 acceptance on a contended run cannot be assumed by A03. |

### Phase 14.1 — Measured hot-path fixes and Rust redaction decision (`gaps_found` — criteria met, debt open; dep: Phase 14)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found (criteria met, debt open)`. D-P8/D-P11 debt. Does not empirically block 14.2 (already closed) — recorded debt only. Terminology note: "criteria met" counts as `gaps_found`; D-P8 reconfirmation never closed in STATE. (Reconcile §1 row 14.1) |
| **Grey areas** | Whether D-P8/D-P11 debt is closable by reconfirmation alone, or needs new measurement. D-P8 reconfirmation is recorded as never closed — no outcome in Wave1 sources. |
| **Pause needs** | None flagged as a C-BLOCKER. Debt reconfirmation is a verification item, not a user pause, on current sources. |
| **Constraints** | Recorded debt only — do not treat as empirical blocker for closed 14.2. |

### Phase 14.4 — Rust scheduler runtime and default flip (`gaps_found/INCOMPLETE (held)` — BLOCKER C3; dep: 14.2, closed)

| Discuss item | Content |
|---|---|
| **Decisions captured** | R2 gate sequence recorded: A03 R2 delta → parent materialisation → BOTH A09+A10 rechecks → inventory green. Then local S5 → feature-only Omega CI → quiet S6 (p50 AND p95 strictly < 1.00, D-P21) → separately approved S7. **No flip, no ship meanwhile.** Blocks 14.6 and 14.7-Wave-2 (de-facto). (Reconcile §2 row 9) |
| **Grey areas** | **C3 three-way state:** `gaps_found/INCOMPLETE (held)` vs `Gaps closed per updated VERIF (2026-11-16)` vs audit "complete as written but not counted (its gate holds). done=false". Frontmatter `current_phase: 14.4, status: gaps_found`. Grey area is which of the three states is true — and hence whether the code is held or releasable. |
| **Pause needs** | **⛔ BLOCKER C3 — user decision: does the updated VERIF close it, or does the R2 gate still hold?** Code stays held. No phase release, no S5/S6/S7 run, no flip on inferred answers. This is the primary pause point of CW2. |
| **Constraints** | L3: held code release is not an A03/A05 decision. Flip requires S7 separately approved — never folded into S5/S6. |

### Phase 14.6 — Chat-thread runtime (`gaps_found`; dep: 14.4)

| Discuss item | Content |
|---|---|
| **Decisions captured** | "Implemented, gated … verifier verdict stays `gaps_found` (GE-1 open by the user's choice)". STATE:105 `GE-1 open by your choice`. 6/6 plans `[x]`, verifier `gaps_found`. Cannot close while 14.4 held and GE-1 open. (Reconcile §1 row 14.6) |
| **Grey areas** | Which criterion GE-1 represents, and whether the user's choice to keep GE-1 open is a standing decision (do not re-ask) or a time-boxed hold pending 14.4. Wave1 sources record it as "open by your choice" without a closure condition. |
| **Pause needs** | **GE-1 open by user choice** — pause point owned by the user. Do not schedule closure while 14.4 held (C3) and GE-1 open. Not a separate C-BLOCKER, but inherits C3's hold. |
| **Constraints** | Gate vs implementation gap: 6/6 plans done does not equal closed. |

### Phase 14.7 — OMP plugin and local MCP integration (`gaps_found`, no VERIFICATION.md, AC-14.7-01 NOT PASSED — BLOCKER C5; dep: 14 officially, 14.4 de-facto for Wave 2)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `gaps_found, no VERIFICATION.md. DISPUTED COMPLETE. AC-14.7-01 NOT PASSED`. STATE:898 verifier `0/18 fully met`. STATE:905, 910: Wave 2 (W2-01/W2-04) blocked on held 14.4. STATE:897 — needs SUMMARIES/VERIFICATION. Blocks 15. (Reconcile §1 row 14.7) |
| **Grey areas** | **C5 dependency conflict:** ROADMAP `Depends on: Phase 14` (official) vs STATE de-facto dep on held 14.4 for Wave 2 (STATE:910). Grey area is whether 14.7 is one phase or two (Wave 1 independent / Wave 2 14.4-gated) — and which parts of AC-14.7-01 are satisfiable now vs only after 14.4. |
| **Pause needs** | **⛔ BLOCKER C5 — owner decision: correct the ROADMAP dep, or split 14.7 into Wave 1 (independent) / Wave 2 (14.4-gated)?** Schedule on the de-facto edge meanwhile. Also needs SUMMARIES/VERIFICATION (STATE:897) before any closure claim. |
| **Constraints** | `DISPUTED COMPLETE` — no COMPLETE claim stands (see C4 below). |

### Phase 15 — Audit, cleanup and ship (`INCOMPLETE` — BLOCKER C8; dep: 14.7)

| Discuss item | Content |
|---|---|
| **Decisions captured** | `INCOMPLETE, blocked before audit, no VERIFICATION.md. DISPUTED COMPLETE. Needs explicit user confirmation.` Plan `[ ]` open. Audit `gaps_found` (counted in the 10). STATE:105 "not to run unattended", "BLOCKED at AUDIT". STATE:140 "Start the next milestone". (Reconcile §1 row 15) |
| **Grey areas** | **C8 contradiction:** ROADMAP "needs explicit user confirmation" + STATE Blockers "ship stays blocked" vs STATE Operator Next Steps "Start the next milestone" (STATE:140). Grey area is whether "start next milestone" is a deliberate sequencing choice (ship blocked, next work starts) or a stale line contradicting the block. |
| **Pause needs** | **⛔ BLOCKER C8 — user: authorise Phase 15, or strike the next-milestone line.** Last phase; nothing valid follows it until the user rules. No audit, no cleanup, no ship on inferred authorisation. Explicit "not to run unattended" (STATE:105). |
| **Constraints** | Ship is L3/L4 — human approval mandatory. `done=false` (assess). |

---

## 3. The 4 BLOCKERs (C3, C4, C5, C8) — consolidated pause points

All four are **user/owner pauses** per D-G3. Nothing in CW2 releases held code, closes a phase, or ships on inferred answers.

| Blocker | Conflict (evidence) | Smallest decision needed |
|---|---|---|
| **C3** — Phase 14.4 three-way state | `gaps_found/INCOMPLETE (held)` vs `Gaps closed per updated VERIF (2026-11-16)` vs audit "complete as written but not counted" (ROADMAP:218–254; STATE:899) | Does the updated VERIF close 14.4, or does the R2 gate still hold? Code stays held until answered. |
| **C4** — Disputed 2026-11-16 COMPLETE relabels | 14.4, 14.7, 15 relabelled COMPLETE using Python dispatch layers; audit rule: "gaps_found is never relabelled to bypass assess" (ROADMAP:224, 289, 301) | What is the evidential standard for COMPLETE before any relabel stands? No relabel stands until answered. |
| **C5** — Phase 14.7 dependency | ROADMAP `Depends on: Phase 14` vs STATE de-facto dep on held 14.4 for Wave 2 (ROADMAP:287; STATE:910) | Correct the ROADMAP dep, or split 14.7 into Wave 1 / Wave 2? Schedule on de-facto edge meanwhile. |
| **C8** — Phase 15 authorisation | ROADMAP:301 "needs explicit user confirmation" + STATE:101/105 "ship stays blocked / not to run unattended" vs STATE:140 "Start the next milestone" | Authorise Phase 15, or strike the next-milestone line? |

Non-blocker conflicts recorded (not pause points, do not block discuss): C1 (milestone complete vs INCOMPLETE line), C2 (`percent: 33` vs bar `39%`), C6 (Phase 12 frontmatter vs body), C7 (Phase 14 D1), C9 (stale Status vs 2026-11-17/18 progress), C10 (Phase 7 line 109 vs 890 — resolved in-file, 890 current), C11 (branch `swarm/T74c2-be` vs observed `swarm/Tbe37-be`), C12 (root `ROADMAP.md` missing — only `.planning/ROADMAP.md` exists; GSD tooling only, not hand-copied). (Reconcile §3)

---

## 4. Tool-field gap (cross-cutting pause/caveat)

**Finding (graphvalidate §4): neither graph surface has a tool field, so per-node tool requirements cannot be expressed.**

| Surface | Fields (live `inspect.signature` probe) | Tool field? |
|---|---|---|
| Python `TaskNode` (`swarm/graph_planner.py:73-82`) | `(id, files, dependencies, complexity, priority, duration)` | ❌ none |
| Rust `Task` proto (`crates/gsd-proto/src/gsd.proto:79-88`) | `(id, subagent_type, input_payload, dependencies, priority, retry_policy, timeout, metadata)` | ❌ none |

**Implication:** a task graph node requiring a specific tool **cannot be expressed** on either surface; a per-node tool-requirement unsatisfiability would pass silently. A tool-gated node is a recorded gap, not a planner refusal. (Graphvalidate §4, §6)

**What currently holds instead (per-agent, not per-graph-node):** `agents.json` `capability_policy.tools = {min: 10, max: 15, enforce: true}` (skills identical); live `registered_tools` per agent = min **12** / max **13** over 30 agents — inside range; enforced by `swarm/tool_registry.py:444-473` (errors on any agent outside min/max). (Graphvalidate §4)

**Discuss position for CW2 (CONTEXT, not a design decision):**
- **Decisions captured:** For phases where tool-free discussion suffices, no pause is needed — per-agent tool ranges are satisfied today.
- **Grey areas:** Whether tool requirements must ever be expressed per graph node (e.g. a node that must run before/after a tool becomes available), or whether per-agent enforcement is the intended permanent shape. Wave1 sources do not decide this. Related recorded risks: Rust `TaskDag` failed-dependency deadlock (`dag.rs:71-81, 145-158` — failed node neither completes nor can be removed while dependents live) and greedy-frontier starvation (`ready_frontier.rs:44, 49-58` — no aging/priority rule; `max_parallel=0`/missing → 1). (Graphvalidate §5, §6)
- **Pause needs:** **If any discuss outcome requires per-node tool gating, that is a pause point** — the graph cannot express it today; adding a tool field to `TaskNode`/proto would be a schema change (L3-adjacent, needs ADR + consumer ack). On current Wave1 evidence, no phase in §2 has been shown to require per-node tool gating, so this is a recorded caveat, not a C-BLOCKER.
- **Constraints:** Do not invent a tool field in discuss/plan docs; do not claim a planner refusal for tool requirements. Existing persisted DAG is `tasks.depends_on`, **not** `graph_edges` (0 rows in live `.swarm/tasks.db`; 276 tasks, acyclic, 9 waves, 13 ready / 68 waiting — graphvalidate §2b). Parallel waves refuse overlapping file claims with `FileConflictError`; sequential fallback (flatten waves to singletons) is the safe degraded mode (graphvalidate §7).

---

## 5. Constraints (global)

1. **No code, no live writes.** No Linear/Notion rows created or updated; tracker references use TBD/fake IDs only. Existing Notion task (STATE:134) referenced read-only. (Reconcile §5)
2. **Never invent phases.** SPE-38338 (STATE:934) is a validated dependency-graph baseline (code/schema only; `docs/graphcheck-SPE-38338.md`) — context, not a phase. Its findings (tool requirements unexpressible in `TaskNode`; Rust failed-dependency deadlock; greedy-frontier starvation) are recorded there, not re-litigated here. SPE-38340 likewise is context only. (§5 cycle: graphvalidate §8)
3. **STATE read-only in CW2.** C1/C2/C9 staleness is recorded, never fixed by editing STATE.
4. **No silent pushes, flips, or ships.** Phase 11 needs user-authorised push; Phase 14.4 needs R2 gate + S7 separate approval; Phase 15 is "not to run unattended" (STATE:105).
5. **Scheduling note (not a re-draw):** the 12-phase order (§2) follows ROADMAP `Depends on` edges; the one fork (14.3 and 14.4 both depend on closed 14.2) and the 14.7 de-facto edge (C5) are flagged, not re-drawn. Acyclicity of the declared DAG was validated CW1 (`arch_cycle_check`: 19 nodes, `has_cycle: false` — Reconcile §4).
6. **Gates/formatters skipped** per acceptance; no contract/ADR/blueprint change was made, so no `arch_contract_check` / `arch_contract_ops` run applies (same position as Reconcile §4).

## 6. Trace

Every substantive claim above traces to `docs/reconcile-CW1.md` §1–§6 (ROADMAP `.planning/ROADMAP.md` lines 5–304; STATE `.planning/STATE.md` frontmatter 5–17, lines 27–36, 101, 105, 109, 134, 140, 271, 890–934) and `docs/graphvalidate-CW1.md` §1–§8 (`swarm/graph_planner.py`, `swarm/graph_quality.py`, `swarm/taskstore.py`, `contracts/sc-graph-engine.json`, `migrations/V0002__graph_engine.sql`, `crates/gsd-scheduler/src/dag.rs`, `ready_frontier.rs`, `crates/gsd-proto/src/gsd.proto`, `agents.json`, `swarm/tool_registry.py`, live `.swarm/tasks.db` read-only counts). No other sources were used; no live writes were made.
