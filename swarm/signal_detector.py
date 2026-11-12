"""Pure signal detector (n2/n3). Read-only. Parity with omp/src/hooks.ts classify + user_prompt_submit.
Fail-open. Detects a01-orchestrator completion (IN_REVIEW or UPLIFT boundary) or SDLC.
"""

from __future__ import annotations
import json
import re

# Full guarded classify (greptile: shared classifier must not change routing; preserve all checks)
NEGATIVE = ("/uplift", "/think", "explain only", "/all-in-one:")
DISPATCH_MARKER = "[agent-swarm:dispatch]"
QUESTION = re.compile(r"^(what|why|how|when|where|who|which|is|are|can|could|does|do|should|explain|describe)\b", re.I | re.A)
TRIVIAL = re.compile(r"\btypos?\b|^rename\b", re.I | re.A)
SDLC = re.compile(
    r"\b(build|implement|add|create|fix|refactor|migrate|deploy|release|ship|write tests?|set up|setup|integrate|scaffold|upgrade|orchestrate)\b",
    re.I | re.A,
)

UPLIFT_ROOT = re.compile(r"^<(BUILD_PROMPT|FIX_PROMPT|RESEARCH_PROMPT|CHANGE_PROMPT|UPLIFTED_PROMPT|uplifted|ultrathink)\b", re.I)
ORIGINAL_OPEN = "<original"
ORIGINAL_CLOSE = "</original>"


def _index_of_ascii(text: str, needle: str, start: int = 0) -> int:
    """Forward ASCII case-insensitive index (parity with omp/src/hooks.ts)."""
    n = len(needle)
    matched = 0
    for i in range(start, len(text)):
        low = text[i].lower()
        if len(low) == 1 and low == needle[matched].lower():
            matched += 1
            if matched == n:
                return i - n + 1
        else:
            matched = 1 if len(low) == 1 and low == needle[0].lower() else 0
    return -1


def _is_ascii_ident(ch: str) -> bool:
    return len(ch) == 1 and (ch.isalnum() or ch == "_")


def extract_unescaped_original(prompt: str) -> str:
    """The user's own words: unescaped <ORIGINAL> of uplift XML, else prompt unchanged.
    Parity with omp/src/hooks.ts classifiedText (unescape &lt; etc; amp last).
    Used for ultrathink enhanced mode classification parity.
    """
    t = prompt.strip()
    if not UPLIFT_ROOT.search(t):
        return prompt
    from_pos = 0
    while from_pos < len(t):
        open_idx = _index_of_ascii(t, ORIGINAL_OPEN, from_pos)
        if open_idx < 0:
            return prompt
        after = t[open_idx + len(ORIGINAL_OPEN): open_idx + len(ORIGINAL_OPEN) + 1]
        if after and _is_ascii_ident(after):
            from_pos = open_idx + len(ORIGINAL_OPEN)
            continue
        gt = t.find(">", open_idx)
        if gt < 0:
            return prompt
        close = _index_of_ascii(t, ORIGINAL_CLOSE, gt + 1)
        if close < 0:
            return prompt
        inner = t[gt + 1 : close]
        # unescape in order: lt/gt/quot/apos then amp last
        return (
            inner.replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&apos;", "'")
            .replace("&amp;", "&")
        )
    return prompt


def classify(text: str) -> str:
    """Guarded SDLC classify (exact parity with user_prompt_submit + omp).
    For ultrathink enhanced mode, extract unescaped ORIGINAL before classification.
    """
    if not text:
        return "non-sdlc"
    # unescaped ORIGINAL extraction for ultrathink classification parity
    text = extract_unescaped_original(text)
    trimmed = text.strip()
    low = trimmed.lower()
    if not trimmed or trimmed.startswith("/") or trimmed.startswith("<system-reminder"):
        return "non-sdlc"
    if DISPATCH_MARKER in low or any(n in low for n in NEGATIVE):
        return "non-sdlc"
    if trimmed.endswith("?") or QUESTION.search(trimmed) or TRIVIAL.search(trimmed):
        return "non-sdlc"
    if SDLC.search(trimmed):
        return "sdlc"
    return "non-sdlc"


def detect_completion(payload: str | dict[str, object] | None) -> dict[str, str] | None:
    """Detect a01-orchestrator complete or uplift end.
    Requires a01/ultrathink identifier + specific state (greptile: avoid unrelated text).
    Uses unescaped ORIGINAL extraction for ultrathink mode.
    """
    if not payload:
        return None
    text = payload if isinstance(payload, str) else json.dumps(payload)
    # extract for classification/detection parity in ultrathink
    text = extract_unescaped_original(text)
    tlow = text.lower()
    # require a01 or ultrathink context
    if not ("a01-orchestrator" in tlow or "a01" in tlow or "ultrathink" in tlow or "orchestrate" in tlow):
        return None
    # a01 task.result marker
    if '"state": "IN_REVIEW"' in text or "IN_REVIEW" in text:
        return {"type": "a01_complete", "reason": "in_review", "raw": text[:512]}
    # uplift boundary
    if "UPLIFT" in text.upper() or "<ORIGINAL>" in text:
        return {"type": "uplift_complete", "reason": "uplift", "raw": text[:512]}
    # swarm.status DONE counts or final
    if '"state": "DONE"' in text or "COMPLETE" in text.upper():
        return {"type": "swarm_complete", "reason": "done", "raw": text[:512]}
    return None


def is_a01_session(transcript: str | list[object]) -> bool:
    """Check if transcript/session is a01-orchestrator or swarm orchestrate."""
    t = str(transcript).lower()
    return "a01-orchestrator" in t or "orchestrate" in t or ("swarm" in t and "plan" in t)

