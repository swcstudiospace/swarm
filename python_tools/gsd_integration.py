"""GSD-Core integration for the Multithreading Subagent Module.

Provides hooks for CLI Agents to submit tasks and receive results without inline processing.
"""

from typing import Dict, Any, List
from .orchestration import SubagentPool, gsd_submit_task, gsd_collect_results

class GSDCoreHook:
    """Integration point for GSD-Core and supported CLI Agents."""

    def __init__(self, pool_size: int = 30):
        self.pool = SubagentPool(size=pool_size)
        self.pool.create()

    def submit_sdlc_task(self, task: Dict[str, Any]) -> List[str]:
        """Receive SDLC task from GSD-Core, decompose, submit to pool.

        Non-blocking.
        """
        return gsd_submit_task(self.pool, task)

    def get_results(self) -> List[Dict[str, Any]]:
        """Collect aggregated results to report back to GSD-Core."""
        return gsd_collect_results(self.pool)

    def shutdown(self) -> None:
        """Graceful shutdown."""
        self.pool.drain()
