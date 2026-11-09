#!/usr/bin/env python3
"""Generate Claude Code, Grok Build, omp and Cursor subagent definitions from agents.json + prompts/.

Claude: .claude/agents/<slug>.md  (name, description, tools incl. mcp__substrate, model: inherit)
Grok:   .grok/agents/<slug>.md    (prompt_mode, permission_mode, agents_md)
omp:    omp/agents/<slug>.md      (tools, spawns, blocking, autoloadSkills, output)
        omp/skills/<slug>/SKILL.md
        omp/skills/swarm-orchestrate/SKILL.md  (with A01)
Cursor: .cursor/agents/<slug>.md  (name, description, model: inherit; body is the prompt plus a Cursor preamble)
Grok Bot: grokbot/swarm/seat-map.json and grokbot/skills/swarm-<lane>/SKILL.md
          (render_grokbot; does not write .grok/ or grokbot/skills/swarm-cloud-dispatch)

Run after editing any prompt or the manifest:  python3 scripts/build_agents.py [--check]
Install into a workspace:  python3 scripts/build_agents.py --install-workspace <ws> [--omp-mode link|copy] [--dry-run]
                               [--runtimes claude,grok,omp,cursor | --no-substrate] [--with-a01-complete-hook] [--allow-shadowed-copy]
The install also wires substrate-mcp (scripts/_install_substrate.py): the `substrate` MCP entry for each runtime and one
0600 env file per agent holding its SUBSTRATE_TOKEN, read from SUBSTRATE_TOKEN_<SURFACE> (docs/substrate-workspace.md).
Copy Cursor agents only (no substrate, no MCP, no env files):
                               python3 scripts/build_agents.py --install-cursor <repo> [--dry-run]
                               python3 scripts/_install_cursor.py --target <repo> [--dry-run|--check]
"""
from __future__ import annotations
import argparse
import errno
import json
import os
import re
import shlex
import shutil
import stat
import sys
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from swarm.manifest import load_manifest  # noqa: E402
from _write_skills import omp_skill, source_skill, swarm_orchestrate_skill, yaml_str  # noqa: E402
from swarm.skill_registry import SHARED_OWNER, load_skills, validate_skills  # noqa: E402
import _install_cursor  # noqa: E402
import _install_omp  # noqa: E402
import _install_substrate  # noqa: E402
from swarm.workspace import CLAUDE_TOOLS  # noqa: E402

CLAUDE_DIR = ROOT / ".claude" / "agents"
GROK_DIR = ROOT / ".grok" / "agents"
OMP_AGENTS_DIR = ROOT / "omp" / "agents"
OMP_SKILLS_DIR = ROOT / "omp" / "skills"
CURSOR_DIR = ROOT / ".cursor" / "agents"
GROKBOT_SWARM_DIR = ROOT / "grokbot" / "swarm"
GROKBOT_SKILLS_DIR = ROOT / "grokbot" / "skills"
SCHEMA = ROOT / "swarm" / "schemas" / "task.result.v1.json"
TOOL_MAP = {"Read": "read", "Grep": "grep", "Glob": "glob", "Bash": "bash", "Write": "write", "Edit": "edit", "Agent": "task"}
BLOCKING = {"A01", "A08", "A09", "A10", "A12"}
# D-01: the omp package's hidden swarm_* tools reach only the agents granted here (omp frontmatter only).
# Every agent gets the registry bridge (v2.2 DESIGN 5.3); the caller is the session's own identity, never a parameter.
REGISTRY_BRIDGE_TOOLS = ["swarm_catalog", "swarm_tool_call"]
_ROLE_SWARM_TOOLS = {
    "A01": ["swarm_plan", "swarm_status", "swarm_ingest", "swarm_transition", "swarm_assignment"],
    "A08": ["swarm_gate"],
    "A09": ["swarm_gate"],
    "A10": ["swarm_gate"],
    "A12": ["swarm_gate"],
}
SWARM_TOOLS = {f"A{n:02d}": _ROLE_SWARM_TOOLS.get(f"A{n:02d}", []) + REGISTRY_BRIDGE_TOOLS for n in range(1, 31)}
# Fail-closed ceiling: only A01 leases and requests canonical assignments; only gate agents record verdicts. The
# registry bridge is the one grant every agent may hold.
ORCH_SWARM_TOOLS = {"swarm_plan", "swarm_status", "swarm_ingest", "swarm_transition", "swarm_assignment"}
# Skills every omp agent autoloads when their sources exist (D6); absent sources are skipped so a tree without them is unchanged.
SHARED_AUTOLOAD = ("swarm-handover-intake", "swarm-handover-emit")
# gate agent → (swarm_gate gate name, gate script stem); mirrors GATE_AGENTS in omp/src/context.ts
GATES = {"A08": ("quality", "qa_gate"), "A09": ("review", "rev_gate"), "A10": ("security", "sec_gate"), "A12": ("release", "rel_plan")}
GATE_AGENTS = set(GATES)

# One rule block for every render. Claude, Grok, omp (all three preambles) and Cursor embed it verbatim, so the merge,
# branch, keyless-advisory and no-bypass rules cannot drift apart (tests/test_render_rules.py pins it). It must stay
# short: omp/agents/a01-orchestrator.md is the largest agent and is capped at OMP_AGENT_MAX_BYTES, 64 KiB (tests/test_omp_agents.py).
# It must not contain the literal pre-change branch text: tests/test_cursor_agents.py forbids it in Cursor output.
SHARED_RULES = """- Never merge a pull request, enable auto-merge, push to a protected branch, or delete a branch. Work ends at a draft PR and a report; a human merges. Where the body below grants merge or auto-merge rights, open a draft PR and report instead.
- Branches are `<seat-prefix>/<task_id>`. In a Programming Desk repo `<seat-prefix>` is the ownership.yaml owner of the files you change, so the branch is bot-0N-<seat>/<task_id> (gates.yml rejects any prefix not matching ^bot-0[0-6]-[a-z0-9-]+$); with more than one owner, stop BLOCKED with needs naming the seats. In any other repo `<seat-prefix>` is swarm.
- Gate authority comes from the trusted runtime's real signer and verified script-written verdict rows. Unset SWARM_ED25519_KEY/SWARM_SIGNING_KEY alone does not prove a controller is keyless: it may resolve an existing private Ed25519 reference. Agent-session flags disable private-reference discovery; keyless or advisory runs never count as APPROVED.
- Never read, print, inject, or replace signing material, enable SWARM_ALLOW_INSECURE_DEV_KEY, sign or hand-write a verdict, or move a task to APPROVED or DONE yourself. Use canonical controller/gate operations and report unsigned or unrecorded results as advisory, never as a pass.
- Never start scripts/swarm_run.py or hooks/autonomous_run.py unless the operator's assignment tells you to (specialists never do). Never use or ask for a permission-bypass mode (bypassPermissions, --dangerously-skip-permissions, --yolo).
"""

CLAUDE_PREAMBLE = """<swarm_runtime>
You are running as a Claude Code subagent inside the AgentSwarm (see README.md, 01-architecture.md, 02-message-protocol.md).
- Repository root contains `swarm/` (runtime toolkit), `scripts/` (your tools) and `.swarm/` (task store, verdicts, event log).
- The assignment you receive is a `task.assign` payload: task_id, correlation_id, capability, inputs[], acceptance[], budget, risk_class. Echo task_id and correlation_id in every script call (`--task-id`, `--correlation-id`) and in your final JSON.
- Run your scripts with `python3 scripts/<script>.py … --json` or `bun scripts/ts/<script>.ts … --json`, read the JSON, then act. Never fabricate script output.
- Only write inside your single-writer artifact zone (see <outputs>). To change anything else, describe the request in your final report for A01 to route.
- Finish with: (1) a short markdown summary, (2) exactly one fenced ```json block that is your `task.result` (or gate verdict) payload as defined in <output_format>. Set "state" to IN_REVIEW when work is complete, FAILED with an "error" {code,message} from the shared taxonomy when it is not, or BLOCKED with "needs" when an input is missing.
- Fail closed. Respect autonomy ceilings: for anything at L3/L4, stop and report `"state": "BLOCKED", "needs": "human-approval: …"`.
- Spawn sibling agents with the Agent tool, `subagent_type` = their slug (a02-requirements … a30-custody-compliance).
""" + SHARED_RULES + "</swarm_runtime>\n"

GROK_PREAMBLE = """You are running as a Grok Build subagent inside the AgentSwarm (see README.md, 01-architecture.md, 02-message-protocol.md).

- Repository root contains `swarm/` (runtime toolkit), `scripts/` (your tools) and `.swarm/` (task store, verdicts, event log).
- The assignment you receive is a `task.assign` payload: task_id, correlation_id, capability, inputs[], acceptance[], budget, risk_class. Echo task_id and correlation_id in every script call (`--task-id`, `--correlation-id`) and in your final JSON.
- Run tools via `python3 scripts/<name>.py --json` or `bun scripts/ts/<name>.ts --json`. Never fabricate script output.
- Spawn sibling agents with `spawn_subagent` and `subagent_type` set to the agent slug (a01-orchestrator … a30-custody-compliance).
- Grok tools: `read_file`, `grep`, `run_terminal_command`. Do not invent Claude-only tool names.
- Only write inside your single-writer artifact zone (see <outputs>). To change anything else, describe the request in your final report for A01 to route.
- Finish with: (1) a short markdown summary, (2) exactly one fenced json block that is your `task.result` (or gate verdict) payload as defined in <output_format>. Set "state" to IN_REVIEW when work is complete, FAILED with an "error" {code,message} from the shared taxonomy when it is not, or BLOCKED with "needs" when an input is missing.
- Fail closed. Respect autonomy ceilings: for anything at L3/L4, stop and report `"state": "BLOCKED", "needs": "human-approval: …"`.
""" + SHARED_RULES


def _body(agent: dict) -> str:
    prompt_path = ROOT / agent["prompt"]
    if not prompt_path.exists():
        raise FileNotFoundError(f"{agent['id']}: prompt missing at {agent['prompt']}")
    return prompt_path.read_text(encoding="utf-8").strip()


def _desc(agent: dict) -> str:
    return agent["description"].replace('"', "'")


def render_claude(agent: dict, defaults: dict) -> str:
    del defaults  # model is always inherit for dual-runtime files
    # the agent's tools list is also what a Claude session may call, so the substrate server's tools are named here or
    # stay invisible however the runner wires the server (a workspace without it simply has none to offer)
    tools = ", ".join([*agent["tools"], CLAUDE_TOOLS])
    desc = _desc(agent)
    fm = [
        "---",
        f"name: {agent['slug']}",
        f'description: "{agent["id"]} {agent["code"]} — {desc}"',
        f"tools: {tools}",
        "model: inherit",
        "---",
    ]
    return "\n".join(fm) + "\n\n" + CLAUDE_PREAMBLE + "\n" + apply_shared_substitutions(agent["id"], _body(agent)) + "\n"


def render_grok(agent: dict, defaults: dict) -> str:
    del defaults
    desc = _desc(agent)
    fm = [
        "---",
        f"name: {agent['slug']}",
        "description: >",
        f"  {agent['id']} {agent['code']} — {desc} Spawn as subagent_type {agent['slug']}.",
        "prompt_mode: full",
        "model: inherit",
        "permission_mode: default",
        "agents_md: true",
        "---",
    ]
    return "\n".join(fm) + "\n\n" + GROK_PREAMBLE + "\n" + apply_shared_substitutions(agent["id"], _body(agent)) + "\n"


# Cursor subagent frontmatter is name, description, model, readonly, is_background
# (https://cursor.com/docs/subagents). Generated files set name, description and model
# only: readonly would block the shell calls the scripts need, and is_background defaults off.
_CURSOR_COMMON = """You are running as a Cursor subagent inside the AgentSwarm (see README.md, 01-architecture.md, 02-message-protocol.md).
- Cursor tools: Read, Grep, Glob, Write, StrReplace, and Shell. Where the shared prompt below names a tool from another runtime, use this list. Do not call MCP tools.
- Runtime root: when SWARM_ROOT is set, it is the absolute path of a pinned agent-swarm checkout. Call swarm tools as python3 "$SWARM_ROOT/scripts/<tool>.py" --root <target repo> --json (bun twin: bun "$SWARM_ROOT/scripts/ts/<tool>.ts" --root <target repo> --json). <target repo> is the git toplevel of the repository you are editing. Every scripts/ path in the body below is relative to that checkout.
- When SWARM_ROOT is unset, repo-local scripts/ are swarm tools only inside the agent-swarm checkout itself (this working tree contains swarm/taskstore.py and scripts/orch_plan.py). Then call python3 scripts/<tool>.py --root <target repo> --json. In any other repository, stop: those scripts/ directories are unrelated. Finish with state BLOCKED and needs "SWARM_ROOT".
- The assignment you receive is a task.assign payload: task_id, correlation_id, capability, inputs[], acceptance[], budget, risk_class. Echo task_id and correlation_id in every script call (--task-id, --correlation-id) and in your final JSON.
- Read the JSON a script prints, then act. Never fabricate script output.
- Only write inside your single-writer artifact zone (see <outputs>). To change anything else, describe the request in your final report for A01 to route.
- Cloud sessions are advisory. No signing key is present (SWARM_ED25519_KEY is unset and SWARM_REQUIRE_KEY is unset). Gate scripts record nothing. Nothing this session produces counts as APPROVED. The merge gate is Greptile, run by Desk Quality.
- A missing signing key (SWARM_ED25519_KEY, SWARM_SIGNING_KEY and SWARM_REQUIRE_KEY unset) is the expected Cursor state and is not E-DEP. Accept an unsigned task.assign from the parent session or a01-orchestrator, and do not sign. A gate call is a non-recording preview under the rule below, so report every gate result as advisory. This overrides the body rules that agents reject unsigned assignments and that a missing signing key means E-DEP. A missing Task Store, python3 or git is still E-DEP.
- Run every gate script (qa_gate, rev_gate, sec_gate, rel_plan) only as a non-recording preview through python3, with SWARM_AGENT_SESSION=1 in its environment, for example SWARM_AGENT_SESSION=1 python3 "$SWARM_ROOT/scripts/qa_gate.py" --root <target repo> --task-id <id> --correlation-id <id> --json. The script then writes an advisory envelope file and records no verdict rows. Do not use bun scripts/ts/sec_gate.ts for this preview: that twin returns scan JSON and appends a script.sec_gate event, and it does not write the advisory envelope. The security preview is python3 "$SWARM_ROOT/scripts/sec_gate.py". Never set SWARM_SIGNING_KEY, SWARM_ED25519_KEY or SWARM_ALLOW_INSECURE_DEV_KEY, never sign or record a verdict, and never ingest a gate result or transition any task to APPROVED or DONE. A gate result that fails, is refused or is unrecorded is advisory, never a pass. When a gate child returns, A01 does not leave that task leased: A01 transitions it to BLOCKED with reason "advisory preview recorded no verdict rows; human records the gate", stops the scheduling loop, and does not spawn tasks that depend on it. Those dependents stay unscheduled. The handoff records no verdict rows and is not APPROVED.
- Do not start an unattended headless runner (scripts/swarm_run.py, hooks/autonomous_run.py) from a Cursor session, even when an assignment asks for one. This overrides the shared runner rule about an operator's assignment. Dispatch only as the nesting rule below says.
- Finish with: (1) a short markdown summary, (2) exactly one fenced json block that is your task.result (or gate verdict) payload as defined in <output_format>. Set "state" to IN_REVIEW when work is complete, FAILED with an "error" {code,message} from the shared taxonomy when it is not, or BLOCKED with "needs" when an input is missing.
- Fail closed. Respect autonomy ceilings: for anything at L3/L4, stop and report "state": "BLOCKED", "needs": "human-approval: …".
"""
_CURSOR_ORCH = (
    "- Nesting: Cursor allows two levels. You may spawn specialists with the Task tool, setting subagent_type "
    "to the slug (a02-requirements through a30-custody-compliance). Launch independent specialists in parallel. A specialist "
    "is the second level and must not spawn further subagents. Do not spawn a01-orchestrator.\n"
)
_CURSOR_SPEC = (
    "- Nesting: Cursor allows two levels. You are a specialist, so you must not spawn subagents and you must "
    "not call the Task tool. If you need another slug, finish BLOCKED with needs set to that slug so A01 can spawn it.\n"
)


def _cursor_preamble(agent_id: str) -> str:
    nesting = _CURSOR_ORCH if agent_id == "A01" else _CURSOR_SPEC
    return "<swarm_runtime>\n" + _CURSOR_COMMON + SHARED_RULES + nesting + "</swarm_runtime>\n"


# Rewrites of the shared prompt body. SHARED_BODY_SUBSTITUTIONS apply to every render (Claude, Grok, omp, Cursor): they
# turn the merge, auto-merge and branch grants of A05, A06, A09 and A14 into the shared rule block's wording. The
# Cursor-only rewrites (A01 and A10 gate handling) apply to Cursor alone. Each old string must occur exactly once in
# that agent's body; a missing or duplicated pattern raises so a later prompt edit cannot drop a fix.
SHARED_BODY_SUBSTITUTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "A05": (
        (
            "PR on branch `swarm/<task_id>`",
            "PR on branch `<seat-prefix>/<task_id>` per the branch rule in the preamble",
        ),
        (
            '"branch": "swarm/T-884"',
            '"branch": "<seat-prefix>/T-884"',
        ),
    ),
    "A06": (
        (
            "PR on branch `swarm/<task_id>`",
            "PR on branch `<seat-prefix>/<task_id>` per the branch rule in the preamble",
        ),
        (
            '"branch": "swarm/T-902"',
            '"branch": "<seat-prefix>/T-902"',
        ),
    ),
    "A09": (
        (
            "trivial auto-fixes go on `auto-fix/*` branches that the producer still merges",
            "trivial auto-fixes go on `<seat-prefix>/<task_id>` branches per the branch rule in the preamble; open a draft PR and report it for a human to merge",
        ),
    ),
    "A14": (
        (
            "semver-compatible + green gates ⇒ auto-mergeable (L2, max N/day per repo to bound blast radius).",
            "semver-compatible + green gates ⇒ open a draft PR and report it (L2, max N/day per repo to bound blast radius). A human merges after the repository's review gate.",
        ),
        (
            "merge semver-compatible bumps behind green gates",
            "open a draft PR for semver-compatible bumps behind green gates and report it for a human to merge",
        ),
    ),}

_CURSOR_ONLY_SUBSTITUTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "A01": (
        (
            "When a subagent returns, apply its result: `orch_status.py --repo <app> --ingest` its task.result, then re-read status. Gate tasks are those with capability `gate.*` and notes.gate set; gate agents' scripts record the signed verdicts on their gate_for targets; never record or hand-write verdicts yourself.",
            "When a subagent returns, ingest a non-gate task.result with `orch_status.py --repo <app> --ingest --advisory`, then re-read status. `--advisory` saves the result and does not attempt APPROVED or DONE, so a keyless requirements result stays IN_REVIEW and the command exits 0. When a gate child returns (capability `gate.*` with notes.gate set), do not ingest its result and do not record a verdict. Transition that gate task to BLOCKED: `orch_status.py --repo <app> --transition <id> BLOCKED --reason \"advisory preview recorded no verdict rows; human records the gate\"`. Then stop the scheduling loop and do not spawn tasks that depend on it; name those unscheduled dependents in the final swarm.status next_actions. Never transition any task to APPROVED or DONE. Gate scripts are non-recording previews under the Cursor gate rule in the preamble.",
        ),
        (
            "5. Ingest each child's JSON via `orch_status.py --repo <app> --ingest`. Gate agents' scripts record the signed verdicts on their gate_for targets; never record or hand-write verdicts yourself. Apply fail-closed gates and the max-2 rework loop.",
            "5. Ingest each non-gate child's JSON via `orch_status.py --repo <app> --ingest --advisory`, which saves the result and does not attempt APPROVED or DONE. When a gate child returns, do not ingest it. Transition it to BLOCKED: `orch_status.py --repo <app> --transition <id> BLOCKED --reason \"advisory preview recorded no verdict rows; human records the gate\"`, then stop the scheduling loop and do not spawn tasks that depend on it. Never transition any task to APPROVED or DONE. Apply the max-2 rework loop only from verdict rows recorded outside this session.",
        ),
    ),
    "A10": (
        (
            "  bun scripts/ts/sec_gate.ts --task-id T-884 [--allow-list .swarm/sec-allow.json] [--strict] [--timeout 600] [--json]",
            "  The bun twin scripts/ts/sec_gate.ts does not write an advisory envelope. Use the python3 command above for the Cursor preview.",
        ),
        (
            "    - bun scripts/ts/sec_gate.ts --task-id $TASK --correlation-id $CORR --json",
            "    - The bun security twin does not write an advisory envelope. Use the python3 command above.",
        ),
        (
            "2. Run `sec_gate.py` / `sec_gate.ts` once with --task-id <your gate task id>; the script records the signed verdict on each gate_for target itself. Fail-closed. Emit security gate.verdict.",
            "2. Run `sec_gate.py` once with --task-id <your gate task id> as the non-recording preview in the Cursor preamble. Do not run the bun security twin. Fail-closed. Emit security gate.verdict.",
        ),
        (
            "2. Run python3 and bun twins for: sec_gate.",
            "2. Run python3 scripts/sec_gate.py for the security gate. The bun security twin does not write the advisory envelope.",
        ),
    ),
}

# Everything the Cursor render rewrites: the shared rewrites first, then the Cursor-only ones.
CURSOR_BODY_SUBSTITUTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    agent: SHARED_BODY_SUBSTITUTIONS.get(agent, ()) + _CURSOR_ONLY_SUBSTITUTIONS.get(agent, ())
    for agent in sorted({*SHARED_BODY_SUBSTITUTIONS, *_CURSOR_ONLY_SUBSTITUTIONS})
}


def _apply_substitutions(table: dict[str, tuple[tuple[str, str], ...]], runtime: str, agent_id: str, body: str) -> str:
    """Rewrite grant and branch lines. Raises if an expected pattern is absent or duplicated."""
    for old, new in table.get(agent_id, ()):
        count = body.count(old)
        if count != 1:
            raise ValueError(f"{agent_id}: {runtime} substitution pattern found {count} times, expected 1: {old}")
        body = body.replace(old, new, 1)
    return body


def apply_shared_substitutions(agent_id: str, body: str) -> str:
    return _apply_substitutions(SHARED_BODY_SUBSTITUTIONS, "shared", agent_id, body)


def apply_cursor_substitutions(agent_id: str, body: str) -> str:
    return _apply_substitutions(CURSOR_BODY_SUBSTITUTIONS, "Cursor", agent_id, body)


def render_cursor(agent: dict, defaults: dict) -> str:
    del defaults
    desc = f"{agent['id']} {agent['code']} — {agent['description']}"
    fm = [
        "---",
        f"name: {agent['slug']}",
        f"description: {yaml_str(desc)}",
        "model: inherit",
        "---",
    ]
    body = apply_cursor_substitutions(agent["id"], _body(agent))
    return "\n".join(fm) + "\n\n" + _cursor_preamble(agent["id"]) + "\n" + body + "\n"


_OMP_COMMON = """You are running as an omp task agent inside the AgentSwarm (see README.md, 01-architecture.md, 02-message-protocol.md in the runtime root).
- Runtime root: the absolute path on the `Runtime root:` line of the `## AgentSwarm runtime` section of your system prompt; when that section is absent, the repository root is the runtime root. It contains `swarm/` (runtime toolkit) and `scripts/` (your tools). The repository you work on (`<repo>`, the git toplevel of your working directory) holds `.swarm/` (task store, verdicts, event log).
- The assignment you receive is a `task.assign` payload: task_id, correlation_id, capability, inputs[], acceptance[], budget, risk_class. Echo task_id and correlation_id in every script call (`--task-id`, `--correlation-id`) and in your final JSON.
- Run your scripts with `python3 <runtime root>/scripts/<script>.py … --root <repo> --json` or `bun <runtime root>/scripts/ts/<script>.ts … --root <repo> --json`, read the JSON, then act; every `scripts/` path in the body below lives under the runtime root. Never fabricate script output.
- Only write inside your single-writer artifact zone (see <outputs>). To change anything else, describe the request in your final report for A01 to route.
- Finish by calling the `yield` tool with your `task.result` (or gate verdict) payload as defined in <output_format> as `data`; under omp this replaces any fenced-json finish instruction in the body below. Set "state" to IN_REVIEW when work is complete, FAILED with an "error" {code,message} from the shared taxonomy when it is not, or BLOCKED with "needs" when an input is missing.
- Fail closed. Respect autonomy ceilings: for anything at L3/L4, stop and report `"state": "BLOCKED", "needs": "human-approval: …"`.
- Registry tools and skills: find them with `swarm_catalog` (optional `kind` tools or skills, optional `query`); call a registry tool with `swarm_tool_call` (`name`, `input`). The caller is always your own identity, never a parameter. Read a skill by opening its `skill://<name>` link. A call returns the registry result object unchanged: `state` SUCCESS or an error state.
""" + SHARED_RULES

OMP_PREAMBLE = "<swarm_runtime>\n" + _OMP_COMMON + "</swarm_runtime>\n"


def _omp_gate_preamble(agent_id: str) -> str:
    """A gate agent's omp preamble: the specialist one plus the swarm_gate-only rule (Phase 5 D-1)."""
    gate, script = GATES[agent_id]
    line = (
        f"- Gate recording (omp): record your {gate} gate only with the `swarm_gate` tool (`gate: \"{gate}\"`); it runs "
        f"`{script}` against this session's workspace and signs the verdict. Never run `<runtime root>/scripts/{script}.py` or "
        f"`<runtime root>/scripts/ts/{script}.ts` through bash (the guard blocks it), even where the body below says to run the script."
    )
    if gate in ("review", "quality", "security"):
        line += (
            " Pass every failing target in `per_target_findings` with at least one finding of severity `major` or "
            "higher; an empty list passes a target."
        )
    return "<swarm_runtime>\n" + _OMP_COMMON + line + "\n</swarm_runtime>\n"


OMP_ORCH_PREAMBLE = """<swarm_runtime>
- Step 0 (mandatory, before reading any file, running any script or writing anything): check your tool definitions and apply the first matching rule below; only then continue with the assignment.
  - (a) If your system prompt says you are in plan mode, or no `bash` (or `_bash`) tool is among your tool definitions: yield state IN_REVIEW with the wave plan in summary_md and spawn nothing.
  - (b) Otherwise, if no `task` (or `_task`) tool is among your tool definitions: tools limited to the advisor review surface (`read`, `grep`, `glob`, and `advise`) are the Omp advisor's balanced-budget review, not this cap. Do not yield and do not skip a later `task` dispatch or work pool because that surface is showing. When orchestrator tools such as `bash`, `write` or `swarm_*` remain and `task` is absent: immediately yield state "BLOCKED" with needs "depth", never IN_REVIEW, without reading, running or writing anything first. That yield is the specialist spawn only. Read-only discovery is not blocked.
- Advisor budget: an `<advisory>` note of any severity, including blocker, is review from the Omp advisor model (weigh, don't blindly obey). It is not a swarm BLOCKED. Keep the advisor model. Do not stop the session because it spoke, and do not skip creating the work pool or the `task` dispatch. If a blocker says read-only nodes were marked blocked because A01 cannot dispatch, those nodes stay open: finish them with read, grep, and glob. Do not hand off with them still blocked, and do not answer that the whole request remains blocked.
""" + _OMP_COMMON + """- Lease: use `swarm_transition` to move each ready task through CLAIMED to IN_PROGRESS before requesting its specialist assignment. Preserve the normal approval controls and single-writer artifact zones.
- Assignment: call only `swarm_assignment` with the leased `task_id` and its `correlation_id`. It reads the current Task Store lease and returns the canonical signed `task.assign` envelope, exact `assignment_prompt` text in `prompt`, and manifest `agent_slug`. Require `status: "ok"` and `assignment_verified: true`; unsigned, advisory, dry-run, refused or missing assignments confer no dispatch authority. Never construct or sign an assignment yourself, provide a signing payload, access signing material, use generic `eval`/`xd://run_code`, or write raw swarm state to obtain one.
- Dispatch: only after the verified assignment, spawn the specialist via the omp `task` tool with `agent` set to the returned `agent_slug` (a02-requirements … a30-custody-compliance) and `task` set to the returned `prompt` unchanged; do not reconstruct the envelope or prompt. Batch independent items in tasks[]. Set `schemaMode: "strict"` on every task item for a08-qa, a09-reviewer, a10-security and a12-release, and on A01 ingest dispatches.
- Final yield (omp): your final `yield` is the task.result that swarm-orchestrate step 8 defines (`task_id`, `state`, `summary_md`). The swarm.status block from <output_format> goes into `summary_md`; it is never yielded as-is.
</swarm_runtime>
"""


def omp_autoload(agent: dict) -> list[str]:
    """Skills an omp agent autoloads: its index skill (A01 also the driver skill), then the shared handover skills that exist."""
    names = ["a01-orchestrator", "swarm-orchestrate"] if agent["id"] == "A01" else [agent["slug"]]
    shared = {s.name for s in load_skills(ROOT) if s.owner_dir == SHARED_OWNER}
    return names + [n for n in SHARED_AUTOLOAD if n in shared]


def generated_skill_names(agents: list[dict]) -> set[str]:
    """Every omp skill the generator writes: the agent index skills, the driver skill and each source skill."""
    return {a["slug"] for a in agents} | {"swarm-orchestrate"} | {s.name for s in load_skills(ROOT) if s.name}


def render_omp(agent: dict, agents: list[dict]) -> str:
    output = json.dumps(json.loads(SCHEMA.read_text(encoding="utf-8")), separators=(",", ":"))
    is_orch = agent["id"] == "A01"
    if not agent["tools"]:
        raise ValueError(f"{agent['id']}: empty tools list")
    unmapped = [t for t in agent["tools"] if t not in TOOL_MAP]
    if unmapped:
        raise ValueError(f"{agent['id']}: unmapped tool(s) for omp: {', '.join(unmapped)}")
    mapped = [TOOL_MAP[t] for t in agent["tools"]]
    if not is_orch and "task" in mapped:
        raise ValueError(f"{agent['id']}: specialists must not get the omp task tool")
    grants = SWARM_TOOLS.get(agent["id"], [])
    allowed = set(REGISTRY_BRIDGE_TOOLS) | (ORCH_SWARM_TOOLS if is_orch else {"swarm_gate"} if agent["id"] in GATE_AGENTS else set())
    denied = [t for t in grants if t not in allowed]
    if denied:
        raise ValueError(f"{agent['id']}: swarm tool(s) not allowed for this agent: {', '.join(denied)}")
    tools = ", ".join(mapped + grants)
    spawns = ", ".join(a["slug"] for a in agents if a["id"] != "A01") if is_orch else '""'
    fm = [
        "---",
        f"name: {yaml_str(agent['slug'])}",
        f'description: {yaml_str(agent["id"] + " " + agent["code"] + " — " + agent["description"])}',
        f"tools: {tools}",
        f"spawns: {spawns}",
    ]
    if agent["id"] in BLOCKING:
        fm.append("blocking: true")
    autoload = omp_autoload(agent)
    generated = generated_skill_names(agents)
    unknown = [n for n in autoload if n not in generated]
    if unknown:
        raise ValueError(f"{agent['id']}: autoload skill(s) with no generated skill (omp drops unknown names silently): {', '.join(unknown)}")
    fm += [f"autoloadSkills: {','.join(autoload)}", f"output: {output}", "---"]
    if is_orch:
        preamble = OMP_ORCH_PREAMBLE
    elif agent["id"] in GATES:
        preamble = _omp_gate_preamble(agent["id"])
    else:
        preamble = OMP_PREAMBLE
    return "\n".join(fm) + "\n\n" + preamble + "\n" + apply_shared_substitutions(agent["id"], _body(agent)) + "\n"


# Grok Bot homes. The seat ids match programming-desk ownership.yaml (bot-00 lead through
# bot-06 quality). Android and iOS are not homes: the swarm has no mobile role, so those
# paths are routing rules. Desktop shells route to bot-02 the same way.
# home is one of desk-lead, a seat id, executor, routine or cloud. seat is who verifies.
_SEAT_VERIFICATION = {
    "bot-00-programming-lead": ["desk_receipt_check"],
    "bot-01-systems-backend": ["cargo", "pytest", "ruff"],
    "bot-02-web-edge": ["bun", "deno"],
    "bot-03-android": ["gradle"],
    "bot-04-ios": ["xcodebuild"],
    "bot-05-infrastructure": ["helm", "terraform"],
    "bot-06-quality-security": ["greptile", "pytest", "ruff"],
}
_ROLE_HOME: dict[str, tuple[str, str]] = {
    "A01": ("desk-lead", "bot-00-programming-lead"),
    "A02": ("executor", "bot-00-programming-lead"),
    "A03": ("bot-01-systems-backend", "bot-01-systems-backend"),
    "A04": ("bot-01-systems-backend", "bot-01-systems-backend"),
    "A05": ("bot-01-systems-backend", "bot-01-systems-backend"),
    "A06": ("bot-02-web-edge", "bot-02-web-edge"),
    "A07": ("bot-01-systems-backend", "bot-01-systems-backend"),
    "A08": ("bot-06-quality-security", "bot-06-quality-security"),
    "A09": ("bot-06-quality-security", "bot-06-quality-security"),
    "A10": ("bot-06-quality-security", "bot-06-quality-security"),
    "A11": ("bot-05-infrastructure", "bot-05-infrastructure"),
    "A12": ("cloud", "bot-05-infrastructure"),
    "A13": ("bot-05-infrastructure", "bot-05-infrastructure"),
    "A14": ("routine", "bot-01-systems-backend"),
    "A15": ("executor", "bot-06-quality-security"),
    "A16": ("executor", "bot-05-infrastructure"),
    "A17": ("executor", "bot-05-infrastructure"),
    "A18": ("bot-06-quality-security", "bot-06-quality-security"),
    "A19": ("bot-01-systems-backend", "bot-01-systems-backend"),
    "A20": ("routine", "bot-05-infrastructure"),
    "A21": ("bot-05-infrastructure", "bot-05-infrastructure"),
    "A22": ("bot-05-infrastructure", "bot-05-infrastructure"),
    "A23": ("bot-05-infrastructure", "bot-05-infrastructure"),
    "A24": ("bot-06-quality-security", "bot-06-quality-security"),
    "A25": ("bot-05-infrastructure", "bot-05-infrastructure"),
    "A26": ("bot-01-systems-backend", "bot-01-systems-backend"),
    "A27": ("bot-06-quality-security", "bot-06-quality-security"),
    "A28": ("bot-05-infrastructure", "bot-05-infrastructure"),
    "A29": ("bot-02-web-edge", "bot-02-web-edge"),
    "A30": ("executor", "bot-06-quality-security"),
}
# Desk path families from ownership.yaml, partitioned so a role does not also claim
# android, ios or desktop. Those three are routing rules below. Script paths from
# agents.json are added for every role, so a role with no desk family still has globs.
_DESK_GLOBS: dict[str, tuple[str, ...]] = {
    "A01": (
        ".claude/agents/**",
        ".claude/commands/**",
        ".claude/hooks/**",
        ".planning/**",
        "docs/desk-operating-model.md",
        "docs/github-sot-orchestration.md",
        "docs/gotxcot-cloud-pipeline.md",
        "docs/intake-e2e-runbook.md",
        "docs/vps-agent-bus.md",
        "grokbot/**",
        "prompts/bot-00-programming-lead.xml",
        "skills/agent-bus/**",
        "skills/desk-bootstrap/**",
        "skills/gotxcot-uplift/**",
        "skills/trackplan-dispatch/**",
        "vendor/ultrathink-policy/**",
    ),
    "A03": ("ARCHITECTURE.md",),
    "A04": ("design/**", "docs/design/**"),
    "A05": (
        "services/**", "crates/**", "**/*.py", "**/*.rs",
        "pyproject.toml", "Cargo.toml", "Cargo.lock", "uv.lock",
    ),
    "A06": (
        "web/**", "apps/web/**", "edge/**", "**/*.ts", "**/*.tsx",
        "package.json", "deno.json", "deno.lock", "vercel.json",
    ),
    "A07": ("migrations/**",),
    "A08": ("ci/gates/**", "ci/hooks/**", "ci/tests/**"),
    "A09": (".gitignore", ".pre-commit-config.yaml", "ownership.yaml"),
    "A10": (
        "SECURITY.md", "ci/security/**", ".github/workflows/security-*.yml",
        "contracts/**", "**/*.proto",
    ),
    "A11": (
        "infra/**", "**/*.tf", "**/*.tfvars", "k8s/**", "Dockerfile*",
        "docker-compose*.yml", ".github/workflows/**", ".mcp.json",
    ),
    "A12": ("deploy/**",),
    "A15": ("README.md", "docs/**"),
    "A16": ("cloud/**", "landing-zone/**"),
    "A17": ("catalog-info.yaml", "platform/**"),
    "A18": ("provenance/**", "sbom/**", "slsa/**"),
    "A19": ("asyncapi/**", "events/**", "functions/**", "serverless.yml"),
    "A20": ("cost/**", "finops/**"),
    "A21": ("cluster-api/**", "clusters/**"),
    "A22": ("**/kustomization.yaml", "argocd/**", "charts/**", "flux/**", "gitops/**", "overlays/**"),
    "A23": ("gateway/**", "istio/**", "mesh/**", "network-policies/**"),
    "A24": ("gatekeeper/**", "kyverno/**", "policies/**"),
    "A25": ("chaos/**", "resilience/**"),
    "A26": ("**/*.sol", "foundry.toml", "hardhat.config.js", "hardhat.config.ts", "onchain/**"),
    "A27": ("audits/**",),
    "A28": ("chain/**", "indexers/**", "nodes/**"),
    "A29": ("apps/dapp/**", "dapp/**", "wallet/**"),
    "A30": ("compliance/**", "custody/**"),
}
_ROUTING: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("android", "bot-03-android", ("android/**", "**/*.kt", "**/*.kts", "**/build.gradle")),
    ("ios", "bot-04-ios", ("ios/**", "**/*.swift", "**/*.xcodeproj/**", "**/Package.swift")),
    ("desktop", "bot-02-web-edge", ("desktop/**", "apps/desktop/**", "electron/**", "tauri/**")),
)
# Headless runners stay out of the lane-skill procedure (_lane_skill). They stay
# in the seat map, including the TypeScript twin, so a broad **/*.py or **/*.ts
# glob cannot take them from the orchestrator.
_RUNNER_SCRIPTS = {"scripts/swarm_run.py"}
SEAT_MAP_SCHEMA = "grokbot.seat-map.v1"
SEAT_MAP_PRECEDENCE = "routing-before-roles; longest role glob wins; equal length: lowest role id"


def _role_line(agent: dict) -> str:
    text = _body(agent)
    start = text.find("<system_role>")
    end = text.find("</system_role>")
    raw = agent["description"] if start == -1 or end == -1 or end < start else text[start + len("<system_role>"):end]
    return " ".join(raw.split())


def _script_globs(agent: dict) -> list[str]:
    """Literal script paths this role owns, plus the TypeScript twin of each Python script."""
    globs: list[str] = []
    for script in agent["scripts"]:
        globs.append(script)
        name = Path(script).name
        if name.endswith(".py"):
            globs.append(f"scripts/ts/{name[:-3]}.ts")
    return globs


def _glob_match(path: str, pattern: str) -> bool:
    """Path glob: ``*`` stays inside one segment, ``**`` crosses segments (including zero)."""
    path = path.replace("\\", "/").strip("/")
    pattern = pattern.strip("/")
    if not path or not pattern or ".." in path.split("/"):
        return False
    return _glob_parts(path.split("/"), pattern.split("/"))


def _glob_parts(parts: list[str], globs: list[str]) -> bool:
    if not globs:
        return not parts
    head, rest = globs[0], globs[1:]
    if head == "**":
        if _glob_parts(parts, rest):
            return True
        return bool(parts) and _glob_parts(parts[1:], globs)
    if not parts or not _segment_match(parts[0], head):
        return False
    return _glob_parts(parts[1:], rest)


def _segment_match(text: str, pattern: str) -> bool:
    if "/" in pattern or pattern == "**":
        return False
    rx = re.escape(pattern).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(rx, text) is not None


def resolve_owner(doc: dict, path: str) -> dict:
    """Seat for ``path`` under the seat map's precedence.

    Routing rules are considered before roles. Within a group the longest matching
    glob wins. Two role globs of equal length go to the lowest role id, which is
    how the shared ``code_checks`` paths (claimed by both A05 and A06) resolve.
    """
    empty = {"seat": None, "role_id": None, "kind": None, "glob": None, "via": None}
    best_route: tuple[int, str, str, str] | None = None
    for route in doc.get("routing") or []:
        for pattern in route.get("path_globs") or []:
            if not _glob_match(path, pattern):
                continue
            cand = (len(pattern), route["kind"], route["seat"], pattern)
            if best_route is None or cand[0] > best_route[0] or (cand[0] == best_route[0] and cand[1] < best_route[1]):
                best_route = cand
    if best_route is not None:
        return {"seat": best_route[2], "role_id": None, "kind": best_route[1], "glob": best_route[3], "via": "routing"}
    best: tuple[int, str, str, str] | None = None
    for role in doc.get("roles") or []:
        for pattern in role.get("path_globs") or []:
            if not _glob_match(path, pattern):
                continue
            cand = (len(pattern), role["id"], role["seat"], pattern)
            if best is None or cand[0] > best[0] or (cand[0] == best[0] and cand[1] < best[1]):
                best = cand
    if best is None:
        return empty
    return {"seat": best[2], "role_id": best[1], "kind": None, "glob": best[3], "via": "role"}


def _seat_map(agents: list[dict]) -> dict:
    roles = []
    for agent in sorted(agents, key=lambda a: a["slug"]):
        home, seat = _ROLE_HOME[agent["id"]]
        roles.append({
            "autonomy_ceiling": agent["autonomy_ceiling"],
            "code": agent["code"],
            "home": home,
            "id": agent["id"],
            "lane": agent["lane"],
            "path_globs": sorted(set(_DESK_GLOBS.get(agent["id"], ())) | set(_script_globs(agent))),
            "seat": seat,
            "slug": agent["slug"],
            "verification_tools": list(_SEAT_VERIFICATION[seat]),
        })
    routing = [
        {
            "kind": kind,
            "path_globs": sorted(globs),
            "seat": seat,
            "verification_tools": list(_SEAT_VERIFICATION[seat]),
        }
        for kind, seat, globs in sorted(_ROUTING, key=lambda row: row[0])
    ]
    return {
        "precedence": SEAT_MAP_PRECEDENCE,
        "roles": roles,
        "routing": routing,
        "schema": SEAT_MAP_SCHEMA,
    }


def _lane_skill(lane: str, members: list[dict]) -> str:
    members = sorted(members, key=lambda a: a["id"])
    slugs = ", ".join(a["slug"] for a in members)
    lines = [
        "---",
        f"name: swarm-{lane}",
        "description: >",
        f"  Grok Bot lane {lane} for {slugs}.",
        "  Use when Desk Lead routes work to this lane of the 30 AgentSwarm roles.",
        "disable-model-invocation: false",
        "---",
        "",
        f"# Swarm lane {lane}",
        "",
        "Generated from agents.json and the role prompts. Do not edit by hand.",
        "Regenerate with `python3 scripts/build_agents.py`.",
        "",
        "## When to Use",
        "",
        f"- The assignment belongs to lane `{lane}`.",
        f"- The role slug is one of: {slugs}.",
        "",
        "## Procedure",
        "",
        "1. Read the role prompt at `$SWARM_ROOT/<prompt>` below. `$SWARM_ROOT` is the pinned agent-swarm checkout. The target repo does not contain these prompts. The swarm has exactly these 30 roles.",
        "2. Route the change with `grokbot/swarm/seat-map.json`. A routing rule wins over role path globs. Among role globs, the longest match wins, and an equal length goes to the lowest role id. Android paths go to bot-03, iOS paths to bot-04, and desktop shells to bot-02.",
        "3. Run that role's scripts from the pinned checkout: `python3 \"$SWARM_ROOT/scripts/<script>.py\" --root <target repo> --json` (the bun twin is `bun \"$SWARM_ROOT/scripts/ts/<script>.ts\" --root <target repo> --json`). `<target repo>` is the git toplevel being edited. Do not run a `scripts/` path from the target tree. Stay inside the role's path globs and autonomy ceiling.",
        "4. Verify with the seat's verification tools from the seat map.",
        "5. Finish with a task.result. A keyless cloud session is advisory. Leave the pull request in draft.",
        "",
        "## Roles",
        "",
    ]
    for agent in members:
        home, seat = _ROLE_HOME[agent["id"]]
        ceiling = ", ".join(f"{key}={value}" for key, value in sorted(agent["autonomy_ceiling"].items()))
        scripts = [s for s in agent["scripts"] if s not in _RUNNER_SCRIPTS]
        script_txt = ", ".join(f"`$SWARM_ROOT/{s}`" for s in scripts) or "(none)"
        lines += [
            f"### {agent['id']} {agent['code']} ({agent['slug']})",
            "",
            f"- Prompt: `$SWARM_ROOT/{agent['prompt']}`",
            f"- Role: {_role_line(agent)}",
            f"- Home: `{home}`. Seat: `{seat}`.",
            f"- Autonomy ceiling: {ceiling}.",
            f"- Scripts: {script_txt}.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def render_grokbot(agents: list[dict]) -> dict[str, str]:
    """Relative path → file text for the seat map and one skill per lane.

    Does not emit swarm-cloud-dispatch or anything under .grok/. Raises when the
    manifest is not the 30 roles, so a new agent cannot appear here by drift.
    """
    ids = [a["id"] for a in agents]
    if len(agents) != 30 or set(ids) != {f"A{n:02d}" for n in range(1, 31)}:
        raise ValueError(f"grokbot seat map covers exactly 30 roles (A01–A30), got {sorted(ids)}")
    missing = [i for i in ids if i not in _ROLE_HOME]
    if missing:
        raise ValueError(f"grokbot seat map has no home for {', '.join(missing)}")
    files = {
        "grokbot/swarm/seat-map.json": json.dumps(_seat_map(agents), indent=2, sort_keys=True) + "\n",
    }
    by_lane: dict[str, list[dict]] = {}
    for agent in agents:
        by_lane.setdefault(agent["lane"], []).append(agent)
    for lane in sorted(by_lane):
        files[f"grokbot/skills/swarm-{lane}/SKILL.md"] = _lane_skill(lane, by_lane[lane])
    return files


def _agent_selected(agent: dict, only: set[str] | None) -> bool:
    if not only:
        return True
    return agent["id"].lower() in only or agent["slug"] in only


def _grok_rel_selected(rel: str, agents: list[dict], only: set[str] | None) -> bool:
    """The seat map is one roster file. Lane skills follow ``--only``."""
    if not only or not rel.startswith("grokbot/skills/"):
        return True
    lane = Path(rel).parent.name.removeprefix("swarm-")
    return any(agent["lane"] == lane and _agent_selected(agent, only) for agent in agents)


def install_targets() -> list[Path]:
    return [CLAUDE_DIR, GROK_DIR]


def _generated_symlink() -> Path | None:
    """A generated directory, or a parent of one up to ROOT, that is a symlink."""
    for base in (CLAUDE_DIR, GROK_DIR, CURSOR_DIR, OMP_AGENTS_DIR, OMP_SKILLS_DIR, GROKBOT_SWARM_DIR, GROKBOT_SKILLS_DIR):
        cur = base
        while cur != ROOT and cur != cur.parent:
            if cur.is_symlink():
                return cur
            cur = cur.parent
    return None


def _classify_target_stat(st: os.stat_result, target: Path) -> str | None:
    """Why `target` must not be written through (T-07-05): a symlink, a directory, a non-regular file,
    or a hardlink (shared inode, which `is_symlink()` misses). None when `st` is a singly-linked file."""
    if stat.S_ISLNK(st.st_mode):
        return f"{target} is a symlink"
    if stat.S_ISDIR(st.st_mode):
        return f"{target} is a directory"
    if not stat.S_ISREG(st.st_mode):
        return f"{target} is not a regular file"
    if st.st_nlink > 1:
        return f"{target} is a hardlink (nlink={st.st_nlink})"
    return None


def _target_refusal(target: Path) -> str | None:
    """Why `target` must not be written right now (T-07-05): `_classify_target_stat` at it, or a
    symlink/non-directory at its parent. Reads only; `lstat` never follows a swapped-in symlink."""
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return f"{target} is unreadable ({exc.strerror})"
    else:
        refusal = _classify_target_stat(st, target)
        if refusal is not None:
            return refusal
    try:
        pst = os.lstat(target.parent)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"{target.parent} is unreadable ({exc.strerror})"
    if stat.S_ISLNK(pst.st_mode):
        return f"{target.parent} is a symlink"
    if not stat.S_ISDIR(pst.st_mode):
        return f"{target.parent} is not a directory"
    return None


def _read_target(target: Path) -> str | None:
    """Current text of `target`, or None when absent or unreadable (T-07-05). The open uses `O_NOFOLLOW`
    so a swapped-in symlink is never followed; only the exact opened inode counts."""
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _safe_write_text(target: Path, content: str) -> str | None:
    """Write `content` to `target` without following a symlink and without truncating a swapped file
    (T-07-05). A swapped-in symlink fails the `O_NOFOLLOW` open with `ELOOP`; when the destination did
    not exist the open uses `O_EXCL`, otherwise the opened file's `fstat` (dev, ino) must equal the
    pre-open `lstat` and a hardlink refuses — all before any truncation. Returns a refusal reason,
    or None on success; writes nothing on refusal."""
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        before = None
    except OSError as exc:
        return f"{target} is unreadable ({exc.strerror})"
    if before is not None:
        refusal = _classify_target_stat(before, target)
        if refusal is not None:
            return refusal
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW | (0 if before is not None else os.O_EXCL), 0o666)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return f"{target} is a symlink"
        if exc.errno == errno.EEXIST:
            return f"{target} changed after the check"
        return f"{target} cannot be opened ({exc.strerror})"
    refusal = None
    try:
        after = os.fstat(fd)
        refusal = _classify_target_stat(after, target)
        if refusal is None and before is not None and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            refusal = f"{target} changed after the check"
        if refusal is None:
            os.ftruncate(fd, 0)
            data = content.encode("utf-8")
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
    except OSError as exc:
        refusal = f"{target} cannot be written ({exc.strerror})"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return refusal


def _write_or_check(target: Path, content: str, check: bool, changed: list, written: list, blocked: list) -> None:
    if _target_refusal(target) is not None:
        changed.append(str(target.relative_to(ROOT)))
        blocked.append(str(target.relative_to(ROOT)))
        return
    if _read_target(target) == content:
        return
    changed.append(str(target.relative_to(ROOT)))
    if not check:
        target.parent.mkdir(parents=True, exist_ok=True)
        if _target_refusal(target) is not None:
            blocked.append(str(target.relative_to(ROOT)))
            return
        refusal = _safe_write_text(target, content)
        if refusal is not None:
            blocked.append(str(target.relative_to(ROOT)))
            print(f"error: refusing to write {target.relative_to(ROOT)} ({refusal})", file=sys.stderr)
            return
        written.append(str(target.relative_to(ROOT)))


def _contained(p: Path, base: Path) -> bool:
    """Lexical containment: every component from ``base`` to ``p`` is a non-symlink.

    A direct child of a non-symlink base qualifies, and so does ``SKILL.md`` under a
    direct skill directory. ``Path.resolve()`` is not used, so a symlink is not followed.
    """
    if p.is_symlink() or base.is_symlink():
        return False
    try:
        rel = p.relative_to(base)
    except ValueError:
        return False
    if not rel.parts or ".." in rel.parts:
        return False
    cur = base
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            return False
    return True


def _unlink_orphan(orphan: Path) -> None:
    """Unlink a direct child of an export directory, or SKILL.md in a direct omp skill directory."""
    if orphan.is_symlink():
        return
    parent = orphan.parent
    if parent.is_symlink():
        return
    if orphan.name == "SKILL.md" and parent.parent in (OMP_SKILLS_DIR, GROKBOT_SKILLS_DIR):
        orphan.unlink()
        if not any(parent.iterdir()):
            parent.rmdir()
        return
    if parent in (CLAUDE_DIR, GROK_DIR, CURSOR_DIR, OMP_AGENTS_DIR, OMP_SKILLS_DIR) and orphan.is_file():
        orphan.unlink()


def _omp_orphans(slugs: set[str]) -> tuple[list[Path], list[Path]]:
    """(removable, unsafe) generated omp entries the manifest no longer produces.

    Unsafe entries are symlinks or resolve outside omp/agents or omp/skills; they are
    reported but never followed or deleted.
    """
    removable: list[Path] = []
    unsafe: list[Path] = []
    for p in OMP_AGENTS_DIR.glob("*.md"):
        if p.stem not in slugs:
            (removable if _contained(p, OMP_AGENTS_DIR) else unsafe).append(p)
    known_skills = slugs | {"swarm-orchestrate"} | {s.name for s in load_skills(ROOT) if s.name}
    for d in (OMP_SKILLS_DIR.iterdir() if OMP_SKILLS_DIR.is_dir() else []):
        if d.name in known_skills:
            continue
        if not _contained(d, OMP_SKILLS_DIR):
            unsafe.append(d)
            continue
        skill = d / "SKILL.md"
        if skill.is_symlink() or skill.exists():
            (removable if _contained(skill, OMP_SKILLS_DIR) else unsafe).append(skill)
    return sorted(removable), sorted(unsafe)


def _cursor_orphans(slugs: set[str]) -> tuple[list[Path], list[Path]]:
    """(removable, unsafe) .cursor/agents entries the manifest no longer produces."""
    removable: list[Path] = []
    unsafe: list[Path] = []
    if not CURSOR_DIR.is_dir():
        return removable, unsafe
    for p in CURSOR_DIR.glob("*.md"):
        if p.stem not in slugs:
            (removable if _contained(p, CURSOR_DIR) else unsafe).append(p)
    return sorted(removable), sorted(unsafe)


def _grokbot_orphans(lane_dirs: set[str]) -> tuple[list[Path], list[Path]]:
    """(removable, unsafe) grokbot/skills/swarm-* entries this export no longer produces.

    swarm-cloud-dispatch is hand-written and is never an orphan. Other skill directories
    under grokbot/skills are left alone.
    """
    removable: list[Path] = []
    unsafe: list[Path] = []
    if not GROKBOT_SKILLS_DIR.is_dir():
        return removable, unsafe
    known = set(lane_dirs) | {"swarm-cloud-dispatch"}
    for directory in GROKBOT_SKILLS_DIR.iterdir():
        if not directory.name.startswith("swarm-") or directory.name in known:
            continue
        if not _contained(directory, GROKBOT_SKILLS_DIR):
            unsafe.append(directory)
            continue
        skill = directory / "SKILL.md"
        if skill.is_symlink() or skill.exists():
            (removable if _contained(skill, GROKBOT_SKILLS_DIR) else unsafe).append(skill)
    return sorted(removable), sorted(unsafe)


def _copy_file(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def hook_command(script: Path) -> str:
    """A shell command that runs `script`. Quoted, so a checkout path with spaces stays one argument (T-07-19)."""
    return f"python3 {shlex.quote(str(script))}"


_HOOK_JSON_CAP = 1_048_576


class HookConfigError(Exception):
    """A workspace hook file is present but cannot be merged without destroying it (T-07-18)."""


def _load_hook_config(path: Path) -> dict:
    """The JSON object at `path`, or {} when the file is absent.

    A present file that is not a JSON object, not a regular file, or larger than the cap is an error. Replacing it
    with {} would drop every other setting. The open is non-blocking, so a FIFO cannot stall the install. `os.read`
    may return short; the read loops until EOF or the buffer exceeds the cap (still too big)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise HookConfigError(f"{path} could not be read ({exc.strerror})") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise HookConfigError(f"{path} is not a regular file")
        if info.st_size > _HOOK_JSON_CAP:
            raise HookConfigError(f"{path} is larger than {_HOOK_JSON_CAP} bytes")
        parts: list[bytes] = []
        total = 0
        while total <= _HOOK_JSON_CAP:
            chunk = os.read(fd, _HOOK_JSON_CAP + 1 - total)
            if chunk == b"":
                break
            parts.append(chunk)
            total += len(chunk)
        raw = b"".join(parts)
    except OSError as exc:
        raise HookConfigError(f"{path} could not be read ({exc.strerror})") from exc
    finally:
        os.close(fd)
    if len(raw) > _HOOK_JSON_CAP:
        raise HookConfigError(f"{path} is larger than {_HOOK_JSON_CAP} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HookConfigError(f"{path} is not valid UTF-8") from exc
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HookConfigError(f"{path} is not valid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise HookConfigError(f"{path} is not a JSON object")
    return data


def _is_a01_complete(hook: object) -> bool:
    """True when the command's argv runs `hooks/on_a01_complete.py`.

    Quoting is parsed, so a checkout path that contains a space still matches. The filename has to be exact — a
    look-alike such as `check_on_a01_complete.py` does not — and its parent directory has to be named `hooks`.
    A command shlex cannot parse is not a match: a reinstall must not delete a hook it cannot read."""
    if not isinstance(hook, dict):
        return False
    command = hook.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    for token in tokens:
        path = PurePosixPath(token)
        if path.name == "on_a01_complete.py" and path.parent.name == "hooks":
            return True
    return False


def _set_a01_complete_hook(hooks: dict, complete_cmd: str, enabled: bool) -> None:
    """Make the `Stop` entry of a hooks mapping match the request: with `enabled` exactly one a01-complete hook (this
    checkout's path) is registered, otherwise none. Other Stop hooks are never touched."""
    complete_hook = {"hooks": [{"type": "command", "command": complete_cmd}]}
    stop = hooks.get("Stop")
    if not isinstance(stop, list):
        if enabled:
            hooks["Stop"] = [complete_hook]
        return
    kept = []
    for entry in stop:
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        if not isinstance(inner, list) or not any(_is_a01_complete(h) for h in inner):
            kept.append(entry)
            continue
        rest = [h for h in inner if not _is_a01_complete(h)]
        if rest:
            kept.append({**entry, "hooks": rest})
    if enabled:
        kept.append(complete_hook)
    if kept:
        hooks["Stop"] = kept
    else:
        hooks.pop("Stop", None)


def merge_claude_settings(settings_path: Path, hook_cmd: str, with_a01_complete_hook: bool = False) -> None:
    data = _load_hook_config(settings_path)
    data.setdefault("enabledPlugins", data.get("enabledPlugins", {}))
    hooks = data.setdefault("hooks", {})
    hooks["UserPromptSubmit"] = [
        {"hooks": [{"type": "command", "command": hook_cmd}]}
    ]
    # n3/n8: the Stop hook that fires the after-orchestrate trigger (on_a01_complete.py, which can detach an unattended
    # runner) is registered only on request; a reinstall without the flag removes an earlier registration of it.
    _set_a01_complete_hook(hooks, hook_cmd.replace("user_prompt_submit.py", "on_a01_complete.py"), with_a01_complete_hook)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2) + "\n")


def write_grok_hooks(path: Path, hook_cmd: str, with_a01_complete_hook: bool = False) -> None:
    payload = _load_hook_config(path)
    hooks = payload.setdefault("hooks", {})
    hooks["UserPromptSubmit"] = [{"hooks": [{"type": "command", "command": hook_cmd}]}]
    _set_a01_complete_hook(hooks, hook_cmd.replace("user_prompt_submit.py", "on_a01_complete.py"), with_a01_complete_hook)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _workspace_hooks(workspace: Path) -> tuple[Path, Path]:
    """(Claude settings, Grok hook file) the workspace install writes."""
    return workspace / ".claude" / "settings.json", workspace / ".grok" / "hooks" / "agent-swarm.json"


def _workspace_copies(workspace: Path) -> list[tuple[Path, Path]]:
    """(source, destination) pairs of the Claude/Grok workspace copy."""
    pairs = []
    for agent in load_manifest():
        slug = agent["slug"]
        pairs.append((CLAUDE_DIR / f"{slug}.md", workspace / ".claude" / "agents" / f"{slug}.md"))
        pairs.append((GROK_DIR / f"{slug}.md", workspace / ".grok" / "agents" / f"{slug}.md"))
        skill_src = ROOT / "skills" / slug / "SKILL.md"
        if skill_src.exists():
            pairs.append((skill_src, workspace / ".claude" / "skills" / "agent-swarm" / slug / "SKILL.md"))
    orch = ROOT / "skills" / "orchestrate" / "SKILL.md"
    if orch.exists():
        pairs.append((orch, workspace / ".claude" / "skills" / "agent-swarm" / "orchestrate" / "SKILL.md"))
    return pairs


def install_workspace(workspace: Path, dry_run: bool = False, with_a01_complete_hook: bool = False) -> None:
    """Copy the Claude/Grok agents, skills and hooks into `workspace`; never writes into this repo (D-08).

    Skill copies get the literal `$SWARM_ROOT` replaced by this checkout's realpath, and the workspace hooks call
    the hook script by absolute path; the tracked repo files stay path-free (OPEN-4)."""
    root = ROOT.resolve()
    hook_cmd = hook_command(root / "hooks" / "user_prompt_submit.py")
    complete_cmd = hook_command(root / "hooks" / "on_a01_complete.py")
    settings, grok_hook = _workspace_hooks(workspace)
    pairs = _workspace_copies(workspace)
    if dry_run:
        for src, dest in pairs:
            print(f"dry-run: would copy {src.relative_to(ROOT)} -> {dest}")
        print(f"dry-run: would set the UserPromptSubmit hook in {settings} (replacing existing ones): {hook_cmd}")
        if with_a01_complete_hook:
            print(f"dry-run: would set the Stop (a01-complete) hook in {settings}: {complete_cmd}")
            print(f"dry-run: would write {grok_hook}: {hook_cmd} + Stop for on_a01_complete")
        else:
            print(f"dry-run: would write {grok_hook}: {hook_cmd}")
            print("dry-run: the Stop (a01-complete) hook is not installed (--with-a01-complete-hook opts in); "
                  "an earlier registration of it would be removed")
        return
    skills_src = ROOT / "skills"
    for src, dest in pairs:
        if src.is_relative_to(skills_src):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text(encoding="utf-8").replace("$SWARM_ROOT", str(root)), encoding="utf-8")
        else:
            _copy_file(src, dest)
    merge_claude_settings(settings, hook_cmd, with_a01_complete_hook)
    write_grok_hooks(grok_hook, hook_cmd, with_a01_complete_hook)

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="fail if generated output differs from disk")
    ap.add_argument("--only", help="comma list of agent ids/slugs")
    ap.add_argument("--install-workspace", help="install into this existing workspace root: Claude/Grok agents, skills and hooks, the omp package, and the substrate-mcp wiring")
    ap.add_argument("--omp-mode", choices=("link", "copy"), help="omp step of --install-workspace: link the package (default) or copy agents and skills only (no tools, no guard)")
    ap.add_argument("--allow-shadowed-copy", action="store_true", help="with --install-workspace --omp-mode copy: proceed when an ancestor workspace shadows the package (the copies still carry no tools and no guard)")
    ap.add_argument("--install-cursor", help="copy .cursor/agents and .cursor/rules/agent-swarm.mdc into this existing repo; no substrate, no MCP, no env files")
    ap.add_argument("--dry-run", action="store_true", help="with --install-workspace or --install-cursor: print the plan, write nothing")
    ap.add_argument("--runtimes", help="with --install-workspace: the runtimes that will execute swarm nodes here, each wired to "
                                       f"substrate-mcp (default {','.join(_install_substrate.RUNTIMES)}); a runtime that cannot call MCP is refused")
    ap.add_argument("--no-substrate", action="store_true", help="with --install-workspace: no substrate-mcp entries and no agent env files "
                                                                "(the swarm then runs with the substrate integration off)")
    ap.add_argument("--with-a01-complete-hook", action="store_true",
                    help="with --install-workspace: also register hooks/on_a01_complete.py as a Claude/Grok Stop hook (off by default: it "
                         "can detach the unattended runner, which itself needs a real signing key and SWARM_ALLOW_AUTONOMOUS=1); "
                         "a reinstall without this flag removes an earlier registration")
    args = ap.parse_args()
    if (args.omp_mode or args.allow_shadowed_copy or args.runtimes or args.no_substrate or args.with_a01_complete_hook) and not args.install_workspace:
        ap.error("--omp-mode, --allow-shadowed-copy, --runtimes, --no-substrate and --with-a01-complete-hook need --install-workspace")
    if args.dry_run and not args.install_workspace and not args.install_cursor:
        ap.error("--dry-run needs --install-workspace or --install-cursor")
    if args.install_cursor and args.install_workspace:
        ap.error("--install-cursor cannot be combined with --install-workspace")
    if args.install_cursor and (args.omp_mode or args.runtimes or args.no_substrate or args.with_a01_complete_hook or args.check or args.only):
        ap.error("--install-cursor only combines with --dry-run")
    if args.runtimes and args.no_substrate:
        ap.error("--runtimes wires substrate-mcp; it cannot be combined with --no-substrate")
    if args.allow_shadowed_copy and args.omp_mode != "copy":
        ap.error("--allow-shadowed-copy needs --omp-mode copy")
    if args.install_cursor:
        # Copies the already generated files. It does not regenerate and it does not call _install_substrate.
        return _install_cursor.install_cursor(args.install_cursor, dry_run=args.dry_run)
    runtimes = ([r.strip() for r in args.runtimes.split(",") if r.strip()] if args.runtimes
                else list(_install_substrate.RUNTIMES))
    workspace = Path(args.install_workspace).expanduser().resolve() if args.install_workspace else None
    mode = args.omp_mode or "link"
    if workspace:
        if args.check:
            ap.error("--check cannot be combined with --install-workspace")
        if not workspace.is_dir():
            print(f"error: workspace {workspace} is not an existing directory", file=sys.stderr)
            return 2
        problem = _install_omp.preflight(workspace, mode, allow_shadowed_copy=args.allow_shadowed_copy)
        if not problem:
            # CR-01: no Claude/Grok write may follow a symlink; refuse before generation or any copy.
            dests = [d for _, d in _workspace_copies(workspace)] + list(_workspace_hooks(workspace))
            unsafe = _install_omp.unsafe_destinations(workspace, dests)
            if unsafe:
                problem = (
                    f"error: refusing to install into {workspace}: {'; '.join(unsafe)}; "
                    "the installer never writes through a symlink. Nothing was written."
                )
        if not problem:
            # T-07-18: a present hook file that is not a JSON object must not be replaced with {}.
            try:
                for path in _workspace_hooks(workspace):
                    _load_hook_config(path)
            except HookConfigError as exc:
                problem = f"error: refusing to install into {workspace}: {exc}; nothing was written"
        if not problem and not args.no_substrate:
            # INST-03/04: a refused runtime, an unreadable config or a missing token stops the install here, before
            # generation or any write; a dry run prints its plan and the missing tokens instead
            problem = _install_substrate.preflight(workspace, runtimes, tokens=not args.dry_run)
        if problem:
            print(problem, file=sys.stderr)
            return 2
        if args.dry_run:
            print("dry-run: skipping generation; the files below are copied as they are on disk")
            install_workspace(workspace, dry_run=True, with_a01_complete_hook=args.with_a01_complete_hook)
            substrate = 0 if args.no_substrate else _install_substrate.install_substrate(workspace, runtimes, True, sys.stdout)
            return max(substrate, _install_omp.install_omp(workspace, mode, True, sys.stdout, allow_shadowed_copy=args.allow_shadowed_copy))
    link = _generated_symlink()
    if link is not None:
        try:
            shown = link.relative_to(ROOT)
        except ValueError:
            shown = link
        print(f"error: refusing to generate or prune through symlink {shown}", file=sys.stderr)
        return 1
    manifest_raw = json.loads((ROOT / "agents.json").read_text())
    defaults = manifest_raw.get("defaults", {})
    only = {s.strip().lower() for s in args.only.split(",")} if args.only else None
    # Validate the Grok Bot roster before any generated file is written. A rejected
    # manifest (SWARM_AGENTS_FILE with a role outside A01–A30) must leave the other
    # exports untouched.
    agents = list(load_manifest())
    try:
        grok_files = render_grokbot(agents)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    sources = load_skills(ROOT)
    skill_errors = validate_skills(sources, {**manifest_raw, "agents": agents}, counts=False)
    if skill_errors:
        print("error: invalid skill sources:\n  " + "\n  ".join(skill_errors), file=sys.stderr)
        return 1
    CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
    GROK_DIR.mkdir(parents=True, exist_ok=True)
    changed, written, blocked = [], [], []
    for agent in agents:
        if not _agent_selected(agent, only):
            continue
        _write_or_check(CLAUDE_DIR / f"{agent['slug']}.md", render_claude(agent, defaults), args.check, changed, written, blocked)
        _write_or_check(GROK_DIR / f"{agent['slug']}.md", render_grok(agent, defaults), args.check, changed, written, blocked)
        _write_or_check(CURSOR_DIR / f"{agent['slug']}.md", render_cursor(agent, defaults), args.check, changed, written, blocked)
        _write_or_check(OMP_AGENTS_DIR / f"{agent['slug']}.md", render_omp(agent, agents), args.check, changed, written, blocked)
        _write_or_check(OMP_SKILLS_DIR / agent["slug"] / "SKILL.md", omp_skill(agent), args.check, changed, written, blocked)
        if agent["id"] == "A01":
            _write_or_check(OMP_SKILLS_DIR / "swarm-orchestrate" / "SKILL.md", swarm_orchestrate_skill(), args.check, changed, written, blocked)
    slug_selected = {a["slug"] for a in agents if _agent_selected(a, only)}
    for skill in sources:
        if only and skill.owner_dir != SHARED_OWNER and skill.owner_dir not in slug_selected:
            continue
        _write_or_check(OMP_SKILLS_DIR / skill.name / "SKILL.md", source_skill(skill), args.check, changed, written, blocked)
    for rel in sorted(grok_files):
        if not _grok_rel_selected(rel, agents, only):
            continue
        _write_or_check(ROOT / rel, grok_files[rel], args.check, changed, written, blocked)
    refused = []
    if blocked and not args.check:
        # A symlinked, hardlinked or swapped file was in the write set. Do not prune; the directory
        # check above already refused a symlinked export dir before any write.
        print("refused to write through symlink or hardlink: " + ", ".join(blocked), file=sys.stderr)
        return 1
    if not only:
        slugs = {a["slug"] for a in agents}
        removable, refused = _omp_orphans(slugs)
        cursor_removable, cursor_refused = _cursor_orphans(slugs)
        removable += cursor_removable
        refused += cursor_refused
        lane_dirs = {Path(rel).parent.name for rel in grok_files if rel.startswith("grokbot/skills/")}
        grok_removable, grok_refused = _grokbot_orphans(lane_dirs)
        removable += grok_removable
        refused += grok_refused
        for orphan in removable:
            changed.append(str(orphan.relative_to(ROOT)))
            if not args.check:
                _unlink_orphan(orphan)
                written.append(f"removed {orphan.relative_to(ROOT)}")
        changed += [str(p.relative_to(ROOT)) for p in refused]
    if args.check:
        print("stale:" if changed else "up-to-date", ", ".join(changed))
        return 1 if changed else 0
    print(f"wrote {len(written)} agent file(s): {', '.join(written) or '(none changed)'}")
    if refused:
        # Symlinked or out-of-tree orphans are never deleted; fail so a human removes them.
        print("refused to remove (symlink or outside the generated dir): " + ", ".join(str(p.relative_to(ROOT)) for p in refused), file=sys.stderr)
        return 1
    if workspace:
        # the substrate step first: its env files live outside the workspace, so a failure there writes nothing in it
        if not args.no_substrate and _install_substrate.install_substrate(workspace, runtimes, False, sys.stdout):
            return 2
        install_workspace(workspace, with_a01_complete_hook=args.with_a01_complete_hook)
        print(f"installed Claude/Grok agents, skills and hook into {workspace}")
        return _install_omp.install_omp(workspace, mode, False, sys.stdout, allow_shadowed_copy=args.allow_shadowed_copy)
    return 0


if __name__ == "__main__":
    sys.exit(main())
