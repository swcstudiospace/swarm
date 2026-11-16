"""gsd_orchestrator package (per ultrathink workflow verification).

Re-exports the production implementation from python_tools/ for compatibility with plan commands.
"""
from python_tools.orchestration import (
    SubagentPool,
    rust_dispatch,
    gsd_submit_task,
    gsd_collect_results,
)
from python_tools.states import PoolState, TaskState, SubagentState
from python_tools.decomposition import decompose_task
from python_tools.validation import (
    get_configurable_pool_size,
    validate_subtask,
    ValidationConfig,
    MAX_POOL_SIZE,
)
__all__ = [
    "SubagentPool", "PoolState", "TaskState", "SubagentState",
    "decompose_task", "gsd_submit_task", "gsd_collect_results",
    "get_configurable_pool_size", "validate_subtask", "ValidationConfig", "MAX_POOL_SIZE",
]
