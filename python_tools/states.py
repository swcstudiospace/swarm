"""States for the Multithreading Subagent Module.

Derived from the BUILD_PROMPT states sections per synthesized ultrathink plan
for wave 1 unit 2 (Pool lifecycle, Task, Subagent states for orchestration/decomp).

Exact states:
- PoolState: Idle, Initializing, Active, Scaling, Draining, Terminating, Error
- TaskState: Received, Decomposed, Queued, Assigned, Executing, Aggregating, Completed, Failed, Cancelled
- SubagentState: Available, Busy, Failed, Terminated
"""
from enum import Enum, auto


class PoolState(Enum):
    """Pool lifecycle states (supports scaling, drain, error per clarifications q1)."""
    Idle = auto()
    Initializing = auto()
    Active = auto()
    Scaling = auto()
    Draining = auto()
    Terminating = auto()
    Error = auto()


class TaskState(Enum):
    """Task states for SDLC task lifecycle in multithreading subagent."""
    Received = auto()
    Decomposed = auto()
    Queued = auto()
    Assigned = auto()
    Executing = auto()
    Aggregating = auto()
    Completed = auto()
    Failed = auto()
    Cancelled = auto()


class SubagentState(Enum):
    """Subagent states for pool of 30 subagents."""
    Available = auto()
    Busy = auto()
    Failed = auto()
    Terminated = auto()
