"""States for the Multithreading Subagent Module.

Derived from the BUILD_PROMPT states sections.
"""

from enum import Enum, auto

class PoolState(Enum):
    """Pool lifecycle states."""
    Idle = auto()
    Initializing = auto()
    Active = auto()
    Scaling = auto()
    Draining = auto()
    Terminating = auto()
    Error = auto()

class TaskState(Enum):
    """Task states."""
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
    """Subagent states."""
    Available = auto()
    Busy = auto()
    Failed = auto()
    Terminated = auto()
