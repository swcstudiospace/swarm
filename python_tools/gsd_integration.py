 """GSD-Core integration for the Multithreading Subagent Module.
 
 Provides hooks for CLI Agents to submit tasks and receive results without inline processing.
 
 Wave 5: uses validation for configurable size, input validation, security, graceful paths.
 """
 
 from typing import Dict, Any, List
from .orchestration import SubagentPool, gsd_submit_task, gsd_collect_results
from .validation import get_configurable_pool_size, security_validate, ValidationConfig
 
class GSDCoreHook:
     """Integration point for GSD-Core and supported CLI Agents.
 
     Wave 5: size configurable, validation+security on submit path.
     """
 
     def __init__(self, pool_size: int = None):
         effective = get_configurable_pool_size(pool_size)
         self.pool = SubagentPool(size=effective)
         self._val_cfg = ValidationConfig(pool_size=effective)
         self.pool.create()
 
     def submit_sdlc_task(self, task: Dict[str, Any]) -> List[str]:
         """Receive SDLC task from GSD-Core, decompose, submit to pool.
 
         Non-blocking.
         Wave 5: security validation applied (isolates).
         """
         security_validate(task, self._val_cfg)
         return gsd_submit_task(self.pool, task)
 
     def get_results(self) -> List[Dict[str, Any]]:
         """Collect aggregated results to report back to GSD-Core."""
         return gsd_collect_results(self.pool)
 
     def shutdown(self) -> None:
         """Graceful shutdown."""
         self.pool.drain()
