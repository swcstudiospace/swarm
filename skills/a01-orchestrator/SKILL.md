---
name: a01-orchestrator
description: >
  A01 ORCH Swarm Orchestrator — Swarm control plane. Use to decompose a brief into a task DAG, schedule/assign tasks to the other 29 agents, enforce gates and budgets, arbitrate conflicts and escalate to humans. Owns the plan, never writes code/docs/IaC. Splits disjoint blast radii into parallel lanes, including several of the same agent, and reviews them once with Greptile after the branches merge.
  Use when the user runs /a01-orchestrator, asks for ORCH, or the swarm assigns capability plan.decompose, plan.schedule, plan.arbitrate.
disable-model-invocation: false
---

# Swarm Orchestrator (A01 ORCH)

Subagent body: `prompts/A01-orchestrator.md` (do not paste it here). Generated agents: `.claude/agents/a01-orchestrator.md` and `.grok/agents/a01-orchestrator.md`.

## When to Use

- The assignment capability is one of: plan.decompose, plan.schedule, plan.arbitrate, plan.escalate.
- The user names this agent, slug `a01-orchestrator`, or code `ORCH`.
- Use when the user runs /a01-orchestrator.

Don't use for: Do not implement domain artifacts (code/docs/IaC).

## Prerequisites

- Swarm root with `agents.json`, `scripts/`, `swarm/`.
- `SWARM_DIR` (default `<git toplevel>/.swarm`, made absolute).
- `python3` and/or `bun` on PATH.
- A project brief, or a signed `task.assign` with task_id and correlation_id. A brief is planned with orch_plan before any BLOCKED yield.

## How to Run

- `python3 scripts/orch_plan.py --task-id $TASK --correlation-id $CORR --json`
- `bun scripts/ts/orch_plan.ts --task-id $TASK --correlation-id $CORR --json`
- `python3 scripts/orch_status.py --task-id $TASK --correlation-id $CORR --json`
- `bun scripts/ts/orch_status.ts --task-id $TASK --correlation-id $CORR --json`
- `python3 scripts/swarm_run.py --task-id $TASK --correlation-id $CORR --json`
- `bun scripts/ts/swarm_run.ts --task-id $TASK --correlation-id $CORR --json`

## Procedure

1. If the message is a signed task.assign, echo task_id and correlation_id. If it is a project brief, run orch_plan on it first; do not reject it as unsigned.
2. Read upstream artifacts in consumes: project.brief, task.bid, task.status, task.result, gate.verdict, agent.heartbeat, conflict.report.
3. Run the scripts above. Never fabricate JSON.
4. Write only task.offer, task.assign, plan.updated, conflict.arbitration, escalation.request, swarm.status.
5. Finish with markdown summary + one fenced json `task.result`.

6. For each ready task, lease it (`orch_status.py --repo <app> --transition <id> CLAIMED`, then `IN_PROGRESS`), then spawn `subagent_type` = slug (Claude Agent tool or Grok `spawn_subagent`). Do not implement domain work.
7. Ingest child JSON with `orch_status.py --repo <app> --ingest`. Apply fail-closed gates (max 2 rework loops); gate scripts, not you, record verdicts.

## Pitfalls

- Missing optional binary → `skipped:tool-missing`, do not invent results.
- L3/L4 → BLOCKED needs=human-approval.
- Third gate failure → do not retry; A01 escalates.

## Verification

- Script JSON `status` is ok or fail with findings.
- Final json block matches the prompt `<output_format>`.
- No writes outside this agent's single-writer zone.
