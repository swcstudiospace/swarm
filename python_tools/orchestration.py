"""Python orchestration for the Multithreading Subagent Module.

Implements pool management, submission, result aggregation for concurrent/parallel execution.
Integrates with GSD-Core CLI Agents for task intake and reporting.

From the plan: tasks submit non-blockingly and results aggregate to GSD-Core.
"""

from typing import List, Dict, Any, Optional
from .states import PoolState, SubagentState
from .decomposition import decompose_task
import threading
import queue
import time

class SubagentPool:
    """Manages a pool of subagents for concurrent/parallel task execution."""

    def __init__(self, size: int = 30):
        self.size = size
        self.state = PoolState.Idle
        self.subagents = [SubagentState.Available for _ in range(size)]
        self.task_queue: queue.Queue = queue.Queue()
        self.results: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None

    def create(self) -> None:
        """Initialize the pool."""
        with self.lock:
            self.state = PoolState.Initializing
            self._running = True
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
            self.state = PoolState.Active

    def submit(self, subtask: Dict[str, Any]) -> str:
        """Non-blocking submit of subtask."""
        with self.lock:
            if self.state != PoolState.Active:
                raise RuntimeError("Pool not active")
            task_id = subtask.get("id", str(time.time()))
            self.task_queue.put((task_id, subtask))
            return task_id

    def _worker_loop(self) -> None:
        """Worker loop for executing subtasks concurrently."""
        while self._running:
            task_id = None
            try:
                task_id, subtask = self.task_queue.get(timeout=1)
                # Find available subagent
                with self.lock:
                    for i, status in enumerate(self.subagents):
                        if status == SubagentState.Available:
                            self.subagents[i] = SubagentState.Busy
                            break
                    else:
                        # No available, requeue
                        self.task_queue.put((task_id, subtask))
                        time.sleep(0.1)
                        continue

                # Simulate execution (in real, dispatch to Rust via PyO3)
                result = self._execute_subtask(subtask)
                with self.lock:
                    self.results.append({"id": task_id, "result": result})
                    for i, status in enumerate(self.subagents):
                        if status == SubagentState.Busy:
                            self.subagents[i] = SubagentState.Available
                            break

                self.task_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                with self.lock:
                    self.results.append({"id": task_id or "unknown", "error": str(e)})
                    # Mark subagent failed or available
                    for i, status in enumerate(self.subagents):
                        if status == SubagentState.Busy:
                            self.subagents[i] = SubagentState.Failed
                            break

    def _execute_subtask(self, subtask: Dict[str, Any]) -> Any:
        """Execute subtask (placeholder for Rust dispatch)."""
        # In full impl, use PyO3 to call Rust execution
        time.sleep(0.01)  # Simulate work
        return {"status": "success", "subtask": subtask.get("description", str(subtask))}

    def aggregate_results(self) -> List[Dict[str, Any]]:
        """Aggregate results."""
        with self.lock:
            return self.results.copy()

    def drain(self) -> None:
        """Drain the pool."""
        with self.lock:
            self.state = PoolState.Draining
            self._running = False
            if self._worker_thread:
                self._worker_thread.join(timeout=5)
            self.state = PoolState.Idle

# GSD-Core integration hook
def gsd_submit_task(pool: SubagentPool, task: Dict[str, Any]) -> List[str]:
    """Hook for GSD-Core CLI Agents to submit task.

    Decomposes and submits non-blockingly.
    Returns list of subtask ids.
    """
    subtasks = decompose_task(task)
    ids = []
    for sub in subtasks:
        tid = pool.submit(sub)
        ids.append(tid)
    return ids

def gsd_collect_results(pool: SubagentPool) -> List[Dict[str, Any]]:
    """Hook to collect results back to GSD-Core."""
    return pool.aggregate_results()
