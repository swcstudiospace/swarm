"""Validation, security and graceful degradation for the Multithreading Subagent Module.

Wave 5 per synthesized ultrathink plan (after wave 1 states+decomp foundation).
Adds:
- Input validation for tasks/subtasks (types, sizes, structure)
- Security: poison task detection, length caps, no-exec sanitization, failure isolation
- Graceful fallback to pure Python when Rust unavailable or validation prefers
- Configurable pool size (default 30, hard cap per clarif q1 and gsd-pool)
- Failure isolation: single bad subtask does not poison pool or siblings

Integrates with orchestration, decomposition, GSD hooks.
Falls back cleanly; dispatch continues in pure-Py path.
Acceptance: validation module present+used, fallback works, size configurable.

Refs: clarifications.md q1-q4, plan/execution-waves.md, python_tools/*, crates/concurrency.
"""

from typing import Dict, Any, Optional, Tuple, Callable
import os
import re

# Configurable defaults from clarifications (q1: max default 30)
DEFAULT_POOL_SIZE: int = 30
MAX_POOL_SIZE: int = 30
MAX_DESC_LEN: int = 100 * 1024  # 100KB cap for safety (matches other 100KB caps)
MAX_SUBTASKS: int = 128
POISON_PATTERNS = [
    r"exec\s*\(",
    r"__import__",
    r"subprocess",
    r"os\.system",
    r"eval\s*\(",
    r"compile\s*\(",
]


class ValidationConfig:
    """Configurable validation + size (supports dynamic sizing up to cap)."""

    def __init__(self, pool_size: int = DEFAULT_POOL_SIZE, max_desc_len: int = MAX_DESC_LEN):
        self.pool_size = max(1, min(pool_size, MAX_POOL_SIZE))
        self.max_desc_len = max_desc_len
        self.enable_security = True
        self.graceful_fallback = True  # always allow pure-Py fallback


def get_configurable_pool_size(override: Optional[int] = None) -> int:
    """Return configurable size; env or arg or default 30 (cap 30)."""
    if override is not None:
        return max(1, min(override, MAX_POOL_SIZE))
    env = os.environ.get("SWARM_SUBAGENT_POOL_SIZE")
    if env:
        try:
            return max(1, min(int(env), MAX_POOL_SIZE))
        except ValueError:
            pass
    return DEFAULT_POOL_SIZE


def _is_pure_python_fallback() -> bool:
    """Detect if we must/should fallback to pure Python (no Rust or forced)."""
    if os.environ.get("SWARM_PURE_PYTHON") == "1":
        return True
    # Will be checked at runtime by importer too; here for graceful
    return False


def validate_subtask(subtask: Dict[str, Any], cfg: Optional[ValidationConfig] = None) -> Dict[str, Any]:
    """Validate and sanitize a single subtask dict. Raises on invalid (E-INPUT style).

    Returns validated (possibly sanitized) copy.
    Enforces structure from decomposition: id, description, parent.
    """
    if cfg is None:
        cfg = ValidationConfig()
    if not isinstance(subtask, dict):
        raise ValueError("Subtask must be a dict (E-INPUT)")
    validated = dict(subtask)  # copy

    sid = validated.get("id")
    if not sid or not isinstance(sid, str) or len(sid) > 128:
        raise ValueError("Subtask id must be non-empty str <=128 (E-INPUT)")

    desc = validated.get("description", "")
    if not isinstance(desc, str):
        desc = str(desc)
    if len(desc) > cfg.max_desc_len:
        desc = desc[: cfg.max_desc_len] + "...[truncated for security]"
    validated["description"] = desc

    parent = validated.get("parent", validated.get("id"))
    if parent and not isinstance(parent, str):
        parent = str(parent)
    validated["parent"] = parent or sid

    # Security: scan for poison
    if cfg.enable_security:
        for pat in POISON_PATTERNS:
            if re.search(pat, desc, re.IGNORECASE):
                raise ValueError(f"Poison pattern detected in subtask {sid}: {pat} (E-POLICY)")

    # Size sanity
    if len(validated) > 32:
        raise ValueError("Subtask has too many keys (E-INPUT)")

    return validated


def validate_task(task: Dict[str, Any], cfg: Optional[ValidationConfig] = None) -> Dict[str, Any]:
    """Validate top-level task before decomp/submit. Failure isolated."""
    if cfg is None:
        cfg = ValidationConfig()
    if not isinstance(task, dict):
        raise ValueError("Task must be a dict (E-INPUT)")
    if "id" not in task and "description" not in task:
        raise ValueError("Task requires id or description (E-INPUT)")
    # Do not mutate original; return validated view
    return {"id": task.get("id", "task"), "description": task.get("description", ""), **task}


def security_validate(payload: Any, cfg: Optional[ValidationConfig] = None) -> None:
    """Security gate: refuse invalid/poison/large inputs. Isolated failure."""
    if cfg is None:
        cfg = ValidationConfig()
    if cfg.enable_security:
        if isinstance(payload, dict):
            if "phases" in payload and isinstance(payload["phases"], list):
                if len(payload["phases"]) > MAX_SUBTASKS:
                    raise ValueError(f"Too many phases: {len(payload['phases'])} > {MAX_SUBTASKS} (E-CAPACITY)")
            for k, v in list(payload.items())[:10]:
                if isinstance(v, str) and len(v) > cfg.max_desc_len:
                    raise ValueError(f"Field {k} exceeds security size limit (E-INPUT)")


def isolate_failure(fn: Callable, *args, fallback: Optional[Callable] = None, **kwargs) -> Any:
    """Failure isolation wrapper: one subtask/task error does not cascade.
    Returns result or fallback result on isolated error. Non-blocking.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        if fallback:
            # Graceful: log style not here; return isolated partial
            try:
                return fallback(*args, **kwargs)
            except Exception:
                pass
        # Return structured failure marker (matches SubResult style)
        return {"status": "isolated_failure", "error": str(e), "isolated": True}


def with_graceful_fallback(
    rust_fn: Callable,
    pure_py_fn: Callable,
    *args,
    force_pure: bool = False,
    **kwargs,
) -> Tuple[Any, str]:
    """Try rust path; on any failure (incl validation) isolate and fallback to pure Python.
    Returns (result, served_by) where served_by in ('rust', 'python_fallback').
    Configurable + always works for dispatch.
    """
    if force_pure or _is_pure_python_fallback():
        res = pure_py_fn(*args, **kwargs)
        return res, "python_fallback"

    try:
        res = rust_fn(*args, **kwargs)
        return res, "rust"
    except Exception:
        # Isolate + fallback (failure isolation)
        res = pure_py_fn(*args, **kwargs)
        return res, "python_fallback"


def validate_and_maybe_fallback(
    data: Dict[str, Any],
    rust_op: Callable[[Dict], Any],
    pure_op: Callable[[Dict], Any],
    cfg: Optional[ValidationConfig] = None,
) -> Tuple[Any, str]:
    """Combined: validate input (security), then graceful dispatch with fallback.
    Used by orchestration dispatch. Size from cfg or default.
    """
    if cfg is None:
        cfg = ValidationConfig(pool_size=get_configurable_pool_size())
    security_validate(data, cfg)
    if isinstance(data, dict) and "subtask" in str(data):  # rough for sub
        validated = validate_subtask(data if isinstance(data, dict) else {}, cfg)
    else:
        validated = validate_task(data, cfg)
    return with_graceful_fallback(rust_op, pure_op, validated, force_pure=cfg.graceful_fallback)


# Public API for module (used by orchestration/gsd)
__all__ = [
    "ValidationConfig",
    "get_configurable_pool_size",
    "validate_subtask",
    "validate_task",
    "security_validate",
    "isolate_failure",
    "with_graceful_fallback",
    "validate_and_maybe_fallback",
    "DEFAULT_POOL_SIZE",
    "MAX_POOL_SIZE",
]
