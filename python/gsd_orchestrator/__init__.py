"""gsd_orchestrator package (per ultrathink workflow verification).

Re-exports the production implementation from python_tools/ for compatibility with plan commands.
"""
import sys
import os

# Make sure both the python/ dir and the python_tools/ dir are on sys.path
# so that "from states import ..." and "from decomposition import ..." inside
# python_tools/*.py resolve when users do PYTHONPATH=python python -c "from gsd_orchestrator ..."
_here = os.path.dirname(os.path.abspath(__file__))
python_root = os.path.dirname(_here)           # .../python
tools_root  = os.path.join(python_root, "python_tools")

for p in (python_root, tools_root):
    if p not in sys.path:
        sys.path.insert(0, p)

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