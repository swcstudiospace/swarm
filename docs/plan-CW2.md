# Plan CW2 — Per-phase Issue mapping with stable idempotency keys (fakes only, zero live writes)

**Assignment:** From Wave1 docs + discuss scope, write per-phase plan: parent Issue (1 per phase) + sub-Issues (1 per work item), stable idempotency keys (milestone/phase/work-item), bidirectional mapping table with TBD external IDs (fakes only, hard-deny live), source-of-truth table, fake test checklist (create/update/conflict/drift/orphans/offline). Zero live writes. Include dry-run re-run check (zero duplicates assertion).

**Branch (assignment context):** `swarm/T74c2-be` — **observed:** `swarm/Tbe37-be` (Conflict C11 in `docs/reconcile-CW1.md`; no branch change made here). Repo: `agent-swarm` (`/root/src/repos/agent-swarm`).

**Wave1 sources (only sources of phases, order, and blockers):**
- `docs/reconcile-CW1.md` — 12 outstanding phases in dependency order (§2), conflicts C1–C12 (§3), constraints (§5).
- `docs/graphvalidate-CW1.md` — graph surfaces, canonical fixture order, `TaskNode` no-tool-field gap, sequential fallback.
- `docs/discuss-CW2.md` — CONTEXT record for the same 12 phases (decisions, grey areas, pause needs, 4 BLOCKERs).
- `docs/waveplan-SPE-38344.md` §5 — Wave 4 per-phase units W4.1–W4.12 (one owned file per phase; discuss→plan→execute protocol).
- `docs/design-orchestration-SPE-38340.md` §§5–9, 16 — source-of-truth table, 1-to-many mapping model, idempotency-key derivation, reconciliation classes, fake test plan.

**Decisions (assignment context, not file-derived):** fakes only (no live Linear/Notion); learning disabled; approver = user via pause; tool-unless-judgement. Issues: SPE-38340 (context only — see §8, not a phase).

**This file is the only write of this task** (`docs/plan-CW2.md`). Gates/formatters skipped per acceptance. STATE (`.planning/STATE.md`) was not edited — read-only. No live tracker write was attempted (§7).

---

## 0. Global rules (apply to every phase below)

| # | Rule | Source |
|---|---|---|
| R-P1 | **Fakes only.** Every tracker ID in this plan is a deterministic fake `TBD-LIN-…`. No Linear POST/PUT, no Notion write, no network call to a real tracker host. | Assignment; orchestration §7, C1; waveplan I1 |
| R-P2 | **Hard-deny live.** Adapter construction with any non-`fake://` base URL (or any real host) is refused with `E-DEP` *before* any byte is sent. No feature flag overrides this in fake mode. | Orchestration §7.2, F7; waveplan I1 |
| R-P3 | **One parent + N children.** Each outstanding phase maps to exactly one parent Issue and one sub-Issue per work item. A phase may reference exactly one Notion context page, read-only. | Orchestration §7.1; waveplan W2.3 |
| R-P4 | **Stable keys, no attempt in key.** Idempotency key derives only from `(milestone, phase, work-item, op, tracker, subject, body_hash)`. Never embeds attempt count, timestamp, or wall-clock. Retry count is metadata, not identity. | Orchestration §7.2 |
| R-P5 | **Closed phases stay closed.** Phases 7, 10, 14.1.1, 14.2, 14.3, 14.5 (`pass_with_recorded_debt`, "no flip", STATE:900) get no parent Issue here; they are listed only to prevent re-opening. | Reconcile §1–§2; discuss D-G4 |
| R-P6 | **Never invent phases.** SPE-38338 / SPE-38340 are baselines/context, not phases. | Reconcile §5; discuss §5.2 |
| R-P7 | **Never hand-edit STATE/ROADMAP to resolve a contradiction.** Contradictions are detected (`drift`/`orphan`/`conflict`), parked, and reported for user ruling. | Orchestration §§5–8; waveplan I13 |
| R-P8 | **Pause gates are part of the plan.** Each work item names its pause gate; a work item under a BLOCKER cannot be executed until the user rules. | Discuss §§2–3; waveplan §9 |
| R-P9 | **No per-node tool field is invented.** `TaskNode` fields stay `(id, files, dependencies, complexity, priority, duration)`; per-agent tool range (10–15) is enforced per-agent, never per-node. Tool-gated work items are recorded as gaps. | Graphvalidate §4; discuss §4 |

---

## 1. Per-phase plan — parent Issue + sub-Issues (1 per work item)

Order follows Reconcile §2 (ROADMAP `Depends on` edges). Milestone for every row: `v2.4`. `phase_id` is the ROADMAP id (`6`, `8`, `9`, `11`, `12`, `13`, `14`, `14.1`, `14.4`, `14.6`, `14.7`, `15`). Fake-ID scheme: parent `TBD-LIN-v24-<phase>-P`, child `TBD-LIN-v24-<phase>-W<k>` (`k` = 01, 02, …). Idempotency-key scheme (§2): `idem_v24_<phase>_<work-item>_<op>` plus the full `sha256_hex(compact_json([correlation_id, phase_id, op, tracker, subject, body_hash]))` derivation in §2. Notion context is read-only and identical for all phases: `https://app.notion.com/p/40abc1a0c7ae81b588b7e01197a8d77c` (STATE:134).

### 1.1 Phase 6 — Compile-first pin and workspace build (`human_needed`) — parent `TBD-LIN-v24-6-P`

Dep: Phase 5 (shipped). Pause: verifier re-run (discuss §2 Phase 6). Wave 4 unit: W4.1.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-6-W01` | WI-6.1 — Current-tree build/test run (workspace build + test) to satisfy the 2 pending truths (`18/20` per 06-VERIFICATION.md) | Verifier re-run evidence; close = digest attached |
| `TBD-LIN-v24-6-W02` | WI-6.2 — Record verdict transition `human_needed{verifier_rerun}` → `passed` or keep `gaps_found` with `awaiting` intact | No silent close; human ruling only if 2 truths still fail |
| `TBD-LIN-v24-6-W03` | WI-6.3 — Baseline claim write-up (green-baseline evidence for downstream phases) | Blocks green-baseline claim until WI-6.1 evidence exists |

### 1.2 Phase 8 — Wasmer full-core spike report (`gaps_found`) — parent `TBD-LIN-v24-8-P`

Dep: Phase 7 (closed). Pause: A10 exception approval (L3 security acceptance). Wave 4 unit: W4.2.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-8-W01` | WI-8.1 — Full-core re-measurement on current tree (2 non-A10 criteria) | Wall-clock evidence only; no SLO invented |
| `TBD-LIN-v24-8-W02` | WI-8.2 — A10 exception approval record (or gap carried forward) | **A10/user decision; never auto-closed** |
| `TBD-LIN-v24-8-W03` | WI-8.3 — Gap-list refresh (3 NOT MET criteria, structured) | `verification → gaps_found` only with non-empty gap list |

### 1.3 Phase 9 — Host-native scheduler join and strangler seam (`gaps_found`) — parent `TBD-LIN-v24-9-P`

Dep: Phase 8. Pause: verifier re-run (quiet host). Wave 4 unit: W4.3.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-9-W01` | WI-9.1 — Re-verify 2026-11-18 row work (rows 1, 2, 7, 16, 18, 22, 26, 37) on quiet host | Verifier re-run; rows are not "committed-then-stale" again |
| `TBD-LIN-v24-9-W02` | WI-9.2 — True MET/PARTIAL/NOT MET count (resolve C9 staleness, e.g. rows 26, 37) | No hand-edit of Status |
| `TBD-LIN-v24-9-W03` | WI-9.3 — Seam/entrypoint parity check (library vs production call path) | Evidence required before 14.4 facade is released |

### 1.4 Phase 11 — Required PyO3 extension and wheel CI (`gaps_found`) — parent `TBD-LIN-v24-11-P`

Dep: Phase 10 (closed). Pause: user-authorised push. Wave 4 unit: W4.4.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-11-W01` | WI-11.1 — Missing GitHub CI step/run for the wheel job (U2/U5 evidence) | **Push/CI authorisation — user authorises the push; no silent push** |
| `TBD-LIN-v24-11-W02` | WI-11.2 — Gate-3 smoke evidence reconciled local-vs-CI (STATE:901 scope) | First GitHub run is the only acceptable CI evidence |

### 1.5 Phase 12 — Port units and strangler flips (`gaps_found`, C6) — parent `TBD-LIN-v24-12-P`

Dep: Phase 11. Pause: verifier re-run (C6). Wave 4 unit: W4.5.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-12-W01` | WI-12.1 — Settle U12.2 item 4 (replay-after-failover test) per STATE:902 | Verifier re-run of VERIF row 4 decides C6 |
| `TBD-LIN-v24-12-W02` | WI-12.2 — Represent frontmatter/body contradiction as `conflict` → park until ruling | `blocked_hold`; no auto-resolve |
| `TBD-LIN-v24-12-W03` | WI-12.3 — 10-units-in-5-waves parity evidence roll-up | No flip beyond recorded debt |

### 1.6 Phase 13 — Browser deliverable with isolation headers (`gaps_found`) — parent `TBD-LIN-v24-13-P`

Dep: Phase 12. Pause: none identified (evidence needed first). Wave 4 unit: W4.6.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-13-W01` | WI-13.1 — Loader depth correction (128 vs 32, row 10) | Evidence |
| `TBD-LIN-v24-13-W02` | WI-13.2 — 100 kB input cap in browser (row 20) | Evidence |
| `TBD-LIN-v24-13-W03` | WI-13.3 — Browser gate run (U-WEB-0..3 static claims → runtime) | Evidence; sequence blocked by Phase 12 regardless |

### 1.7 Phase 14 — Acceptance proof (`gaps_found`, C7) — parent `TBD-LIN-v24-14-P`

Dep: Phase 13. Pause: user ruling on D1 (C7). Wave 4 unit: W4.7.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-14-W01` | WI-14.1 — Quiet-host A2 evidence review (contended run vs fresh run) | Evidence only; see WI-14.2 |
| `TBD-LIN-v24-14-W02` | WI-14.2 — **User ruling: does D1 (STATE:271) close the last criterion?** | **User ruling; status stays `gaps_found` until ruled** |

### 1.8 Phase 14.1 — Measured hot-path fixes and Rust redaction decision (`gaps_found`, debt open) — parent `TBD-LIN-v24-14.1-P`

Dep: Phase 14. Pause: none as C-BLOCKER (debt reconfirmation). Wave 4 unit: W4.8.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-14.1-W01` | WI-14.1.1 — D-P8 reconfirmation (Rust redaction prototype) | User reconfirmation; never closed in STATE today |
| `TBD-LIN-v24-14.1-W02` | WI-14.1.2 — D-P11 reconfirmation (SQLite WAL opt-in) | Record as accepted debt if reconfirmed (`debt[]` non-empty, `accepted_by=user`) |

### 1.9 Phase 14.4 — Rust scheduler runtime and default flip (`gaps_found/INCOMPLETE (held)`, BLOCKER C3) — parent `TBD-LIN-v24-14.4-P`

Dep: 14.2 (closed). Pause: **⛔ C3 + R2 gate**; code held. Wave 4 unit: W4.9 (no code).

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-14.4-W01` | WI-14.4.1 — Present three-way state (held / "Gaps closed" / "complete as written but not counted") for ruling | **User ruling (C3); code stays held** |
| `TBD-LIN-v24-14.4-W02` | WI-14.4.2 — R2 gate record (A03 R2 delta → parent materialisation → BOTH A09+A10 rechecks → inventory green) | No code, no flip, no ship meanwhile |
| `TBD-LIN-v24-14.4-W03` | WI-14.4.3 — Gate sequence record (local S5 → feature-only Omega CI → quiet S6 p50 AND p95 < 1.00 → separately approved S7) | Execution only after WI-14.4.1 ruling |

### 1.10 Phase 14.6 — Chat-thread runtime (`gaps_found`, GE-1 open by user choice) — parent `TBD-LIN-v24-14.6-P`

Dep: 14.4. Pause: GE-1 user choice + inherited C3 hold. Wave 4 unit: W4.10.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-14.6-W01` | WI-14.6.1 — GE-1 open-gate representation (explicit, not hidden) | **User choice (reverse GE-1 or keep open; no default)** |
| `TBD-LIN-v24-14.6-W02` | WI-14.6.2 — Closure record (6/6 plans done ≠ closed; verifier `gaps_found` stands) | Cannot close while 14.4 held and GE-1 open |

### 1.11 Phase 14.7 — OMP plugin and local MCP integration (`gaps_found`, no VERIFICATION, BLOCKER C5) — parent `TBD-LIN-v24-14.7-P`

Dep: 14 officially, **14.4 de-facto for Wave 2** (C5). Pause: **⛔ C5 owner decision**. Wave 4 unit: W4.11.

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-14.7-W01` | WI-14.7.1 — **Owner ruling: correct ROADMAP dep to 14.4→14.7, or split into Wave 1 (independent) / Wave 2 (14.4-gated)?** | **Owner ruling; schedule on de-facto edge meanwhile** |
| `TBD-LIN-v24-14.7-W02` | WI-14.7.1W — Wave 1 scope (test harness, protocol parity, read-only) | Schedulable on official edge |
| `TBD-LIN-v24-14.7-W03` | WI-14.7.2W — Wave 2 scope (W2-01/W2-04) — unschedulable on official edge | Parked until C5 ruled + 14.4 released |
| `TBD-LIN-v24-14.7-W04` | WI-14.7.3 — SUMMARIES/VERIFICATION authoring (STATE:897) + AC-14.7-01 disposition | No COMPLETE claim stands (C4) |

### 1.12 Phase 15 — Audit, cleanup and ship (`INCOMPLETE`, BLOCKER C8) — parent `TBD-LIN-v24-15-P`

Dep: 14.7. Pause: **⛔ C8 user confirmation**. Wave 4 unit: W4.12 (no audit/cleanup/ship run).

| Sub-Issue (TBD) | Work item | Pause gate / close condition |
|---|---|---|
| `TBD-LIN-v24-15-W01` | WI-15.1 — **User confirmation token (named, dated)** or strike the next-milestone line (STATE:140) | **User confirmation; "not to run unattended" (STATE:105)** |
| `TBD-LIN-v24-15-W02` | WI-15.2 — Audit scope record (15-PLAN.md `[ ]`) | No audit run before WI-15.1 |
| `TBD-LIN-v24-15-W03` | WI-15.3 — Cleanup/ship scope record (explicit per-row confirmation) | Ship is L3/L4 — human approval mandatory |

**Closed-with-debt phases (no parent Issue):** 7, 10, 14.1.1, 14.2, 14.3, 14.5 — see R-P5. They appear in §3 only as `closed_with_debt` rows so no reader re-opens them.

---

## 2. Stable idempotency keys (milestone / phase / work-item)

### 2.1 Derivation (normative; reuses `swarm/memory.py::idempotency_key`)

```
idempotency_key = sha256_hex( compact_json([
  correlation_id,          // one swarm correlation id per logical request
  phase_id,                // e.g. "14.4"
  op,                      // "create_primary" | "create_child:<work-item-id>" | "update_primary" | "update_child:<work-item-id>"
  tracker,                 // "linear"
  subject,                 // TBD-LIN-v24-<phase>-P | TBD-LIN-v24-<phase>-W<k>
  body_hash                // sha256_hex of the canonical request body
]) )
```

Human-readable alias (same tuple, for the mapping table): `idem_v24_<phase>_<work-item>_<op>`. Example: `idem_v24_14.4_WI-14.4.1_create_child:WI-14.4.1`. The alias is documentation only; the `sha256_hex` value is the key. Properties: same logical write ⇒ same key (even after lost reply); different op/subject/body ⇒ different key; no attempt/timestamp in key.

### 2.2 Key catalogue (one `create` + one `update` per Issue; replay-safe)

| phase_id | Subject (TBD) | `create` key (alias) | `update` key (alias) |
|---|---|---|---|
| 6 | `TBD-LIN-v24-6-P` | `idem_v24_6_primary_create` | `idem_v24_6_primary_update` |
| 6 | `TBD-LIN-v24-6-W01` | `idem_v24_6_WI-6.1_create` | `idem_v24_6_WI-6.1_update` |
| 6 | `TBD-LIN-v24-6-W02` | `idem_v24_6_WI-6.2_create` | `idem_v24_6_WI-6.2_update` |
| 6 | `TBD-LIN-v24-6-W03` | `idem_v24_6_WI-6.3_create` | `idem_v24_6_WI-6.3_update` |
| 8 | `TBD-LIN-v24-8-P` | `idem_v24_8_primary_create` | `idem_v24_8_primary_update` |
| 8 | `TBD-LIN-v24-8-W01` | `idem_v24_8_WI-8.1_create` | `idem_v24_8_WI-8.1_update` |
| 8 | `TBD-LIN-v24-8-W02` | `idem_v24_8_WI-8.2_create` | `idem_v24_8_WI-8.2_update` |
| 8 | `TBD-LIN-v24-8-W03` | `idem_v24_8_WI-8.3_create` | `idem_v24_8_WI-8.3_update` |
| 9 | `TBD-LIN-v24-9-P` | `idem_v24_9_primary_create` | `idem_v24_9_primary_update` |
| 9 | `TBD-LIN-v24-9-W01` | `idem_v24_9_WI-9.1_create` | `idem_v24_9_WI-9.1_update` |
| 9 | `TBD-LIN-v24-9-W02` | `idem_v24_9_WI-9.2_create` | `idem_v24_9_WI-9.2_update` |
| 9 | `TBD-LIN-v24-9-W03` | `idem_v24_9_WI-9.3_create` | `idem_v24_9_WI-9.3_update` |
| 11 | `TBD-LIN-v24-11-P` | `idem_v24_11_primary_create` | `idem_v24_11_primary_update` |
| 11 | `TBD-LIN-v24-11-W01` | `idem_v24_11_WI-11.1_create` | `idem_v24_11_WI-11.1_update` |
| 11 | `TBD-LIN-v24-11-W02` | `idem_v24_11_WI-11.2_create` | `idem_v24_11_WI-11.2_update` |
| 12 | `TBD-LIN-v24-12-P` | `idem_v24_12_primary_create` | `idem_v24_12_primary_update` |
| 12 | `TBD-LIN-v24-12-W01` | `idem_v24_12_WI-12.1_create` | `idem_v24_12_WI-12.1_update` |
| 12 | `TBD-LIN-v24-12-W02` | `idem_v24_12_WI-12.2_create` | `idem_v24_12_WI-12.2_update` |
| 12 | `TBD-LIN-v24-12-W03` | `idem_v24_12_WI-12.3_create` | `idem_v24_12_WI-12.3_update` |
| 13 | `TBD-LIN-v24-13-P` | `idem_v24_13_primary_create` | `idem_v24_13_primary_update` |
| 13 | `TBD-LIN-v24-13-W01` | `idem_v24_13_WI-13.1_create` | `idem_v24_13_WI-13.1_update` |
| 13 | `TBD-LIN-v24-13-W02` | `idem_v24_13_WI-13.2_create` | `idem_v24_13_WI-13.2_update` |
| 13 | `TBD-LIN-v24-13-W03` | `idem_v24_13_WI-13.3_create` | `idem_v24_13_WI-13.3_update` |
| 14 | `TBD-LIN-v24-14-P` | `idem_v24_14_primary_create` | `idem_v24_14_primary_update` |
| 14 | `TBD-LIN-v24-14-W01` | `idem_v24_14_WI-14.1_create` | `idem_v24_14_WI-14.1_update` |
| 14 | `TBD-LIN-v24-14-W02` | `idem_v24_14_WI-14.2_create` | `idem_v24_14_WI-14.2_update` |
| 14.1 | `TBD-LIN-v24-14.1-P` | `idem_v24_14.1_primary_create` | `idem_v24_14.1_primary_update` |
| 14.1 | `TBD-LIN-v24-14.1-W01` | `idem_v24_14.1_WI-14.1.1_create` | `idem_v24_14.1_WI-14.1.1_update` |
| 14.1 | `TBD-LIN-v24-14.1-W02` | `idem_v24_14.1_WI-14.1.2_create` | `idem_v24_14.1_WI-14.1.2_update` |
| 14.4 | `TBD-LIN-v24-14.4-P` | `idem_v24_14.4_primary_create` | `idem_v24_14.4_primary_update` |
| 14.4 | `TBD-LIN-v24-14.4-W01` | `idem_v24_14.4_WI-14.4.1_create` | `idem_v24_14.4_WI-14.4.1_update` |
| 14.4 | `TBD-LIN-v24-14.4-W02` | `idem_v24_14.4_WI-14.4.2_create` | `idem_v24_14.4_WI-14.4.2_update` |
| 14.4 | `TBD-LIN-v24-14.4-W03` | `idem_v24_14.4_WI-14.4.3_create` | `idem_v24_14.4_WI-14.4.3_update` |
| 14.6 | `TBD-LIN-v24-14.6-P` | `idem_v24_14.6_primary_create` | `idem_v24_14.6_primary_update` |
| 14.6 | `TBD-LIN-v24-14.6-W01` | `idem_v24_14.6_WI-14.6.1_create` | `idem_v24_14.6_WI-14.6.1_update` |
| 14.6 | `TBD-LIN-v24-14.6-W02` | `idem_v24_14.6_WI-14.6.2_create` | `idem_v24_14.6_WI-14.6.2_update` |
| 14.7 | `TBD-LIN-v24-14.7-P` | `idem_v24_14.7_primary_create` | `idem_v24_14.7_primary_update` |
| 14.7 | `TBD-LIN-v24-14.7-W01` | `idem_v24_14.7_WI-14.7.1_create` | `idem_v24_14.7_WI-14.7.1_update` |
| 14.7 | `TBD-LIN-v24-14.7-W02` | `idem_v24_14.7_WI-14.7.1W_create` | `idem_v24_14.7_WI-14.7.1W_update` |
| 14.7 | `TBD-LIN-v24-14.7-W03` | `idem_v24_14.7_WI-14.7.2W_create` | `idem_v24_14.7_WI-14.7.2W_update` |
| 14.7 | `TBD-LIN-v24-14.7-W04` | `idem_v24_14.7_WI-14.7.3_create` | `idem_v24_14.7_WI-14.7.3_update` |
| 15 | `TBD-LIN-v24-15-P` | `idem_v24_15_primary_create` | `idem_v24_15_primary_update` |
| 15 | `TBD-LIN-v24-15-W01` | `idem_v24_15_WI-15.1_create` | `idem_v24_15_WI-15.1_update` |
| 15 | `TBD-LIN-v24-15-W02` | `idem_v24_15_WI-15.2_create` | `idem_v24_15_WI-15.2_update` |
| 15 | `TBD-LIN-v24-15-W03` | `idem_v24_15_WI-15.3_create` | `idem_v24_15_WI-15.3_update` |

**Counts (checkable by hand):** 12 parents + 33 children = 45 Issues; 45 `create` keys + 45 `update` keys = 90 keys. Children per phase: 6→3, 8→3, 9→3, 11→2, 12→3, 13→3, 14→2, 14.1→2, 14.4→3, 14.6→2, 14.7→4, 15→3 (sum = 33).

---

## 3. Bidirectional mapping table (TBD external IDs — fakes only, hard-deny live)

How to read: forward = phase → Issues → keys; reverse = TBD fake ID → phase/work-item → key. Both directions must join 1:1. `tbd_real_id` is `null` for every row in fake mode; the fake→real cutover is a single field fill per row plus explicit user approval (never a flag flip). Notion is read-only for every row.

| phase_id | Parent (fake) | Children (fake) | Notion (read-only) | `tbd_real_id` | Reverse lookup |
|---|---|---|---|---|---|
| 6 | `TBD-LIN-v24-6-P` | `TBD-LIN-v24-6-W01, W02, W03` | `notion://40abc1a0…b7c` (STATE:134) | `null` | `W01` → phase 6 WI-6.1, `create` key `idem_v24_6_WI-6.1_create` → record `phase_issue_map.phase_id=6` |
| 8 | `TBD-LIN-v24-8-P` | `TBD-LIN-v24-8-W01, W02, W03` | same | `null` | `W02` → phase 8 WI-8.2 (A10 exception) |
| 9 | `TBD-LIN-v24-9-P` | `TBD-LIN-v24-9-W01, W02, W03` | same | `null` | `W01` → phase 9 WI-9.1 (rows 1,2,7,16,18,22,26,37) |
| 11 | `TBD-LIN-v24-11-P` | `TBD-LIN-v24-11-W01, W02` | same | `null` | `W01` → phase 11 WI-11.1 (wheel CI) |
| 12 | `TBD-LIN-v24-12-P` | `TBD-LIN-v24-12-W01, W02, W03` | same | `null` | `W02` → phase 12 WI-12.2 (C6 conflict park) |
| 13 | `TBD-LIN-v24-13-P` | `TBD-LIN-v24-13-W01, W02, W03` | same | `null` | `W01` → phase 13 WI-13.1 (loader depth) |
| 14 | `TBD-LIN-v24-14-P` | `TBD-LIN-v24-14-W01, W02` | same | `null` | `W02` → phase 14 WI-14.2 (D1 ruling) |
| 14.1 | `TBD-LIN-v24-14.1-P` | `TBD-LIN-v24-14.1-W01, W02` | same | `null` | `W01` → phase 14.1 WI-14.1.1 (D-P8) |
| 14.4 | `TBD-LIN-v24-14.4-P` | `TBD-LIN-v24-14.4-W01, W02, W03` | same | `null` | `W01` → phase 14.4 WI-14.4.1 (C3 ruling) |
| 14.6 | `TBD-LIN-v24-14.6-P` | `TBD-LIN-v24-14.6-W01, W02` | same | `null` | `W01` → phase 14.6 WI-14.6.1 (GE-1) |
| 14.7 | `TBD-LIN-v24-14.7-P` | `TBD-LIN-v24-14.7-W01, W02, W03, W04` | same | `null` | `W01` → phase 14.7 WI-14.7.1 (C5 ruling) |
| 15 | `TBD-LIN-v24-15-P` | `TBD-LIN-v24-15-W01, W02, W03` | same | `null` | `W01` → phase 15 WI-15.1 (C8 confirmation) |
| 7, 10, 14.1.1, 14.2, 14.3, 14.5 | — (closed) | — (closed) | same (context only) | `null` | No row. `closed_with_debt` — do not re-open. |

---

## 4. Source-of-truth table

| Data | Source of truth | Mirrors (derived, never authoritative) | Why / precedence |
|---|---|---|---|
| **Phase verdict + gates/holds** | `.planning/STATE.md` frontmatter + Current Position (parsed) | ROADMAP Status (prose, read-only mirror); Linear workflow status (fake, derived) | Only artefact carrying milestone assessment (`done=false`) + per-phase verifier state. ROADMAP prose diverged 3× (B1/B2). |
| **ROADMAP `Depends on` DAG** | `.planning/ROADMAP.md` (parsed) | Scheduler order (derived); `graph_edges` (today 0 rows) | Defines scheduling edges; STATE does not carry the DAG. Official-vs-de-facto mismatch is `drift` (C5). |
| **Workflow status** (open / in-progress / done) | Linear fake (TBD IDs) | STATE mapping record (derived, reconciliation input) | Tracker is workflow tool for humans; STATE is never hand-edited to chase tracker state. |
| **Discussion/context notes** | Notion read-only context (`40abc1a0…b7c`, STATE:134) | Reconciliation report excerpts (citations only) | Context is evidence/reference, never a state store. No Notion write. |
| **Mapping records** (parent/children/keys) | `phase_issue_map` table (disposable, fake-only) | This plan §3 (plan-time projection) | Only place fake IDs + keys live; disposable test rows are fake-only. |
| **Graph order** | Live `tasks.depends_on` (276 tasks, acyclic, 9 waves) + canonical fixtures (`start, extra1, n1, n2, n3, n4, m1, m2, m3, last`, makespan 4.0) | `graph_edges` (0 rows, future only) | Persisted DAG lives in `depends_on`, not `graph_edges`. |

**Precedence rule (single rule, no case-by-case judgement):** `STATE verdict > Linear workflow status` for phase completion; Notion never overrides either. Linear `done` for a STATE `gaps_found` phase = `conflict` (park for user ruling), never auto-close. Linear open for a STATE `passed` phase = `drift` (auto-correctable in fake mode).

---

## 5. Fake test checklist (create / update / conflict / drift / orphans / offline + dry-run re-run)

All tests run against fakes only: fake tracker adapter (`fake://` base), fake clock (seeded), fake disk-fault injector. No network, no real Linear/Notion, no live credentials. Each test names the design clause it proves (orchestration §16 IDs reused verbatim).

### 5.1 Create

- [ ] **T-CREATE** — First `create_primary` for each of the 12 parents creates 1 record; replay with same idempotency key returns same record, `deduped:true`, row count unchanged. Proves §7.3 / NFR-DEDUP.
- [ ] **T-CREATE-CHILDREN** — 1-to-many: each parent + its children map with deterministic fake IDs (`TBD-LIN-v24-<phase>-P/W<k>`); each child has distinct key; re-running whole mapping is a no-op. Proves §7.1. Covers all 45 Issues (12 + 33).

### 5.2 Update

- [ ] **T-UPDATE** — Update replay with same key ⇒ same record; update with new body ⇒ new key, same fake ID, `updated_at` advances. Proves §7.2 semantics. Apply to all 45 `update` keys in §2.2.

### 5.3 Conflict

- [ ] **T-CONFLICT** — Concurrent fake updates to same subject (two writers, different bodies): last-writer-wins on `updated_at`, loser logged as reconciliation `conflict`. Proves §7.2. Seed one subject per phase-shape (12-conflict, 14-D1, Linear-`done`-vs-STATE-`gaps_found`).

### 5.4 Drift

- [ ] **T-DRIFT** — Seed Linear-fake `done` vs STATE `gaps_found` (mirror direction per §5) ⇒ `drift`, `auto_corrected`, both values in report. Seed B1 33%-vs-39% (`percent`) and Phase-9 rows 26/37 staleness (C9) as named fixtures. Proves §8.1.

### 5.5 Orphans

- [ ] **T-ORPHANS** — Seed orphan Linear-fake issue (no phase, e.g. `TBD-LIN-0099`), orphan phase (no mapping), orphan `.planning/.*.tmp` ⇒ 3 `orphan` rows, `disposition: reported`, **zero deletions**. Proves §8 / C2.

### 5.6 Offline

- [ ] **T-OFFLINE-ENQUEUE** — Tracker down: `enqueueSync` succeeds (202), intent persisted in outbox; delivery attempted on recovery in FIFO order. Proves §9.1 / transactional outbox.
- [ ] **T-OFFLINE-REPLAY** — Transport accepts but reply is lost: retry with same idempotency key ⇒ `create-or-return`, still exactly 1 fake record. Proves §7.2 lost-reply rule / NFR-DEDUP.
- [ ] **T-RETRY / T-RETRY-EXHAUST / T-NONTRANSIENT** — `E-TIMEOUT` 3× then success ⇒ delivered, attempt 4, exact seeded-jitter backoff (1s, 2s, 4s); forever ⇒ exactly 5 attempts then DLQ `pending_redrive`; `E-INPUT`/`E-CONTRACT`/`E-INTERNAL`/fake-host-deny ⇒ exactly 1 attempt, immediate DLQ, no backoff. Proves §9.2 / D5.
- [ ] **T-REDIVE / T-DLQ-DISCARD** — Redrive re-enqueues with same key, new attempt series, still dedupes; `discarded` only via explicit user action. Proves §9.3 / C2.
- [ ] **T-NOLIVE** — Adapter with `https://linear.example…` or `https://notion.example…` base URL ⇒ refused with `E-DEP` before any network attempt; grep for real tracker hostnames in orchestration core = 0 hits. Proves C1 / F7 / R-P2.

### 5.7 Dry-run re-run check (zero duplicates assertion — mandatory)

Run the full mapping creation twice against the fake store (dry-run mode, `fake://` only):

```
run1 = map_all_phases(12 phases, 45 Issues, 90 keys)   # create path
run2 = map_all_phases(12 phases, 45 Issues, 90 keys)   # identical inputs, same correlation_id, same keys
assert count(phase_issue_map) == 45                     # 12 parents + 33 children; zero new rows on run2
assert all(run2.deduped)                                # every run2 create returns deduped:true
assert duplicates(phase_id, subject, idempotency_key) == 0
assert count(sync_outbox DISTINCT idempotency_key) == count(sync_outbox)  # no duplicate keys
assert live_network_calls == 0 and live_tracker_writes == 0             # zero live writes
```

Any non-zero duplicate count or any live call fails the check and parks the plan in `blocked_hold`. The check is re-run after every plan edit; the assertion output (row counts + duplicate counts) is recorded with the plan evidence.

---

## 6. Execution order (plan-level, no code)

1. **Discuss** — `docs/discuss-CW2.md` is the CONTEXT record (already written). File B-ruling asks for C3/C4/C5/C8 before execution.
2. **Plan (this doc)** — 12 parents + 33 children + 90 keys recorded above; `plan_graph` validation (0 cycles, 0 dangling) on the 12-phase `Depends on` DAG.
3. **Execute (future, fakes only)** — map all 45 Issues via §2 keys in §1 order (6 → 8 → 9 → 11 → 12 → 13 → 14 → 14.1 → 14.4 → 14.6 → 14.7 → 15); parallel only where file-disjoint and dependency-free.
4. **Verify** — run §5 checklist + §5.7 dry-run re-run; emit VERIFICATION artefact + verifier digest per phase. `passed` only with digest + 0 unmet criteria; never relabel `gaps_found` to bypass assess.
5. **Cutover gate (not in this plan)** — real IDs only via `tbd_real_id` fill + explicit user approval; live transport is a separate A10-co-signed L3 tranche.

---

## 7. Zero live writes (evidence)

- No Linear/Notion row was created or updated in this plan. All external IDs are `TBD-…` or `null`.
- No network call was made. No `POST`/`PUT` to any tracker host exists in this repo change (only `docs/plan-CW2.md` was written).
- Hard-deny enforcement is specified in R-P2 / §5.6 `T-NOLIVE` (fake-host-deny `E-DEP` before any byte).
- STATE/ROADMAP were not edited — read-only per target.

---

## 8. Trace

| Claim | Source |
|---|---|
| 12 outstanding phases + §2 order + C1–C12 | `docs/reconcile-CW1.md` §§1–3 |
| 4 BLOCKERs (C3, C4, C5, C8) + tool-field gap + sequential fallback | `docs/discuss-CW2.md` §§2–4; `docs/graphvalidate-CW1.md` §§4–7 |
| W4.1–W4.12 one-file-per-phase protocol | `docs/waveplan-SPE-38344.md` §5 |
| Source-of-truth table + precedence rule | `docs/design-orchestration-SPE-38340.md` §5 |
| 1-to-many mapping model + fake-ID scheme | Orchestration §7.1 |
| Idempotency-key derivation (`sha256_hex(compact_json([...]))`, no attempt) | Orchestration §7.2; `swarm/memory.py::idempotency_key` |
| Drift / orphan / conflict classification | Orchestration §8 |
| Offline queue / transient-only retry (≤5) / DLQ + redrive | Orchestration §9 |
| Fake test IDs (T-CREATE … T-NOLIVE, T-DRIFT … T-33-39) | Orchestration §16 |
| Notion context ref (STATE:134), `done=false` audit, `current_phase: 14.4` | `.planning/STATE.md` (read-only) |
| Canonical fixture order, `TaskNode` fields, 276 tasks / 0 `graph_edges` | `docs/graphvalidate-CW1.md` §§2–4 |

*Plan only. Gates/formatters skipped per acceptance. No code changed, no live Linear/Notion write attempted.*
