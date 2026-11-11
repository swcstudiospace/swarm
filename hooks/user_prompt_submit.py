#!/usr/bin/env python3
"""Fail-open UserPromptSubmit hook: inject AgentSwarm orchestration on SDLC-shaped prompts.

classify() mirrors omp/src/hooks.ts classifyPrompt (D-10); tests/fixtures/classifier_prompts.json pins both.
Skips entirely inside headless swarm agent sessions (SWARM_CHILD=1)."""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Prompts containing these (case-insensitive) are owned by other plugins or explicitly opt out.
NEGATIVE = ("/uplift", "/think", "explain only", "/all-in-one:")
# Uplift wrappers, mirrored from swarm/signal_detector.py UPLIFT_ROOT. This file must run without that import, so the fallback
# below uses it: a wrapped prompt is silent, since the fallback cannot unescape <ORIGINAL> the way the primary classifier does.
UPLIFT_ROOT = re.compile(r"^<(BUILD_PROMPT|FIX_PROMPT|RESEARCH_PROMPT|CHANGE_PROMPT|UPLIFTED_PROMPT|uplifted|ultrathink)\b", re.I | re.A)

# Tags the /swarm dispatch prompt so the hook never re-steers the swarm's own dispatch.
DISPATCH_MARKER = "[agent-swarm:dispatch]"

# re.ASCII: JavaScript's \b and \w are ASCII-only, so the omp classifier sees "fixé" as "fix" + "é"; without the
# flag Python's Unicode \b would not, and the two runtimes would disagree on accented prompts (D-10 parity).
QUESTION = re.compile(r"^(what|why|how|when|where|who|which|is|are|can|could|does|do|should|explain|describe)\b", re.I | re.A)
TRIVIAL = re.compile(r"\btypos?\b|^rename\b", re.I | re.A)
SDLC = re.compile(
    r"\b(build|implement|add|create|fix|refactor|migrate|deploy|release|ship|write tests?|set up|setup|integrate|scaffold|upgrade|orchestrate)\b",
    re.I | re.A,
)

CONTEXT = """## AgentSwarm orchestration (mandatory)

This is SDLC work (prompts containing "orchestrate", "build", "implement" etc. trigger this): route through AgentSwarm.
Load skill `agent-swarm-orchestrate`.
Spawn subagent `a01-orchestrator` with the user request as the brief.
A01 plans and spawns a02–a30 by slug for parallel orchestrated execution.
Do not implement domain work in the parent session.
"""


def extract_prompt(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("prompt", "user_prompt"):
        if isinstance(payload.get(key), str):
            return payload[key]
    inputs = payload.get("inputs")
    if isinstance(inputs, dict) and isinstance(inputs.get("prompt"), str):
        return inputs["prompt"]
    return ""


def classify(prompt: str) -> bool:
    """True only for SDLC-shaped prompts (D-09): silent on empty, slash, system-reminder, opt-out tokens,
    the dispatch marker, questions/explanations and trivial edits.
    Thin delegate to swarm.signal_detector for parity (n8 plan).
    """
    try:
        from swarm.signal_detector import classify as _sd_classify
        res = _sd_classify(prompt)
        return res == "sdlc"
    except Exception as exc:
        # fail-open fallback to original logic; the reason goes to stderr so a broken import is visible, not silent
        print(f"user_prompt_submit: swarm.signal_detector unavailable ({exc!r}); using fallback classifier", file=sys.stderr)
        trimmed = prompt.strip()
        low = trimmed.lower()
        if not trimmed or trimmed.startswith("/") or trimmed.startswith("<system-reminder"):
            return False
        if UPLIFT_ROOT.match(trimmed):
            return False
        if DISPATCH_MARKER in low or any(n in low for n in NEGATIVE):
            return False
        if trimmed.endswith("?") or QUESTION.search(trimmed) or TRIVIAL.search(trimmed):
            return False
        return SDLC.search(trimmed) is not None

def main() -> int:
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            print("{}")
            return 0
        prompt = extract_prompt(payload)
        if os.environ.get("SWARM_DISABLED") == "1":
            # Operator kill switch (SWARM_DISABLED=1): no swarm context, no kickoff, same as omp's extension.
            print("{}")
        elif os.environ.get("SWARM_CHILD") == "1":
            # Headless swarm agent session: never re-inject orchestration or re-kick the swarm.
            print("{}")
        elif classify(prompt):
            # Context only. The detached runner is started once by the all-in-one plugin
            # after Prompt Uplift finishes (src/swarm/kickoff.ts), not here in parallel.
            json.dump({"additionalContext": CONTEXT}, sys.stdout)
        else:
            print("{}")
    except Exception as exc:
        print(f"user_prompt_submit: {exc!r}; injecting nothing", file=sys.stderr)
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
