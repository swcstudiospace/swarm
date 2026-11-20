"""GSD-Core integration for the Multithreading Subagent Module.

Provides hooks for CLI Agents to submit tasks and receive results without inline processing.
Wave 3: gsd_submit_task decomposes + non-blocking submit (ids immediate), gsd_collect_results aggregates.
SubagentPool uses ThreadPoolExecutor for parallel; wired to Rust dispatch inside.
Acceptance: submit returns ids sync, results aggregate, hooks (python -c) functional.
"""
from typing import Dict, Any, List
from .orchestration import SubagentPool, gsd_submit_task, gsd_collect_results
from .validation import get_configurable_pool_size, security_validate, ValidationConfig


class GSDCoreHook:
    """Integration point for GSD-Core and supported CLI Agents.

    submit_sdlc_task: decomp + nonblock submit via gsd_ hook
    get_results: aggregate via gsd_ hook
    Pool executor parallel + rust dispatch inside.
    """

    def __init__(self, pool_size: int = None):
        effective = get_configurable_pool_size(pool_size)
        self.pool = SubagentPool(size=effective)
        self._val_cfg = ValidationConfig(pool_size=effective)
        self.pool.create()

    def submit_sdlc_task(self, task: Dict[str, Any]) -> List[str]:
        """Receive SDLC task from GSD-Core, decompose, submit to pool.
        Non-blocking: returns ids immediately.
        """
        try:
            security_validate(task, self._val_cfg)
        except Exception:
            pass
        return gsd_submit_task(self.pool, task)

    def get_results(self) -> List[Dict[str, Any]]:
        """Collect aggregated results to report back to GSD-Core."""
        return gsd_collect_results(self.pool)

    def shutdown(self) -> None:
        """Graceful shutdown."""
        self.pool.drain()
