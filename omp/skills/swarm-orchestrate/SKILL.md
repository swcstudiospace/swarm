---
name: swarm-orchestrate
description: "Drive AgentSwarm SDLC waves in-session via A01: status → lease → one tasks[] call (explicit agent per task) → ingest (runs reconcile) → DONE or ESCALATED. Parent never implements domain work."
disable-model-invocation: false
---

# swarm-orchestrate (in-session A01 driver)

## When to Use
- You are a01-orchestrator and a wave is ready, or the user explicitly asked for the full SDLC through the swarm.
- You were given a plan whose tasks carry `agent` fields.

Never use from a specialist session.

## Prerequisites
- Tools: swarm_plan, swarm_status, swarm_transition, swarm_assignment, swarm_ingest, task (plus read tools). You do **not** have `swarm_gate`: the gate agents (a08-qa, a09-reviewer, a10-security, a12-release) run it themselves.
- A correlation from swarm_plan or /swarm.

## Procedure

1. `swarm_status` for the correlation → the ready tasks, each with its `agent` and gate notes (`gate`, `gate_for`).
2. Lease every task of the ready wave with `swarm_transition`: PLANNED → CLAIMED, then CLAIMED → IN_PROGRESS. Lease gate tasks before their gate agent runs: `swarm_gate` refuses a gate task that is not IN_PROGRESS.
   Obtain each leased task's canonical prompt with `swarm_assignment({task_id, correlation_id})`; require `assignment_verified: true`. Do not construct signatures or use generic code-execution devices.
3. Make **one** `task` call for the wave:
   - `tasks[]` holds one item per task, each with `agent: <slug>` from the verified assignment and its exact canonical signed prompt unchanged.
   - Add `schemaMode: "strict"` to every item for a08-qa, a09-reviewer, a10-security and a12-release.
   - Gate agents run `swarm_gate` themselves. Never pass them findings or a verdict.
4. `swarm_ingest` every child's task.result before the next wave. Ingest applies the result and runs reconcile itself; there is no separate reconcile call. Reconcile:
   - moves a target failing a required gate CHANGES_REQUESTED → IN_PROGRESS (rework, feedback in its notes) and creates one gate rerun task per gate lineage, `<base>.r<N>` (base = the gate task id without trailing `.rN`; N = the lineage's next rerun sequence, not the target's rework count). Tasks that depended on the lineage wait for its rerun, and a release-gate rerun also waits for the other gate reruns of the same rework;
   - moves a target passing every required gate APPROVED → DONE;
   - after MAX_REWORK_LOOPS=2 rework loops, moves a still-failing target to ESCALATED and emits `escalation.request`.
5. Missing yield (D-07): a child whose output ends with `SUBAGENT_WARNING_MISSING_YIELD`, or has no parseable task.result, is ingested as FAILED with `error: {code: "E-CONTRACT", message: "missing yield"}`, or as BLOCKED with `needs` when it stated a need. Never IN_REVIEW.
6. Refusals and blocks:
   - Ingest refuses a gate result with E-CONTRACT `review verdict mismatch:` or `gate script not run`: the result is not applied and the gate task stays leased (IN_PROGRESS). Reconcile holds that gate's targets (never APPROVED) until the gate task that recorded the row has an accepted result; a FAILED gate task holds them until step 7 retries or escalates it. Re-dispatch that gate agent with the error text. Never transition around it.
   - A BLOCKED task: escalate to the human. Never self-release it.
7. FAILED tasks: nothing in-session retries them (only the headless runner does). For each task FAILED after ingest, read `attempt` from `swarm_status` (`max_attempts` defaults to 3):
   - `attempt` < `max_attempts`: `swarm_transition` FAILED → RETRY, re-lease it RETRY → CLAIMED → IN_PROGRESS, and re-dispatch it to its agent with the error in the payload;
   - otherwise: FAILED → ESCALATED, and report it as an escalation.
8. Loop: re-dispatch rework tasks (IN_PROGRESS after reconcile, already leased) to their agent with the feedback from notes. Repeat from step 1 until every task is DONE, ESCALATED, CANCELLED or BLOCKED awaiting a human. Then yield one task.result, your output schema (a bare swarm.status object is rejected):
   - `task_id`: the `correlation_id` of your plan.execute assignment (it carries no task_id of its own);
   - `state`: IN_REVIEW when every task is DONE. Otherwise BLOCKED when any task is BLOCKED awaiting a human, else FAILED;
   - `needs` (with BLOCKED): each BLOCKED task and its need;
   - `error` (with FAILED): `{code, message}`, code the escalation's `reason_code` (e.g. E-CONTRACT), message naming every ESCALATED, FAILED and CANCELLED task;
   - `summary_md`: the swarm.status summary, i.e. the per-state task counts and every ESCALATED, FAILED and BLOCKED task with its escalation or need.

## Parent contract
The session that loaded this skill (the human's top-level) **must never** do domain work. All implementation goes through A01 waves.

## Depth cap & plan mode
- If you have no `task` tool and are not in plan mode, you may only `yield {state: "BLOCKED", needs: "depth"}` when orchestrator tools (`bash`, `write`, `swarm_*`) are still present, or you are inside a workpool child. The advisor review surface (`read`, `grep`, `glob`, and `advise`) is not that cap: do not yield depth for it.
- An `<advisory>` note of any severity, including blocker, is the Omp advisor model's balanced-budget review. Weigh it. It is not a swarm BLOCKED. Keep the advisor. Do not skip the work pool or the `task` dispatch because the advisor spoke.
- If A01 cannot dispatch, only the specialist spawn waits. Read-only discovery is not blocked. Finish it with read, grep, and glob. Do not hand off with every node blocked, and do not answer an advisory blocker by repeating that the work remains blocked.
- In plan mode: yield a plan; perform zero Task Store writes.

## Negatives you must honour
- Specialists never receive the `task` tool.
- A01 running inside a workpool child yields BLOCKED depth.
- Plan mode never mutates the Task Store.
