"""Task decomposition for the Multithreading Subagent Module.

Implements decompose_task per synthesized ultrathink plan (wave 1 unit 2)
and BUILD_PROMPT decomposition rules for SDLC tasks into subtasks.

- Accepts dict with 'id', optional 'description', 'phases' (list for split)
- Returns list[dict] of subtasks each with id/description/parent (flat for pool)
- Generic: phases list splits stages; falls back to description wrapper
- Used by orchestration + GSD hooks for concurrent/parallel execution of 30 subagents.
"""
from typing import List, Dict, Any


def decompose_task(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decompose an SDLC task into subtasks for concurrent/parallel pool execution.

    Per plan: takes SDLC task dict (e.g. from GSD-Core with 'description' or 'phases').
    Returns list of subtask dicts.

    - if 'phases': list -> one subtask per phase (for staged SDLC)
    - else: single subtask from 'description' (or str fallback)
    Each subtask: {'id', 'description', 'parent'}
    """
    if not isinstance(task, dict):
        raise ValueError("Task must be a dict")

    task_id = task.get("id", "task")
    subtasks: List[Dict[str, Any]] = []

    phases = task.get("phases")
    if isinstance(phases, list):
        for i, phase in enumerate(phases):
            desc = phase if isinstance(phase, str) else str(phase)
            subtasks.append({
                "id": f"{task_id}_phase_{i}",
                "description": desc,
                "parent": task_id,
            })
    else:
        # fallback to description (or whole as str)
        desc = task.get("description", str(task))
        if not isinstance(desc, str):
            desc = str(desc)
        subtasks.append({
            "id": f"{task_id}_0",
            "description": desc,
            "parent": task_id,
        })

    return subtasks
