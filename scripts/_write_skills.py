#!/usr/bin/env python3
"""Generate per-agent SKILL.md files and the orchestration skill."""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from swarm.skill_registry import Skill, load_skills, skills_for_owner, validate_skills  # noqa: E402
AGENTS = json.loads((ROOT / "agents.json").read_text())["agents"]

DONT = {
    "A01": "Do not implement domain artifacts (code/docs/IaC).",
    "A02": "Do not design architecture or write product code.",
    "A03": "Do not write product source or schema DDL.",
    "A04": "Do not implement backend/frontend source.",
    "A05": "Do not edit frontend, schema, or deploy.",
    "A06": "Do not edit backend or schema.",
    "A07": "Do not write application business logic.",
    "A08": "Do not patch product code.",
    "A09": "Do not edit product code.",
    "A10": "Do not edit product code or accept risk.",
    "A11": "Do not write product business logic.",
    "A12": "Do not write application code.",
    "A13": "Do not ship features; you observe.",
    "A14": "Do not add unrelated features.",
    "A15": "Do not rewrite product code to match docs.",
    "A16": "Do not edit A03 contracts or write IaC; file a change request.",
    "A17": "Do not apply infrastructure or edit CI pipelines.",
    "A18": "Do not hold signing keys, accept risk or sign a gate verdict.",
    "A19": "Do not edit system API contracts, persistence or unrelated backend code.",
    "A20": "Do not buy commitments, resize production or delete resources.",
    "A21": "Do not mutate production clusters or delete a cluster without approval.",
    "A22": "Do not merge PRs or prune production without approval.",
    "A23": "Do not flip strict mTLS or change production mesh without approval.",
    "A24": "Do not accept risk, waive findings or sign a gate verdict.",
    "A25": "Do not run production experiments outside the error budget or fail over DR.",
    "A26": "Do not deploy to mainnet, upgrade live proxies, move funds or touch key material.",
    "A27": "Do not edit contract source, accept risk or change a severity unapproved.",
    "A28": "Do not run mainnet node changes or validator key operations.",
    "A29": "Do not request, store or log keys, or ship a flow without simulation and confirmation.",
    "A30": "Do not read, generate, store, sign or broadcast with key material.",
}


def agent_skill(a: dict) -> str:
    stems = [Path(s).stem for s in a["scripts"]]
    cmds = "\n".join(
        f"- `python3 scripts/{s}.py --task-id $TASK --correlation-id $CORR --json`\n"
        f"- `bun scripts/ts/{s}.ts --task-id $TASK --correlation-id $CORR --json`"
        for s in stems
    )
    spawn = ""
    if a["id"] == "A01":
        spawn = """
6. For each ready task, lease it (`orch_status.py --repo <app> --transition <id> CLAIMED`, then `IN_PROGRESS`), then spawn `subagent_type` = slug (Claude Agent tool or Grok `spawn_subagent`). Do not implement domain work.
7. Ingest child JSON with `orch_status.py --repo <app> --ingest`. Apply fail-closed gates (max 2 rework loops); gate scripts, not you, record verdicts.
"""
    return f"""---
name: {a['slug']}
description: >
  {a['id']} {a['code']} {a['name']} — {a['description']}
  Use when the user runs /{a['slug']}, asks for {a['code']}, or the swarm assigns capability {', '.join(a['capabilities'][:3])}.
disable-model-invocation: false
---

# {a['name']} ({a['id']} {a['code']})

Subagent body: `prompts/{Path(a['prompt']).name}` (do not paste it here). Generated agents: `.claude/agents/{a['slug']}.md` and `.grok/agents/{a['slug']}.md`.

## When to Use

- The assignment capability is one of: {', '.join(a['capabilities'])}.
- The user names this agent, slug `{a['slug']}`, or code `{a['code']}`.
- Use when the user runs /{a['slug']}.

Don't use for: {DONT[a['id']]}

## Prerequisites

- Swarm root with `agents.json`, `scripts/`, `swarm/`.
- `SWARM_DIR` (default `<git toplevel>/.swarm`, made absolute).
- `python3` and/or `bun` on PATH.
- Signed `task.assign` with task_id and correlation_id.

## How to Run

{cmds}

## Procedure

1. Validate the signed task.assign. Echo task_id and correlation_id.
2. Read upstream artifacts in consumes: {', '.join(a['consumes'][:8])}.
3. Run the scripts above. Never fabricate JSON.
4. Write only {', '.join(a['produces'][:6])}.
5. Finish with markdown summary + one fenced json `task.result`.
{spawn}
## Pitfalls

- Missing optional binary → `skipped:tool-missing`, do not invent results.
- L3/L4 → BLOCKED needs=human-approval.
- Third gate failure → do not retry; A01 escalates.

## Verification

- Script JSON `status` is ok or fail with findings.
- Final json block matches the prompt `<output_format>`.
- No writes outside this agent's single-writer zone.
"""


def orch_skill() -> str:
    slugs = ", ".join(a["slug"] for a in AGENTS)
    return f"""---
name: agent-swarm-orchestrate
description: >
  Run the AgentSwarm of 30 subagents (a01-orchestrator through a30-custody-compliance).
  Use when the user says run the swarm, AgentSwarm, implement a feature with the 30 agents,
  a01-orchestrator, SDLC swarm, or /swarm. UserPromptSubmit hook injects this skill for SDLC-shaped prompts.
disable-model-invocation: false
---

# AgentSwarm orchestration

This skill is mandatory when the UserPromptSubmit hook injected it (`hooks/user_prompt_submit.py`).
The parent session must not implement domain work.

## When to Use

- Implement / fix / refactor / release / deploy / hotfix a product with the swarm.
- Explicit `/swarm` or "run the swarm" / "AgentSwarm".
- Hook additionalContext named this skill.

Don't use for: pure questions ("what is"), `/uplift`, `/think`.

## Prerequisites

- Agent-swarm checkout at `$SWARM_ROOT` (the installer fills in the absolute path of the agent-swarm checkout).
- Target application repo (`--repo` / cwd).
- python3; bun optional; claude or grok CLI for unattended `swarm_run`.

## Procedure

1. Identify the target application repo (`--repo` or cwd).
2. `python3 $SWARM_ROOT/scripts/orch_plan.py --repo <app> --brief-text "<user request>" --pattern feature|hotfix|dependency|cloud-native|cluster|web3|parallel --json`
   (or `bun scripts/ts/orch_plan.ts` with the same flags).
3. Spawn subagent `a01-orchestrator` (Claude Agent tool / Grok `spawn_subagent` `subagent_type=a01-orchestrator`) with the plan JSON and the user brief.
4. A01 plans and spawns: {slugs}.
5. Parent MUST NOT write application code, docs, or IaC.
6. Unattended alternative: `python3 scripts/swarm_run.py --repo <app> --runtime auto --json` (dry-run with `--dry-run`).

## Hook

`hooks/user_prompt_submit.py` is fail-open (always exit 0). On SDLC-shaped prompts it injects additionalContext telling the parent to load this skill and spawn a01-orchestrator.

## Verification

- A plan JSON exists under `<app>/.swarm/plans/` (the same `--repo` the run uses).
- Child slugs match agents.json.
- Parent transcript contains no product-code patches from the parent itself.
"""

def swarm_orchestrate_skill() -> str:
    """Phase 5: omp-native in-session driver (separate file, separate slug)."""
    return """---
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
"""


def yaml_str(text: str) -> str:
    """Encode text as a YAML double-quoted scalar (a JSON string literal is one)."""
    return json.dumps(text, ensure_ascii=False)


def _one_line(text: str, limit: int = 110) -> str:
    """First sentence or line of `text`, single-spaced and clipped to `limit` characters."""
    flat = " ".join(text.split())
    cut = flat.find(". ")
    if 0 < cut < limit:
        flat = flat[: cut + 1]
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _index_sections(a: dict, skills: list[Skill]) -> str:
    """`## Registered tools` and `## Skills` for an agent's index skill; a section with no entries is omitted."""
    out = ""
    tools = [t for t in a.get("registered_tools") or [] if isinstance(t, dict) and t.get("name")]
    if tools:
        lines = "\n".join(
            f"- `{t['name']}` ({t.get('side_effect_tier', 'READ')}): {_one_line(str(t.get('purpose', '')))}" for t in tools
        )
        out += (
            "\n## Registered tools\n\n"
            "Discover with `swarm_catalog`, call with `swarm_tool_call` (the caller is your own identity).\n\n"
            f"{lines}\n"
        )
    if skills:
        lines = "\n".join(f"- `skill://{s.name}`: {_one_line(s.description, 130)}" for s in skills)
        out += f"\n## Skills\n\nRead the one that fits with its link; they are hidden from the skill listing.\n\n{lines}\n"
    return out


def omp_skill(a: dict, skills: list[Skill] | None = None) -> str:
    """The visible per-agent index skill. `skills` defaults to this agent's sources under prompts/skills/<slug>."""
    if skills is None:
        skills = skills_for_owner(load_skills(ROOT), a["slug"])
    desc = yaml_str(
        f"{a['id']} {a['code']} {a['name']} — {a['description']} "
        f"Use when the swarm assigns capability {', '.join(a['capabilities'][:3])}."
    )
    return f"""---
name: {yaml_str(a['slug'])}
description: {desc}
---

# {a['name']} ({a['id']} {a['code']}) — omp

## When to Use

- The assignment capability is one of: {', '.join(a['capabilities'])}.
- The user names this agent, slug `{a['slug']}`, or code `{a['code']}`.

Don't use for: {DONT[a['id']]}

## Procedure

1. Dispatch via the omp `task` tool with `agent: {a['slug']}` (definition: `omp/agents/{a['slug']}.md`), passing the signed `task.assign` payload.
2. The agent finishes by calling `yield` with a `task.result` object; read it from the task result.
3. Don't use it for: {DONT[a['id']]}
{_index_sections(a, skills)}"""


def source_skill(s: Skill) -> str:
    """A hidden skill rendered from its source file: omp lists it only by `skill://` link, never in the prompt."""
    return (
        "---\n"
        f"name: {yaml_str(s.name)}\n"
        f"description: {yaml_str(s.description)}\n"
        "disable-model-invocation: true\n"
        "---\n\n"
        f"{s.body.strip()}\n\n"
        f"<!-- generated from {s.path}; edit the source and run scripts/build_agents.py -->\n"
    )


def source_skill_names(skills: list[Skill] | None = None) -> list[str]:
    return [s.name for s in (load_skills(ROOT) if skills is None else skills) if s.name]


def main() -> int:
    # validate before the first write: a source skill that fails (empty name, reserved name, bad path) must not
    # produce stray files or overwrite a generated skill
    sources = load_skills(ROOT)
    document = json.loads((ROOT / "agents.json").read_text())
    errors = validate_skills(sources, document, counts=False)
    if errors:
        print("error: invalid skill sources, nothing written:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    for a in AGENTS:
        d = ROOT / "skills" / a["slug"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(agent_skill(a))
        print("wrote", d / "SKILL.md")
    # legacy root orchestrate (kept)
    od = ROOT / "skills" / "orchestrate"
    od.mkdir(parents=True, exist_ok=True)
    (od / "SKILL.md").write_text(orch_skill())
    print("wrote", od / "SKILL.md")

    # Phase 5: canonical omp swarm-orchestrate
    omp_orch = ROOT / "omp" / "skills" / "swarm-orchestrate"
    omp_orch.mkdir(parents=True, exist_ok=True)
    (omp_orch / "SKILL.md").write_text(swarm_orchestrate_skill())
    print("wrote", omp_orch / "SKILL.md")

    for a in AGENTS:
        d = ROOT / "omp" / "skills" / a["slug"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(omp_skill(a, skills_for_owner(sources, a["slug"])), encoding="utf-8")
        print("wrote", d / "SKILL.md")

    for s in sources:
        d = ROOT / "omp" / "skills" / s.name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(source_skill(s), encoding="utf-8")
        print("wrote", d / "SKILL.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
