# Wave Plan — SPE-38344 (synthesis of Waves 2–3, plan only, no code)

**Issue:** SPE-38344 (Wave4 wave-plan; execution plan for Waves 5–9 in swarm terms — see §0 naming).
**Branch:** `swarm/T74c2-be` (repo `agent-swarm`, observed `git branch --show-current`; HEAD `53b42ce` per `INVENTORY.md` §1).
**Status:** plan — no product code, no migration DDL, no live Linear/Notion writes, gates/formatters/skipped per assignment (no suite was run for this doc).
**Decisions carried forward (assignment context, not file-derived):** fakes only (no live Linear/Notion writes), learning disabled until policy, approver = user via pause, tool-unless-judgement.
**Toolchain (observed, `INVENTORY.md` §2):** Rust `1.98.1` pinned (`rust-toolchain.toml`), Python `3.12.3` (`>=3.12`, `pyproject.toml`), `bun 1.4.0`, `ruff 0.15.18`, `maturin 1.15.0`, `cargo 1.98.1`, pytest `>=8.0,<10.0.0`.

> This doc is the `architecture.blueprint` single-writer artifact for A03 for this tranche. Consumers: A05 (backend), A06 (frontend), A07 (data), A08 (QA), A10 (security co-sign for trust-boundary notes).
> File paths under "Owned files" marked **(proposed)** do not exist yet and are created by the named unit only. All other paths cite existing repo files.

---

## Table of Contents

0. How to read this plan (wave numbering, order rule, file-disjointness)
1. Invariants that every wave obeys
2. Wave 1 — Inventory + graph validation (re-baseline)
3. Wave 2 — Core + sync (ports-and-adapters, atomic state, idempotent mapping, fakes)
4. Wave 3 — Scheduler (topo, critical path, fallback), then creator (descriptors, sandbox, approval, feedback)
5. Wave 4 — Per-phase discuss/plan/execute (the 12 outstanding phases, B1–B5 rulings)
6. Wave 5 — Audit → complete → cleanup
7. Verification command catalogue (exact, from INVENTORY/CI)
8. Rollback catalogue (per wave)
9. Pausing gates (user approvals, live-writes forbidden)
10. File-disjointness matrix
11. Ordering DAG + fitness evidence
12. Open questions / user decisions
13. Evidence trace
14. UNVERIFIED (explicit)

---

## 0. How to read this plan

**Wave numbering.** The assignment numbers five implementation waves (Wave1–Wave5 below). The swarm's own run history calls the design tranche "Waves 2–3" (SPE-38340/41/43) and reconcile/graphcheck "Wave1" (SPE-38338, `INVENTORY.md`). This plan uses **assignment numbering**: Wave1 = the inventory/graph-validation baseline that implementation re-proves before touching code (Reconcile-§2 order is Wave4's input, not Wave1's output). The five assignment waves are strictly sequential:
$W1 \prec W2 \prec W3 \prec W4 \prec W5$ (single chain, no cross-wave parallelism).

**Order rule (normative).** Wave N+1 starts only when every unit in Wave N has a green verify (§7) and no pausing gate (§9) is open. Inside one wave, units listed in order run in that order; two units in one wave may run in parallel **iff** their owned-file sets are disjoint (§10) and neither depends on the other's output. Where a wave lists a sequence (W2.1→W2.5, W3S→W3C, Wave4 phases), the sequence is mandatory even though files are disjoint — sequence protects behaviour, disjointness protects merge safety.

**What "wave" means here.** Each wave is a mergeable tranche ending in a known-good tree. A failed unit rolls back by itself (§8) without touching earlier waves (earlier waves' outputs are inputs, not edits).

---

## 1. Invariants that every wave obeys

| # | Invariant | Source |
|---|---|---|
| I1 | Fakes only. Tracker IDs are `TBD-…`. No live POST/PUT to Linear or Notion in design, tests, or cutover until user supplies real IDs + approves. Adapter constructed with non-`fake://` URL is refused (`E-DEP`) before any byte is sent. | Assignment; `docs/design-orchestration-SPE-38340.md` C1, §7.2; critique "Linear/Notion authority" |
| I2 | Learning disabled until a user-ratified policy file exists. `learning: true` in a graph/agent descriptor is refused; feedback (§4 W3C.4) is record-only. HTIL notes are user-recorded choices, not policy. | Assignment; critique "Session policy"; DECISIONS D6; `docs/design-graph-SPE-38341.md` §4.2; `docs/design-creator-SPE-38342.md` §11 |
| I3 | Approver = user via pause. Publication, ship, Phase-15, default flip, Discard, Deprecation, Rollback, and every B1–B5 contradiction are pause points that park in `human_needed` / `blocked_hold` / `paused_user_choice` and wait. | Assignment; orchestration §4.3; creator §9.1; critique B1–B5 |
| I4 | Tool-unless-judgement. Validation, transition legality, descriptor checks, sandbox checks and replay are tool-checked. Judgement is reserved for what tools cannot answer (need content quality, F4 risk acceptance, relax-a-rule). | Assignment; graph §7.4; creator §4.5 |
| I5 | Python stays default / parity oracle. `GSD_ENGINE=python`, `GSD_ENGINE_BIN` set only for the Rust legs. Rust is per-route opt-in only on parity evidence. | DECISIONS D4 (via orchestration §1); architecture §13 |
| I6 | No auto-retry on any tool until its row exists in the per-tool idempotency/side-effects inventory (DECISIONS D5). Retry only for `transient` failures, bounded, then dead-letter. | DECISIONS D5; architecture §6; orchestration §9.2 |
| I7 | **SUPERSEDED for this run.** D6 ("gsd-autonomous out of scope, no milestone-phase planning", `DECISIONS.md` HEAD `53b42ce`, Graph standalone tranche) is stale. This run's authority is the new stakeholder scope SPE-38338+ (this waveplan): it explicitly includes per-phase discuss/plan/execute for the 12 outstanding phases (Wave 4) and B1–B5 rulings. Milestone-phase planning is **in scope** here; the old standalone cut no longer applies. I7's 12 phase executions are still gated by B1–B5 rulings and fakes-only sync — no gate is waived by this re-scope. |
| I8 | Workflowz = DAG dispatch policy only (`swarm/graph_planner.py` + `scripts/swarm_run.py`). No workflowz DSL, no new `workflowz-*` crates. | DECISIONS D2 |
| I9 | No new crate, no DSL, no plugin registry. Graph tool-gap closes at graph-validation level only; `TaskNode` gains no field and stays `(id, files, dependencies, complexity, priority, duration)`. | Architecture ADR-0014; graph §4.4 |
| I10 | Workers cap 30, ceiling 32; 31st submit ⇒ `429 E-CAPACITY`, 0 spawns beyond. | `swarm/graph_quality.py:85-86` (via graphcheck §5); AC-23 |
| I11 | Additive migrations only (`CREATE TABLE IF NOT EXISTS`, no `ALTER`/`DROP`/FK/CHECK; joins by TEXT logical key). Existing rows read back as `dependency/hard` where new columns are widened. | `migrations/V0002__graph_engine.sql:15-17`; graph §5 store mapping; creator §1.3 |
| I12 | Never relabel `gaps_found` to bypass assess. A phase is `done` only when stage = `done-hold` **and** verdict ∈ {`passed`, `passed_with_recorded_debt`} with a verifier evidence digest. | Orchestration C6, §4.1; critique B3 |
| I13 | STATE/ROADMAP are never hand-edited to resolve a contradiction. Contradictions are detected (`drift`/`orphan`/`conflict`), parked, and reported for user ruling. | Orchestration §8; reconcile §4 |
| I14 | Single writer on STATE / phase projection. Only `state_store` writes `.planning/STATE.md` under lease + `BEGIN IMMEDIATE`; atomic temp + `os.replace` publish. | Orchestration §6.1; `swarm/taskstore.py:985-986` (via architecture §4.1) |
| I15 | Sequential fallback is the floor (degraded mode), never a second scheduler. Canonical fixture order `start, extra1, n1, n2, n3, n4, m1, m2, m3, last`, makespan `4.0`. Optimisation is out of scope until the baseline is green. | Graphcheck §§2,7; critique "Optimisation priority" (option A) |
| I16 | Perf judged on wall-clock only; no p95/SLO gate is invented at implementation time (A08 measures, does not assert). | DECISIONS D3 |
| I17 | Every script invocation carries `--task-id` + `--correlation-id` and reading is via `--json`. | Runtime script contract |

---

## 2. Wave 1 — Inventory + graph validation (re-baseline)

**Goal.** Re-prove the Wave1 baseline on the current tree before any implementation wave lands: layout/inventory, canonical graph order, 0 cycles, `TaskNode` no-tool-field gap, frontier starvation + failed-dependency dead-end, and the D1–D6 + B1–B5 decision record. No code is written or changed in Wave 1.

| Unit | Change (what, why) | Owned files (disjoint within wave) | Exact verify commands (§7 catalogue ids) | Rollback | Pausing gates |
|---|---|---|---|---|---|
| **W1.1** Inventory re-baseline | Refresh `INVENTORY.md` §6 unknowns against `DECISIONS.md` D1–D6 (re-read DECISIONS.md in full; D5 was truncated at "Per-tool idemp…" in the Wave1 read — `INVENTORY.md` §6). Record per-tool counts vs `capability_policy` (12–13 tools/agent, 10–15 enforced) and the 4-copy SDLC classifier location list. No other file is touched. | `docs/wave1-inventory-SPE-38344.md` **(proposed, new)** | V0 cheap probes; W1 only — no full suite | Delete the annex; baseline is `INVENTORY.md` + `DECISIONS.md` as committed | None (read-only) |
| **W1.2** Graph validation re-run | Re-run the canonical fixtures read-only through `swarm.graph_planner.GraphPlanner`: expect `subtasks=10 predecessor_links=12 cycles=0 dangling_links=0`, waves `(start) → (extra1, n1, n2, n3, n4) → (m1, m2, m3) → (last)`, makespan `4.0`; chain fixture `a → b → c → d → e`, makespan `5.0`. Probes: cycle path `a -> c -> b -> a`, `UnknownDependencyError` (`ghost`), `DuplicateNodeError` (`d`), `FileConflictError` (`docs/a.md`). Record live `.swarm/tasks.db` counts (Wave1: 242 `tasks`, 0 `graph_edges`) without writing rows. | `docs/wave1-graphcheck-SPE-38344.md` **(proposed, new)** | V0 cheap probes; W1 only | Delete the annex; graph remains fixture-only (0 persisted edges) | None (read-only) |
| **W1.3** Decisions + blockers freeze | Freeze D1–D6 (DECISIONS.md) plus the five B1–B5 pause decisions verbatim from critique D-B1–D-B5, with the safe interim assumption per blocker (operative status = frontmatter `gaps_found` + audit `done=false`; 14.4 held; de-facto 14.4→14.7 edge governs scheduling; no next milestone / no ship; per-agent 10–15 governs, per-node tools are recorded gaps). User rulings are *asked* here but *land* in Wave4 (§5) — Wave1 only files the questions. | `docs/wave1-decisions-SPE-38344.md` **(proposed, new)** | V0 cheap probes; doc-link check (V7) | Delete the annex; rulings remain open questions (§12) | **B1–B5 filed (questions, not rulings)** — rulings gate Wave4, not Wave1 |

Wave1 summary judgement (to confirm at W1.2 run): baseline is sound-to-build-on iff W1.2 reproduces makespan `4.0` and `cycles=0` exactly; any deviation stops the plan — escalate, do not design over a changed baseline.

---

## 3. Wave 2 — Core + sync

**Goal.** Land the ports-and-adapters deterministic core and the offline-safe sync path against fakes only, in this fixed order: port seam → atomic state → idempotent mapping → offline queue/DLQ → fake tracker. Sequence is mandatory (each unit consumes the previous unit's API); merge safety comes from file disjointness (see §10).

| Unit | Change (design anchor) | Owned files (disjoint) | Exact verify commands | Rollback | Pausing gates |
|---|---|---|---|---|---|
| **W2.1** Ports seam | Port surface = `SchedulerPorts` (16 methods, `crates/gsd-sched-core/src/ports.rs:599-722`); core depends on `dyn SchedulerPorts`, never imports the adapter; no Rust lock held across a port call. Python binding adapter implements the trait once; tests use a fake implementing the **same** trait. Sequential fallback = same order through the same ports, one node at a time (architecture §§4–5.3, §8). | `swarm/scheduler_ports.py` **(proposed, new)**; `tests/test_scheduler_ports_core.py` **(proposed, new)** | V1 (`ruff check .`) + V2 targeted (`tests/test_scheduler_ports_core.py`) + V5 (`cargo test -p gsd-sched-core -p gsd-scheduler -p gsd-python --locked --no-fail-fast`) + V6 mypy strict + dry run D1 | Delete the 2 new files; core stays as today. No existing file is modified in W2.1 | None |
| **W2.2** Atomic state | Single-writer `state_store`: read → parse frontmatter → apply one guarded transition → recompute derived numbers (`percent`, `current_phase`, `status`) → serialize → `write_text_atomic` (unique temp + `os.replace`, no fsync bought). Parser refuses hand-edited `percent` → `drift`; torn/partial file is rejected, orphan `.*.tmp` reported as `drift`, never silently deleted. `passed`/`passed_with_recorded_debt` require a verifier digest; debt requires non-empty `debt[]` + `accepted_by=user`. (Orchestration §6; architecture §4.1) | `swarm/phase_state_store.py` **(proposed, new)**; `swarm/atomic_file.py` *(existing — only W2.2 touches it, to pin the temp+rename contract)*; `tests/test_phase_state_store.py` **(proposed, new)** | V1 + V2 targeted + V6 + dry run D1 | Restore `atomic_file.py` to HEAD (`git checkout -- swarm/atomic_file.py`); delete the 2 new files. STATE is never edited by hand in rollback | None (fakes only) |
| **W2.3** Idempotent mapping | 1-to-many phase→issue mapping: one primary + zero-or-more children, fake IDs `TBD-LIN-<phase>-<k>`, Notion page read-only. Key = `sha256_hex(compact_json([correlation_id, phase_id, op, tracker, subject, body_hash]))` — stable across retries/lost replies, no attempt/timestamp in key. Create is `create-or-return`; update is last-writer-wins on `updated_at`, loser logged as `conflict`. (Orchestration §7) | `swarm/issue_mapper.py` **(proposed, new)**; `tests/test_issue_mapper.py` **(proposed, new)** | V1 + V2 targeted + V6 + dry run D1 | Delete the 2 new files. `phase_issue_map` rows (if any test rows) are fake-only and disposable; no live tracker row exists to roll back | None |
| **W2.4** Offline queue + DLQ | Transactional outbox: tracker intent written in the same SQLite transaction as the phase-state change that caused it; `POST /sync/enqueue` succeeds offline (202); delivery is a separate replayable step. Retry is **transient-only** (`E-TIMEOUT`, `E-DEP`-transport, `E-CAPACITY`; ≤5 attempts, bounded backoff, seeded jitter); terminal codes attempt exactly once then DLQ. DLQ redrive re-enqueues with the **same** idempotency key; `discarded` only via explicit user action. (Orchestration §9) | `swarm/sync_queue.py` **(proposed, new)**; `tests/test_sync_queue.py` **(proposed, new)** | V1 + V2 targeted + V6 + dry run D1 | Delete the 2 new files; outbox/DLQ tables are `CREATE TABLE IF NOT EXISTS` (existing tables untouched) | **Discard needs user** (§9) — the reconciler never discards |
| **W2.5** Fake tracker (fakes only) | Fake Linear (workflow) + read-only Notion (context) adapters. Hard-deny: any non-`fake://` base URL (or any real host) is refused with `E-DEP` before any network attempt; CI grep for real tracker hostnames in the orchestration core = 0 hits. All test evidence is fake/TBD. (Orchestration §7.2, F7; architecture T-FAKE-3) | `swarm/fake_tracker.py` **(proposed, new)**; `tests/test_fake_tracker.py` **(proposed, new)** | V1 + V2 targeted (`T-NOLIVE` included) + a hostname grep (V7) + dry run D1 | Delete the 2 new files. There is no live transport to roll back | **Live writes forbidden** — fake→real is a separate L3 tranche with real IDs + A10 co-sign |

Wave2 test anchors (implementer runs, plan does not): T-STATE-ATOMIC / SINGLEWRITER / PERCENT, T-CREATE / CHILDREN / UPDATE / CONFLICT, T-OFFLINE-ENQUEUE / REPLAY, T-RETRY / EXHAUST / NONTRANSIENT, T-REDIVE, T-DLQ-DISCARD, T-NOLIVE — orchestration §16.2–16.5.

---

## 4. Wave 3 — Scheduler, then creator

**Goal.** First make the graph schedulable (topo → critical path → fallback), then make capabilities creatable under least privilege (descriptors → sandbox → approval → feedback). The scheduler half lands completely before any creator file is touched — creator units consume the scheduler's validated-graph boundary (graph §4.4: scheduling projection is unchanged `TaskNode`, validation runs *before* `plan_graph`).

### 4.1 Scheduler (W3S)

| Unit | Change (design anchor) | Owned files | Verify | Rollback | Pausing gates |
|---|---|---|---|---|---|
| **W3S.1** Topo validation | Validation order V1 structural → V2 bindings → V3 tool requirements → V4 schedule → V5 findings (fail closed, no partial dispatch). Closes the graphcheck §4 gap: a task's `tool_reqs` must (1) exist as a Tool node, (2) be bound by its owner, (3) list the owner in `permitted_callers` — else refusal naming task, tool and owner. `DESTRUCTIVE`/`EXTERNAL` and `learning: true` refused. (Graph §§4–6) | `swarm/graph_validate.py` **(proposed, new)**; `tests/test_graph_validate.py` **(proposed, new)** | V1 + V2 targeted (B1–B10, C1–C6, T1–T6) + V6 + V5 Rust legs + dry run D1 | Delete 2 new files; `plan_graph` unchanged | None |
| **W3S.2** Critical path | Longest path by summed effective duration over hard `dependency` + hard `data-flow`, source→sink; ties by ascending id; float = makespan − longest-path-through-task. Reports parallelism profile (widths `1,5,3,1`, makespan `4.0` on the canonical fixture), never auto-retries an `unsafe` task on the critical path (finding F4). (Graph §§7.1–7.2) | `swarm/graph_analysis.py` **(proposed, new)**; `tests/test_graph_analysis.py` **(proposed, new)** | V1 + V2 targeted (P1–P5, T6) + dry run D1 | Delete 2 new files | Judgement: F4 risk acceptance = user pause if the owner asks |
| **W3S.3** Sequential fallback | Strictly sequential schedule in canonical topological order; makespan recomputed as `Σ duration`. Triggers: unresolved file conflict, dead-end D2, starvation, operator request (recorded). V2 bindings are **not** relaxed; failed/`unsafe` chain excluded, not retried; silent fallback is a defect (witness line required). (Graph §8; architecture §4.5) | `swarm/graph_fallback.py` **(proposed, new)**; `tests/test_graph_fallback.py` **(proposed, new)** | V1 + V2 targeted (F1–F5, T-SEQ-1–4) + V5 + scheduler gate V8 + dry run D1 | Delete 2 new files; parallel path unchanged | Operator-triggered fallback is recorded, not silently taken |

### 4.2 Creator (W3C) — learning disabled, approver = user

| Unit | Change (design anchor) | Owned files | Verify | Rollback | Pausing gates |
|---|---|---|---|---|---|
| **W3C.1** Descriptors | `ToolDescriptor` + `AgentDescriptor` schemas extending `swarm/schemas/tool.contract.v1.json` (existing checker unchanged). Gap threshold: ≥3 distinct sessions / ≥2 distinct agents / 30-day window, dedup'd by gap key; dedup against `published`/`specified`. Lifecycle `proposed→…→published→deprecated/rolled_back` with T1–T11 guards; registry writes keyed by `(descriptor_id, version, descriptor_digest)` (idempotent, compare-and-set). Additive tables `registry_descriptors`, `registry_transitions`, `registry_feedback` via new migration. (Creator §§3–6, 13) | `migrations/V0005__registry_lifecycle.sql` **(proposed, new — only W3C.1)**; `swarm/registry_descriptors.py` **(proposed, new)**; `tests/test_registry_descriptors.py` **(proposed, new)** | V1 + V2 targeted (V-1–9, T-1–5, I-1–4, G-1–6) + V7 registry checks + dry run D1 + ADR dry run D2 | Delete new files; the new migration is additive and revertible (drop the new table version) without touching M-2026-003 tables | **No generation from `proposed`** — T3 needs `specified` |
| **W3C.2** Sandbox + replay | Sandbox constructed from descriptor `permissions` (deny-all defaults; `network: "none"` unless literal host allow-listed; secret-named env refused at T2; `timeout_s` kill; telemetry records every refused op as `undeclared` vs sandbox bug). Contract gate + replay gate (0 regressions, 0 divergences vs baseline); `high_severity_findings > 0` ⇒ `promotions = 0`, refusal recorded. `stub: true` cannot pass T5. (Creator §§7–8) | `swarm/creator_sandbox.py` **(proposed, new)**; `tests/test_creator_sandbox.py` **(proposed, new)** | V1 + V2 targeted (S-1–9, R-1–5) + V6 + dry run D1 | Delete 2 new files; no published tool exists yet to withdraw | None (nothing published) |
| **W3C.3** Approval + publication | T6 ends at the user pause: the user approves *that digest at that version* (full descriptor JSON + digests + provenance + gate reports + gap evidence presented). Major bump needs a staged-rollout note before the pause. Publishing is an idempotent registry write; consumers pin by digest; an artefact changed after approval cannot publish under the old approval. (Creator §9) | `swarm/creator_approval.py` **(proposed, new)**; `tests/test_creator_approval.py` **(proposed, new)** | V1 + V2 targeted (P-1–3, X-1–3) + dry run D1 | Pre-publication: nothing to roll back. Post-publication deprecation/rollback: see §8 | **User pause (approver = user)** — no agent/gate/run may publish. Major bump = L3-style approval + rollout note |
| **W3C.4** Feedback (record-only) | `registry_feedback` rows capture `outcome`, `replay_divergence`, and explicit `user_signal ∈ {accept, reject, none}` (never inferred, never mined from session text). **Learning stays disabled**: {inertness proof F-3: full lifecycle with a populated feedback table ⇒ byte-identical registry state to the empty-feedback run}. (Creator §11) | `swarm/creator_feedback.py` **(proposed, new)**; `tests/test_creator_feedback.py` **(proposed, new)** | V1 + V2 targeted (F-1–4, incl. F-3 inertness) + V7 (fake/TBD session fixtures only) + dry run D1 | Delete 2 new files; feedback rows are append-only evidence, never edited | **Enabling learning needs a ratified policy file — L3, not in this tranche** |

Wave3 test anchors: graph §9 (C/T/P/D/S/B/F/H tables), architecture §10 (T-TRANS / T-IDEM / T-RETRY / T-DLQ / T-DESC / T-FAKE / T-CONC / T-SEQ), creator §14 (V/T/I/S/R/G/P/X/F tables).

---

## 5. Wave 4 — Per-phase discuss/plan/execute (the 12)

**Goal.** Drive the 12 outstanding phases through discuss → plan → execute using Waves 2–3 machinery, in ROADMAP `Depends on` order (reconcile §2). Each phase is one unit owning exactly one file, so phases are file-disjoint and order-safe; the unit sequence is the dependency order, not parallelism. The unit never hand-edits STATE/ROADMAP: verdict changes go through `state_store` (§3 W2.2); contradiction → park + ruling report.

**Per-phase protocol (every unit, same shape).** (1) *Discuss* — CONTEXT artefact + B-ruling ask if the phase carries a B-blocker. (2) *Plan* — PLAN artefact + `plan_graph` validation (0 cycles, 0 dangling) + frontier/starve check. (3) *Execute* — produce EXEC artefacts via the validated graph (V1–V4 pass first). (4) *Verify* — VERIFICATION artefact + verifier digest; verdict per §4.3 (`passed` only with digest + 0 unmet criteria).

| # | Unit (phase) | ROADMAP order / `Depends on` | Change in this unit (discuss/plan/execute only) | Owned file | Verify (per unit) | Rollback | Pausing gate — ruling needed |
|---|---|---|---|---|---|---|---|
| 1 | **W4.1 — Phase 6** (compile pin + workspace build) | 1st; depends Phase 5 (shipped). `human_needed` | Verify the 2 pending truths with a current-tree build/test run; close `human_needed{verifier_rerun}` or keep `gaps_found` | `.planning/wave4/06.md` **(proposed)** | V4 build + V5 Rust legs + V2 full pytest (workspace-green evidence) | Revert to `human_needed` with `awaiting` intact | **Verifier re-run** (no ruling blocks the run; close needs evidence) |
| 2 | **W4.2 — Phase 8** (Wasmer spike) | 2nd; depends 7 (closed). `gaps_found` | Re-measure the 2 non-A10 criteria; A10 exception approval stays a recorded gap, never auto-closed | `.planning/wave4/08.md` **(proposed)** | V2 targeted + wall-clock evidence (D3: no SLO) | `gaps_found` with updated gap list | **A10 exception approval** — user/A10 decision, do not infer |
| 3 | **W4.3 — Phase 9** (scheduler join) | 3rd; depends 8. `gaps_found` | Re-verify the 2026-11-18 row work (rows 1, 2, 7, 16, 18, 22, 26, 37) on a quiet host; rows may not be committed-then-stale again | `.planning/wave4/09.md` **(proposed)** | V5 + V8 scheduler gate + V2 targeted | `gaps_found` (partial row progress does not auto-close) | Verifier re-run (quiet host) |
| 4 | **W4.4 — Phase 11** (PyO3 + wheel CI) | 4th; depends 10 (closed). `gaps_found` | Add the missing GitHub CI step/run for the wheel job; record U5/U2 evidence | `.planning/wave4/11.md` **(proposed)** | V8 + V9 parity + CI line from §7 | Local-only until CI green; no ship | **Push/CI authorisation** — user authorises the push (no silent push) |
| 5 | **W4.5 — Phase 12** (port units) | 5th; depends 11. `gaps_found` frontmatter vs `pass_with_recorded_debt` body (**conflict**) | Settle U12.2 item 4; represent the frontmatter/body contradiction as `conflict` → park until ruling | `.planning/wave4/12.md` **(proposed)** | V1 + V2 targeted + plan validation | `blocked_hold` (no auto-resolve) | **User ruling** on which verdict line stands |
| 6 | **W4.6 — Phase 13** (browser deliverable) | 6th; depends 12. `gaps_found` | Loader depth, 100 kB cap, browser gate | `.planning/wave4/13.md` **(proposed)** | V2 + browser gate + V1 lint | `gaps_found` | Browser-gate run (evidence, not ruling) |
| 7 | **W4.7 — Phase 14** (acceptance proof) | 7th; depends 13. `gaps_found`, open only for D1/A2 | Rule on whether D1 (accept contended A2 run, STATE:271) closes the last criterion — status stays `gaps_found` until the user rules | `.planning/wave4/14.md` **(proposed)** | V2 + quiet-host A2 evidence | `gaps_found` | **User ruling** (D1 vs status) |
| 8 | **W4.8 — Phase 14.1** (hot-path / redaction) | 8th; depends 14. `gaps_found`, debt open | Reconfirm D-P8 + D-P11 or record them as accepted debt (non-empty `debt[]`, `accepted_by=user`) | `.planning/wave4/14.1.md` **(proposed)** | V5 + V2 targeted | `gaps_found` (debt open) | Debt acceptance = user (named, dated) |
| 9 | **W4.9 — Phase 14.4** (scheduler runtime + flip) | 9th; depends 14.2 (closed). **`gaps_found/INCOMPLETE (held)` — BLOCKER B2/B3** | **No code.** Present the three-way state (held vs "Gaps closed" vs "complete as written but not counted") + disputed COMPLETE relabel for ruling; meanwhile S5→S6→S7 gate sequence is the only path | `.planning/wave4/14.4.md` **(proposed)** | None beyond W1.2 baseline + ruling record | N/A — no change made | **B2 + B3 rulings (user)**; code stays held, Python default, no flip/ship |
| 10 | **W4.10 — Phase 14.6** (chat-thread runtime) | 10th; depends 14.4. `gaps_found` (GE-1 open **by user choice**) | Cannot close while GE-1 stays open by choice; represent as explicit open gate | `.planning/wave4/14.6.md` **(proposed)** | V5 (GE-1 = `cargo test --workspace`) only if the user reverses the choice | `gaps_found`, gate stays open | **User choice** (reverse GE-1 or keep open — no default) |
| 11 | **W4.11 — Phase 14.7** (OMP plugin + MCP) | 11th; depends 14 officially, **14.4 de-facto** (B4). No VERIFICATION, AC-14.7-01 NOT PASSED | Wave 2 (W2-01–W2-04) is unschedulable on the official edge; schedule on the de-facto edge until ROADMAP is corrected | `.planning/wave4/14.7.md` **(proposed)** | V2 (`bun test` legs V10) only for Wave-1-complete scope | Wave 1 stays closed; Wave 2 stays parked | **B4 ruling** (correct edge or split 14.7 into Wave1/Wave2) |
| 12 | **W4.12 — Phase 15** (audit, cleanup, ship) | 12th; depends 14.7. `INCOMPLETE`, blocked before audit | **No audit/cleanup/ship run.** File the B5 question; ship needs an explicit user confirmation token; unattended run forbidden | `.planning/wave4/15.md` **(proposed)** | None (no commands run) | N/A — no change made | **B5 ruling (user confirmation)**; audit runs in Wave5 only after confirmation |

Closed-with-debt phases (14.3, 14.5 — `pass_with_recorded_debt`, "no flip", STATE:900) are **not** wave-4 work and do not re-open without a new user decision (reconcile §2). Any phase whose unit hits a D-B1–D-B5 ambiguity parks in `blocked_hold`/`human_needed` with `awaiting` recorded and stops the wave — the core never edits STATE by hand (orchestration §2, C2).

---

## 6. Wave 5 — Audit → complete → cleanup

**Goal.** Only after all 12 Wave4 units have verdicts (and B1–B5 have user rulings): audit the milestone, mark complete through the verifier, then clean up report-mostly artefacts. Ship still needs its own explicit user confirmation (B5) — Wave5 does not ship by itself.

| Unit | Change | Owned files | Verify | Rollback | Pausing gates |
|---|---|---|---|---|---|
| **W5.1** Audit | Run the reconciler over all 12 phases (STATE ↔ Linear-fake ↔ Notion context): `drift` auto-corrected in fake mode, `orphan` reported (never deleted), `conflict` parked. Emit `ReconciliationReport` with `source_a`/`source_b` citations and T-33-39 regression (the 33% vs 39% B1 seed yields exactly the B1 `conflict` + `drift` rows). Recompute `percent`/`current_phase`/`status` via the parser (F6) — never by hand. (Orchestration §§6–8, 12) | `swarm/reconcile_runner.py` **(proposed, new)**; `tests/test_reconcile_runner.py` **(proposed, new)**; `docs/wave5-audit-report-SPE-38344.md` **(proposed, new)** | V1 + V2 targeted (T-DRIFT/ORPHANS/CONFLICT-PARK/REPORT-CITATIONS/33-39) + V6 + dry run D1 | Delete the 2 new files; the report is regenerated, never hand-edited | Conflict ⇒ parked (user ruling required) |
| **W5.2** Complete | Milestone `done=true` iff every phase is done-eligible (verifier digest present) **and** B1–B5 have rulings **and** the audit tail (`done=false` 2026-11-18) is superseded by a fresh assess run. Otherwise the milestone stays `gaps_found`. (Orchestration sections 4 and 6; critique B3) | `swarm/milestone_complete.py` **(proposed, new)**; `tests/test_milestone_complete.py` **(proposed, new)** | V1 + V2 targeted (T-PASSED-REQUIRES-DIGEST, T-USER-CONFIRM-SHIP regressions) + V6 + dry run D1 | Revert to `gaps_found` (no flip, no ship) | **B1–B5 rulings + B5 ship confirmation** — DONE state is not reachable without them |
| **W5.3** Cleanup | Reap orphan tmp files (report first, delete only the reported ones), close out DLQ discard entries (only with explicit user action per row), ratify deprecated/rolled_back pointers, archive the milestone record. Cleanup never deletes registry history or reconciler evidence. | `swarm/cleanup_runner.py` **(proposed, new)**; `tests/test_cleanup_runner.py` **(proposed, new)** | V1 + V2 targeted (T-DLQ-DISCARD, X-1–3 regression) + V6 + dry run D1 | Report-only until user confirms each destructive act | **Per-row user confirmation for any delete/discard** |

W5.1 to W5.3 run in order. Ship remains a separate B5-confirmed action after W5.2, not part of W5.3.

---

## 7. Verification command catalogue (exact, from INVENTORY/CI)

Toolchain: cargo 1.98.1 / rustc 1.98.1, Python 3.12.3, bun 1.4.0, ruff 0.15.18, maturin 1.15.0 (INVENTORY.md section 2). Sources: .github/workflows/ci.yml, scripts/ci_scheduler_rust.sh, scripts/ci_parity_rust.sh, pyproject.toml, mypy.ini, INVENTORY.md section 2. This plan authoring ran **none** of these (assignment: SKIP gates/formatters; no full suite).

| Id | Exact command | Where used | Proves | Source |
|---|---|---|---|---|
| V0 | `env -u SWARM_ED25519_KEY -u SWARM_SIGNING_KEY -u SWARM_ALLOW_INSECURE_DEV_KEY` prefix; `cargo --version`; `python3 --version`; `bun --version`; `python3 -c 'import swarm._native'`; `cargo metadata --no-deps`; `cargo test --workspace --locked -- --list` | W1.1 to W1.3 | Cheap baseline probes (25 names; listing only) | INVENTORY.md section 7 |
| V1 | `ruff check .` | Every unit | Python lint (line-length 140, E,F,W; E501 ignored) — 0 findings | ci.yml (via architecture section 9) |
| V2 | `python3 -m pytest` / targeted `python3 -m pytest tests/test_<unit>.py -q`, scheduler legs with `env -u SWARM_PURE_PYTHON` | Every unit | Python suite | pyproject.toml; ci_scheduler_rust.sh |
| V3 | `cargo fmt --all --check` | Any unit touching Rust | Rust formatting | ci.yml clippy job |
| V4 | `cargo fetch --locked` && `cargo build --workspace --locked` | W4.1, W5 heavy gate | Locked build | ci.yml |
| V5 | `cargo test -p gsd-sched-core -p gsd-scheduler -p gsd-python --locked --no-fail-fast`; full `cargo test --workspace --locked --no-fail-fast` | W2.1, W3S, W4.1, W4.3 to W4.8; full = W5 only | Scheduler legs; full suite | ci_scheduler_rust.sh; ci.yml |
| V6 | `python3 -m mypy` and `python3 -m mypy --strict swarm/ tests/typecheck/test_scheduler_ports_typecheck.py`; `python3 -m mypy.stubtest swarm._native` | W2.1, W2.2 to W2.5, W3C, W5 | Python types; stub honesty (AC-34) | mypy.ini; ci_scheduler_rust.sh |
| V7 | `python3 -m swarm.tool_registry check` && `docs --check` && `gen-tests --check`; `python3 -m swarm.skill_registry check` && `docs --check`; `python3 scripts/build_agents.py --check`; `python3 scripts/_check_pins.py`; `python3 scripts/_registry_intake.py --check`; real-hostname grep = 0 hits | W1.3, W2.5, W3C.1, W3C.4, W5.1 | Registry/docs/pin drift; no-live guard (F7) | ci.yml; orchestration F7 |
| V8 | `GSD_ENGINE_BIN=<abs path> scripts/ci_scheduler_rust.sh scheduler-out` | W3S.3, W4.3, W4.4 | Scheduler gate (binding, threads, stubs, vectors, both engines) | ci.yml; ci_scheduler_rust.sh |
| V9 | `scripts/ci_parity_rust.sh parity-out` | W4.4, W5 | Python/Rust parity + fault tests | ci.yml; ci_parity_rust.sh |
| V10 | `bun test tests/ts` (root); `bun run test` (in omp/); `node transpile.mjs` && `node serve.mjs` (in web/) | W4.11, W4.6 | TS/web suites | ci.yml; web package.json |
| D1 | `python3 scripts/arch_contract_check.py --root . --dry-run --json --task-id <task> --correlation-id <corr>` | Every unit | Deterministic canned output, no side effects | arch_contract_check.py --help (observed: --dry-run = deterministic canned output, no side effects) |
| D2 | `python3 scripts/arch_adr.py --dry-run --json --task-id <task> --correlation-id <corr>` | ADR recording (Wave 2/3) | Preview scaffold without writing docs/adr/ | arch_adr.py --help (observed this tranche) |

Per-unit rule: run the row's catalogue ids plus D1; a new Python module always adds V1+V2+V6.

---

## 8. Rollback catalogue (per wave)

| Wave | Software rollback | Data rollback | One-command selector rollback |
|---|---|---|---|
| W1 | Delete the 3 annex docs/wave1-*-SPE-38344.md files. | None (read-only; 0 graph_edges rows) | N/A |
| W2 | Per unit: delete the 2 new files; W2.2 also restores swarm/atomic_file.py (git checkout) | Disposable fake tables via additive revert; existing tables untouched | `GSD_ENGINE=python` (AC-38) |
| W3 | Per unit: delete the 2 new files; W3C.1 reverts V0005 migration (additive); M-2026-003 untouched | Feedback rows append-only evidence, never edited | `GSD_ENGINE=python`; fallback reverts to recorded sequential order |
| W4 | Revert phase unit file to gaps_found/human_needed/blocked_hold with awaiting intact | Phase projection rewritten through state_store only | `GSD_ENGINE=python` |
| W5 | Delete 2 new files per unit; report regenerated, never hand-edited | Destructive steps report-only until per-row user confirmation | N/A (post-complete) |

Global: no flag day, no auto-redrive of pre-flip DLQ entries, no hand-edit of STATE/ROADMAP during rollback.

---

## 9. Pausing gates (user approvals, live writes forbidden)

| Gate | Trigger | Preserved state while paused | Who releases |
|---|---|---|---|
| B1 milestone status | W4.1 / W5.1 conflict | gaps_found + done=false stands; no next milestone, no ship claim | User ruling (critique D-B1 A/B/C) |
| B2 Phase 14.4 hold | W4.9 | 14.4 held; Python default; no flip/ship | User ruling (R2 gate) |
| B3 relabel standard | W4.9 / W5.2 | 14.4/14.7/15 stay open | User ruling |
| B4 14.7 dependency | W4.11 | De-facto 14.4 to 14.7 edge governs; Wave 2 unscheduled | User/owner (correct edge or split) |
| B5 Phase 15 / ship | W4.12 / W5.2 | No audit/cleanup/ship; no new milestone | User confirmation (named, dated) |
| Publication pause (T6 to T7) | W3C.3 | Descriptor stays approved, not callable | User approves exact digest+version |
| Major-bump pause (L3) | W3C.3 | Refused at pause without staged-rollout note | User + rollout note |
| Deprecation / rollback pause (T8/T9) | W5.3 | History intact; pointer moves only on approved transition | User |
| Discard pause | W2.4 / W5.3 | DLQ row stays pending_redrive | User, per row |
| Push/CI pause | W4.4 | Local-only; no CI claim | User authorises push |
| A10 exception / debt acceptance | W4.2 / W4.8 | Gap stays recorded | A10/user, named and dated |
| Live-writes forbidden (I1) | All waves | fake:// hard-deny; TBD IDs; grep 0 hits | Real IDs + A10 co-sign + separate L3 tranche |

---

## 10. File-disjointness matrix

Every (proposed) path is owned by exactly one unit across the whole plan. No two units in the same wave share a file; cross-wave sharing is ordered.

| Wave | Units in order | Owned-file sets (all disjoint) | Shared edits |
|---|---|---|---|
| W1 | W1.1, W1.2, W1.3 | docs/wave1-inventory / wave1-graphcheck / wave1-decisions | None (read-only) |
| W2 | W2.1 to W2.5 | scheduler_ports(+test) / phase_state_store + atomic_file + test / issue_mapper(+test) / sync_queue(+test) / fake_tracker(+test) | swarm/atomic_file.py owned solely by W2.2 |
| W3 | W3S.1 to W3S.3, then W3C.1 to W3C.4 | graph_validate / graph_analysis / graph_fallback, then V0005 + registry_descriptors / creator_sandbox / creator_approval / creator_feedback (each + own test) | V0005 owned solely by W3C.1; TaskNode/graph_edges untouched (I9) |
| W4 | W4.1 to W4.12 (reconcile section 2 order) | .planning/wave4/{06,08,09,11,12,13,14,14.1,14.4,14.6,14.7,15}.md | STATE/ROADMAP never edited directly (I14) |
| W5 | W5.1, W5.2, W5.3 | reconcile_runner + test + wave5-audit-report / milestone_complete(+test) / cleanup_runner(+test) | None |

---

## 11. Ordering DAG + fitness evidence

Wave order (single chain): W1, then W2, then W3, then W4, then W5, with mandatory sub-sequences W2.1 to W2.5, W3S to W3C, W4.1 to W4.12, W5.1 to W5.3.

Fitness evidence (registry tools run this tranche, tool + key field):
- arch_cycle_check SUCCESS: has_cycle=false, node_count=5, cyclic_node_count=0, cyclic_components=[] (5-wave chain).
- arch_c4_fanout SUCCESS: container_count=5, relation_count=4, max_fan_out=1, over_budget=[], isolated=[] (fan-out budget 2).

The wave chain is acyclic and every wave has exactly one predecessor.

---

## 12. Open questions / user decisions

| # | Question | Why it matters | Default if unanswered (this plan) |
|---|---|---|---|
| Q1 | B1 to B5 rulings (critique D-B1 to D-B5 verbatim) | Wave 4 cannot close phases or ship without them | Park in blocked_hold/human_needed; operative status gaps_found + done=false |
| Q2 | Confirm sequential fallback as the floor; optimisation stays out | Keeps correctness-first (critique optimisation row, option A) | Baseline accepted as record; fallback only (I15) |
| Q3 | Confirm anti-starvation 3 waves, retry cap 5 (300 ms seeded jitter), creator threshold 3/2/30d, 30-day notice, network none default | Config in the designs; owners may tune | As designed (orchestration section 18, creator section 15 Q1 to Q5) |
| Q4 | Confirm DESTRUCTIVE/EXTERNAL generation-forbidden (creator D6) | Sets egress posture | Forbidden to generate; lifecycle may describe it with T3 manual |
| Q5 | Dry-run semantics (D1/D2) still need one canned-output comparison before reliance | Dry-run must never write | Run D1/D2 with --dry-run only |

---

## 13. Evidence trace

| Claim | Source (read, not invented) |
|---|---|
| 12 phases + order + B1 to B5 | docs/reconcile-SPE-38338.md sections 1 to 3 |
| Canonical order 0 cycles makespan 4.0; chain 5.0; probe wordings | docs/graphcheck-SPE-38338.md sections 2 to 3; swarm/graph_quality.py:620-644 |
| 4-copy classifier; 0 graph_edges; INVENTORY unknowns | INVENTORY.md sections 4 and 6 |
| D1 to D6 (TS host, DAG dispatch only, wall-clock only, Python default, D5 no auto-retry, D6 no autonomous runner) | DECISIONS.md D1 to D6 (via orchestration section 1) |
| Pause decisions + safe interim assumptions | docs/critique-SPE-38339.md D-B1 to D-B5 + cross-cutting rows |
| Ports seam, 16 SchedulerPorts methods, lock-order, DI/fakes, sequential fallback | docs/design-architecture-SPE-38343.md sections 3 to 5, 8 |
| Atomic STATE, idempotency keys, outbox/transient-only/DLQ, 5-op contract, 4 NFRs | docs/design-orchestration-SPE-38340.md sections 3 to 12 |
| 3 node types, typed edges, V1 to V5 order, critical path, D1 to D3 / S-A to C, fallback F-1 to F-5 | docs/design-graph-SPE-38341.md sections 3 to 9 |
| Lifecycle T1 to T11, gap threshold, both descriptor schemas, generation rule, sandbox + replay, T6 pause, learning-disabled F-3 | docs/design-creator-SPE-38342.md sections 3 to 14 |
| Verify commands V0 to V10 + dry runs D1 to D2 | INVENTORY.md section 2 and 7; docs/design-architecture-SPE-38343.md section 9; arch_contract_check.py --help + arch_adr.py --help (run this tranche) |
| Wave DAG acyclic + fan-out within budget | arch_cycle_check SUCCESS has_cycle=false; arch_c4_fanout SUCCESS max_fan_out=1 over_budget=[] (run this tranche; tool + key field cited) |

---

## 14. UNVERIFIED (explicit, never used as fact above)

- Full-suite green state (full cargo test, full pytest, bun test) — UNVERIFIED (this plan ran none; INVENTORY.md section 7 lists 1624 Rust entries only).
- mypy --version and whether mypy/stubtest currently pass — UNVERIFIED (INVENTORY.md section 7).
- Whether swarm._native.abi3.so and swarm/_native.pyi are in sync — UNVERIFIED (INVENTORY.md section 4).
- Whether capability_policy is evaluated against registered_tools or tools — UNVERIFIED (INVENTORY.md section 3).
- HEAD may have advanced since 53b42ce — the implementer re-proves W1.2 on the landed tree before Wave 2.
- No B1 to B5 user ruling exists yet; section 12 Q1 is an open question, not a decision.

---

*Plan only. Gates/formatters skipped per assignment. No code changed, no live Linear/Notion write attempted.*
...[truncated 8188 chars]