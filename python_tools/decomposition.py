"""Task decomposition for the Multithreading Subagent Module.

Basic implementation to satisfy the synthesized plan unit:
- modules import cleanly
- decompose_task accepts SDLC task input

From the brief: decompose SDLC tasks into subtasks for concurrent/parallel execution.
"""

from typing import List, Dict, Any

def decompose_task(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Decompose an SDLC task into subtasks.

    Accepts a dict representing an SDLC task (e.g. from GSD-Core).
    Returns list of subtask dicts.

    For now, simple split: if 'phases' in task, split into one per phase.
    Otherwise, wrap as single subtask.
    """
    if not isinstance(task, dict):
        raise ValueError("Task must be a dict")

    subtasks = []
    if "phases" in task and isinstance(task["phases"], list):
        for i, phase in enumerate(task["phases"]):
            sub = {
                "id": f"{task.get('id', 'task')}_phase_{i}",
                "description": phase if isinstance(phase, str) else str(phase),
                "parent": task.get("id", "task"),
            }
            subtasks.append(sub)
    else:
        # Single subtask wrapper
        sub = {
            "id": f"{task.get('id', 'task')}_0",
            "description": task.get("description", str(task)),
            "parent": task.get("id", "task"),
        }
        subtasks.append(sub)

    return subtasks
