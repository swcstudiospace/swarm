#!/usr/bin/env python3
"""A01 — autonomous swarm runner: executes a planned task DAG with headless agent sessions.

Each ready task is dispatched to its agent as a headless session, run with the task.assign prompt on stdin:
    claude -p --agent <slug> --output-format json --permission-mode <mode> …
        [--mcp-config <repo>/.mcp.json --strict-mcp-config] --allowedTools … [mcp__substrate]   (--runtime claude)
    omp -p --mode json --no-title -e <agent-swarm>/omp --cwd <repo>
        --approval-mode write --tools <frontmatter tools>,yield
        --append-system-prompt $SWARM_DIR/agents/<slug>.md --max-time <n>s                   (--runtime omp)
--runtime auto picks grok when SWARM_RUNTIME=grok or grok is on PATH and claude is not, else claude; it never
picks omp (use --runtime omp or SWARM_RUNTIME=omp). Preflight checks only the selected runtime's binary (and
`claude auth status` for claude). The omp result is the `yield` payload of the terminal agent_end, falling back
to the last fenced json block in the final assistant text.
Grok dispatch is refused: its documented single-prompt interface does not support this runner's stdin assignment
transport. No runtime is launched with permission bypasses; OMP keeps extension discovery and its explicit guard.

Agent sessions run without signing keys (SWARM_AGENT_SESSION=1, SWARM_CHILD=1, SWARM_AGENT=<slug>); OMP receives
only bounded deny-path guard metadata, never loading references. A gate script they run records nothing.
For each gate task the runner itself runs the gate script on the gate task's own id after the session, while the task is
still leased; the script records signed verdicts, which are applied between rounds by the Task Store.
With SUBSTRATE_URL set (never on --dry-run) each task is worked under a substrate lease the runner holds for its agent:
claimed before dispatch (a denied claim waits), beaten every TTL/3 while the session runs and through review, completed at
DONE, released and re-claimed on CHANGES_REQUESTED, released otherwise; work left in review by an earlier run is claimed
again at start (swarm/substrate_lease.py, docs/substrate-leases.md).
Each boundary that changes the accountable agent (a dependency owned by another agent, a failing gate, an escalation) is
written as a `coord_handoff` packet by its sender, and each assignment carries the task's context rebuilt from the
ledger (swarm/substrate_handoff.py, docs/substrate-handoffs.md).
Each session, each of its lease calls and each packet it sends carries the agent's own SUBSTRATE_TOKEN, from that agent's
env file for the workspace (<repo>); no other SUBSTRATE_TOKEN* reaches the session. The bracketed flags appear when the
install wired the substrate MCP entry into <repo> (swarm/workspace.py, docs/substrate-workspace.md).

  python3 scripts/swarm_run.py                          # run latest plan to completion
  python3 scripts/swarm_run.py --runtime omp            # run it on omp (needs only `omp` on PATH)
  python3 scripts/swarm_run.py --dry-run                # canned agent results; prints each task's invocation to stderr
  python3 scripts/swarm_run.py --once --max-parallel 4  # a single scheduling round
  python3 scripts/swarm_run.py --swarm-timeout 3600     # cancel the whole run after an hour (like a signal)

Scheduling (DESIGN v2.2 D5): a persistent pool of daemon worker threads is refilled the moment a session finishes (reconcile +
dispatchable + select_batch run after every completion, so one slow task never holds back newly ready work). Order is
priority (P0 first), DAG depth, creation time. The global cap is --max-parallel (default SWARM_MAX_PARALLEL, else 4; at
most SWARM_MAX_PARALLEL_CEILING, 32); each agent's manifest max_parallel still applies, and notes.claims keep
overlapping tasks apart. A provider rate limit halves the effective parallelism once per burst (AIMD, swarm.degraded;
swarm.degraded is cleared when it recovers) and a success adds one back after SWARM_RATE_COOLDOWN_S; a transient failure
waits swarm.retry.backoff_s (notes.retry_after). An isolated failure never lowers the parallelism (the task follows the
retry/escalation ladder); SWARM_FAIL_STREAK (default 3, 0 disables) is the circuit for systemic trouble: that many
consecutive non-rate-limit failures of distinct tasks with no success in between halve the effective parallelism once
per streak through the same AIMD limiter (swarm.degraded reason consecutive_failures; a success resets the streak and
recovers the cap additively).
SWARM_SCHEDULER=rounds restores the old per-round barrier as a fallback. One swarm.telemetry.Telemetry per run writes
swarm.run.* events and .swarm/live.json; a monitor thread beats every SWARM_HEARTBEAT_S and flags a silent omp stream
after SWARM_STALL_AFTER_S. SIGINT/SIGTERM/SIGHUP or --swarm-timeout write <swarm_dir>/cancel/<correlation_id>
({requested_wall, reason, grace_s}), wait up to SWARM_CANCEL_GRACE_S (10) for the sessions to leave, then kill their
process groups. The whole shutdown fits in SWARM_SHUTDOWN_BUDGET_S (default 15, the supervisor's SIGTERM -> SIGKILL
window; hooks/autonomous_run.py exports its grace there): the cooperative wait is shortened so that the kill starts
KILL_GRACE_S + KILL_SETTLE_S before the budget ends. A worker exception fails that one task (E-INTERNAL), never its
siblings.
"""
from __future__ import annotations
import codecs
import queue
import importlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, wait as futures_wait
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable, NamedTuple
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from swarm.atomic_file import write_text_atomic  # noqa: E402
from swarm.observability import emit_gsd_omp, stream_subagent_status  # noqa: E402
from swarm.script_base import AgentScript  # noqa: E402
from swarm.taskstore import TaskStore, TaskState as S  # noqa: E402
from swarm.manifest import get_agent, by_capability  # noqa: E402
from swarm.blast_radius import join_result_text, lane_section, prepare_slice_worktree, take_agent_slot  # noqa: E402
from swarm.envelope import build_envelope, sign_envelope  # noqa: E402
from swarm.paths import swarm_dir, latest_correlation, logging_paths  # noqa: E402
from swarm.runlog import log_phase  # noqa: E402  # logger unit: phases
from swarm.errors import SwarmError, ErrorCode, ErrorHandler  # noqa: E402
from swarm.gates import SEVERITIES  # noqa: E402
from swarm.verdicts import GATE_SCRIPTS, simulated_failures  # noqa: E402
from swarm.results import (parse_result, validate_result, apply_result, reconcile, reject,  # noqa: E402
                           agent_failed, agent_findings, agent_verdict)
from swarm import memory as swarm_memory, substrate_client, substrate_handoff, substrate_lease, substrate_tee, workspace  # noqa: E402
from swarm.claims import ClaimStore, make_claim_table, parse_claims  # noqa: E402
from swarm.lifecycle import RunEvent  # noqa: E402
from swarm.limiter import AimdLimiter, detect_rate_limit  # noqa: E402
from swarm.retry import backoff_s, is_transient  # noqa: E402
from swarm.subagent_lifecycle import get_manager, resolve_max_parallel  # noqa: E402
from swarm.telemetry import Telemetry, heartbeat_default  # noqa: E402
from swarm.redact import redact_text  # noqa: E402
from swarm.tools.shared_runtime import cancel_flag_path  # noqa: E402  # one path rule for the writer here and the reader tool
# WR-12: agent sessions are untrusted principals. They get no key material and no SWARM_REQUIRE_KEY: they record
# nothing, so a key-less gate-script preview signs with the dev key instead of exiting 2. The runner keeps all
# three; it performs every APPROVED transition and the recorded gate run.
# SWARM_TASK_ID and SWARM_CORRELATION_ID are the runner's own defaults for script flags. A child that
# inherits them binds its gate scripts to the parent's task (T-06-19).
AGENT_SESSION_STRIPPED = (
    "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY",
    "SWARM_TASK_ID", "SWARM_CORRELATION_ID",
)

# Deny-only guard policy, never a signer loading reference. Keep the bounds aligned
# with the OMP extension parser; rejecting overflow avoids silently losing a denial.
SIGNER_DENY_PATHS = "SWARM_SIGNER_DENY_PATHS"
SIGNER_DENY_MAX_PATHS = 32
SIGNER_DENY_MAX_PATH_LENGTH = 4096
SIGNER_DENY_MAX_JSON_LENGTH = 131072


def signer_deny_metadata(controller_env: dict[str, str]) -> str | None:
    """Capture configured/private-source denials before removing loadable references.

    Paths are lexical and relative to the controller's actual cwd, just like the
    signer. Do not expand home/env syntax, resolve symlinks, or inspect any file.
    """
    def invalid():
        raise SwarmError(ErrorCode.E_INPUT, "invalid or oversized signer deny-path policy")

    prior = controller_env.get(SIGNER_DENY_PATHS)
    paths = []
    if prior is not None:
        if len(prior) > SIGNER_DENY_MAX_JSON_LENGTH:
            invalid()
        try:
            paths = json.loads(prior)
        except (ValueError, RecursionError):
            invalid()
        if not isinstance(paths, list) or len(paths) > SIGNER_DENY_MAX_PATHS:
            invalid()
        if any(not isinstance(path, str) or not path.startswith("/") or "\0" in path
               or len(path.encode("utf-8", errors="surrogatepass")) > SIGNER_DENY_MAX_PATH_LENGTH
               for path in paths):
            invalid()
    reference = controller_env.get("SWARM_ED25519_KEY_FILE")
    if reference:
        if "\0" in reference:
            invalid()
        paths = [os.path.abspath(reference), *paths]
    # Python preserves exactly two leading slashes; OMP's POSIX normalizer does not.
    paths = list(dict.fromkeys("/" + os.path.normpath(path).lstrip("/") for path in paths))
    if len(paths) > SIGNER_DENY_MAX_PATHS or any(
            len(path.encode("utf-8", errors="surrogatepass")) > SIGNER_DENY_MAX_PATH_LENGTH for path in paths):
        invalid()
    if not paths:
        return None
    encoded = json.dumps(paths, separators=(",", ":"))
    if len(encoded) > SIGNER_DENY_MAX_JSON_LENGTH:
        invalid()
    return encoded


def upstream_context(store: TaskStore, task: dict) -> str:
    parts = []
    for dep in task["depends_on"]:
        d = store.get(dep)
        outs = "\n".join(f"  - {o['kind']}: {o['uri']} (v{o['version']})" for o in d["outputs"]) or "  - (no registered artifacts)"
        result = (d["notes_json"].get("result") or {})
        parts.append(f"### {dep} — {d['title']} ({d['agent_id']}, {d['state']})\n{outs}\n"
                     + (f"summary: {result.get('summary_md', '')[:1200]}" if result else ""))
    return "\n".join(parts) or "(no upstream tasks)"


HANDOVER_DEP_CHARS = 1200  # per dependency: the same budget as the upstream summary above
HANDOVER_TOTAL_CHARS = 4000
HANDOVER_LIST_MAX = 5


def _handover_block(dep: str, doc: dict) -> str:
    """One dependency's latest handover as a compact block: goal, what is left, the open points, artifacts, verdict."""
    def clip(value, n: int = 200) -> str:
        text = " ".join(str(value).split())
        return text if len(text) <= n else text[:n - 1] + "…"

    lines = [f"### {dep} ({doc.get('from_agent') or '?'} → {doc.get('to_agent') or '?'})", f"goal: {clip(doc.get('goal'))}"]
    for key in ("remaining", "blockers", "open_questions", "constraints"):
        items = [clip(i, 160) for i in (doc.get(key) or []) if i][:HANDOVER_LIST_MAX]
        if items:
            lines += [f"{key.replace('_', ' ')}:", *(f"- {i}" for i in items)]
    artifacts = [a for a in (doc.get("artifacts") or []) if isinstance(a, dict)][:HANDOVER_LIST_MAX]
    if artifacts:
        lines += ["artifacts:", *(f"- {clip(a.get('kind'), 40)}: {clip(a.get('uri'), 120)} {clip(a.get('digest'), 24)}".rstrip()
                                  for a in artifacts)]
    verification = doc.get("verification")
    if isinstance(verification, dict):
        lines.append(f"verification: {clip(verification.get('status'), 20)} (confidence {verification.get('confidence')})")
    block = "\n".join(lines)
    return block if len(block) <= HANDOVER_DEP_CHARS else block[:HANDOVER_DEP_CHARS - 1] + "…"


def handover_section(task: dict, repo: Path) -> str:
    """The `## Handover` section of an assignment: for each dependency with a valid latest handover
    (swarm.handover, schema swarm.handover.v1) a compact block. '' when the module is not there, or no dependency has a
    usable handover (missing, malformed or stale ones are skipped: a handover is context, never a gate on dispatch).
    Bounded: HANDOVER_DEP_CHARS per dependency, HANDOVER_TOTAL_CHARS overall."""
    try:
        handover = importlib.import_module("swarm.handover")  # written by the handover unit: absent means nothing to add
    except ImportError:
        return ""
    inspect_fn = getattr(handover, "inspect_handover", None)
    if inspect_fn is None:
        return ""
    blocks: list[str] = []
    receiver = task.get("agent_id")
    for dep in task["depends_on"]:
        try:
            # one inspection decides and supplies the document: the dependency's plan (correlation), staleness and the
            # receiver's own handover first (a task can hand over to several receivers, A15 and A14 of one release), the
            # newest one as context when none is addressed to it: exactly what the shared handover tools would return
            status, doc = inspect_fn(dep, root=repo, to_agent=receiver or None, correlation_id=task.get("correlation_id"))
        except (SwarmError, OSError, ValueError):
            continue
        if (status or {}).get("status") in ("ok", "partial") and isinstance(doc, dict):
            blocks.append(_handover_block(dep, doc))
    if not blocks:
        return ""
    body = "\n".join(blocks)
    return "\n\n## Handover\n" + (body if len(body) <= HANDOVER_TOTAL_CHARS else body[:HANDOVER_TOTAL_CHARS - 1] + "…")


_SUBSTRATE_CONTEXT: dict[tuple[str, str], str] = {}
_SUBSTRATE_CONTEXT_LOCK = threading.Lock()


def substrate_memory_section(repo: Path, correlation_id: str) -> str:
    """The `## Substrate memory` block (A01's run-start context), computed once per run and cached; '' when off or down."""
    if not substrate_client.enabled():
        return ""
    key = (str(repo), correlation_id)
    with _SUBSTRATE_CONTEXT_LOCK:
        if key not in _SUBSTRATE_CONTEXT:
            try:
                graph_id = substrate_tee.lookup_graph_id(correlation_id, root=repo)
                brief = swarm_memory.run_start_context(substrate_tee.repo_slug(repo), graph_id)
            except Exception:  # noqa: BLE001 - fail open: memory context never stops a dispatch
                brief = ""
            _SUBSTRATE_CONTEXT[key] = f"## Substrate memory\n{brief}\n\n" if brief else ""
        return _SUBSTRATE_CONTEXT[key]


def assignment_prompt(store: TaskStore, task: dict, agent: dict, repo: Path, lease_s: int | None = None,
                      handoffs: str = "") -> str:
    """The session's stdin. `lease_s` is the granted TTL of the task's substrate lease (LEASE-05); a task dispatched
    unleased keeps the old meaning, its wall-clock budget. `handoffs` is the task's context rebuilt from the substrate
    ledger (substrate_handoff.render), after the Task Store's upstream section; '' leaves the prompt as it was."""
    assign = {"task_id": task["task_id"], "correlation_id": task["correlation_id"], "agent_id": agent["id"],
              "capability": task["capability"], "title": task["title"],
              "lease_s": lease_s if lease_s is not None else task["budget"].get("max_wall_s", 1800),
              "inputs": task["inputs"], "acceptance": task["acceptance"], "budget": task["budget"],
              "risk_class": task["risk_class"], "priority": task["priority"], "attempt": task["attempt"],
              "rework_loop": task["rework_loops"]}
    notes = task["notes_json"]
    if notes.get("gate"):
        assign["gate"] = notes["gate"]
        assign["gate_for"] = notes["gate_for"]
    env = sign_envelope(build_envelope(source="A01@runner", target=agent["id"], msg_type="task.assign", payload=assign,
                                       correlation_id=task["correlation_id"], priority=task["priority"],
                                       risk_class=task["risk_class"]))
    rework = ""
    if task["rework_loops"] and notes.get("feedback"):
        rework = "\n## Rework feedback (CHANGES_REQUESTED)\nAddress every finding below before reporting IN_REVIEW:\n" \
                 + json.dumps(notes["feedback"], indent=2)
    lane_note = lane_section(notes)
    gate_note = ""
    if notes.get("gate"):
        gate_note = (f"\n## Gate instructions\nYou are issuing the **{notes['gate']}** gate for tasks {notes['gate_for']}. "
                     f"Run your gate script with `--task-id {task['task_id']}` (this gate task's own id) to see its findings; "
                     f"in this headless session it records nothing. After the session the runner re-runs it with the "
                     f"signing key and records the verdict on each gate_for target. Then report one JSON with "
                     f"`\"gate\": \"{notes['gate']}\"` and `\"verdicts\": {{\"<target_task_id>\": {{\"verdict\": \"pass|fail\", \"findings\": [...]}}}}` "
                     f"(advisory: fail findings become rework feedback, review findings are passed to the review gate "
                     f"script; only script-written verdicts count).")
    handover_note = handover_section(task, repo)
    return substrate_memory_section(repo, task["correlation_id"]) + f"""# task.assign (signed envelope)
```json
{json.dumps(env, indent=2)}
```

## Brief
{notes.get('brief_excerpt', '')}
{lane_note}
## Working repository
{repo}
Swarm runtime lives at {ROOT} (scripts: `python3 {ROOT}/scripts/<script>.py --root {repo} --task-id {task['task_id']} --correlation-id {task['correlation_id']} --json`).

## Upstream artifacts
{upstream_context(store, task)}{handoffs}{handover_note}
{gate_note}{rework}

Execute the task per your agent instructions and finish with your summary and the single ```json result block."""


def preflight_signing() -> None:
    """Refuse before any task is claimed when task.assign cannot be signed.

    Same messages as sign_envelope. A REQUIRE_KEY or unloadable-Ed25519 misconfiguration
    is reported first; otherwise a keyless run raises ``fail-closed: no signing key configured``.
    ``--dry-run`` signs task.assign too, so it takes the same check. Nothing here claims a task.
    """
    from swarm.envelope import validate_signing_key, insecure_dev_key, real_key_configured
    err = validate_signing_key()
    if err:
        raise SwarmError(ErrorCode.E_POLICY, err)
    if not real_key_configured() and insecure_dev_key() is None:
        raise SwarmError(
            ErrorCode.E_POLICY,
            "fail-closed: no signing key configured — task transitions and gate verdicts are signed, so a run"
            " cannot start without one. Set SWARM_ED25519_KEY to a 64-hex Ed25519 seed (or SWARM_SIGNING_KEY to"
            " any secret) and re-run; SWARM_ALLOW_INSECURE_DEV_KEY=1 permits an unsigned local dev key (never"
            " combine it with SWARM_REQUIRE_KEY=1).",
        )


def preflight_auth(claude_bin: str) -> None:
    """Fail fast (E-DEP) when the CLI has no credentials instead of burning task attempts."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        proc = subprocess.run([claude_bin, "auth", "status"], capture_output=True, text=True, timeout=30)
        status = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return  # older CLI without `auth status`; let the first task surface any problem
    if status.get("loggedIn") is False:
        raise SwarmError(ErrorCode.E_DEP, "claude CLI is not logged in — run `claude login` (or `claude setup-token` for headless use) "
                                          "or export ANTHROPIC_API_KEY; use --dry-run to simulate without credentials")


RUNTIMES = ("claude", "grok", "omp")
CLAUDE_PERMISSION_MODES = ("default", "acceptEdits", "plan")
OMP_APPROVAL_MODES = ("write", "always-ask")
# the child-env deltas every runtime gets; --dry-run prints exactly these (never key material)
CHILD_ENV_KEYS = ("SWARM_DIR", "SWARM_CHILD", "SWARM_AGENT_SESSION", "SWARM_AGENT", "AIO_UPLIFT", "AIO_SWARM")
FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
FRONTMATTER_TOOLS = re.compile(r"^tools:[ \t]*(.*?)[ \t]*$", re.M)


def resolve_runtime(explicit: str) -> str:
    """claude|grok|omp as given; auto: SWARM_RUNTIME=grok|omp, else grok when grok is on PATH and claude is not,
    else claude. auto never picks omp from PATH (OPEN-3)."""
    if explicit in RUNTIMES:
        return explicit
    if os.environ.get("SWARM_RUNTIME") in ("grok", "omp"):
        return os.environ["SWARM_RUNTIME"]
    if shutil.which("grok") and not shutil.which("claude"):
        return "grok"
    return "claude"


def runtime_bin(runtime: str, args) -> str:
    return {"claude": getattr(args, "claude_bin", "claude"), "grok": getattr(args, "grok_bin", "grok"),
            "omp": getattr(args, "omp_bin", "omp")}[runtime]

def validate_runtime_approval(runtime: str, args) -> None:
    """Refuse unsupported transports and bypass modes before a task or lease is claimed."""
    if runtime == "grok":
        raise SwarmError(ErrorCode.E_DEP, "grok headless dispatch is unsupported: its documented single-prompt "
                         "interface does not accept this runner's stdin assignment transport; use --runtime claude "
                         "or --runtime omp with normal approval controls")
    if runtime == "omp" and getattr(args, "approval_mode", "write") not in OMP_APPROVAL_MODES:
        raise SwarmError(ErrorCode.E_INPUT, "omp requires normal approval mode: write or always-ask")
    if runtime == "claude" and args.permission_mode not in CLAUDE_PERMISSION_MODES:
        raise SwarmError(ErrorCode.E_INPUT, "claude requires normal permission mode: default, acceptEdits or plan")



def omp_agent(slug: str, sdir: Path) -> tuple[str, Path]:
    """(--tools CSV incl. yield, body file): omp/agents/<slug>.md's frontmatter tools, and its body with the
    frontmatter stripped, written to $SWARM_DIR/agents/<slug>.md (a missing file would be appended as literal text)."""
    src = ROOT / "omp" / "agents" / f"{slug}.md"
    try:
        raw = src.read_text()
    except OSError as e:
        raise SwarmError(ErrorCode.E_DEP, f"omp agent body {src} unreadable ({e.strerror}); run scripts/build_agents.py") from e
    fm = FRONTMATTER.match(raw)
    tools_m = FRONTMATTER_TOOLS.search(fm.group(1)) if fm else None
    if fm is None or tools_m is None:
        raise SwarmError(ErrorCode.E_DEP, f"omp agent body {src} has no frontmatter tools: line")
    tools = [t.strip() for t in tools_m.group(1).strip("\"'").split(",") if t.strip()]
    if "yield" not in tools:
        tools.append("yield")
    body = sdir / "agents" / f"{slug}.md"
    body.parent.mkdir(parents=True, exist_ok=True)
    # atomic: parallel tasks of one agent rewrite the same file while an earlier child may be reading it
    tmp = body.with_name(f".{slug}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(raw[fm.end():])
    os.replace(tmp, body)
    return ",".join(tools), body


def headless_command(runtime: str, agent: dict, repo: Path, sdir: Path, args) -> tuple[list[str], dict, Path]:
    """(argv, env, cwd) of one agent session; the prompt goes on stdin. Shared by the live path and --dry-run.

    `repo` is the installed workspace (ADR 0001 S4). The child's environment holds no token but its own: every
    SUBSTRATE_TOKEN* the runner has is dropped, and SUBSTRATE_TOKEN is set from the agent's env file, the one the lease
    calls use too (swarm/workspace.py). Each runtime reads the `substrate` MCP entry the install wrote:
    - claude runs in this checkout, where a workspace `.mcp.json` is never read, so it is passed as --mcp-config
      (strict: the agent's tools list admits no other server anyway) and its tools are added to --allowedTools;
    - grok is refused until a supported stdin assignment transport is available;
    - omp reads `.mcp.json` from its --cwd, and its --tools list does not hide MCP tools (they are xd:// devices)."""
    validate_runtime_approval(runtime, args)
    slug = agent["slug"]
    # Child sessions are full CLI sessions: their brief fires UserPromptSubmit. Mark them so
    # Prompt Uplift (20 min/agent) and the swarm kickoff hooks stay off — uplift runs once, on the user's prompt.
    controller_env = dict(os.environ)
    deny_metadata = signer_deny_metadata(controller_env) if runtime == "omp" else None
    env, _source, _problem = workspace.child_env(
        {k: v for k, v in controller_env.items()
         if k not in AGENT_SESSION_STRIPPED and k != SIGNER_DENY_PATHS}, repo, agent["id"])
    if deny_metadata is not None:
        env[SIGNER_DENY_PATHS] = deny_metadata
    env.pop("SWARM_SUBSTRATE_AGENT", None)  # never inherit another process's MCP authorization marker
    # SWARM_AGENT: the session's agent identity; the omp swarm_gate tool runs only that agent's gate (WR-03)
    env.update(SWARM_DIR=str(sdir), SWARM_CHILD="1", SWARM_AGENT_SESSION="1", SWARM_AGENT=slug,
               AIO_UPLIFT="0", AIO_SWARM="0")
    model = ["--model", args.model] if args.model else []
    binary = runtime_bin(runtime, args)
    disabled = (env.get("SUBSTRATE_DISABLED") or "").strip() == "1"
    wired = (not disabled) and workspace.projected_mcp(repo, runtime)
    if disabled:
        env.pop(workspace.TOKEN, None)
        env.pop("SUBSTRATE_URL", None)
        env.pop("SWARM_SUBSTRATE_AGENT", None)
    if runtime == "omp":
        if wired and env.get(workspace.TOKEN):
            env["SWARM_SUBSTRATE_AGENT"] = slug
        tools, body = omp_agent(slug, sdir)
        # omp aborts cleanly before the python timeout: a 10% margin of 5–60 s, never below 1 s (WR-03)
        margin = min(60, max(5, args.task_timeout // 10))
        # -e spelled as omp's absolute root path: omp dedups extension roots by that string, not realpath (D-01)
        return [binary, "-p", "--mode", "json", "--no-title",
                "-e", str((ROOT / "omp").resolve()), "--cwd", str(repo),
                "--approval-mode", getattr(args, "approval_mode", "write"),
                "--tools", tools, "--append-system-prompt", str(body),
                "--max-time", f"{max(1, args.task_timeout - margin)}s", *model], env, repo
    cmd = [binary, "-p", "--agent", slug, "--output-format", "json",
           "--permission-mode", args.permission_mode, "--max-turns", str(args.max_turns),
           "--add-dir", str(ROOT), *model]
    allowed = [t.strip() for t in (args.allowed_tools or "").split(",") if t.strip()]
    if wired:
        cmd += ["--mcp-config", str(workspace.mcp_config(repo, runtime)), "--strict-mcp-config"]
        allowed.append(workspace.CLAUDE_TOOLS)
    if allowed:
        cmd += ["--allowedTools", *allowed]
    return cmd + ["--add-dir", str(repo)], env, ROOT  # cwd ROOT: .claude/agents resolves


def omp_stream_text(stdout: str) -> tuple[str, dict]:
    """Normalise an `omp -p --mode json` stream to (final text, meta). The text is the last assistant message's
    text parts from the last terminal agent_end, plus — when the session yielded successfully — a trailing fenced
    json block of the yield data, so parse_result (last block wins) takes the yield payload first."""
    session_id, turns, end = None, 0, None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "session":
            session_id = ev.get("id")
        elif ev.get("type") == "turn_end":
            turns += 1
        elif ev.get("type") == "agent_end" and ev.get("isTerminal") is not False:
            end = ev
    meta = {"session_id": session_id, "total_cost_usd": None, "num_turns": turns, "is_error": True}
    if end is None:
        return "", meta
    messages = [m for m in end.get("messages") or [] if isinstance(m, dict)]
    assistants = [m for m in messages if m.get("role") == "assistant"]
    last = assistants[-1] if assistants else {}
    content = last.get("content") or []
    text = content if isinstance(content, str) else "\n".join(
        p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") == "text")
    meta["total_cost_usd"] = sum(((m.get("usage") or {}).get("cost") or {}).get("total") or 0 for m in assistants)
    meta["is_error"] = last.get("stopReason") in ("error", "aborted")
    for m in reversed(messages):
        if m.get("role") != "toolResult" or m.get("toolName") != "yield":
            continue
        details = m.get("details")
        details = details if isinstance(details, dict) else {}
        if m.get("isError") is False and details.get("status") == "success":
            if isinstance(details.get("data"), dict):
                text += f"\n```json\n{json.dumps(details['data'])}\n```"
        else:
            # D-02: an error yield fails the session, whatever json block the assistant text carries (WR-02)
            meta["yield_error"] = details.get("error") or f"yield status={details.get('status')} isError={m.get('isError')}"
            meta["is_error"] = True
        break
    return text, meta


class AgentTimeout(subprocess.TimeoutExpired):
    """The session outlived --task-timeout and its process group was killed; text/meta hold its partial output."""

    def __init__(self, cmd, timeout, text: str, meta: dict):
        super().__init__(cmd, timeout)
        self.text, self.meta = text, meta


class LeaseStopped(Exception):
    """The lease keeper stopped the dispatch: the substrate says the runner no longer holds the task's node (LEASE-06),
    or the task moved on in another process. `reason` is the FAILED reason from substrate_lease's refusal table (or
    substrate_lease.moved_on_reason); text/meta hold the session's partial output."""

    def __init__(self, reason: str, text: str, meta: dict):
        super().__init__(reason)
        self.reason, self.text, self.meta = reason, text, meta


class DispatchWatch:
    """The lease keeper's handle on one dispatch (LEASE-06), attached before the session spawns and detached once the
    result is applied. `stop(reason)` records the first reason and stops whichever child works the task at that moment:
    the agent session, then the runner's gate script. A child that attaches after a reason was recorded is stopped at
    once, and `applying` refuses the result, so a lost lease never gets a verdict recorded or a result accepted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reason: str | None = None
        self._child: Callable[[str], None] | None = None

    def stop(self, reason: str) -> None:
        with self._lock:  # waits while a result is being applied: that result was written under a held lease
            if self.reason is None:
                self.reason = reason
            child = self._child
        if child is not None:
            child(self.reason)

    def attach(self, child: Callable[[str], None] | None) -> None:
        """`on_session` / `on_process`: the running child's stop, or None once it has ended."""
        with self._lock:
            self._child = child
            reason = self.reason
        if child is not None and reason is not None:
            child(reason)

    def check(self, text: str, meta: dict) -> None:
        if self.reason is not None:
            raise LeaseStopped(self.reason, text, {**meta, "lease_stopped": self.reason})

    @contextmanager
    def applying(self, text: str, meta: dict):
        """Write the result only while no stop has been recorded, and hold the keeper's stop off until it is written."""
        with self._lock:
            self.check(text, meta)
            yield


# process groups of the running children (agent sessions, the runner's gate scripts): they run in their own session,
# so a signal to the runner's group misses them
_SESSIONS: dict[int, ChildGroup] = {}
_SESSIONS_LOCK = threading.Lock()
_STOPPING = threading.Event()  # the runner is ending its sessions: a session that registers now is killed at once
_FORWARDED = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)  # each one starts a cooperative cancel (see CancelRequest)
STOP_GRACE_S = 10  # a stopped or timed-out child group gets this long between SIGTERM and SIGKILL
SHUTDOWN_REASON = "E-TIMEOUT: runner shutdown"
_stop_reason = SHUTDOWN_REASON  # what a session that registers after kill_sessions began is stopped with


def kill_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _end_group(pgid: int, deadline: float) -> None:
    """Return once no process is left in the group, SIGKILLing what is still there at `deadline` (monotonic). The
    leader's exit proves nothing: a tool with its own pipes can outlive SIGTERM after its parent has gone."""
    while _group_alive(pgid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _group_alive(pgid):
        kill_group(pgid, signal.SIGKILL)
        settle = time.monotonic() + 2  # SIGKILL cannot be caught; what is left is the init process reaping orphans
        while _group_alive(pgid) and time.monotonic() < settle:
            time.sleep(0.05)


def kill_sessions(grace: float = 5, reason: str = SHUTDOWN_REASON) -> None:
    """SIGTERM every session group, so omp disposes its session and tool processes; SIGKILL what outlives `grace` s
    (WR-09). The workers still reap their sessions and record the killed tasks as rejected results (FAILED with
    `reason`), clearing notes.running, so the next run retries them."""
    global _stop_reason
    with _SESSIONS_LOCK:
        _STOPPING.set()
        _stop_reason = reason
        sessions = list(_SESSIONS.values())
    for group in sessions:
        group.stop(reason, grace=grace)
    groups = [group.pgid for group in sessions]
    deadline = time.monotonic() + grace
    while groups and time.monotonic() < deadline:
        time.sleep(0.1)
        groups = [g for g in groups if _group_alive(g)]
    for pgid in groups:
        kill_group(pgid, signal.SIGKILL)


def reap_group(proc: subprocess.Popen) -> tuple[str, str]:
    """SIGTERM the child's whole process group, SIGKILL it after STOP_GRACE_S; the partial (stdout, stderr). Returns
    once every process in the group is gone, not only the leader."""
    deadline = time.monotonic() + STOP_GRACE_S
    for sig in (signal.SIGTERM, signal.SIGKILL):
        kill_group(proc.pid, sig)
        try:
            out = proc.communicate(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            continue
        _end_group(proc.pid, deadline)
        return out
    proc.kill()  # a descendant that left the group still holds the pipes
    proc.wait()
    return "", ""


class ChildGroup:
    """A child in its own session and process group (WR-04), registered for kill_sessions (WR-09). `stop(reason)` is the
    lease keeper's handle: SIGTERM the group and SIGKILL what is left STOP_GRACE_S later. `close()` forgets the group only
    once every process in a stopped group has exited, so a stopped session leaves no tool running (LEASE-06)."""

    def __init__(self, cmd: list[str], *, cwd, env: dict, stdin=subprocess.PIPE, text: bool = True):
        # text streams are block-buffered, so a short stderr line is invisible until the child exits. The agent
        # session is read unbuffered (text=False) so an extension-load failure can stop it while it is still running.
        self.proc = subprocess.Popen(cmd, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
                                     bufsize=-1 if text else 0, cwd=cwd, env=env, start_new_session=True)
        self.pgid = self.proc.pid
        self.stopped: list[str] = []
        self._lock = threading.Lock()
        self._deadline: float | None = None
        self._killer: threading.Timer | None = None
        self._closed = False
        with _SESSIONS_LOCK:
            _SESSIONS[self.pgid] = self
            stopping = _STOPPING.is_set()
        if stopping:  # spawned after kill_sessions took its snapshot (WR-09)
            self.stop(_stop_reason, grace=0)

    def stop(self, reason: str, *, grace: float | None = None) -> None:
        """Lease keeper / runner shutdown: record `reason` (so the result is never applied) and end the group."""
        with self._lock:
            self.stopped.append(reason)
            self._kill_locked(grace)

    def kill(self, grace: float | None = None) -> None:
        """End the group without recording a lease stop. The runner classifies the result itself (task timeout, an
        extension that failed to load). A second call, or one after `close`, does not restart the grace. `grace=0`
        while a stop is already in progress SIGKILLs now: the timer's backstop must not no-op."""
        with self._lock:
            self._kill_locked(grace)

    def _kill_locked(self, grace: float | None) -> None:
        grace = STOP_GRACE_S if grace is None else grace
        if self._closed:
            return
        if self._deadline is not None:
            if grace == 0:
                kill_group(self.pgid, signal.SIGKILL)
            return
        self._deadline = time.monotonic() + grace
        kill_group(self.pgid, signal.SIGTERM)
        if grace == 0:
            kill_group(self.pgid, signal.SIGKILL)
            return
        # a process that ignores SIGTERM is killed after the grace even when it no longer holds the child's pipes
        self._killer = threading.Timer(grace, kill_group, (self.pgid, signal.SIGKILL))
        self._killer.daemon = True
        self._killer.start()

    def close(self) -> None:
        """After the leader has been reaped: wait out a stopped group (SIGKILL at the grace), then forget it."""
        with self._lock:
            self._closed = True
            deadline, killer = self._deadline, self._killer
        try:
            if deadline is not None:
                _end_group(self.pgid, deadline)
        finally:
            if killer is not None:
                killer.cancel()
            with _SESSIONS_LOCK:
                _SESSIONS.pop(self.pgid, None)


# omp 18.3.1 `-e`: a package that fails to load is only this stderr line (main.ts formatExtensionLoadNotifications),
# rc 0, and the session runs on without it — for ours, without the swarm guard (T-06-06).
# T-06-27: the runner watches stderr while the session runs and stops the group on this line, before the unguarded
# session does any work. The pattern matches a line that has not been newline-terminated yet, so a chunk that ends
# on the message is enough; a match split across reads still hits once both halves are in the window.
OMP_EXTENSION_LOAD_ERROR = re.compile(r"^(?:\x1b\[[0-9;]*m)*(Failed to load extension .*?)(?:\x1b\[[0-9;]*m)*$", re.M)
STRANDED_STALE_S = 6 * 3600  # a legacy IN_PROGRESS row with notes.running but no deadline, older than this, is stranded
RUNNING_DEADLINE_GRACE_S = 30  # past --task-timeout: the live runner's own SIGTERM window, so a peer does not steal it


class StderrScan:
    """Find one omp extension-load failure in a stderr stream that arrives in pieces."""

    def __init__(self, pattern: re.Pattern[str] = OMP_EXTENSION_LOAD_ERROR, window: int = 8192):
        self._pattern = pattern
        self._window = window
        self._buf = ""
        self._message: str | None = None
        self._lock = threading.Lock()
        self.hit = threading.Event()

    @property
    def message(self) -> str | None:
        with self._lock:
            return self._message

    def feed(self, chunk: str) -> str | None:
        with self._lock:
            if self._message is not None or not chunk:
                return self._message
            self._buf = (self._buf + chunk)[-self._window:]
            found = self._pattern.search(self._buf)
            if found:
                self._message = found.group(1)[:500]
                self.hit.set()
            return self._message


def _write_stdin(proc: subprocess.Popen, prompt: str) -> None:
    """stdin is the unbuffered binary pipe of an agent session."""
    try:
        if prompt and proc.stdin is not None:
            proc.stdin.write(prompt.encode())
        if proc.stdin is not None:
            proc.stdin.close()
    except (BrokenPipeError, ValueError, OSError):
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except (BrokenPipeError, ValueError, OSError):
                pass


def _drain(stream, parts: list[str], scan: StderrScan | None, tap: OmpActivity | None = None) -> None:
    """Read an unbuffered binary pipe and append decoded text as it arrives, not when the child exits. `scan` and
    `tap` see each piece as it lands; a fault in `tap` (a display feed) never stops the drain."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def seen(piece: str) -> None:
        parts.append(piece)
        if scan is not None:
            scan.feed(piece)
        if tap is not None:
            try:
                tap.feed(piece)
            except Exception:  # noqa: BLE001
                _LOG.warning("activity feed failed", exc_info=True)

    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            if text:
                seen(text)
        tail = decoder.decode(b"", final=True)
        if tail:
            seen(tail)
    except (ValueError, OSError):
        pass


def session_output(runtime: str, stdout: str, stderr: str, returncode) -> tuple[str, dict]:
    meta = {"returncode": returncode, "stderr": (stderr or "")[-2000:]}
    if runtime == "omp":
        text, stream_meta = omp_stream_text(stdout or "")
        meta.update(stream_meta)
        load_error = OMP_EXTENSION_LOAD_ERROR.search(stderr or "")
        if load_error:
            meta.update(extension_error=load_error.group(1)[:500], is_error=True)
        return text, meta
    text = stdout or ""
    try:
        data = json.loads(text)
        meta.update({k: data.get(k) for k in ("total_cost_usd", "duration_ms", "num_turns", "is_error", "session_id")})
        text = data.get("result", "") if isinstance(data, dict) else text
    except json.JSONDecodeError:
        pass
    return text, meta


def run_agent_headless(agent: dict, prompt: str, repo: Path, args, *, on_session=None) -> tuple[str, dict]:
    """Run one agent session. `on_session(stop)` is called once the session is spawned and `on_session(None)` once it
    has ended; `stop(reason)` ends the session's process group and makes this raise LeaseStopped (the lease keeper's
    handle on a session whose node the runner no longer holds). A stopped session returns only once its whole process
    group is gone. The leader exiting is not the end of the session: a tool child can keep the pipes open, so the
    drains are waited out until they finish or the task deadline. A deadline that arrives with a drain still blocked
    is a timeout, and close reaps the group because that kill set a deadline."""
    runtime = resolve_runtime(getattr(args, "runtime", "auto"))
    cmd, env, cwd = headless_command(runtime, agent, repo, swarm_dir(repo), args)
    try:
        # own session: a timeout kills the whole group, omp's tool processes and MCP servers included (WR-04)
        group = ChildGroup(cmd, cwd=cwd, env=env, text=False)
    except OSError as e:
        raise SwarmError(ErrorCode.E_DEP, f"cannot spawn {cmd[0]} ({e.strerror or e})") from e
    proc = group.proc
    out_parts: list[str] = []
    err_parts: list[str] = []
    # omp only: a load failure is a stderr line while the session keeps going. Read it as it arrives and stop the
    # group then, so the unguarded session does not get as far as a tool call (T-06-27).
    scan = StderrScan() if runtime == "omp" else None
    # progress for the scheduler's telemetry: omp's `--mode json` stdout is read line by line while it runs (the final
    # result is still extracted from the whole stream by omp_stream_text); other runtimes report elapsed time only
    handle = current_run()
    tap = None
    if handle is not None:
        handle.streams = runtime == "omp"
        handle.started(runtime)
        tap = OmpActivity(handle.activity) if runtime == "omp" else None
    pumps = [
        threading.Thread(target=_write_stdin, args=(proc, prompt), daemon=True),
        threading.Thread(target=_drain, args=(proc.stdout, out_parts, None, tap), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_parts, scan), daemon=True),
    ]
    timed_out = False
    drains = pumps[1:]  # stdout and stderr; the leader's exit does not finish either while a tool holds the pipe
    try:
        if on_session is not None:
            on_session(group.stop)
        for pump in pumps:
            pump.start()
        deadline = time.monotonic() + args.task_timeout
        while True:
            if scan is not None and scan.hit.is_set():
                group.kill()  # not stop(): this is not a lost lease, and the result is refused as E-DEP below
                break
            if group.stopped:  # the lease keeper already signalled the group
                break
            leader_done = proc.poll() is not None
            drains_done = not any(pump.is_alive() for pump in drains)
            # A tool that outlived the leader still holds the pipes. Wait for both drains, and if the task deadline
            # arrives first, kill the group so close() reaps it instead of forgetting a live child.
            if time.monotonic() >= deadline and not (leader_done and drains_done):
                timed_out = True
                group.kill()
                break
            if leader_done and drains_done:
                break
            if scan is not None:
                scan.hit.wait(0.05)
            else:
                time.sleep(0.05)
        try:
            proc.wait(timeout=STOP_GRACE_S + 2)
        except subprocess.TimeoutExpired:
            group.kill(grace=0)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    finally:
        for pump in pumps:
            if pump.ident is not None:  # join() raises on a thread that was never started
                pump.join(timeout=STOP_GRACE_S + 2)
        if on_session is not None:
            on_session(None)
        group.close()
    out, err = "".join(out_parts), "".join(err_parts)
    # T-06-10: the live path records the producing runtime and argv like --dry-run does, so a stored
    # result names what produced it. Last so a session payload can never clobber the runner's own keys.
    live = {"runtime": runtime, "argv": cmd}
    if timed_out:
        text, meta = session_output(runtime, out, err, proc.returncode)
        raise AgentTimeout(cmd, args.task_timeout, text, {**meta, **live, "timed_out": True})
    text, meta = session_output(runtime, out, err, proc.returncode)
    meta = {**meta, **live}
    if scan is not None and scan.message:
        meta.setdefault("extension_error", scan.message)
        meta["is_error"] = True
    if group.stopped:  # a result finished under a lease the runner no longer holds is never applied
        raise LeaseStopped(group.stopped[0], text, {**meta, "lease_stopped": group.stopped[0]})
    return text, meta


def dry_run_invocation(task: dict, agent: dict, repo: Path, sdir: Path, args) -> dict:
    """--dry-run: print the exact session invocation (key-var unsets, env deltas, argv, stdin file) to stderr as a
    replayable `env -u … K=V … argv < file` line, then where the session's SUBSTRATE_TOKEN comes from; spawn nothing.
    The token's value is never printed: a replay sources the agent's env file for it."""
    runtime = resolve_runtime(getattr(args, "runtime", "auto"))
    cmd, env, _cwd = headless_command(runtime, agent, repo, sdir, args)
    slug = agent["slug"]
    token, source, problem = workspace.credential(agent["id"], repo, os.environ)
    # names only: the live child env drops these, so a replay from the runner's shell must too (WR-06, INST-03).
    # SUBSTRATE_TOKEN is not unset only when the agent has an own credential: the operator sources the agent's env file
    # for it before replay. When the agent has no credential, the runner's token is unset so replay does not borrow it.
    stripped = [*AGENT_SESSION_STRIPPED, *sorted(
        k for k in os.environ
        if workspace.carries_token(k) and (token is not None and k != workspace.TOKEN or token is None)
    )]
    unset = " ".join(f"-u {k}" for k in stripped)
    disabled = (env.get("SUBSTRATE_DISABLED") or "").strip() == "1"
    if runtime == "omp" and not disabled and workspace.projected_mcp(repo, runtime) and token is not None:
        env["SWARM_SUBSTRATE_AGENT"] = slug
    else:
        env.pop("SWARM_SUBSTRATE_AGENT", None)
    keys = [*CHILD_ENV_KEYS, *(["SWARM_SUBSTRATE_AGENT"] if "SWARM_SUBSTRATE_AGENT" in env else [])]
    deltas = " ".join(f"{k}={shlex.quote(env[k])}" for k in keys if k in env)
    stdin = sdir / "assignments" / f"{task['task_id']}.a{task['attempt']}.md"
    print(f"dry-run {task['task_id']} [{agent['id']}]: env {unset} {deltas} {shlex.join(cmd)} < {shlex.quote(str(stdin))}",
          file=sys.stderr, flush=True)
    source_str = f"from {source}" if token is not None else f"none ({problem})"
    print(f"dry-run {task['task_id']} [{agent['id']}] SUBSTRATE_TOKEN: {source_str}", file=sys.stderr, flush=True)
    return {"dry_run": True, "runtime": runtime, "argv": cmd}


def canned_result(task: dict, agent: dict) -> str:
    notes = task["notes_json"]
    if notes.get("gate"):
        fails = simulated_failures()
        verdicts = {}
        for t in notes["gate_for"]:
            if f"{t}:{notes['gate']}" in fails:
                verdicts[t] = {"verdict": "fail", "findings": [{"id": "SIM-1", "severity": "major", "kind": "functional",
                                                                "summary": "simulated gate failure"}]}
            else:
                verdicts[t] = {"verdict": "pass", "findings": []}
        payload = {"gate": notes["gate"], "task_id": task["task_id"], "state": "IN_REVIEW",
                   "verdicts": verdicts, "summary_md": f"dry-run {notes['gate']} gate"}
    else:
        payload = {"task_id": task["task_id"], "state": "IN_REVIEW",
                   "outputs": [{"kind": task["capability"], "uri": f"dry://{task['task_id']}", "version": "1", "digest": ""}],
                   "metrics": {}, "summary_md": f"dry-run output of {agent['id']} for {task['title']}"}
    return f"dry-run\n```json\n{json.dumps(payload)}\n```"


def run_gate_script(task, repo, sdir, *, dry_run, per_target_findings=None, timeout=300, on_process=None) -> None:
    """Run the gate task's real gate script on its own id with the runner's keys (D-12/D-13): with --dry-run in a
    runner dry-run, otherwise after the agent session while the gate task is still leased. The script derives and
    records the verdict; per_target_findings adds the agent's findings as gate input. `on_process` works as
    run_agent_headless's `on_session`: the lease keeper stops a gate script whose task lost its lease, and this raises
    LeaseStopped."""
    gate = task["notes_json"]["gate"]
    script = ROOT / "scripts" / f"{GATE_SCRIPTS[gate]}.py"
    cmd = [sys.executable, str(script), *(["--dry-run"] if dry_run else []), "--task-id", task["task_id"],
           "--correlation-id", task["correlation_id"], "--root", str(repo), "--json"]
    if per_target_findings is not None:
        cmd += ["--per-target-findings", str(per_target_findings)]
    # the autonomous hook starts this runner with SWARM_CHILD=1; the runner's own gate run must still record
    env = {k: v for k, v in os.environ.items() if k not in ("SWARM_AGENT_SESSION", "SWARM_CHILD")}
    env["SWARM_DIR"] = str(Path(sdir).resolve())
    group = ChildGroup(cmd, cwd=None, env=env, stdin=subprocess.DEVNULL)
    try:
        if on_process is not None:
            on_process(group.stop)
        try:
            out, err = group.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            reap_group(group.proc)
            raise
    finally:
        if on_process is not None:
            on_process(None)
        group.close()
    if group.stopped:  # verdicts it wrote before the stop never approve anything: this gate task's result is refused
        raise LeaseStopped(group.stopped[0], "", {"lease_stopped": group.stopped[0]})
    rc = group.proc.returncode
    if rc == 2 or rc < 0 or rc >= 129:  # 1 means findings; signals must never accept a partially written release plan
        raise SwarmError(ErrorCode.E_CONTRACT, f"{script.name} failed (exit {rc}): {(out or err)[-400:]}",
                         task_id=task["task_id"])


FINDINGS_GATES = ("review", "quality", "security")


def _evidence_candidates(results: Path, tid: str, attempt: int, suffix: str):
    """Candidate evidence paths in order: the canonical name first, then `.r<n>` suffixes."""
    yield results / f"{tid}.a{attempt}{suffix}"
    n = 1
    while True:
        yield results / f"{tid}.a{attempt}.r{n}{suffix}"
        n += 1


def evidence_path(sdir, tid: str, attempt: int, suffix: str = ".md") -> Path:
    """Evidence path that never overwrites: `results/<tid>.a<attempt><suffix>` on first write,
    `results/<tid>.a<attempt>.r<n><suffix>` once a same-attempt redispatch finds it taken, so the
    prior attempt's file stays readable (T-06-10). No reader depends on the exact name: post-mortem
    and debug tooling list the directory. Single-threaded probe only: concurrent writers MUST use
    write_evidence, which reserves the path with O_EXCL instead of check-then-write."""
    results = Path(sdir) / "results"
    for path in _evidence_candidates(results, tid, attempt, suffix):
        if not path.exists():
            return path
    raise AssertionError("unreachable")


def write_evidence(sdir, tid: str, attempt: int, text: str | None, suffix: str = ".md") -> Path:
    """Atomically reserve a fresh evidence path and write through the reserving handle, so two
    runners on the same IN_PROGRESS rework task (same attempt) never share a path: the loser of
    the O_EXCL race takes the next suffix. Uncontended writes keep the canonical name.
    Agent output (`.md` evidence) is secret-redacted on the way to disk, like every run-log payload (AC12); the
    structured `.findings.json` is a machine input of the gate scripts and is written as given."""
    if suffix == ".md":
        text = redact_text(text or "")
    results = Path(sdir) / "results"
    results.mkdir(parents=True, exist_ok=True)
    for path in _evidence_candidates(results, tid, attempt, suffix):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text or "")
        return path
    raise AssertionError("unreachable")


def gate_findings_file(task: dict, text: str, sdir: Path, emit) -> Path | None:
    """Review, quality and security gates: write {target: [findings]} — what the agent reported under verdicts{}
    for each gate_for target — to results/<tid>.a<N>.findings.json for the gate script's --per-target-findings, so
    an agent-reported failure reaches the recorded verdict and fails only its own target (WR-14). Normalization fails closed and reports every change (WR-16/WR-17):
    - an entry fails its target unless its verdict is in results.PASS_VERDICTS (pass, A09's legacy approve;
      case-insensitive — request_changes, block, waive, unknown and missing verdicts fail); a failing entry
      without a finding of major or worse gets one synthesized major finding (results.agent_findings, IN-15),
      reported in gate.findings.synthesized;
    - a severity outside SEVERITIES (case-insensitive) or a non-object finding counts as major;
    - a missing severity counts as major under a failing entry, else minor;
    - findings under a key that is not a gate_for id cannot be attributed, so they apply to every target.
    Changes emit one gate.findings.coerced event; unattributed keys emit gate.findings.unattributed.
    None for the release gate or when the session left no parseable result. The agent's verdicts never count:
    the gate script still derives each verdict."""
    notes = task["notes_json"]
    if notes.get("gate") not in FINDINGS_GATES:
        return None
    result = parse_result(text or "")
    if result is None:
        return None
    verdicts = result.get("verdicts")
    verdicts = verdicts if isinstance(verdicts, dict) else {}
    gate_for = list(notes.get("gate_for", []))
    coerced: list[dict] = []
    synthesized: list[dict] = []

    def entries(key: str, applied_to: list[str]) -> list[tuple[object, bool]]:
        if key not in verdicts:
            return []
        v = verdicts[key]
        items, synth = agent_findings(v)
        if synth:
            synthesized.append({"key": key, "verdict": agent_verdict(v), "applied_to": applied_to})
        failed = agent_failed(v)
        return [(f, failed) for f in items]

    def normalize(f, failed: bool) -> dict:
        if not isinstance(f, dict):
            coerced.append({"from": "non-object", "to": "major"})
            return {"severity": "major", "kind": "semantic", "summary": str(f)[:300]}
        sev = f.get("severity")
        if isinstance(sev, str) and sev.lower() in SEVERITIES:
            return {**f, "severity": sev.lower()}
        to = "major" if sev is not None or failed else "minor"
        coerced.append({"from": "missing" if sev is None else str(sev), "to": to})
        return {**f, "severity": to, "evidence": f"{f.get('evidence') or ''} [agent severity {sev!r}]".strip()}

    stray = sorted(k for k in verdicts if k not in gate_for)
    unattributed = [normalize(f, failed) for k in stray for f, failed in entries(k, gate_for)]
    per_target = {}
    for target in gate_for:
        findings, seen = [], set()
        for f in [normalize(f, failed) for f, failed in entries(target, [target])] + unattributed:
            key = json.dumps(f, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                findings.append(f)
        per_target[target] = findings
    if coerced:
        emit("gate.findings.coerced", {"task_id": task["task_id"], "coerced": coerced})
    if synthesized:
        emit("gate.findings.synthesized", {"task_id": task["task_id"], "synthesized": synthesized})
    if stray:
        emit("gate.findings.unattributed", {"task_id": task["task_id"], "keys": stray, "applied_to": gate_for,
                                            "findings": len(unattributed)})
    path = write_evidence(sdir, task['task_id'], task['attempt'], json.dumps(per_target, indent=2),
                           ".findings.json")
    return path


def dispatchable(store: TaskStore, corr: str, emit, handoffs=None, *, skip=frozenset()) -> list[dict]:
    """Tasks to dispatch now: ready (PLANNED/RETRY with their dependencies met), reworks, and FAILED tasks with attempts
    left (moved to RETRY here); FAILED tasks out of attempts are escalated. Tasks named in `skip` (the scheduler's
    in-flight dispatches) are left exactly as they are."""
    ready = [t for t in store.ready(corr) if t["task_id"] not in skip]
    # rework tasks sit in IN_PROGRESS with no running session; treat them as ready too
    for t in store.list(correlation_id=corr, state=S.IN_PROGRESS.value):
        if t["task_id"] not in skip and t["notes_json"].get("running") is None and store.deps_satisfied(t):
            ready.append(t)
    for t in store.list(correlation_id=corr, state=S.FAILED.value):
        if t["task_id"] in skip:
            continue
        if t["attempt"] < t["max_attempts"]:
            store.transition(t["task_id"], S.RETRY, reason="auto-retry")
            ready.append(store.get(t["task_id"]))
        else:
            tid = t["task_id"]
            leases = handoffs.leases if handoffs is not None else None
            # FAILED normally released the lease already. If one remains, keep it until the escalation packet lands.
            with leases.transition(tid) if leases is not None else nullcontext():
                store.transition(tid, S.ESCALATED, reason="max_attempts reached")
                emit("escalation.request", {"task_id": tid, "reason_code": "E-CONTRACT", "evidence": ["max_attempts reached"],
                                            "options": ["human review", "cancel", "re-plan"]})
                if handoffs is not None:
                    failed = [h["reason"] for h in store.history(tid) if h["to_state"] == S.FAILED.value]
                    handoffs.escalated(tid, reason="max_attempts reached"
                                       + (f"; last failure: {failed[-1]}" if failed and failed[-1] else ""))
                if leases is not None:
                    leases.settle(tid)
    return ready


def select_batch(store: TaskStore, ready: list[dict], max_parallel: int, leases, *, running: dict[str, int] | None = None,
                 claims: ClaimStore | None = None, skipped: dict[str, str] | None = None) -> tuple[list[tuple[dict, dict]], list[str]]:
    """(task, agent) pairs to dispatch now, and the tasks left waiting on a lease.

    `max_parallel` is the number of free global slots. The same agent fills more than one of them, up to that
    agent's manifest `max_parallel` less its `running` replicas (agent id -> sessions already in flight); a class that
    is at its ceiling does not consume a slot and does not block a later task of a different class. With `claims` (the
    scheduler's ClaimTable), a task whose notes.claims overlap a held exclusive claim waits, so conflicting tasks never
    run together; a malformed claim blocks the task rather than running it unclaimed. With leases on, each selected
    task must first hold its node (or run unleased when the substrate cannot answer). A denied, refused or busy claim
    leaves the task where it is (PLANNED/RETRY, or IN_PROGRESS for a rework) without taking a slot, for a later pass
    or run (LEASE-05).

    `skipped`, when given, receives {task_id: reason} for every task left out: `no-free-slot`,
    `class-ceiling:<agent>`, `claim-conflict:<holder task>`, `lease:<status>`."""
    batch, waiting, slots, replicas = [], [], max_parallel, dict(running or {})
    skip = skipped if skipped is not None else {}
    for t in ready:
        tid = t["task_id"]
        if slots <= 0:
            skip.setdefault(tid, "no-free-slot")
            continue
        try:
            agent = resolve_agent(t)
        except (KeyError, IndexError):
            store.transition(tid, S.BLOCKED, reason="no agent for capability")
            slots -= 1
            continue
        ceiling = max(1, int(agent.get("max_parallel") or 1))
        if replicas.get(agent["id"], 0) >= ceiling:
            skip[tid] = f"class-ceiling:{agent['id']}"
            continue  # a later task of another class can still take a free global slot
        if claims is not None:
            try:
                wanted = parse_claims(t["notes_json"])
            except ValueError as e:
                store.transition(tid, S.BLOCKED, reason=f"E-INPUT: invalid notes.claims: {e}"[:500])
                slots -= 1
                continue
            taken = claims.acquire(tid, wanted)
            if not taken.ok:
                skip[tid] = f"claim-conflict:{taken.conflicts[0].owner}"
                continue
        if leases is not None:
            claim = leases.acquire(t, agent["id"])
            if not claim.dispatch:
                if claims is not None:
                    claims.release(tid)
                holder = (claim.holder or {}).get("session_id")
                waiting.append(f"{tid}: {claim.status}" + (f" (held by {holder})" if holder else ""))
                skip[tid] = f"lease:{claim.status}"
                continue
        if not take_agent_slot(replicas, agent["id"], ceiling):
            if claims is not None:
                claims.release(tid)
            skip[tid] = f"class-ceiling:{agent['id']}"
            continue
        batch.append((t, agent))
        slots -= 1
    return batch, waiting


def execute_one(store_path, task, agent, args, ctx, repo, leases=None, handoffs=None):
    """Dispatch one task to its agent and apply the result. With leases on, the dispatch is watched by the lease keeper
    and the task's lease is settled from its final state, whatever happened on the way (LEASE-07): only after the
    session's and the gate script's process groups are gone. With handoffs on, the task's dependency packets are written
    first and its assignment carries the context rebuilt from them (HAND-01/02)."""
    try:
        return _execute_one(store_path, task, agent, args, ctx, repo, leases, handoffs)
    finally:
        if leases is not None:
            leases.settle(task["task_id"])


def _execute_one(store_path, task, agent, args, ctx, repo, leases, handoffs):
    store = TaskStore(store_path)  # sqlite: one connection per thread
    sdir = store.path.parent
    tid = task["task_id"]
    if task["state"] != S.IN_PROGRESS.value:
        store.transition(tid, S.CLAIMED, reason=f"awarded to {agent['id']}")
        store.transition(tid, S.IN_PROGRESS, reason="lease started")
    # A01 is the only writer of notes.dry_run: dry-run gate rows record and count only on flagged tasks,
    # and a real dispatch clears a flag left by an earlier dry-run
    # running_deadline is wall-clock time a later process can see. While it is in the future the task is live and
    # another run leaves it alone; once it passes with notes.running still set, the runner that held it is gone
    # (T-06-26) and recover_stranded fails it so the retry ladder can pick it up.
    # A gate task spends one task_timeout in the session and a second in run_gate_script. Its deadline covers both,
    # plus the grace. Every other task is one timeout plus the grace.
    started = time.time()
    timeouts = 2 if task["notes_json"].get("gate") else 1
    store.set_notes(tid, running=started,
                    running_deadline=started + timeouts * args.task_timeout + RUNNING_DEADLINE_GRACE_S,
                    dry_run=bool(args.dry_run), retry_after=None)
    # LEASE-06: the keeper watches the whole dispatch, from before the session spawns until its result is written, so
    # a lease lost (or a task moved on elsewhere) at any point stops the session or gate script and rejects the result
    watch = DispatchWatch()
    if leases is not None:
        leases.watch(tid, watch.stop)
    text, meta, raw_recorded = "", {}, False
    handle = current_run()  # the scheduler's RunHandle for this dispatch (None outside a scheduler)
    try:
        task = store.get(tid)
        workspace = repo
        if task["notes_json"].get("isolate") == "worktree" and not args.dry_run:
            # same-agent lanes edit disjoint paths; each gets a worktree so they do not share the index
            workspace = prepare_slice_worktree(repo, sdir / "worktrees" / tid, str(task["notes_json"].get("branch") or ""))
        context = ""
        if handoffs is not None:  # sent before the session starts, so its own context already holds them
            handoffs.dispatched(task, agent["id"])
            context = handoffs.context(tid, agent["id"])
        prompt = assignment_prompt(store, task, agent, workspace, lease_s=leases.lease_s(tid) if leases is not None else None,
                                   handoffs=context)
        (sdir / "assignments").mkdir(parents=True, exist_ok=True)
        (sdir / "assignments" / f"{tid}.a{task['attempt']}.md").write_text(prompt)
        if args.dry_run:
            if handle is not None:
                handle.started("dry-run")
            meta = dry_run_invocation(task, agent, repo, sdir, args)
            if task["notes_json"].get("gate"):
                run_gate_script(task, repo, sdir, dry_run=True)
            text = canned_result(task, agent)
        elif task["notes_json"].get("role") == "join":
            # the runner merges the lane branches; a model is not required for the join itself
            text, meta = join_result_text(repo, task), {"returncode": 0, "joined": True}
            if handle is not None:
                handle.started("join")
        else:
            watch.check(text, meta)  # lost before the session spawned: it never starts
            text, meta = run_agent_headless(agent, prompt, workspace, args, on_session=watch.attach)
        write_evidence(sdir, tid, task['attempt'], text or "")
        result, err = None, None
        try:
            result = validate_result(parse_result(text), task_id=tid)
        except SwarmError as e:
            err = e
        if meta.get("yield_error"):  # D-02: an error yield is E-CONTRACT, whatever json the text carries (WR-02)
            result, err = None, SwarmError(ErrorCode.E_CONTRACT, f"agent yielded an error: {meta['yield_error']}"[:500],
                                           task_id=tid)
        elif meta.get("is_error") or meta.get("returncode"):
            result, err = None, SwarmError(ErrorCode.E_CONTRACT, f"agent session reported error (rc={meta.get('returncode')})",
                                           task_id=tid)
        if err is not None and not meta.get("yield_error") and not args.dry_run \
                and (meta.get("is_error") or meta.get("returncode")):
            # backpressure (7): a failed session whose output reports a provider rate limit or overload is E-CAPACITY,
            # transient, and tells the scheduler to shrink its parallelism. A session that finished cleanly is never
            # read for the marker: a task about rate limiting would trip it.
            hit = detect_rate_limit(f"{meta.get('stderr') or ''}\n{(text or '')[-2000:]}")
            if hit is not None:
                if handle is not None:
                    handle.rate_limited = hit
                result, err = None, SwarmError(ErrorCode.E_CAPACITY, "provider rate limit or overload"
                                               + (f" (retry after {hit:g}s)" if hit else ""), task_id=tid)
        if meta.get("extension_error"):  # T-06-06: an unguarded session's result is never applied, nor its gate recorded
            result, err = None, SwarmError(ErrorCode.E_DEP, "omp ran the session without the swarm guard: "
                                           f"{meta['extension_error']}"[:500], task_id=tid)
        # CR-04: record a gate only for a session that completed and asked to finish it — a crashed, errored or
        # BLOCKED/FAILED gate session gets no script run, so its targets keep the gate absent
        if (not args.dry_run and task["notes_json"].get("gate") and result is not None
                and result["state"] == S.IN_REVIEW.value and not meta.get("is_error") and not meta.get("returncode")):
            # WR-12: the key-holding runner, not the agent, records this gate — once per dispatch, still leased
            run_gate_script(task, repo, sdir, dry_run=False, timeout=args.task_timeout,
                            per_target_findings=gate_findings_file(task, text, sdir, ctx.emit), on_process=watch.attach)
        ctx.emit("task.result.raw", {"task_id": tid, "agent": agent["id"], "meta": meta})
        store.set_notes(tid, meta=meta)
        raw_recorded = True
        with watch.applying(text, meta):
            if result is None:
                outcome = reject(store, tid, attempt=task["attempt"], reason=str(err), mode="headless", emit=ctx.emit)
            else:
                outcome = apply_result(store, task, agent_id=agent["id"], result=result, meta=meta, emit=ctx.emit,
                                       mode="headless")
    except AgentTimeout as e:
        # WR-04: keep what the session wrote before its process group was killed
        write_evidence(sdir, tid, task['attempt'], e.text or "")
        ctx.emit("task.result.raw", {"task_id": tid, "agent": agent["id"], "meta": e.meta})
        store.set_notes(tid, meta=e.meta)
        store.transition(tid, S.FAILED, reason="E-TIMEOUT: task_timeout exceeded")
        outcome = "FAILED"
    except LeaseStopped as e:
        # LEASE-06: the substrate says another session (or nobody) holds the node, or the task moved on elsewhere, so
        # this result is never applied, whether the stop came during the session, the gate script or before the write
        text, meta = e.text or text, {**meta, **e.meta}
        write_evidence(sdir, tid, task['attempt'], text or "")
        if not raw_recorded:
            ctx.emit("task.result.raw", {"task_id": tid, "agent": agent["id"], "meta": meta})
        store.set_notes(tid, meta=meta)
        state = store.get(tid)["state"]
        if state in substrate_lease.LET_GO:  # moved on in another process (CANCELLED): that transition stands
            outcome = state
        else:
            store.transition(tid, S.FAILED, reason=e.reason[:500])
            outcome = "FAILED"
    except subprocess.TimeoutExpired:
        store.transition(tid, S.FAILED, reason="E-TIMEOUT: task_timeout exceeded")
        outcome = "FAILED"
    except SwarmError as e:
        store.transition(tid, S.FAILED, reason=str(e)[:500])
        outcome = "FAILED"
    finally:
        if leases is not None:
            leases.watch(tid, None)
        store.set_notes(tid, running=None, running_deadline=None)
    return tid, agent["id"], outcome


def lease_bridge(args, store_path: Path, corr: str, repo: Path, ctx):
    """The run's substrate leases (ADR 0001 S2), or None: --dry-run never touches the substrate, and off means off."""
    if args.dry_run or not substrate_client.enabled():
        return None
    try:
        return substrate_lease.LeaseBridge(store_path, corr, root=repo, emit=ctx.emit)
    except ValueError as e:  # SWARM_REPLICA or SWARM_LEASE_TTL_S: refused at start, not at every claim
        raise SwarmError(ErrorCode.E_INPUT, str(e)) from e


def handoff_bridge(store_path: Path, corr: str, repo: Path, ctx, leases):
    """The run's handoff packets (ADR 0001 S2 criteria 7-9), on exactly when its leases are: never on --dry-run, off
    means off."""
    if leases is None:
        return None
    return substrate_handoff.HandoffBridge(store_path, corr, root=repo, emit=ctx.emit, env=leases.env, leases=leases)


def _running_mark(notes: dict, key: str):
    """A wall-clock mark written by set_notes, or None. Bool is an int and is never a timestamp."""
    value = notes.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _expired_running(notes: dict, now: float, stale_s: float) -> tuple[float, float | None] | None:
    """`(running, deadline)` when that mark is stranded at `now`, else None.

    A `running_deadline` still in the future is a live session. A legacy row with `running` and no deadline is
    stranded only once the mark is `stale_s` old. A cleared running mark is not stranded."""
    running = _running_mark(notes, "running")
    if running is None:
        return None
    deadline = _running_mark(notes, "running_deadline")
    if deadline is not None:
        if deadline > now:
            return None
    elif now - running < stale_s:
        return None
    return running, deadline


def recover_stranded(store: TaskStore, corr: str, *, now: float | None = None, stale_s: float = STRANDED_STALE_S,
                     emit=None) -> list[str]:
    """Fail IN_PROGRESS tasks whose runner died before it could clear notes.running (T-06-26).

    A task with `running_deadline` still in the future is a live session and is left alone. A legacy row that has
    `running` but no deadline is stranded only once that mark is `stale_s` old (6 h), so a long session started by a
    runner from before this field existed is not failed on the next kick. Anything else in IN_PROGRESS — rework, which
    has no running mark — is not this function's business. The FAILED row is retryable: dispatchable's ladder applies
    on this same run when attempts remain.

    The list is a candidate set. Fail and clear `running` / `running_deadline` in one transaction, and only after a
    re-read still shows that same IN_PROGRESS attempt with the deadline (or the legacy stale mark) still expired.
    A peer that already moved the row — FAILED, RETRY, a new attempt, a future deadline, a cleared running mark — is
    skipped. A SwarmError from the transition skips that row and does not abort the run."""
    now = time.time() if now is None else now
    recovered: list[str] = []
    for task in store.list(correlation_id=corr, state=S.IN_PROGRESS.value):
        if _expired_running(task["notes_json"], now, stale_s) is None:
            continue
        tid = task["task_id"]
        attempt = task["attempt"]
        try:
            with store.transaction():
                current = store.get(tid)
                expired = _expired_running(current["notes_json"], now, stale_s)
                if current["state"] != S.IN_PROGRESS.value or current["attempt"] != attempt or expired is None:
                    continue
                running, deadline = expired
                store.transition(tid, S.FAILED,
                                 reason="E-TIMEOUT: stranded, the runner left it IN_PROGRESS past its deadline")
                store.set_notes(tid, running=None, running_deadline=None)
        except SwarmError:
            continue
        if emit is not None:
            emit("task.stranded", {"task_id": tid, "running": running, "running_deadline": deadline})
        recovered.append(tid)
    return recovered


def _extension_failed(store: TaskStore, tid: str) -> bool:
    meta = store.get(tid)["notes_json"].get("meta") or {}
    return bool(isinstance(meta, dict) and meta.get("extension_error"))


# ---------------------------------------------------------------------------------------------------------------------
# Scheduler (DESIGN D5, 4.4-4.6, 7): a persistent pool refilled as sessions finish, one Telemetry per run, a monitor
# thread for heartbeats and stall detection, a cooperative cancel flag, AIMD backpressure and claim-aware batches.
# ---------------------------------------------------------------------------------------------------------------------
_LOG = logging.getLogger("swarm.run")
PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SCHEDULERS = ("continuous", "rounds")
CANCEL_GRACE_DEFAULT_S = 10.0
SHUTDOWN_BUDGET_DEFAULT_S = 15.0  # the supervisor's SIGTERM -> SIGKILL window (hooks/autonomous_run.py default)
KILL_GRACE_S = 5.0  # SIGTERM -> SIGKILL between the runner and its session groups at the end of a cancel
KILL_SETTLE_S = 3.0  # what SIGKILL, the reaping of the sessions and the final records need after the kill window
WAIT_TICK_S = 0.25  # how often the loop looks at the cancel flag and the clock while sessions run
INTERNAL_FAILURE_LIMIT = 3  # a task whose worker raised this often is left alone for the rest of the run
SKIPPED_REPORT_MAX = 64  # entries of `skipped` in one scheduler tick


def _env_seconds(name: str, default: float) -> float:
    """A non-negative number of seconds from the environment; a malformed value is refused, naming the variable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not value >= 0 or value == float("inf"):
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be a number of seconds >= 0, got {raw!r}")
    return value


FAIL_STREAK_DEFAULT = 3  # SWARM_FAIL_STREAK: consecutive failures of distinct tasks that trip the circuit (0 = off)


def _env_count(name: str, default: int) -> int:
    """A non-negative whole number from the environment; a malformed value is refused, naming the variable."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = -1
    if value < 0:
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be a whole number >= 0, got {raw!r}")
    return value


def scheduler_mode() -> str:
    """`continuous` (default) or `rounds`, the documented per-round-barrier fallback (SWARM_SCHEDULER)."""
    mode = (os.environ.get("SWARM_SCHEDULER") or "").strip().lower() or "continuous"
    if mode not in SCHEDULERS:
        raise SwarmError(ErrorCode.E_INPUT, f"SWARM_SCHEDULER must be one of {list(SCHEDULERS)}, got {mode!r}")
    return mode


def resolve_agent(task: dict) -> dict:
    """The manifest agent a task is dispatched to; KeyError/IndexError when none matches."""
    return get_agent(task["agent_id"]) if task["agent_id"] else by_capability(task["capability"])[0]


def order_ready(ready: list[dict]) -> list[dict]:
    """Dispatch order: priority (P0 first), then DAG depth, then creation time. Stable for ties."""
    return sorted(ready, key=lambda t: (PRIORITY_RANK.get(t.get("priority"), PRIORITY_RANK["P2"]),
                                        t.get("dag_depth") or 0, t.get("created_at") or 0))


def _safe(fn, *args, **kwargs):
    """Telemetry is a display projection: a failure in it is logged and never breaks a dispatch."""
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        _LOG.warning("telemetry call %s failed", getattr(fn, "__name__", fn), exc_info=True)
        return None


def _error_code(reason: str | None) -> str | None:
    match = re.match(r"\s*(E-[A-Z]+)\b", reason or "")
    return match.group(1) if match else None


def last_failure(store: TaskStore, tid: str) -> str:
    """The reason of the task's most recent FAILED transition, '' when it never failed."""
    reasons = [h["reason"] or "" for h in store.history(tid) if h["to_state"] == S.FAILED.value]
    return reasons[-1] if reasons else ""


# ---- cooperative cancel (4): a flag the sessions can see, a grace, then the process-group kill ---------------------
class CancelRequest:
    """One run's cancel state. The first request (a signal or the swarm timeout) fixes the reason; a signal that
    arrives while a cancel is under way means `force`: skip the grace and kill now."""

    def __init__(self) -> None:
        self.event = threading.Event()
        self.force = threading.Event()
        self.reason: str | None = None
        self.signum: int | None = None
        self.at: float | None = None  # time.monotonic() of the first request: the shutdown budget runs from here
        self._lock = threading.Lock()

    def request(self, reason: str, signum: int | None = None) -> bool:
        with self._lock:
            if self.event.is_set():
                if signum is not None:
                    self.force.set()
                return False
            self.reason, self.signum, self.at = reason, signum, time.monotonic()
            self.event.set()
            return True




def write_cancel_flag(sdir: Path, corr: str, reason: str, grace_s: float) -> Path:
    """`<swarm_dir>/cancel/<correlation_id>` = {requested_wall, reason, grace_s}, atomic. Sessions poll it (the shared
    cancel-check tool); the runner waits `grace_s` for them to finish before it kills their process groups."""
    path = cancel_flag_path(sdir, corr)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps({"requested_wall": time.time(), "reason": reason, "grace_s": grace_s}))
    return path


def clear_cancel_flag(sdir: Path, corr: str) -> None:
    cancel_flag_path(sdir, corr).unlink(missing_ok=True)


# ---- one run's handle: what the worker thread reports to Telemetry and back to the scheduler -------------------------
class RunHandle:
    """The scheduler's view of one dispatch. The worker (and the session's stdout pump) report progress through it;
    `rate_limited` carries a provider rate-limit hit (seconds to wait, 0.0 for a bare marker) back to the loop."""

    def __init__(self, tel: Telemetry, run_id: str, task_id: str, agent: str, sid: str, budget_s: float):
        self.tel, self.run_id, self.task_id, self.agent, self.sid = tel, run_id, task_id, agent, sid
        self.budget_s = budget_s
        self.streams = False  # the runtime reports tool activity (omp --mode json); the others report elapsed only
        self.rate_limited: float | None = None
        self.submitted_at = time.monotonic()  # AIMD: a hit from a run older than the last decrease is the same burst
        self._started = threading.Event()
        self._acquired = False
        self._lock = threading.Lock()

    @property
    def is_started(self) -> bool:
        return self._started.is_set()

    def slot_acquired(self) -> None:
        """queued -> starting once; later calls are no-ops."""
        with self._lock:
            if self._acquired or self._started.is_set():
                return
            self._acquired = True
        _safe(self.tel.transition, self.run_id, RunEvent.SLOT_ACQUIRED)

    def started(self, runtime: str | None) -> None:
        """queued/starting -> running once; later calls are no-ops."""
        with self._lock:
            if self._started.is_set():
                return
            self._started.set()
        _safe(self.tel.run_started, self.run_id, sid=self.sid or None, runtime=runtime,
              budget_max_wall_s=self.budget_s)

    def activity(self, **fields) -> None:
        _safe(self.tel.activity, self.run_id, **fields)


_RUN_LOCAL = threading.local()


def current_run() -> RunHandle | None:
    """The RunHandle of the dispatch running on this thread, if a scheduler set one."""
    return getattr(_RUN_LOCAL, "handle", None)


@contextmanager
def run_scope(handle: RunHandle | None):
    _RUN_LOCAL.handle = handle
    try:
        yield
    finally:
        _RUN_LOCAL.handle = None


class OmpActivity:
    """Incremental reader of an `omp -p --mode json` stdout stream (JSONL): `feed(text)` as it arrives, one `report`
    call per tool start or turn end, plus a throttled bare ping for any other event so a talking session is never
    `stalled`. Parsing here never replaces `omp_stream_text`, which still reads the whole stream at the end."""

    MAX_LINE = 1 << 20
    PING_EVERY_S = 0.5
    _ARG_KEYS = ("path", "file", "command", "pattern", "query", "url", "name")

    def __init__(self, report) -> None:
        self._report = report
        self._buf = ""
        self._last_ping = 0.0
        self.tool_calls = 0
        self.turns = 0
        self.tokens = 0

    def feed(self, chunk: str) -> None:
        *lines, self._buf = (self._buf + chunk).split("\n")
        if len(self._buf) > self.MAX_LINE:
            self._buf = ""  # a pathological line without a newline: drop it rather than grow
        for line in lines:
            self._line(line.strip())

    def _line(self, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        if not isinstance(ev, dict):
            return
        kind = ev.get("type")
        if kind == "tool_execution_start":
            name = str(ev.get("toolName") or "tool")
            self.tool_calls += 1
            self._report(tool=name, text=self._describe(name, ev), tool_calls=self.tool_calls)
        elif kind == "turn_end":
            self.turns += 1
            usage = (ev.get("message") or {}).get("usage") if isinstance(ev.get("message"), dict) else None
            if isinstance(usage, dict):
                total = usage.get("totalTokens")
                if not isinstance(total, (int, float)) or isinstance(total, bool):
                    total = sum(v for k in ("input", "output") if isinstance(v := usage.get(k), (int, float)))
                self.tokens += int(total)
            self._report(turns=self.turns, tokens=self.tokens)
        else:
            now = time.monotonic()
            if now - self._last_ping >= self.PING_EVERY_S:
                self._last_ping = now
                self._report()

    def _describe(self, name: str, ev: dict) -> str:
        intent = ev.get("intent")
        if isinstance(intent, str) and intent.strip():
            return f"{name}: {intent.strip()}"
        args = ev.get("args")
        if isinstance(args, dict):
            for key in self._ARG_KEYS:
                if isinstance(args.get(key), str) and args[key].strip():
                    return f"{name} {args[key].strip()}"
        return name


class RunBook:
    """Telemetry runs of one scheduler: maps (task, attempt) to a run id and registers each exactly once. A task that
    waits on dependencies is `waiting`, a ready one `queued`; the run ends through Telemetry.finish."""

    def __init__(self, tel: Telemetry) -> None:
        self.tel = tel
        self._state: dict[str, str] = {}  # run_id -> waiting | queued | open | done
        self._labels: dict[str, str] = {}

    @staticmethod
    def run_id(task: dict) -> tuple[str, int]:
        """`<task>.a<attempt>`; a rework of the same attempt (CHANGES_REQUESTED -> IN_PROGRESS) is `.r<n>`. A task not
        yet claimed will be claimed as attempt + 1."""
        tid, state = task["task_id"], task["state"]
        if state == S.IN_PROGRESS.value:
            attempt = max(int(task["attempt"]), 1)
            loops = int(task.get("rework_loops") or 0)
            return (f"{tid}.a{attempt}.r{loops}" if loops else f"{tid}.a{attempt}"), attempt
        attempt = int(task["attempt"]) + 1
        return f"{tid}.a{attempt}", attempt

    def label(self, task: dict) -> str:
        """The agent slug shown for a task ('?' when no agent matches)."""
        key = task["agent_id"] or task["capability"]
        if key not in self._labels:
            try:
                self._labels[key] = resolve_agent(task)["slug"]
            except (KeyError, IndexError):
                self._labels[key] = str(key or "?")
        return self._labels[key]

    def seen(self, task: dict, *, waiting: bool, queue_depth: int) -> str:
        run_id, attempt = self.run_id(task)
        state = self._state.get(run_id)
        if state is None:
            _safe(self.tel.run_queued, run_id, self.label(task), attempt=attempt, priority=task["priority"],
                  deps=task["depends_on"], queue_depth=queue_depth, task_id=task["task_id"], waiting_on_deps=waiting)
            self._state[run_id] = "waiting" if waiting else "queued"
        elif state == "waiting" and not waiting:
            _safe(self.tel.transition, run_id, RunEvent.DEPS_SATISFIED)
            self._state[run_id] = "queued"
        return run_id

    def opened(self, run_id: str) -> None:
        self._state[run_id] = "open"

    def done(self, run_id: str) -> None:
        self._state[run_id] = "done"

    def count(self, state: str) -> int:
        return sum(1 for s in self._state.values() if s == state)

    def unsettled(self) -> list[str]:
        return [r for r, s in self._state.items() if s != "done"]


class Done(NamedTuple):
    """What a worker hands back: never raises, so one task's fault stays that task's. `error` is set only when the
    worker itself failed (an internal fault), not when the agent did."""
    task_id: str
    agent_id: str
    outcome: str
    error: str | None = None


class Inflight(NamedTuple):
    task: dict
    agent: dict
    sid: str
    run: RunHandle
    future: Future


class Pass(NamedTuple):
    """One scheduling pass: dispatchable tasks seen, tasks submitted, lease waits, earliest backoff expiry (wall)."""
    ready: int
    submitted: int
    waiting: list[str]
    backoff_wall: float | None


class WorkerPool:
    """`max_workers` daemon threads running submitted callables: the part of ThreadPoolExecutor the scheduler uses.
    Daemon on purpose. ThreadPoolExecutor's threads are joined when the interpreter exits, so a worker still stuck in a
    session after the kill ("abandoned") would keep the finished runner alive; these never do."""

    def __init__(self, max_workers: int, name: str) -> None:
        self._max = max(1, int(max_workers))
        self._name = name
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._threads: list[threading.Thread] = []
        self._idle = 0  # workers that finished a call and have not been handed another yet
        self._closed = False
        self._lock = threading.Lock()

    def submit(self, fn, *args) -> Future:
        future: Future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("worker pool is shut down")
            self._queue.put((future, fn, args))
            if self._idle > 0:
                self._idle -= 1
            elif len(self._threads) < self._max:
                thread = threading.Thread(target=self._work, name=f"{self._name}-{len(self._threads)}", daemon=True)
                self._threads.append(thread)
                thread.start()
        return future

    def _work(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, fn, args = item
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args))
                except BaseException as exc:  # noqa: BLE001 - the caller reads it from the future
                    future.set_exception(exc)
            with self._lock:
                self._idle += 1

    def shutdown(self, wait: bool = True) -> None:
        """Stop the workers once the queue is empty. `wait=False` drops calls not yet started and returns at once."""
        with self._lock:
            self._closed = True
            threads = list(self._threads)
            if not wait:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if item is not None:
                        item[0].cancel()
            for _ in threads:
                self._queue.put(None)
        if wait:
            for thread in threads:
                thread.join()


class Scheduler:
    """Runs a plan's tasks on a persistent pool. `barrier=False` (continuous, default): reconcile + dispatchable +
    select_batch run after every completion and refill free slots at once, so a slow task never holds back newly ready
    work. `barrier=True` (SWARM_SCHEDULER=rounds): the old per-round barrier, kept as the documented fallback.

    The global cap is the lifecycle manager's; the effective parallelism is that cap lowered by the AIMD limiter: a
    provider rate limit halves it, and so does a streak of `fail_streak` consecutive failures of distinct tasks (the
    circuit). One failed task never lowers it. ErrorHandler is kept for its spec/status and its other callers only."""

    def __init__(self, args, ctx, *, store: TaskStore, store_path: Path, repo: Path, corr: str, mgr, tel: Telemetry,
                 leases, handoffs, cancel: CancelRequest, barrier: bool, sdir: Path,
                 cancel_grace: float = CANCEL_GRACE_DEFAULT_S,
                 shutdown_budget: float = SHUTDOWN_BUDGET_DEFAULT_S,
                 fail_streak: int = FAIL_STREAK_DEFAULT) -> None:
        self.args, self.ctx, self.store, self.store_path = args, ctx, store, store_path
        self.repo, self.corr, self.mgr, self.tel, self.sdir = repo, corr, mgr, tel, sdir
        self.leases, self.handoffs, self.cancel, self.barrier = leases, handoffs, cancel, barrier
        self.limiter = AimdLimiter(mgr.max_concurrent)
        self.claims = make_claim_table()
        self.error_handler = ErrorHandler()
        self.book = RunBook(tel)
        self.inflight: dict[str, Inflight] = {}
        self.log: list[str] = []
        self.rounds = 0  # scheduling passes
        self.dispatched = 0
        self.closed = False  # no further passes: the run only drains what is in flight
        self.extension_down = False
        self.once = bool(getattr(args, "once", False))
        self.max_rounds = max(1, int(getattr(args, "max_rounds", 50)))
        # continuous mode keeps `--max-rounds` meaningful: a round was a batch of at most max_parallel tasks
        self.budget = self.max_rounds * mgr.max_concurrent
        self.timeout_s = float(getattr(args, "swarm_timeout", 0) or 0)
        self.cancel_grace, self.shutdown_budget = cancel_grace, shutdown_budget
        self._last_decrease = 0.0  # time.monotonic() of the limiter's last rate-limit decrease (AIMD: once per burst)
        self.fail_streak = max(0, int(fail_streak))  # 0: the circuit is off
        self._streak: set[str] = set()  # distinct tasks that failed since the last success
        self._streak_tripped = False  # the current streak already halved the parallelism
        self.kill_reason = "E-TIMEOUT: runner shutdown"
        self._wake_wall: float | None = None
        self._internal: dict[str, int] = {}
        self.t0 = time.monotonic()
        self.pool: WorkerPool | None = None
        self._monitor_stop = threading.Event()
        self._monitor: threading.Thread | None = None

    # ---- threads -----------------------------------------------------------------------------------------------
    def start(self) -> None:
        self.t0 = time.monotonic()
        self.pool = WorkerPool(self.mgr.max_concurrent, "swarm-run")
        self._monitor = threading.Thread(target=self._watch, name="swarm-monitor", daemon=True)
        self._monitor.start()

    def close(self) -> None:
        """Stop the monitor, end the pool, and give every run that never finished a terminal record."""
        self._monitor_stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=5)
        if self.pool is not None:
            self.pool.shutdown(wait=not self.inflight)
        for item in list(self.inflight.values()):  # abandoned after the kill: the lifecycle slot is not theirs any more
            if item.sid:
                self.mgr.release(item.sid)
        reason = self.cancel.reason
        outcome = "timeout" if reason == "swarm-timeout" else "cancelled"
        why = f"scheduler stopped ({reason})" if reason else "scheduler stopped before the run was dispatched"
        for run_id in self.book.unsettled():
            _safe(self.tel.finish, run_id, outcome, error=why)
            self.book.done(run_id)
        _safe(self.tel.flush_live, True)

    def abort(self) -> None:
        """An exception left the loop (or the process is being interrupted without a handler): end every session."""
        kill_sessions()

    def _watch(self) -> None:
        """Monitor thread: live.json at 1 Hz, heartbeats every SWARM_HEARTBEAT_S, stall detection, the swarm timeout.
        A runtime that streams no activity (claude, grok) is pinged while its process lives, so only a silent omp
        stream can read as stalled."""
        heartbeat_s = heartbeat_default()
        ping_s = max(0.05, self.tel.stall_after_s / 3.0)
        tick = max(0.05, min(WAIT_TICK_S, heartbeat_s / 2.0))
        last_beat = last_ping = time.monotonic()
        while not self._monitor_stop.wait(tick):
            now = time.monotonic()
            try:
                handles = [i.run for i in list(self.inflight.values()) if i.run.is_started]
                if now - last_ping >= ping_s:
                    last_ping = now
                    for handle in handles:
                        if not handle.streams:
                            handle.activity()
                self.tel.stalled_runs()
                if now - last_beat >= heartbeat_s:
                    last_beat = now
                    for handle in handles:
                        self.tel.heartbeat(handle.run_id)
                self.tel.flush_live()
                if self.timeout_s > 0 and now - self.t0 >= self.timeout_s:
                    self.cancel.request("swarm-timeout")
            except Exception:  # noqa: BLE001 - the monitor must outlive a telemetry fault
                _LOG.warning("swarm monitor tick failed", exc_info=True)

    # ---- parallelism ---------------------------------------------------------------------------------------------
    def effective_parallel(self) -> int:
        cap = self.mgr.max_concurrent
        return max(1, min(cap, self.limiter.current(), self.error_handler.effective_parallel(cap)))

    # ---- one scheduling pass -----------------------------------------------------------------------------------
    def _schedule(self) -> Pass:
        self.rounds += 1
        corr, store, ctx = self.corr, self.store, self.ctx
        self.log += reconcile(store, corr, ctx.emit, leases=self.leases, handoffs=self.handoffs)
        log_phase("reconcile", {"round": self.rounds}, source="A01@swarm_run", correlation_id=corr, root=self.repo)
        if self.handoffs is not None:
            self.handoffs.flush()
        ready = dispatchable(store, corr, ctx.emit, handoffs=self.handoffs, skip=frozenset(self.inflight))
        now = time.time()
        skipped: dict[str, str] = {}
        eligible: list[dict] = []
        backoff_wall: float | None = None
        for t in order_ready(ready):
            until = t["notes_json"].get("retry_after")
            if isinstance(until, (int, float)) and not isinstance(until, bool) and until > now:
                skipped[t["task_id"]] = f"backoff:{until - now:.1f}s"
                backoff_wall = until if backoff_wall is None else min(backoff_wall, until)
            elif self._internal.get(t["task_id"], 0) >= INTERNAL_FAILURE_LIMIT:
                skipped[t["task_id"]] = "internal-failure"
            else:
                eligible.append(t)
        # telemetry: every task of the plan has a run; those past their dependencies are queued
        tasks = store.list(correlation_id=corr)
        ready_ids = {t["task_id"] for t in ready}
        for t in tasks:
            if t["task_id"] in self.inflight:
                continue
            if t["task_id"] in ready_ids:
                self.book.seen(t, waiting=False, queue_depth=len(ready))
            elif t["state"] in (S.CREATED.value, S.VALIDATED.value, S.PLANNED.value, S.RETRY.value):
                self.book.seen(t, waiting=True, queue_depth=len(ready))
        eff = self.effective_parallel()
        free = max(0, eff - len(self.inflight))
        if not self.barrier:  # `--max-rounds` is a total: never start more than what is left of it in one pass
            free = min(free, max(0, self.budget - self.dispatched))
        per_class = Counter(i.agent["id"] for i in self.inflight.values())
        batch, waiting = select_batch(store, eligible, free, self.leases, running=per_class, claims=self.claims,
                                      skipped=skipped)
        self.error_handler.detect_parallel(len(batch), self.mgr.max_concurrent)
        for t, a in batch:
            self._submit(t, a)
        self._tick(len(ready), len(batch), eff, free, skipped)
        self._wake_wall = backoff_wall
        return Pass(len(ready), len(batch), waiting, backoff_wall)

    def _tick(self, ready: int, submitted: int, eff: int, free: int, skipped: dict) -> None:
        per_agent: dict[str, dict] = {}
        for item in self.inflight.values():
            slug = item.agent["slug"]
            row = per_agent.setdefault(slug, {"running": 0, "limit": max(1, int(item.agent.get("max_parallel") or 1))})
            row["running"] += 1
        reported = dict(list(skipped.items())[:SKIPPED_REPORT_MAX])
        _safe(self.tel.scheduler_tick, round=self.rounds, ready=ready, running=len(self.inflight),
              queued=max(0, ready - submitted), waiting=self.book.count("waiting"),
              free_slots=max(0, free - submitted), max_parallel=self.mgr.max_concurrent, effective_parallel=eff,
              per_agent=per_agent, skipped=reported)

    def _submit(self, task: dict, agent: dict) -> None:
        tid = task["task_id"]
        corr, ctx, dry = self.corr, self.ctx, self.args.dry_run
        run_id = self.book.seen(task, waiting=False, queue_depth=0)
        self.book.opened(run_id)
        run = RunHandle(self.tel, run_id, tid, agent["slug"], "",
                        float(task["budget"].get("max_wall_s") or self.args.task_timeout))
        try:
            run.sid = sid = self.mgr.new_subagent_id(tid, root=ctx.root, correlation_id=corr)
            # immediate emit calls for spawn events, subagent IDs, real-time status to OMP
            stream_subagent_status(sid, "dispatching.immediate", task_id=tid, phase="concurrent", wave="wave1",
                                   corr=corr, root=ctx.root, dry_run=dry)
            emit_gsd_omp("launch.immediate", subagent_id=sid, task_id=tid, wave1="gsd-autonomous", corr=corr,
                         root=ctx.root, dry_run=dry)
            emit_gsd_omp("consolidated", subagent_id=sid, task_id=tid, audit="spawn", wave="wave1", corr=corr,
                         root=ctx.root, dry_run=dry)
            future = self.pool.submit(self._worker, task, agent, sid, run)
        except Exception as exc:  # noqa: BLE001 - a dispatch that cannot even be queued fails that task only
            future = Future()
            future.set_result(self._internal_failure(task, agent, exc))
            if run.sid:
                self.mgr.release(run.sid)
        self.inflight[tid] = Inflight(task, agent, run.sid, run, future)
        self.dispatched += 1

    # ---- the worker (runs on a pool thread) ------------------------------------------------------------------------
    def _worker(self, task: dict, agent: dict, sid: str, run: RunHandle) -> Done:
        tid = task["task_id"]
        try:
            if self.cancel.event.is_set():  # cancelled between the pass and the pool picking this up: never starts
                if self.leases is not None:
                    self.leases.settle(tid)
                return Done(tid, agent["id"], "SKIPPED")
            self.mgr.acquire(sid)
            run.slot_acquired()
            with run_scope(run):
                tid, aid, outcome = execute_one(self.store_path, task, agent, self.args, self.ctx, self.repo,
                                                self.leases, self.handoffs)
            return Done(tid, aid, outcome)
        except Exception as exc:  # noqa: BLE001 - worker isolation (5): this task fails, its siblings run on
            return self._internal_failure(task, agent, exc)
        finally:
            self.mgr.release(sid)  # every exit path, SKIPPED and a failed acquire included; release is idempotent

    def _internal_failure(self, task: dict, agent: dict, exc: Exception) -> Done:
        """A worker exception becomes a structured failure of that one task: E-INTERNAL (or the SwarmError's own code),
        recorded as the task's FAILED reason (a task not yet claimed is claimed first, so the failure counts as an
        attempt and the retry ladder ends in ESCALATED), and counted so a persistent fault cannot spin."""
        tid = task["task_id"]
        reason = str(exc)[:500] if isinstance(exc, SwarmError) else \
            f"{ErrorCode.E_INTERNAL.value}: {type(exc).__name__}: {exc}"[:500]
        _LOG.warning("task %s worker failed: %s", tid, reason, exc_info=True)
        self._internal[tid] = self._internal.get(tid, 0) + 1
        outcome = S.FAILED.value
        try:
            store = TaskStore(self.store_path)
            with store.transaction():
                state = store.get(tid)["state"]
                if state in (S.PLANNED.value, S.RETRY.value, S.BLOCKED.value):
                    store.transition(tid, S.CLAIMED, reason=f"claimed to record: {reason}"[:500])
                    state = S.CLAIMED.value
                if state in (S.CLAIMED.value, S.IN_PROGRESS.value):
                    store.transition(tid, S.FAILED, reason=reason)
                else:
                    outcome = state  # the row moved on in another process: its state stands
                store.set_notes(tid, running=None, running_deadline=None)
        except Exception:  # noqa: BLE001 - the store itself is the fault: the failure is reported, not recorded
            _LOG.warning("task %s failure could not be recorded", tid, exc_info=True)
        self.ctx.emit("task.result.rejected", {"task_id": tid, "mode": "headless", "reason": reason})
        if self.leases is not None:
            try:
                self.leases.settle(tid)
            except Exception:  # noqa: BLE001 - settling is best effort here; the keeper's own lease expiry backs it
                _LOG.warning("lease for %s could not be settled", tid, exc_info=True)
        return Done(tid, agent["id"], outcome, reason)

    # ---- completions (main thread) -------------------------------------------------------------------------------
    def _run_outcome(self, done: Done, reason: str) -> str:
        """The Telemetry outcome of a finished dispatch: ok | error | timeout | cancelled. A run that fails once a cancel
        was requested (or was killed by it) is the cancel's, not an error of its own."""
        if done.outcome in ("SKIPPED", S.CANCELLED.value):
            return "cancelled"
        if done.error is not None or done.outcome in (S.FAILED.value, S.BLOCKED.value, S.ESCALATED.value):
            if self.cancel.event.is_set() or "runner shutdown" in reason or "swarm timeout" in reason:
                return "timeout" if self.cancel.reason == "swarm-timeout" else "cancelled"
            return "timeout" if reason.startswith("E-TIMEOUT") else "error"
        return "ok"

    def _complete(self, future: Future) -> None:
        item = next(i for i in self.inflight.values() if i.future is future)
        tid, run = item.task["task_id"], item.run
        del self.inflight[tid]
        if item.sid:
            self.mgr.release(item.sid)  # the worker's own release is the normal path; this one is idempotent insurance
        self.claims.release(tid)
        crashed = future.exception()
        if crashed is not None:  # a BaseException that escaped the worker's own guard: still this task's failure only
            done = self._internal_failure(item.task, item.agent, crashed if isinstance(crashed, Exception)
                                          else RuntimeError(repr(crashed)))
        else:
            done = future.result()
        aid, outcome = done.agent_id, done.outcome
        failed = done.error is not None or outcome == S.FAILED.value
        reason = (done.error or last_failure(self.store, tid)) if failed else ""
        verdict = self._run_outcome(done, reason)
        if verdict == "ok":
            run.started(None)  # a stubbed or in-process session that never reported a spawn still ran
        else:
            run.slot_acquired()  # a run that failed before its slot was taken still goes queued -> starting -> end
        message = reason or (f"task ended {outcome}" if verdict == "error" else "")
        _safe(self.tel.finish, run.run_id, verdict, error_code=_error_code(message) if message else None,
              error=message or None)
        self.book.done(run.run_id)
        self.log.append(f"round {self.rounds}: {tid} [{aid}] sid={item.sid} → {outcome}")
        print(self.log[-1], file=sys.stderr, flush=True)
        stream_subagent_status(item.sid, outcome.lower(), task_id=tid, phase="result", wave="wave1", corr=self.corr,
                               root=self.ctx.root, dry_run=self.args.dry_run)
        emit_gsd_omp("result.immediate", subagent_id=item.sid, task_id=tid, outcome=outcome, wave1="gsd-autonomous",
                     corr=self.corr, root=self.ctx.root, dry_run=self.args.dry_run)
        emit_gsd_omp("consolidated", subagent_id=item.sid, task_id=tid, audit="result", outcome=outcome,
                     corr=self.corr, root=self.ctx.root, dry_run=self.args.dry_run)
        self._backpressure(run, failed, item.task, reason)
        if failed and not self.cancel.event.is_set():
            self._schedule_retry(item.task, reason, run.rate_limited)
        if _extension_failed(self.store, tid) and not self.extension_down:
            self.extension_down = True
            self.closed = True
            self.log.append("stopped: omp extension failed to load; no further sessions this run")

    def _backpressure(self, run: RunHandle, failed: bool, task: dict, reason: str) -> None:
        """AIMD: a rate-limited session halves the effective parallelism (swarm.degraded) once per burst, a success adds
        one back once the cooldown has passed (and swarm.degraded is cleared when the cap is whole again). Any other
        failure is isolated: it leaves the parallelism alone (the retry ladder handles the task) unless it is the
        `fail_streak`-th consecutive failure of a distinct task with no success in between, which halves the
        parallelism once per streak through the same limiter. A rate-limited failure neither counts nor resets."""
        before = self.limiter.current()
        if run.rate_limited is not None:
            if run.submitted_at < self._last_decrease:
                # one provider burst hits every session that was already running: that is one decrease, not k
                self.log.append(f"rate limited at {task['task_id']}: same burst, effective parallelism stays {before}")
                return
            self.limiter.on_rate_limit(run.rate_limited or None)
            after = self.limiter.current()
            if after < before:
                self._last_decrease = time.monotonic()
            self.log.append(f"rate limited at {task['task_id']}: effective parallelism {before} → {after}")
            if after < before:
                _safe(self.tel.degraded, "rate_limited", mode="reduced_parallelism",
                      detail=f"effective_parallel {before} -> {after}")
        elif failed:
            if self.fail_streak <= 0 or self.cancel.event.is_set():  # circuit off, or a shutdown killing sessions
                return
            self._streak.add(task["task_id"])
            if len(self._streak) < self.fail_streak or self._streak_tripped:
                return
            self._streak_tripped = True
            self.limiter.on_rate_limit(None)
            after = self.limiter.current()
            self.log.append(f"{len(self._streak)} failures in a row: effective parallelism {before} → {after}")
            if after < before:
                _safe(self.tel.degraded, "consecutive_failures", mode="reduced_parallelism",
                      detail=f"{len(self._streak)} failures in a row; effective_parallel {before} -> {after}")
        else:
            self._streak.clear()
            self._streak_tripped = False
            self.limiter.on_success()
            after = self.limiter.current()
            if after > before:
                self.log.append(f"rate limit cooled: effective parallelism {before} → {after}")
                if after >= self.mgr.max_concurrent:
                    _safe(self.tel.clear_degraded, "reduced_parallelism")

    def _schedule_retry(self, task: dict, reason: str, rate_hit: float | None) -> None:
        """A transient failure (swarm.retry.is_transient) waits `backoff_s` before its re-dispatch, via
        notes.retry_after (wall clock, so another process sees it); a provider Retry-After lengthens it, up to the cap."""
        current = self.store.get(task["task_id"])
        if current["attempt"] >= current["max_attempts"] or not is_transient(None, reason):
            return
        delay = backoff_s(max(1, int(current["attempt"])))
        if rate_hit:
            delay = max(delay, min(float(rate_hit), backoff_s(62, jitter=0)))  # 62 doublings: the configured cap
        self.store.set_notes(task["task_id"], retry_after=time.time() + delay)
        self.log.append(f"{task['task_id']}: transient failure, re-dispatch in {delay:.1f}s")

    # ---- the loop ----------------------------------------------------------------------------------------------
    def _wait(self, timeout: float = WAIT_TICK_S) -> set[Future]:
        futures = [i.future for i in self.inflight.values()]
        return futures_wait(futures, timeout=timeout, return_when=FIRST_COMPLETED).done if futures else set()

    def _idle(self, res: Pass) -> str:
        """Nothing is running and the pass submitted nothing: 'stop', 'sleep' (a retry is cooling down) or 'retry'."""
        store, corr = self.store, self.corr
        if res.ready == 0:
            remaining = [t for t in store.list(correlation_id=corr)
                         if t["state"] not in (S.DONE.value, S.CANCELLED.value, S.ESCALATED.value)]
            if remaining:
                self.log.append(f"stalled: {[(t['task_id'], t['state']) for t in remaining]}")
            return "stop"
        if res.backoff_wall is not None and not self.closed:
            return "sleep"
        if res.waiting:
            self.log.append(f"waiting on leases: {res.waiting}")
            return "stop"
        return "retry"

    def loop(self) -> None:
        need_pass, idle = True, 0
        while True:
            if self.cancel.event.is_set():
                self._cancel_drain()
                return
            if need_pass and not self.closed and not (self.barrier and self.inflight):
                need_pass = False
                res = self._schedule()
                if self.once or self.extension_down or (self.barrier and self.rounds >= self.max_rounds) \
                        or (not self.barrier and self.dispatched >= self.budget):
                    self.closed = True
                if not self.inflight and res.submitted == 0:
                    verdict = self._idle(res)
                    idle = idle + 1 if verdict == "retry" else 0
                    if verdict == "stop" or idle >= INTERNAL_FAILURE_LIMIT:
                        if verdict == "retry":
                            self.log.append("stalled: ready tasks could not be dispatched")
                        return
                    if verdict == "sleep":
                        self._sleep_until(res.backoff_wall)
                    need_pass = True
                    continue
            if not self.inflight:
                if self.closed:
                    return
                need_pass = True
                continue
            done = self._wait()
            for future in done:
                self._complete(future)
                need_pass = True
            if not done and self._wake_wall is not None and time.time() >= self._wake_wall:
                need_pass = True

    def _sleep_until(self, wall: float | None) -> None:
        """Wait for a retry's backoff to pass, waking early on a cancel."""
        while wall is not None and time.time() < wall and not self.cancel.event.is_set():
            time.sleep(min(WAIT_TICK_S, max(0.0, wall - time.time())))

    def _cancel_drain(self) -> None:
        """4: write the cooperative flag, give sessions up to SWARM_CANCEL_GRACE_S to leave on their own, then kill the
        process groups (a second signal skips the grace). Nothing new is dispatched. The whole shutdown fits in
        `shutdown_budget` (the supervisor's SIGTERM -> SIGKILL window, counted from the request): the cooperative wait
        ends KILL_GRACE_S + KILL_SETTLE_S before it, so the kill always completes before the supervisor's own SIGKILL
        would orphan the sessions."""
        reason = self.cancel.reason or "cancelled"
        kill_grace = max(0.5, min(KILL_GRACE_S, float(STOP_GRACE_S), self.shutdown_budget - KILL_SETTLE_S))
        grace = max(0.0, min(self.cancel_grace, self.shutdown_budget - kill_grace - KILL_SETTLE_S))
        requested = self.cancel.at if self.cancel.at is not None else time.monotonic()
        self.kill_reason = "E-TIMEOUT: swarm timeout" if reason == "swarm-timeout" else "E-TIMEOUT: runner shutdown"
        capped = f" (SWARM_CANCEL_GRACE_S {self.cancel_grace:g}s, shutdown budget {self.shutdown_budget:g}s)" \
            if grace < self.cancel_grace else ""
        self.log.append(f"cancelling: {reason}; {len(self.inflight)} session(s), grace {grace:g}s{capped}")
        _safe(self.tel.degraded, f"cancel requested: {reason}", mode="cancelling", detail=f"grace {grace:g}s")
        try:
            write_cancel_flag(self.sdir, self.corr, reason, grace)
        except OSError:
            _LOG.warning("cancel flag could not be written; sessions will only see the kill", exc_info=True)
        deadline = requested + grace
        while self.inflight and time.monotonic() < deadline and not self.cancel.force.is_set():
            for future in self._wait():
                self._complete(future)
        if self.inflight:
            kill_sessions(grace=kill_grace, reason=self.kill_reason)
            hard = time.monotonic() + kill_grace + KILL_SETTLE_S
            while self.inflight and time.monotonic() < hard:
                for future in self._wait():
                    self._complete(future)
            if self.inflight:
                self.log.append(f"abandoned after kill: {sorted(self.inflight)}")


def non_negative_seconds(value, name: str) -> float:
    """A CLI duration >= 0 (0 = off)."""
    seconds = float(value or 0)
    if not seconds >= 0 or seconds == float("inf"):
        raise SwarmError(ErrorCode.E_INPUT, f"{name} must be a number of seconds >= 0, got {value!r}")
    return seconds


def run(args, ctx) -> dict:
    from swarm.envelope import perform_signing_key_lifecycle
    repo = Path(args.repo).resolve()
    # A malformed deny-path policy is a controller configuration error. Refuse it here, before the lifecycle, leases or
    # any claim; per-task rejection would burn each task's attempts (headless_command re-checks it defensively).
    signer_deny_metadata(dict(os.environ))
    # the run's workspace: its keys, state and status writes are scoped to it, not to the cwd it started in
    ctx.root = repo
    key_state = perform_signing_key_lifecycle(root=ctx.root)
    message = key_state.get("message") or ""
    if message:
        print(message, file=sys.stderr, flush=True)
        ctx.emit("signing.key.lifecycle", key_state)
    if key_state.get("status") == "blocked":
        raise SwarmError(ErrorCode.E_POLICY, message or "signing key lifecycle blocked")
    try:
        from swarm.state_store import get_swarm_state_store
        ss = get_swarm_state_store(root=ctx.root)
        ss.append_gsd_line("LOADING", root=ctx.root)
        ss.update("LOADING", corr=ctx.correlation_id, extra={"lifecycle": key_state.get("status")})
        ss.append_gsd_line("INITIALIZING", root=ctx.root)
        ss.update("INITIALIZING", corr=ctx.correlation_id)
        # drive more states via state store or emits, integrate with subagent lifecycle
        ss.append_gsd_line("RUNNING", root=ctx.root)
        ss.update("RUNNING", corr=ctx.correlation_id, extra={"wave": "wave1"})
    except Exception:
        pass
    sdir = swarm_dir(repo, create=True)
    # one absolute state dir for this process and every child it spawns (D-10)
    os.environ["SWARM_DIR"] = str(sdir)
    store_path = sdir / "tasks.db"
    store = TaskStore(store_path)
    corr = ctx.correlation_id
    if corr is None:
        terminal = {"DONE", "CANCELLED", "ESCALATED"}
        live = sorted({t["correlation_id"] for t in store.list() if t["state"] not in terminal})
        if len(live) > 1:
            raise SwarmError(ErrorCode.E_INPUT, f"{len(live)} non-terminal plans {live}; pass --correlation-id")
        corr = live[0] if live else latest_correlation(repo)
    if not corr:
        raise SwarmError(ErrorCode.E_INPUT, "no plan found — run scripts/orch_plan.py first")
    ctx.correlation_id = corr
    if not store.list(correlation_id=corr):
        raise SwarmError(ErrorCode.E_INPUT, f"no tasks for correlation {corr} in {store_path}; plan with "
                         "orch_plan.py --repo <same repo> (or export one SWARM_DIR)")
    mode = scheduler_mode()
    swarm_timeout = non_negative_seconds(getattr(args, "swarm_timeout", 0), "--swarm-timeout")
    args.swarm_timeout = swarm_timeout
    # read once, before anything is dispatched: a malformed value is an input error now, not a surprise at cancel time
    cancel_grace = _env_seconds("SWARM_CANCEL_GRACE_S", CANCEL_GRACE_DEFAULT_S)
    shutdown_budget = _env_seconds("SWARM_SHUTDOWN_BUDGET_S", SHUTDOWN_BUDGET_DEFAULT_S)
    fail_streak = _env_count("SWARM_FAIL_STREAK", FAIL_STREAK_DEFAULT)
    # logger unit integration (init phase) + paths spec + subagent lifecycle
    lp = logging_paths(repo)
    log_phase("init", {"swarm_dir": str(sdir), "logging_paths": lp, "runtime": getattr(args, 'runtime', None)}, source="A01@swarm_run", correlation_id=corr, root=repo)
    # the one lifecycle manager of this run: global cap = min(--max-parallel, SWARM_MAX_PARALLEL_CEILING) (D5)
    mgr = get_manager(resolve_max_parallel(getattr(args, "max_parallel", None)), root=ctx.root,
                      overdue_after_s=args.task_timeout, dry_run=args.dry_run, correlation_id=corr)
    emit_gsd_omp("init.immediate", corr=corr, root=ctx.root, wave1="gsd-autonomous", dry_run=args.dry_run)
    stream_subagent_status("runner-init", "ready", phase="init", wave="wave1", corr=corr, root=ctx.root,
                           dry_run=args.dry_run)
    emit_gsd_omp("consolidated", audit="run.init", wave="wave1", corr=corr, root=ctx.root, dry_run=args.dry_run)
    args.runtime = resolve_runtime(args.runtime)  # once per run: auto cannot flip mid-run
    validate_runtime_approval(args.runtime, args)
    if not args.dry_run:
        name = runtime_bin(args.runtime, args)
        binary = shutil.which(name)
        if not binary:
            raise SwarmError(ErrorCode.E_DEP, f"{name} not on PATH (--runtime {args.runtime}; use --dry-run to simulate)")
        # absolute: sessions spawn with cwd=repo (omp, grok) or ROOT (claude), where a relative path breaks (WR-05)
        binary = os.path.abspath(binary)
        setattr(args, f"{args.runtime}_bin", binary)
        if args.runtime == "claude":
            preflight_auth(binary)
    # before lease_bridge and any claim, including --dry-run (it signs task.assign)
    preflight_signing()
    tel = Telemetry(root=ctx.root, correlation_id=corr, tee=not args.dry_run)  # one per run: its correlation id
    leases = lease_bridge(args, store_path, corr, repo, ctx)
    handoffs = handoff_bridge(store_path, corr, repo, ctx, leases)
    log: list[str] = []
    cancel = CancelRequest()
    # T-06-12: SIGTERM/SIGHUP/SIGINT to the runner start a cooperative cancel (flag, grace, then the process-group
    # kill), then the previous handlers come back. WR-10: a signal inherited as ignored (nohup, a parent's SIG_IGN)
    # stays ignored.
    _STOPPING.clear()

    def _on_signal(signum, _frame) -> None:
        cancel.request(f"signal {signal.Signals(signum).name}", signum)

    sched = Scheduler(args, ctx, store=store, store_path=store_path, repo=repo, corr=corr, mgr=mgr, tel=tel,
                      leases=leases, handoffs=handoffs, cancel=cancel, barrier=mode == "rounds", sdir=sdir,
                      cancel_grace=cancel_grace, shutdown_budget=shutdown_budget, fail_streak=fail_streak)
    clear_cancel_flag(sdir, corr)  # a flag left by an earlier run of this plan must not cancel this one
    previous = ({s: signal.signal(s, _on_signal) for s in _FORWARDED
                 if signal.getsignal(s) is not signal.SIG_IGN}
                if threading.current_thread() is threading.main_thread() else {})
    try:
        if leases is not None:
            leases.resume()
            leases.start()
        if not args.dry_run:
            recovered = recover_stranded(store, corr, emit=ctx.emit)
            if recovered:
                log.append(f"recovered stranded: {recovered}")
        log.append(f"lifecycle_mgr: cap={mgr.max_concurrent} scheduler={mode}")
        # OMP gsd-autonomous: register for visibility and stream status
        mgr.register_omp(corr)
        emit_gsd_omp("dispatch.start.immediate", corr=corr, root=ctx.root, max=mgr.max_concurrent,
                     wave1="gsd-autonomous", dry_run=args.dry_run)
        stream_subagent_status("dispatch", "started", phase="wave1", corr=corr, root=ctx.root, dry_run=args.dry_run)
        emit_gsd_omp("consolidated", audit="dispatch.start", wave="wave1", corr=corr, root=ctx.root,
                     dry_run=args.dry_run)
        log.append("error_handler_unit: " + sched.error_handler.spec()["version"])
        sched.start()
        try:
            sched.loop()
        except BaseException:
            sched.abort()
            raise
        finally:
            sched.close()
    finally:
        for s, handler in previous.items():
            signal.signal(s, signal.SIG_DFL if handler is None else handler)
        clear_cancel_flag(sdir, corr)
        if leases is not None:
            leases.close()
    log += sched.log
    # note: the outer finally was collapsed; original had reconcile after
    log += reconcile(store, corr, ctx.emit, leases=leases, handoffs=handoffs)
    if handoffs is not None:
        handoffs.flush()
    tasks = store.list(correlation_id=corr)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    complete = all(t["state"] in (S.DONE.value, S.CANCELLED.value) for t in tasks)
    escalated = [t["task_id"] for t in tasks if t["state"] == S.ESCALATED.value]
    if cancel.signum is not None:  # the signal's exit status, once the sessions are gone and the records are final
        raise SystemExit(128 + cancel.signum)
    return {
        "status": "ok" if complete else "fail",
        "correlation_id": corr,
        "rounds": sched.rounds,
        "scheduler": mode,
        "max_parallel": mgr.max_concurrent,
        "effective_parallel": sched.effective_parallel(),
        "cancelled": cancel.reason,
        "counts": counts,
        "escalated": escalated,
        "complete": complete,
        "log": log,
        "summary": f"correlation {corr}: {counts} complete={complete} escalated={escalated or 'none'}",
        "error_handler_spec": sched.error_handler.spec(),
        "error_handler_status": sched.error_handler.status()
    }


def add_args(p):
    p.add_argument("--repo", default=".", help="codebase the agents work on")
    p.add_argument("--max-parallel", type=int, default=None,
                   help="global concurrent sessions (default SWARM_MAX_PARALLEL, else 4; at most "
                        "SWARM_MAX_PARALLEL_CEILING, default 32); each agent's own max_parallel from agents.json "
                        "still applies")
    p.add_argument("--max-rounds", type=int, default=50,
                   help="bound on dispatches: max-rounds x max-parallel tasks (rounds scheduler: that many barrier rounds)")
    p.add_argument("--once", action="store_true", help="single scheduling round")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--task-timeout", type=int, default=1800)
    p.add_argument("--swarm-timeout", type=float, default=0.0,
                   help="seconds the whole run may last before it is cancelled like a signal (0 = off)")
    p.add_argument("--permission-mode", default="acceptEdits", choices=CLAUDE_PERMISSION_MODES,
                   help="normal Claude permission mode; bypass modes are not supported")
    p.add_argument("--approval-mode", default="write", choices=OMP_APPROVAL_MODES,
                   help="normal OMP tool approval mode (default: write)")
    p.add_argument("--model", help="override model for all agents")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--grok-bin", default="grok")
    p.add_argument("--omp-bin", default="omp")
    p.add_argument("--runtime", choices=["auto", *RUNTIMES], default="auto",
                   help="headless runner: claude -p --agent, omp -p --mode json, or auto "
                        "(claude/grok; Grok dispatch is currently refused; omp only when explicit or SWARM_RUNTIME=omp)")
    p.add_argument("--allowed-tools", default="Bash(python3:*),Bash(git diff:*),Bash(git log:*),Bash(git status:*),Bash(ls:*),Read,Grep,Glob",
                   help="comma list passed to claude --allowedTools so headless agents can run their scripts unattended")


if __name__ == "__main__":
    sys.exit(AgentScript("A01", "swarm_run", run, description=__doc__, add_args=add_args).main())
