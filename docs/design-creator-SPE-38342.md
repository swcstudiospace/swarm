# Unified Registry Lifecycle — Tool & Agent Descriptor Design (SPE-38342)

**Issue:** SPE-38342 (design only, no code)
**Branch:** `swarm/T74c2-be` (`agent-swarm`, repo `swcstudiospace/swarm`)
**Status:** Design — lifecycle + schemas + test checklist. No implementation.
**Wave 1 inputs (read, not re-derived):**
- `docs/reconcile-SPE-38338.md` — roadmap/state reconciliation; 12 outstanding phases, 5 blockers (B1–B5).
- `docs/graphcheck-SPE-38338.md` — baseline validation of the Swarm dependency graph from code/schema only.
- `docs/requirements/spec-T6dc4-req.md` — VALIDATED spec for the self-improving creator (US-31..US-36, AC-61..AC-72).
- `docs/data/model.md` § "Creator extension (T6dc4-data, A07)" — migration M-2026-003, six tables, SC-Creator.
- `docs/adr/0010-add-self-improving-tool-creator-with-specialist-register.md` — accepted ADR (API-GsdQualityGate 1.4.0, additive L2).
- `contracts/sc-creator.json` — SC-Creator v1.0.0 (field set persisted by M-2026-003).
- `swarm/schemas/tool.contract.v1.json` — the existing tool contract every registered tool must satisfy.
- `agents.json` `capability_policy` — `tools {min: 10, max: 15, enforce: true}`, same for skills.
- Sibling designs in this wave: `docs/design-graph-SPE-38341.md` (graph model, SPE-38341), `docs/design-orchestration-SPE-38340.md` (orchestration core, SPE-38340).

**Decisions carried forward (assignment context, not file-derived):**
**fakes only** (no live Linear/Notion writes), **learning disabled** until policy,
**approver = user via pause**, **tool-unless-judgement** (prefer tool evidence; use judgement
only where a tool cannot answer).

**What this doc is:** the target model for how a capability a session *needs* becomes a
tool or agent the swarm can *call*. Today that path is a set of counters (requests,
gaps, registers, revisions, specialists, runs) with no lifecycle, no descriptor, and no
generation rule. This design adds exactly those three things — a single lifecycle
(`proposed → specified → generated → sandbox → review → approved → published →
deprecated / rolled back`), two schemas (`ToolDescriptor`, `AgentDescriptor`), and one
generation rule (approved descriptors only) — without changing any existing behaviour
until the follow-up implementation task lands.

**Scope guard:** design only. No code, no migration, no contract version change, no
gate run (gates/formatters skipped per assignment). The test checklist in §14 is a
checklist for the implementer, not a test run.

---

## 1. Current state (ground truth)

### 1.1 The creator surface counts; it does not govern

`docs/data/model.md` (Creator extension) and `contracts/sc-creator.json` describe the
current creator as six operations on API-GsdQualityGate 1.4.0
(`submitCreatorRequest`, `listCreatorGaps`, `registerCapability`, `reviseCapability`,
`createSpecialist`, `getCreatorGate`), backed by migration M-2026-003
(`migrations/V0003__self_improving_creator.sql`) with six new tables, all
`CREATE TABLE IF NOT EXISTS`, no `ALTER`/`DROP`/FK/CHECK, joins by TEXT logical key:

| Table | What it proves today |
|---|---|
| `creator_requests` | 1 verdict record per missing-capability request, 0 lost (`AC-61`, `AC-62`: 30 agents) |
| `creator_gaps` | 10 sessions checked → 10 gap records, 0 uncounted, 0 dangling (`AC-63`, `AC-64`) |
| `capability_registers` | 1 owner + 1 test record per row, 0 missing owners, 0 duplicates (`AC-65`, `AC-66`) |
| `capability_revisions` | 0 regressions, 1 improvement record, 0 divergences (`AC-67`, `AC-68`, NFR-P5) |
| `specialists` | 1 domain → 1 new agent, 1 capability set, 1 owner record, cap 30, 0 spawns beyond (`AC-69`, `AC-70`) |
| `creator_runs` | promotion refused while high-severity findings remain, exit 0 (`AC-71`, `AC-72`, NFR-S5, NFR-C3) |

The spec (`docs/requirements/spec-T6dc4-req.md` § Scope) says the same thing in prose:
1 creator reachable by 30 agents, every 0-match session listed as 1 gap, every new
capability registered with 1 owner + 1 test record, revisions at 0 regressions,
specialists capped at 30, runs gated on 0 high-severity findings.

**What that gives us:** reachability, counting, and a promotion gate with a *negative*
rule (refuse). **What it does not give us:** a positive path. There is no state for a
capability between "someone asked" and "it is registered"; no schema that says what a
tool *is* beyond a name and an owner; no rule that says what may be generated, from
what, or with what permissions. `capability_registers` stores a `capability_id` and an
`owner`; it does not store input/output types, side effects, permission requirements,
a version, or a deprecation state. A capability can be "registered" and there is no
machine-checkable way to tell whether it is safe to call.

### 1.2 The existing tool contract is a checker shape, not a descriptor

`swarm/schemas/tool.contract.v1.json` requires every registered tool to carry
`name, owner, purpose, input_schema, output_schema, errors, timeout_s, idempotency,
permitted_callers, entrypoint, handler, examples`, with `name` lane-prefixed
(`be_`, `fe_`, `shared_`, …), `idempotency ∈ {idempotent, at-least-once, unsafe}`,
`owner` an agent id `A01–A30` or `shared`, and `side_effect_tier ∈ {READ, WRITE,
DESTRUCTIVE, EXTERNAL}` (absent = READ; the v2.2 checker rejects DESTRUCTIVE/EXTERNAL).
`swarm/tool_registry.py` enforces this shape per agent against `agents.json`
`capability_policy` (`tools min 10 / max 15, enforce true`; live counts 12–13).

This is the right floor — §5's `ToolDescriptor` extends it rather than replacing
it — but it is a *registration-time* contract. It has no version, no lifecycle state,
no permission set, no success criteria, and no deprecation marker. Nothing in it can
answer "may the generator produce this tool, and may a caller invoke this version of
it?"

### 1.3 Standing constraints this design inherits

From Wave 1 and the repo, binding on everything below:

- **Fakes only.** Any fixture, tracker mirror, or evidence sample used to exercise the
  lifecycle must be a fake/TBD; no live Linear/Notion writes (SPE-38338 realtime-tracker
  limit, `docs/reconcile-SPE-38338.md` §4). Gap evidence and session samples in tests are
  synthetic records, never live tracker rows.
- **Learning disabled.** The lifecycle must not require a learning component. Session
  feedback may be *recorded* (§11) but must not *change* any descriptor, threshold, or
  policy until a policy artefact is ratified. HTIL notes are user-recorded, not policy.
- **Approver = user via pause.** Publication (§9) is a pause point for the user, never
  auto-approved. No role, agent, or gate may publish on the user's behalf.
- **Tool-unless-judgement.** Descriptor validation, transition legality, sandbox
  permission checks, and replay are tool-checked. Judgement is reserved for what tools
  cannot answer (descriptor *content* quality, whether a gap is a real need — §4.5).
- **Additive only.** Migration convention is `CREATE TABLE IF NOT EXISTS`, no `ALTER`,
  no `DROP` (`V0002__graph_engine.sql:15-17`, M-2026-003). Versioning is additive
  (`1.4.0` → a new minor, never a breaking bump to an existing contract; ADR-0010/0011).
- **Cap 30.** Active workers hold at 30 with 0 spawns beyond the cap
  (`docs/GOAL.md` pool of 30, `specialists.worker_cap`). A new specialist agent is a
  descriptor, not a spawn beyond the cap (§6.4).
- **No auto-retry** on any tool until a per-tool idempotency/side-effects inventory
  records eligibility (DECISIONS.md D5) — the descriptor's `idempotency` field (§5) is
  where that inventory row would live.
- **`gsd-autonomous` out of scope** (D6). This design covers the creator for existing
  phases only.

### 1.4 What the design must not do

- Design a learning system. §11 is a feedback *record format* with the loop disabled.
- Design a live tracker integration. Fakes only, always.
- Widen `tool.contract.v1.json` in place. Descriptors are a new schema that *references*
  the contract's fields; the existing checker keeps working unchanged.
- Let generation invent capabilities. §7 is a closed rule, not a guideline.

---

## 2. Design principles

1. **One lifecycle for every capability.** Tool or agent, every item in the registry
   is in exactly one lifecycle state (§3) at any time. No side channels: if it is
   callable, it is `published`; if it is not `published`, a caller must be refused.
2. **Descriptor before artefact.** Nothing is generated from a chat message, a session
   transcript, or a gap record. Generation reads one approved descriptor and nothing
   else (§7). The descriptor is the single source of truth; the artefact is derived.
3. **Recurrence, not anecdote.** A single session asking for a tool is evidence, not a
   proposal. A proposal exists only when recurring evidence crosses a frequency
   threshold (§4). This is the cheapest defence against a noisy session generating a
   permanent capability.
4. **Least privilege by construction.** A descriptor declares the *minimum* permissions
   its job needs; the sandbox grants exactly those and nothing else (§8). Generation
   cannot add a permission the descriptor did not declare, and the sandbox cannot
   grant one the generator did not emit.
5. **Immutable versions, binding deprecation.** A published descriptor version never
   changes. Change = new version. Deprecation is a state transition on a version, not a
   deletion, and it is binding on callers (§10). History is never rewritten.
6. **Tool-checked gates, human-checked publication.** Every transition guard that can be
   a tool *is* a tool (§3.3, §14). The one guard that cannot be — "should this capability
   exist?" — is a human pause (§9), and it is the only pause.
7. **Feedback is recorded, never learned from — yet.** Session outcomes are stored
   against the descriptor version that produced them (§11) so that, *if* a policy is
   later ratified, there is evidence to learn from. Until then the loop is inert and
   provably so (§14, test F-3).

---

## 3. Unified registry lifecycle

### 3.1 States

```
proposed ──▶ specified ──▶ generated ──▶ sandbox ──▶ review ──▶ approved ──▶ published ──▶ deprecated
   ▲             │              │              │             │              │               │
   │             └─▶ rejected   └─▶ rejected   └─▶ rejected  └─▶ rejected   └─▶ rejected    └─▶ rolled_back
   └─ (new evidence re-proposes a rejected item as a new proposal; the old one stays rejected)
published ────────────────────────────────────────────────────────────────────────────────▶ rolled_back
```

- `proposed` — a gap has crossed the threshold (§4); a `ToolDescriptor` or
  `AgentDescriptor` skeleton exists. Not callable. No artefact.
- `specified` — the descriptor is complete and schema-valid (§§5–6). Not callable. No
  artefact yet; the descriptor is the only thing being reviewed by tools.
- `generated` — a deterministic artefact has been produced from the approved-for-
  generation descriptor (§7). Not callable. The artefact is bound to the descriptor
  digest.
- `sandbox` — the artefact is running under least-privilege sandboxing (§8) with
  recorded sessions replayed against it (§8.3). Not callable by swarm agents.
- `review` — contract and replay gates have run; results are attached to the descriptor
  version. Still not callable.
- `approved` — gates passed and the user has approved publication at the pause (§9).
  Still not callable — approval is permission to publish, not publication.
- `published` — the descriptor version is in the registry and callable by its
  `permitted_callers`. This is the *only* callable state.
- `deprecated` — a published version that callers must stop using (§10). Not callable
  for new work; existing pinned callers get an explicit `DEPRECATED` refusal, never a
  silent failure.
- `rolled_back` — a published version withdrawn after an incident or a bad deploy
  (§10.3). Terminal. Not callable. History intact.
- `rejected` — terminal, reachable from any pre-publication state when a gate fails
  or the user declines at the pause. The descriptor and its evidence stay in the
  registry; a *new* proposal may re-propose the same need (§3.3).

### 3.2 Transition table

Every transition requires the guard to be tool-verified *before* the state changes.
An unmet guard leaves the item in its current state and records the refusal — it never
moves the item "a little way".

| # | From → To | Guard (tool-checked unless noted) | Evidence recorded |
|---|---|---|---|
| T1 | (none) → `proposed` | Gap evidence meets §4 threshold; descriptor `kind` (`tool`/`agent`) declared; requester agent id valid in `agents.json` roster | Gap records + session refs (§4) |
| T2 | `proposed` → `specified` | Descriptor validates against §5/§6 schema; all required fields present; `owner` resolves; `version` is valid semver | Validation report (tool output) |
| T3 | `specified` → `generated` | Descriptor is in `specified` (not `proposed`); generation inputs are the descriptor digest only (§7); output digest recorded | Descriptor digest + artefact digest + generator version |
| T4 | `generated` → `sandbox` | Artefact digest matches T3 record; sandbox policy file exists for the descriptor's declared permissions | Sandbox launch record |
| T5 | `sandbox` → `review` | Sandbox run completed; recorded sessions replayed (§8.3); zero credential/permission violations observed | Replay report + sandbox telemetry |
| T6 | `review` → `approved` | Contract gate pass (§8.3); replay gate pass (0 regressions, 0 divergences vs. baseline); 0 high-severity findings (AC-71/AC-72) — **then** user approval at the pause (§9) | Gate outputs + approval record (approver = user) |
| T7 | `approved` → `published` | Approval record present and names this exact descriptor digest + version; registry write is idempotent (§3.4) | Registry row (descriptor verbatim + digests) |
| T8 | `published` → `deprecated` | Deprecation notice names a successor version or `none`; `deprecated_at` set; notice period recorded (§10) | Deprecation record |
| T9 | `published`/`deprecated` → `rolled_back` | Incident or regression evidence (§10.3); user approval required (same pause as T6) | Rollback record + incident ref |
| T10 | any pre-publication → `rejected` | Gate failure, schema failure, sandbox violation, or user declines at the pause | Refusal record with the failing check named |
| T11 | `rejected` → `proposed` (new) | New evidence since the rejection *or* a revised descriptor; creates a **new** proposal id, never reopens the rejected one | New gap evidence |

### 3.3 Illegal transitions (must be refused, with the concrete reason)

- Any transition not in §3.2 (e.g. `proposed → published`, `specified → approved`,
  `sandbox → published`, `review → published`, `generated → review` skipping `sandbox`).
- `approved → published` where the registry digest differs from the approved digest
  (the artefact changed after approval — re-approval required, back to `review` as a new
  version, never a silent re-publish).
- Any transition out of `deprecated` except `→ rolled_back`.
- Any transition out of `rolled_back` or `rejected` (both terminal; T11 creates a new item).
- `published → deprecated` / `→ rolled_back` without the §10 notice/evidence records.
- A transition whose guard tool did not run (no evidence = no move). This is the
  tool-unless-judgement rule applied to the lifecycle itself.

### 3.4 Idempotent registry writes

A registry write is keyed by `(descriptor_id, version, descriptor_digest)`. Re-applying
the same write with the same digest is a no-op that returns the existing row (the same
rule `capability_registers.capability_id UNIQUE` already gives the current surface —
AC-66, 0 duplicate rows). Re-applying with a *different* digest for an existing
`(id, version)` is a refusal, never an overwrite: an immutable version cannot be
edited; the fix is a new version. Writers use compare-and-set on the digest so two
concurrent publishers cannot interleave.

### 3.5 State vs. the existing surface

The lifecycle is a new layer over the current tables, not a rewrite of them:
`creator_requests`/`creator_gaps` feed T1 (§4); `capability_registers` becomes the
published-state index (one row per published descriptor version, with lifecycle state
added); `capability_revisions` records T3–T5 evidence per version; `creator_runs`
records T6 gate results; `specialists` becomes the `agent`-kind rows of the same
registry (§6.4). §13 lists the additive schema mapping.

---

## 4. Gap detection — recurring evidence + frequency threshold

### 4.1 Evidence sources

A gap is a *session* in which an agent needed a capability and no published descriptor
matched. Evidence comes from exactly three sources, all already counted by the current
surface:

1. **Missing-capability requests** (`creator_requests`): an agent explicitly recorded
   "I needed X and there is no tool for it" — 1 verdict record per request, 0 lost (AC-61).
2. **Session scans** (`creator_gaps`): sessions checked for 0 matching tools and listed
   as gap records — 10 sessions → 10 gap records in the AC-63 baseline, 0 uncounted.
3. **Session links** (`creator_gaps.dangling_links`): each session that produced a gap
   must link to exactly one gap record; dangling links are a data-integrity failure
   (AC-64: 0 dangling), not a gap.

No other source counts. In particular, a single agent's chat message, a tracker issue
title, or a judgement call is not evidence — it is, at most, a pointer to look for
evidence. Fakes only: in tests these are synthetic session records.

### 4.2 What counts as "the same gap"

Two evidences are the same gap iff they normalise to the same **gap key**:

```
gap_key = (kind, normalised_need, input_shape)
```

- `kind` — `tool` or `agent` (a missing *agent* for a domain is not a missing *tool*).
- `normalised_need` — the capability need with surface variation removed: lower-cased,
  whitespace/punctuation collapsed, synonyms mapped through a fixed, versioned synonym
  table (the table is part of the proposal record; a synonym change creates a new key
  version, it does not re-key old evidence).
- `input_shape` — the types of the inputs the session had available (e.g. `(text, path)`
  vs `(text,)`). Two needs with different input shapes are different capabilities; they
  may both be proposed, and §5's descriptor validation will refuse a descriptor whose
  `input_schema` does not match the shape in its evidence.

Exact-duplicate evidence (same session, same key, submitted twice) is deduplicated by
the session id + gap key pair and counts **once**. The counter is a count of distinct
sessions, never a count of submissions.

### 4.3 Threshold

A gap becomes a `proposed` item (T1) when, within a rolling window, it has:

- **recurring evidence:** distinct sessions from **at least 2 distinct agents** (a single
  agent's repeated need is one agent's workflow, not a swarm gap), **and**
- **frequency:** **≥ 3 distinct sessions** in the rolling **30-day** window.

Both numbers are configuration, stored on the proposal record with the window and the
synonym-table version that produced the key, so the decision is reproducible from the
evidence alone. A gap that has crossed the threshold stays proposed until it is
specified, rejected, or superseded; evidence below the threshold stays in the gap
list (visible, counted, 0 uncounted) and is never silently dropped. A gap that drops
out of the window is marked `stale`, not deleted — its evidence remains countable.

*Why these numbers:* 3 sessions / 2 agents / 30 days is the smallest setting that
distinguishes "recurring" from "anecdotal" while still firing inside one planning
cycle. The values are deliberately in the descriptor's config, not in code, so A08 can
tune them against real gap rates without a contract change. The test plan (§14, G-1..G-4)
pins the boundary behaviour; the *values* are a user decision (§15, Q2).

### 4.4 Deduplication against the registry

Before T1 fires, the gap key is matched against all `published` and `specified`
descriptors (by `normalised_need` + `input_shape` + `kind`). A match means the gap is
already covered: the evidence is attached to that descriptor as a *usage signal*
(counted, linked, not a new proposal), and the gap is closed as `covered`. Only an
unmatched gap above threshold proposes anything. This is what stops the creator from
generating a second tool for a job a published tool already does — the failure mode
that makes a registry grow without bound.

### 4.5 Judgement boundary

Tool-checked: session counting, distinct-agent counting, window arithmetic, dedup,
registry matching, threshold comparison. Judgement (human, at the T2→T6 review, not at
detection): whether the *content* of the need is real and worth a capability. Detection
never uses judgement — a judgement call cannot create or suppress a proposal, it can
only reject one at T10 with a named reason.

---

## 5. ToolDescriptor schema

A `ToolDescriptor` is the complete, versioned, machine-checkable statement of one tool.
It *extends* `swarm/schemas/tool.contract.v1.json` (every contract field is present;
the contract checker keeps passing unchanged) with the fields the contract lacks:
lifecycle, version, permissions, success criteria, and deprecation. Proposed JSON-Schema
shape (implementer pins the `$id`; field names are fixed by this design):

| Field | Type | Required | Semantics |
|---|---|---|---|
| `descriptor_id` | string | yes | Stable id across versions, e.g. `tool.be_diff_summary`. Never reused for a different need. |
| `kind` | `"tool"` | yes | Discriminator. `"agent"` uses §6. |
| `version` | string (semver) | yes | This descriptor's version. Immutable once published (§3.4). New behaviour = new version. |
| `name` | string | yes | Lane-prefixed tool name, exactly as `tool.contract.v1.json` requires (`be_`, `shared_`, …). One name per descriptor id. |
| `owner` | string | yes | Agent id `A01–A30` or `shared` (contract field). Must resolve in `agents.json`. |
| `purpose` | string | yes | One-sentence job statement (contract field). |
| `input_schema` | object (JSON Schema) | yes | Typed inputs (contract field). Every input names its type; no `type: "any"` at the top level. |
| `output_schema` | object (JSON Schema) | yes | Typed outputs (contract field). |
| `success_criteria` | array of string | yes | Machine-checkable outcomes, each naming its evidence source (test suite, gate, replay). Mirrors the graph model's `success_criteria` (`docs/design-graph-SPE-38341.md` §4.1). A descriptor with no success criteria cannot reach `specified`. |
| `errors` | array of enum | yes | Contract error set (`INVALID_INPUT`, `UNAUTHORIZED`, `DEPENDENCY_UNAVAILABLE`, `TIMEOUT`, `PARTIAL_SUCCESS`). |
| `timeout_s` | number > 0 | yes | Contract field; also the sandbox wall-clock budget (§8.2). |
| `idempotency` | enum | yes | `idempotent` \| `at-least-once` \| `unsafe` (contract field). `unsafe` tools may not be auto-retried (D5) and must declare it. |
| `side_effect_tier` | enum | yes | `READ` \| `WRITE` \| `DESTRUCTIVE` \| `EXTERNAL` (contract field, here *required*, not optional). `DESTRUCTIVE`/`EXTERNAL` descriptors cannot be generated by the creator at all — they require a human-authored tool (§7.3). |
| `permitted_callers` | array of string | yes | Agent ids (or `shared`) allowed to call the *published* version. Empty is invalid — a published tool with no callers is dead weight. |
| `permissions` | object | yes | Least-privilege grant set (§8.1): `fs_read: [paths]`, `fs_write: [paths]`, `network: "none" \| [hosts]`, `env: [names]` (names only, never values), `subprocess: bool`. Defaults are deny-all; every entry is an explicit allow. |
| `entrypoint` / `handler` | string | yes | Contract fields. `handler` is `swarm.tools.<module>:<function>`; the generator (§7) emits exactly this, nothing else. |
| `examples` | object | yes | Contract field; at least one input→output example that the replay gate (§8.3) can run. |
| `lifecycle_state` | enum | yes | §3.1 state. Stored, never inferred. |
| `gap_evidence` | object | yes | The §4 evidence that proposed this descriptor: `gap_key`, session refs, distinct-agent count, threshold config version. |
| `descriptor_digest` | string (`sha256:` + 64 hex) | yes (from `generated`) | Digest of the canonical descriptor JSON. The generation and approval binding (§§7, 9). |
| `deprecated` | object \| null | yes | `null` while published; `{ successor: version \| "none", notice_at, reason }` when deprecated (§10). |
| `stub` | boolean | no | Contract field, carried through. A stub descriptor cannot pass T5 (sandbox needs a real artefact). |

**Validation invariants (ToolDescriptor):**

- D1. `name` is lane-prefixed, unique in the registry, and matches `descriptor_id`'s
  lane. A name collision with an existing *different* descriptor is a refusal (T10).
- D2. `owner` resolves to a roster agent; `permitted_callers` ⊆ roster ∪ {`shared`}.
  A descriptor whose owner is not a roster agent cannot reach `specified`.
- D3. `input_schema`/`output_schema` are valid JSON Schemas; every property has a
  concrete `type`. `success_criteria` is non-empty.
- D4. `permissions.network` defaults to `"none"`; any host allow-listed must be a
  literal hostname (no wildcards). `permissions.env` names must not match the secret
  pattern set (`*KEY*`, `*TOKEN*`, `*SECRET*`, `*PASSWORD*` — §8.4) — a descriptor that
  asks for a secret-named env var is refused at T2, not sandboxed.
- D5. `version` is valid semver 2.0.0. A change to `input_schema`/`output_schema`/
  `permissions`/`side_effect_tier` requires at least a minor bump; a change to
  `input_schema` that removes or retypes an input requires a major bump (and, per the
  repo's autonomy rules, a major bump is L3 — §9.3).
- D6. `side_effect_tier ∈ {DESTRUCTIVE, EXTERNAL}` ⇒ the descriptor is flagged
  `generation_forbidden: true` and can only become published via a human-authored
  artefact through the same lifecycle (it still needs T2–T9; only T3 is manual).
- D7. The descriptor JSON, canonicalised (sorted keys, no whitespace), hashes to
  `descriptor_digest`. Any field change changes the digest; §§7 and 9 bind to it.

---

## 6. AgentDescriptor schema

An `AgentDescriptor` is the same idea for a specialist agent (§1.1, `specialists`):
one domain, one capability set, one owner record, inside the cap of 30.

| Field | Type | Required | Semantics |
|---|---|---|---|
| `descriptor_id` | string | yes | e.g. `agent.domain.diff_review`. Stable across versions. |
| `kind` | `"agent"` | yes | Discriminator. |
| `version` | string (semver) | yes | As §5. |
| `domain` | string | yes | The specialist's domain (the `specialists.domain` column). One domain per descriptor. |
| `owner` | string | yes | The agent id that owns the specialist (the `specialists` owner record). Must resolve in `agents.json` or be the `shared` pool. |
| `capability_set` | array of string | yes | Tool names (published descriptor names) the specialist may call. Every entry must resolve to a `published` ToolDescriptor at the specialist's publish time; a capability_set entry that is `deprecated` or `rolled_back` refuses T7. Length must keep the specialist inside `capability_policy` (10–15 tools) — a specialist with 0 or >15 tools cannot reach `specified`. |
| `purpose` | string | yes | One-sentence job statement, as §5. |
| `inputs` / `outputs` | array of `IORef` | yes (may be empty) | Typed I/O, same shape as the graph model's `IORef` (`docs/design-graph-SPE-38341.md` §4.1): `{ name, schema, source }`. |
| `success_criteria` | array of string | yes | As §5. |
| `permissions` | object | yes | Union ceiling: the specialist's permissions are the *intersection* of its declared set and the union of its capability_set tools' permissions. A specialist can never hold a permission none of its tools holds (§8.1). |
| `lifecycle_state` | enum | yes | §3.1 state. |
| `gap_evidence` | object | yes | As §5 (the domain gap). |
| `descriptor_digest` | string | yes (from `generated`) | As §5. |
| `deprecated` | object \| null | yes | As §5. |

### 6.4 Specialist creation is a descriptor, not a spawn

`createSpecialist` today records 1 new agent with 1 capability set and 1 owner record
(AC-69) and 0 spawns beyond the cap (AC-70). In this design that record *is* an
`AgentDescriptor` moving through §3.2. The cap is enforced at T7: publishing a
specialist when 30 agents are already active is a refusal (`E-CAPACITY`), never a
spawn-and-hope. Retiring a specialist is `deprecated` → `rolled_back` like any other
descriptor, which is how the pool stays at 30 without ever exceeding it.

---

## 7. Generation — only from approved descriptors

### 7.1 The closed rule

**The generator emits an artefact iff there is an approved descriptor, and it emits
exactly what that descriptor says.** Concretely, T3 (`specified → generated`) runs iff:

1. The descriptor is in state `specified` (i.e. it passed T2 schema validation) —
   `proposed` descriptors cannot be generated from, ever.
2. The generator's input is the canonical descriptor JSON *by digest*. The generator
   takes no transcript, no session text, no gap description, no free-text prompt as
   input. If it cannot resolve the digest, it refuses.
3. The output is a deterministic function of (descriptor digest, generator version):
   the same descriptor + same generator version ⇒ byte-identical artefact, every time.
   The generator version is recorded with the artefact (T3). Non-determinism
   (timestamps, random ids, host paths) in the output is a generator bug that fails
   the replay gate (§8.3), not a licence to vary.

### 7.2 What the generator may and may not emit

- **May emit:** the handler skeleton named by `entrypoint`/`handler`, the typed
  I/O parsers for `input_schema`/`output_schema`, the permission declarations for
  `permissions`, the error mapping for `errors`, and the examples as self-tests. The
  *body* of the handler — the actual capability logic — is generated from a fixed
  template library keyed by `purpose` pattern, **never** from session text. A template
  that cannot cover the descriptor's stated purpose refuses generation (T10,
  `no_template`) rather than improvising.
- **May not emit:** credentials or secret values (§8.4); any permission not in
  `permissions`; any caller not in `permitted_callers`; any network access beyond
  `permissions.network`; any capability outside `purpose`. The emitted artefact is
  checked against the descriptor by a *separate* verifier (not the generator) before
  T4: field-by-field, the artefact's declared permissions ⊆ the descriptor's, its
  callers = the descriptor's, its I/O schemas = the descriptor's. A mismatch is a
  T10 rejection with the differing field named.

### 7.3 Forbidden generation

A descriptor with `side_effect_tier ∈ {DESTRUCTIVE, EXTERNAL}` (D6) or with
`generation_forbidden: true` cannot pass T3. Its artefact must be human-authored; the
lifecycle still applies (T2, T4–T9), only T3 is manual and the human author is recorded
in `gap_evidence`. This keeps the creator inside the repo's existing tier rules:
`tool.contract.v1.json` reserves DESTRUCTIVE/EXTERNAL and the checker rejects them, so
a generated tool can only ever be READ or WRITE.

### 7.4 Provenance

Every generated artefact carries a provenance record: `(descriptor_id, version,
descriptor_digest, generator_version, artefact_digest, generated_at)`. The registry
stores it verbatim. A published tool with no provenance record is a finding, not a
tool — the verifier (§7.2) refuses it at T4.

---

## 8. Sandbox and gates — least privilege, contract, replay

### 8.1 Least-privilege sandbox

The sandbox is constructed *from the descriptor's `permissions`*, not from a policy
file a human edits per tool:

- **Filesystem:** only `fs_read`/`fs_write` paths are mounted (read-only where
  declared read). No repo-writable mount unless `fs_write` names it. The swarm state
  dir is the default write root for WRITE tools (`tool.contract.v1.json` side-effect
  rule); anything outside it must be declared.
- **Network:** `network: "none"` ⇒ no sockets, no DNS. Any allow-listed host is
  proxied through the sandbox egress gate, which logs every connection and refuses
  everything not on the list. Wildcard hosts are invalid (D4).
- **Environment:** only the *names* in `permissions.env` exist in the sandbox; values
  are injected by the sandbox host from its own store, never from the descriptor,
  never from the session. A name matching the secret pattern set is refused at T2
  (D4) — the sandbox never sees a secret-named variable at all.
- **Subprocess:** `subprocess: false` (default) ⇒ no child processes. `true` ⇒ child
  processes inherit the same sandbox, and the gate records the spawn.
- **Time and memory:** `timeout_s` is the wall-clock budget; the sandbox kills at the
  budget and records a `TIMEOUT` (§5 `errors`). Resource limits (CPU, memory) are
  sandbox defaults, not descriptor fields — a descriptor cannot raise its own limits.

### 8.2 Sandbox telemetry

Every sandbox run records: descriptor id/version/digest, artefact digest, permission
set actually granted, every refused syscall/socket/spawn (with the descriptor field
that should have allowed it, or `undeclared`), wall time, exit state. A refused
operation that the descriptor *did* declare is a sandbox bug (finding against the
sandbox); a refused operation the descriptor did *not* declare is the sandbox working
as intended and is evidence *for* the descriptor, recorded as such. Telemetry is
evidence for T5, not a log nobody reads.

### 8.3 Contract and replay gates (T5 → T6)

Two gates, both tool-run, both recorded on the descriptor version:

1. **Contract gate.** The artefact is exercised against its own descriptor:
   `input_schema`/`output_schema` conformance (valid inputs accepted, invalid refused
   with `INVALID_INPUT`), `errors` mapping (each declared error reachable and correctly
   shaped), `idempotency` (an `idempotent` tool run twice with the same input yields
   the same output and no double effect), `permitted_callers` (a caller not on the
   list is refused with `UNAUTHORIZED`), `examples` (every example passes).
2. **Replay gate.** The recorded sessions that constitute the gap evidence (§4) — and a
   fixed baseline set of recorded sessions for revisions — are replayed against the
   artefact in the sandbox. Pass = **0 regressions** (no session that previously
   succeeded now fails) and **0 divergences** (no session whose output differs from
   the recorded output beyond the declared tolerance), matching the existing AC-67/
   AC-68/NFR-P5 budgets. A revision that regresses is refused and the previous
   published version stays published — the gate never publishes a worse tool over a
   working one.

Both gates feed the existing `creator_runs` record: `high_severity_findings` must be
0 for T6 to pass (AC-71), and a run with findings > 0 records a refusal (`promotions`
0, `refusal_records` ≥ 1) instead of publishing (AC-71/AC-72).

---

## 9. Publication — human-approved, versioned

### 9.1 The pause

T6 (`review → approved`) ends with the user approval pause. The pause presents the
user with: the descriptor (full JSON), the descriptor digest, the artefact digest,
the provenance record, both gate reports, the gap evidence (§4), and the exact
`version` that would be published. The user approves *that digest at that version* —
approval of "the tool" in the abstract does not publish anything. The approval record
names the approver (always the user; there is no other approver role in this design),
the digest, the version, and the timestamp. Declining is T10 (`rejected`) with the
user's reason recorded.

### 9.2 Versioned publication (T7)

Publishing writes the descriptor verbatim + digests + approval record to the registry
under `(descriptor_id, version, descriptor_digest)` (§3.4). Consumers pin by digest:
a caller that pins `tool.be_diff_summary@1.2.0` with digest `sha256:…` keeps working
even after `1.3.0` publishes, because publication is additive — a new version never
mutates an existing one. The registry's "current" pointer moves only on a successful
T7, and moving it is the publication; there is no other way for a version to become
callable.

### 9.3 Autonomy of the act

- Publishing a **semver-compatible** version (minor/patch, D5) after gates + user
  approval is the designed path — L2 in the repo's autonomy terms, with the user
  pause as the approval.
- A **major** version bump (D5: removed/retyped input) is L3: it needs the same pause
  *plus* a staged-rollout note on the descriptor (which callers migrate, in what order)
  before the user is asked to approve. The design does not auto-decide this; it refuses
  to present a major bump for approval without the rollout note.
- No agent, gate, or run may publish. `getCreatorGate` (the existing promotion gate)
  remains a *refusal* rule; this design adds the *approval* record it currently lacks.

---

## 10. Deprecation and rollback — binding changes, history intact

### 10.1 Deprecation is a transition, not a deletion

`published → deprecated` (T8) sets `deprecated: { successor, notice_at, reason }` on
the version. The descriptor, its digests, its gate reports, and its provenance stay in
the registry forever. Deprecation is **binding**: from `notice_at`, a caller that
resolves the deprecated version for *new* work is refused with an explicit
`DEPRECATED` error naming the successor version (or `none`, if the capability is being
retired with no successor). A caller pinned to the deprecated digest keeps working
during the notice period (default 30 days, recorded on the record) and is refused
after it. Silence is never an option — a deprecated tool that fails with a generic
error is a design bug (§14, X-2).

### 10.2 Successor discipline

`successor` must name a `published` version of the *same* `descriptor_id` (or `none`).
A deprecation that points at a different capability is a new gap (§4), not a
deprecation — it must go through T1. This stops "deprecated, use this other thing"
from laundering an unreviewed capability past the lifecycle.

### 10.3 Rollback

`published`/`deprecated → rolled_back` (T9) is for incidents and bad deploys: a
published version that is causing harm is withdrawn immediately, with the same user
pause as publication (a human decides; the system prepares the record). Rollback is
terminal: the version can never be re-published (a fix is a new version, T1–T7 again).
After a rollback, the registry's current pointer reverts to the previous published
version of the same descriptor — *if* that version is still `published` and not itself
deprecated/rolled back; if it is not, the capability has no callable version and
callers get `NO_PUBLISHED_VERSION`, which is honest and grep-able. Rollback evidence
(incident ref or regression report) is recorded on the version and linked from the
successor proposal, so the next attempt starts from the post-mortem, not from zero.

---

## 11. Session-learning feedback — recorded, disabled until policy

**Confirmed decision: learning is disabled until policy. This design is gated on that
decision, and the gate is structural, not a flag someone might flip.**

### 11.1 What is recorded

Every published tool invocation (and every sandbox replay, §8.3) records a feedback
row against the exact descriptor version that served it:

```
feedback = {
  descriptor_id, version, descriptor_digest,
  session_ref,           # fake/TBD in tests; never a live tracker id
  outcome,               # success | error | refused | deprecated_refusal
  replay_divergence,     # bool + magnitude, from §8.3
  user_signal,           # explicit only: accept / reject / none — never inferred
  recorded_at
}
```

`user_signal` is captured only when the user explicitly gives one (e.g. at a review or
a pause). It is never mined from session text, never inferred from whether a session
continued, never scored by a model. There is no sentiment field, no embedding, no
"quality score" — because there is no ratified definition of any of them.

### 11.2 What is not done with it

- No descriptor field is modified from feedback. No threshold (§4.3) changes. No
  permission is widened. No version is auto-published. No gap is auto-proposed.
- The feedback table is read by exactly two things: the replay/divergence accounting
  (§8.3, which is deterministic) and a human reading a post-mortem (§10.3).
- If a future policy artefact ratifies a learning loop, it will define: what may be
  learned, from which signals, with what evidence bar, and what stays human-approved.
  That policy is a separate design and a separate decision; this doc does not pre-empt
  it. Until it exists, the proof obligation is negative: §14 test F-3 requires that
  *running the full lifecycle with feedback present produces byte-identical registry
  state to running it without* — i.e. the loop is provably inert, not merely switched off.

---

## 12. Security sketch (STRIDE, abbreviated)

| Threat | Where it lands | Design answer |
|---|---|---|
| **Spoofing** — a descriptor claims a lane/name it does not own | T2 (D1) | Name uniqueness + lane binding; collision is a refusal with both ids named. |
| **Tampering** — artefact changed after approval | T7 (§3.3) | Approval binds the digest; a changed artefact cannot publish under the old approval. Verifier (§7.2) re-checks at T4. |
| **Repudiation** — "I never approved that" | T6/T9 | Approval record names approver, digest, version, timestamp; stored verbatim. Rollback record likewise. |
| **Information disclosure** — a tool exfiltrates session data | §8.1 | `network: "none"` default; egress gate logs + refuses undeclared hosts; env values never in the descriptor. |
| **Denial of service** — a tool hangs or spins | §8.1 | `timeout_s` kill + resource caps the descriptor cannot raise; sandbox telemetry. |
| **Elevation of privilege** — a tool calls a tool it is not permitted to call | §5 `permitted_callers`, §8.3 contract gate | Caller check is a gate (refused with `UNAUTHORIZED`), not a convention. Specialist permissions are an intersection (§6), never a union. |
| **Credential theft** — a generated tool wants a secret | D4, §8.1, §8.4 | Secret-named env refused at T2; descriptor carries names only; sandbox injects values from its own store. A descriptor asking for a credential is evidence of a bad gap, not a permission to grant. |
| **Registry poisoning** — a flood of low-quality proposals | §4 | Recurrence + distinct-agent threshold; dedup against published; a single agent cannot propose anything alone. |

---

## 13. Data mapping — additive onto M-2026-003

The lifecycle needs new facts, but the repo convention is additive tables only (§1.3).
Proposed (implementer confirms against `data_reversible_pair_check` conventions;
migration name left to A07):

- **`registry_descriptors`** — one row per `(descriptor_id, version)`: the descriptor
  JSON verbatim, `descriptor_digest`, `lifecycle_state`, `kind`, `owner`,
  `gap_evidence` JSON, `deprecated` JSON (nullable), `ts`. `UNIQUE (descriptor_id,
  version)`; digest compare-and-set enforced in application code (§3.4).
- **`registry_transitions`** — one row per §3.2 transition attempt (including refused
  ones): `descriptor_id, version, from_state, to_state, guard, evidence_digest,
  actor, ts`. This is the audit trail T10 refusals land in.
- **`registry_feedback`** — §11.1 rows, keyed by `(descriptor_id, version)`.
- **Existing tables gain no columns.** `capability_registers` is read as the
  published-state index (a row exists iff the descriptor is `published` or was);
  `capability_revisions` stores T3–T5 evidence by `(capability_id = descriptor_id,
  version)`; `creator_runs` stores T6 gate results by `run_id`; `specialists` stores
  agent-kind descriptors' domain/owner facts. SC-Creator's field set is unchanged;
  a new contract id (e.g. SC-Registry) carries the descriptor fields, BACKWARD
  compatible, consumers pin by digest — the ADR-0010/0011 pattern.

---

## 14. Test plan — checklist (acceptance)

Checklist for the implementer. Each item is one test (or one test case) unless marked
otherwise; every item names the section it pins. "Refused" always means: state unchanged,
refusal recorded in `registry_transitions`, error names the concrete reason.

### Descriptor validation (V)

- **V-1** A minimal valid ToolDescriptor (§5) reaches `specified` (T2 pass); validation
  report names each required field present. *(§5, T2)*
- **V-2** Each missing required field (parametrised over §5's table) is refused at T2
  with the field named. *(D3)*
- **V-3** `input_schema`/`output_schema` that are not valid JSON Schemas, or with a
  top-level `type: "any"`, are refused. *(D3)*
- **V-4** `owner` not in the `agents.json` roster, and a `permitted_callers` entry not
  in the roster ∪ {`shared`}, are refused. *(D2)*
- **V-5** `name` without a lane prefix, and a `name` colliding with a different existing
  descriptor, are refused. *(D1)*
- **V-6** `success_criteria: []` cannot reach `specified`. *(D3)*
- **V-7** `version` that is not valid semver 2.0.0 is refused; a descriptor that changes
  `input_schema` (removes/retypes an input) without a major bump is refused. *(D5)*
- **V-8** An AgentDescriptor whose `capability_set` has 0 entries or > 15 entries, or
  an entry resolving to a non-`published` ToolDescriptor, is refused at T2. *(§6)*
- **V-9** An AgentDescriptor whose `permissions` exceed the intersection ceiling
  (a permission none of its capability_set tools holds) is refused at T2. *(§6)*

### Illegal transitions (T)

- **T-1** Parametrised over §3.3: every transition not in §3.2 is refused
  (`proposed→published`, `specified→approved`, `sandbox→published`, `review→published`,
  `generated→review`, any out of `deprecated` except `→rolled_back`, any out of
  `rolled_back`/`rejected`). *(§3.3)*
- **T-2** `approved→published` with an artefact digest different from the approved
  digest is refused and the item returns to `review` as a new version. *(§3.3)*
- **T-3** A transition whose guard tool was not run (no evidence row) is refused.
  *(§3.3, tool-unless-judgement)*
- **T-4** `published→deprecated` without a successor/`none` + notice record is refused.
  *(§10.1)*
- **T-5** `rejected→proposed` on the *same* proposal id is refused; T11 creates a new
  proposal id. *(§3.2 T11)*

### Idempotent registry writes (I)

- **I-1** Publishing the same `(descriptor_id, version, digest)` twice returns the
  existing row; row count unchanged; 0 duplicate rows (the AC-66 rule, generalised).
  *(§3.4)*
- **I-2** Publishing the same `(descriptor_id, version)` with a different digest is
  refused, never an overwrite. *(§3.4)*
- **I-3** Two concurrent publishers of the same `(id, version)` with different digests:
  exactly one succeeds (compare-and-set); the loser is refused with the winner's digest
  named. *(§3.4)*
- **I-4** Re-running T3 generation from the same descriptor digest + generator version
  produces a byte-identical artefact digest. *(§7.1)*

### Sandbox permission / credential security (S)

- **S-1** A tool with `permissions.network: "none"` that attempts a socket/DNS in the
  sandbox is refused, the attempt appears in sandbox telemetry as `undeclared`, and the
  run is recorded as sandbox-working-as-intended. *(§8.1–8.2)*
- **S-2** A tool with an allow-listed host that connects to a *different* host is
  refused by the egress gate; a wildcard host in `permissions.network` is refused at T2.
  *(D4, §8.1)*
- **S-3** `fs_read`/`fs_write` outside the declared paths is refused; a WRITE tool with
  no declared `fs_write` cannot write anywhere except the swarm state dir default.
  *(§8.1)*
- **S-4** `subprocess: false` ⇒ a spawn attempt is refused and recorded; `subprocess:
  true` ⇒ the child inherits the sandbox and the spawn is recorded. *(§8.1)*
- **S-5** A descriptor requesting an env name matching the secret pattern set
  (`*KEY*`/`*TOKEN*`/`*SECRET*`/`*PASSWORD*`) is refused at T2 — the sandbox never runs it.
  *(D4)*
- **S-6** A descriptor containing an env *value* (not just a name) is refused at T2;
  the generator emits no env values. *(§§7.2, 8.1)*
- **S-7** A generated artefact declaring a permission, caller, or I/O schema not in its
  descriptor is refused by the independent verifier at T4 with the differing field named.
  *(§7.2)*
- **S-8** `timeout_s` is enforced: a tool that overruns is killed and recorded
  `TIMEOUT`; a descriptor cannot raise the sandbox resource caps. *(§8.1)*
- **S-9** A caller not in `permitted_callers` invoking the published tool is refused
  with `UNAUTHORIZED` (contract gate). *(§8.3)*

### Replay (R)

- **R-1** Replaying the gap-evidence sessions against a generated artefact: a session
  whose recorded output is not reproduced within tolerance is a divergence; any
  divergence fails the replay gate. *(§8.3)*
- **R-2** Replaying a revision against the baseline session set: 0 regressions required
  to pass T6; a regressing revision is refused and the previous published version stays
  published. *(§8.3, AC-67/AC-68)*
- **R-3** A run with `high_severity_findings > 0` records `promotions = 0`,
  `refusal_records ≥ 1`, and does not publish (AC-71); the refusal names the finding.
  *(§8.3)*
- **R-4** Contract gate: invalid input refused with `INVALID_INPUT`; each declared
  `errors` entry reachable; an `idempotent` tool run twice yields the same output with
  no double effect; every `examples` entry passes. *(§8.3)*
- **R-5** A `stub: true` descriptor cannot pass T5. *(§5)*

### Gap detection (G)

- **G-1** One session asking for a need ⇒ 1 gap record, no proposal (below threshold).
  *(§4.3)*
- **G-2** 3 sessions / 2 distinct agents / 30 days for one gap key ⇒ exactly one
  `proposed` item (T1); the same 3 sessions from 1 agent ⇒ no proposal. *(§4.3)*
- **G-3** Duplicate submissions (same session + key) count once; the counter is distinct
  sessions. *(§4.2)*
- **G-4** A gap key matching a `published`/`specified` descriptor closes as `covered`
  (evidence attached as a usage signal), never as a new proposal. *(§4.4)*
- **G-5** `uncounted_gaps = 0` and `dangling_links = 0` hold across the full suite
  (the AC-63/AC-64 budgets, asserted as a suite-level invariant). *(§4.1)*
- **G-6** Evidence outside the 30-day window marks the gap `stale`; the evidence remains
  countable and is never deleted. *(§4.3)*

### Publication, deprecation, rollback (P/X)

- **P-1** No approval record ⇒ `getCreatorGate` refuses publication (AC-71); an approval
  record naming a different digest/version than the one being published is refused.
  *(§9.1)*
- **P-2** The approval record names the user as approver, the digest, and the version;
  it is stored verbatim and readable after publication. *(§9.1)*
- **P-3** A major bump presented for approval without a staged-rollout note is refused
  at the pause; with the note, it proceeds. *(§9.3)*
- **X-1** Deprecating a published version keeps the descriptor, digests, gate reports,
  and provenance in the registry (history intact); the row is never deleted.
  *(§10.1)*
- **X-2** After `notice_at`, resolving the deprecated version for new work is refused
  with `DEPRECATED` naming the successor (or `none`); a pinned caller keeps working
  inside the notice period and is refused after it. *(§10.1)*
- **X-3** Rollback makes the version terminal (T9): re-publishing it is refused; the
  registry current pointer reverts to the previous still-`published` version, or
  callers get `NO_PUBLISHED_VERSION` if none exists. *(§10.3)*

### Learning-disabled proof (F)

- **F-1** Feedback rows are recorded against the exact descriptor version that served
  the session, with `user_signal ∈ {accept, reject, none}` and no inferred fields.
  *(§11.1)*
- **F-2** With feedback present, no descriptor field, threshold, permission, or version
  changes without a fresh T1–T7 pass. *(§11.2)*
- **F-3** (the inertness proof) Running the full lifecycle with a populated feedback
  table produces byte-identical registry state (all tables, all digests) to running it
  with an empty feedback table. *(§11.2)*
- **F-4** Session fixtures in every test above are fake/TBD records; no test performs a
  live Linear/Notion write (fakes-only, asserted by a transport-level guard in the test
  harness). *(§1.3)*

---

## 15. Open questions / user decisions

| # | Question | Why it matters | Default if unanswered |
|---|---|---|---|
| Q1 | Confirm `network: "none"` as the default for *all* generated tools, with per-host allow-lists as the only exception? | Sets the egress posture for every future tool. | Yes (this design assumes it). |
| Q2 | Confirm the §4.3 threshold (3 sessions / 2 agents / 30 days)? | The values are config, but the *order of magnitude* is a product decision. | As designed; A08 tunes against real gap rates. |
| Q3 | Confirm the default deprecation notice period of 30 days? | How long pinned callers keep working after deprecation. | 30 days. |
| Q4 | Is `DESTRUCTIVE`/`EXTERNAL` generation-forbidden (D6), or should the creator be allowed to *propose* such descriptors for a human author to implement? | Decides whether the creator can even *describe* a destructive tool. | Forbidden to generate; the lifecycle still describes it (T3 manual). |
| Q5 | Should the synonym table (§4.2) be a fixed versioned artefact, or derived per run? | A derived table makes gap keys non-reproducible across runs. | Fixed, versioned, recorded on the proposal. |

None of these block the schema or lifecycle; Q1–Q5 are config/default decisions the
user makes at implementation time.

---

## 16. Evidence trace

| Claim in this doc | Source (read, not invented) |
|---|---|
| Creator = 6 ops on API-GsdQualityGate 1.4.0; 6 tables in M-2026-003; AC-61..AC-72 budgets | `docs/data/model.md` § Creator extension; `contracts/sc-creator.json`; `docs/requirements/spec-T6dc4-req.md`; `docs/adr/0010-*.md` |
| Tool contract fields, lane prefixes, idempotency enum, side-effect tiers, checker rejects DESTRUCTIVE/EXTERNAL | `swarm/schemas/tool.contract.v1.json` |
| `capability_policy` 10–15 tools/skills, enforce true; 30-agent roster; worker cap 30 | `agents.json:10-13`; `docs/data/model.md` (`specialists.worker_cap`); `docs/GOAL.md` (via spec-T6dc4 assumptions) |
| Fakes only / learning disabled / approver=user via pause / tool-unless-judgement | Assignment context; `docs/reconcile-SPE-38338.md` §4; `docs/design-graph-SPE-38341.md` §1.5; `docs/design-orchestration-SPE-38340.md` §§1, 3 |
| Additive-only migration convention (`CREATE TABLE IF NOT EXISTS`, no ALTER/DROP) | `migrations/V0002__graph_engine.sql:15-17` (via `docs/design-graph-SPE-38341.md` §2); M-2026-003 (via `docs/data/model.md`) |
| No auto-retry until per-tool idempotency inventory exists; `gsd-autonomous` out of scope | DECISIONS.md D5, D6 (via `docs/design-orchestration-SPE-38340.md` §1) |
| Gap counting budgets (1 verdict/0 lost; 10 sessions→10 gaps/0 uncounted/0 dangling) | `docs/acceptance.md` AC-61..AC-64; `tests/test_creator_be.py` (AC-61..AC-64 cases); `contracts/sc-creator.json` |
| Revision budgets (0 regressions, 0 divergences, wall-clock parity) and promotion gate (0 high-severity findings) | AC-65..AC-72; `contracts/sc-creator.json` (`regressions`, `divergence_cases`, `high_severity_findings`, `refusal_records`); `docs/requirements/spec-T6dc4-req.md` § Scope |
| Everything under §§3–11 that is not in the left column | **Proposed by this design.** Markers: transition table (§3.2), threshold (§4.3), descriptor fields (§§5–6), generation rule (§7), sandbox (§8), pause (§9), deprecation (§10), feedback format (§11) are design proposals, not repo fact. |

---

*Design doc only. Gates/formatters skipped per assignment. No code changed.*
