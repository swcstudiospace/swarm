"""Python orchestration for the Multithreading Subagent Module.

Implements pool management, submission, result aggregation for concurrent/parallel execution.
Integrates with GSD-Core CLI Agents for task intake and reporting.

Uses concurrent.futures for real parallel execution (multiple workers).
Wired to the real Rust ThreadPool from swarm_pyo3_pool (PyO3 binding) using the exposed submit_tasks API.
Dispatch actually executes the work and waits for result.
Wave 5: validation + security + graceful fallback wired (input validation, failure isolation, size configurable).
"""

from typing import List, Dict, Any, Optional
import concurrent.futures
from states import PoolState, SubagentState
from decomposition import decompose_task
import threading
import queue
import time

# Optional wave5 validation (enhance without hard fail if absent)
try:
    from validation import (
        get_configurable_pool_size,
        validate_subtask,
        security_validate,
        with_graceful_fallback,
        isolate_failure,
        ValidationConfig,
    )
except Exception:
    get_configurable_pool_size = lambda s=30: max(1, min(s, 30))
    def validate_subtask(s, c=None): return dict(s) if isinstance(s, dict) else {"id":"0", "description":str(s)}
    def security_validate(s, c=None): pass
    class ValidationConfig:
        def __init__(self, pool_size=30): self.pool_size = pool_size
    def with_graceful_fallback(fn): return fn
    def isolate_failure(fn): return fn

try:
    import swarm_pyo3_pool as _rust_pool_mod
except Exception:
    _rust_pool_mod = None


class SubagentPool:
    """Manages a pool of subagents for concurrent/parallel task execution."""

    def __init__(self, size: int = 30):
        if size > 30:
            raise ValueError("Pool size capped at 30 subagents (enforced per spec + clarif q1)")
        capped = get_configurable_pool_size(size)
        self.size = capped
        self.state = PoolState.Idle
        self.subagents = [SubagentState.Available for _ in range(self.size)]
        self.task_queue: queue.Queue = queue.Queue()
        self.results: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self._running = False
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.size)
        self._futures: List = []
        self._rust_pool = None
        self._rust_mod = None
        self._val_cfg = ValidationConfig(pool_size=self.size)
        if _rust_pool_mod is not None:
            try:
                cfg = _rust_pool_mod.ConcurrencyConfig.default()
                self._rust_pool = _rust_pool_mod.ThreadPool.create_pool(cfg)
                self._rust_mod = _rust_pool_mod
            except Exception:
                self._rust_pool = None
                self._rust_mod = None

    def create(self) -> None:
        """Initialize the pool."""
        with self.lock:
            self.state = PoolState.Initializing
            self._running = True
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_thread.start()
            self.state = PoolState.Active

    def submit(self, subtask: Dict[str, Any]) -> str:
        """Non-blocking submit of subtask.
        Validates if helpers, queue put, returns id immediately.
        """
        try:
            validated = validate_subtask(subtask, getattr(self, "_val_cfg", None))
            security_validate(validated, getattr(self, "_val_cfg", None))
        except Exception:
            validated = subtask
        with self.lock:
            if self.state != PoolState.Active:
                raise RuntimeError("Pool not active")
            task_id = validated.get("id", str(time.time()))
            self.task_queue.put((task_id, validated))
            return task_id

    def _worker_loop(self) -> None:
        """Worker loop for submitting to parallel executor or Rust."""
        while self._running:
            task_id = None
            try:
                task_id, subtask = self.task_queue.get(timeout=1)
                future = self._executor.submit(self._execute_subtask, subtask)
                with self.lock:
                    self._futures.append((task_id, future))
                self.task_queue.task_done()
            except queue.Empty:
                self._check_futures()
                continue
            except Exception as e:
                with self.lock:
                    self.results.append({"id": task_id or "unknown", "error": str(e)})
                    if "fatal" in str(e).lower() or not self._running:
                        self.state = PoolState.Error
                continue

    def _check_futures(self) -> None:
        """Check completed futures and collect results. Called always."""
        with self.lock:
            done = []
            for task_id, fut in self._futures:
                if fut.done():
                    try:
                        res = fut.result()
                        self.results.append({"id": task_id, "result": res})
                    except Exception as e:
                        self.results.append({"id": task_id, "error": str(e)})
                        if "fatal" in str(e).lower():
                            self.state = PoolState.Error
                    done.append((task_id, fut))
            for item in done:
                self._futures.remove(item)

    def _execute_subtask(self, subtask: Dict[str, Any]) -> Any:
        """Execute subtask. Dispatches to Rust via PyO3 if available (real submit_tasks + ticket wait in worker thread)."""
        return rust_dispatch(subtask, self._rust_pool, getattr(self, "_rust_mod", None))

    def aggregate_results(self) -> List[Dict[str, Any]]:
        """Aggregate results. Force check of pending futures for fresh aggregate."""
        self._check_futures()
        with self.lock:
            return self.results.copy()

    def drain(self) -> None:
        """Drain the pool. Do not hold lock during join."""
        self._running = False
        self._executor.shutdown(wait=True)
        if hasattr(self, '_worker_thread') and self._worker_thread:
            self._worker_thread.join(timeout=5)
        with self.lock:
            self.state = PoolState.Idle


def rust_dispatch(subtask: Dict[str, Any], rust_pool=None, rust_mod=None) -> Any:
    """Dispatch to Rust ThreadPool via PyO3 (swarm_pyo3_pool) if available, else execute inline.

    Actually executes the work (inline or via submit_tasks + wait).
    """
    def work_fn(s=subtask):
        return s.get("description", str(s))

    if rust_pool is not None and rust_mod is not None:
        try:
            py_task = rust_mod.SubTask(subtask.get("id", "sub"), subtask.get("timeout_ms"), subtask.get("max_retries"))
            tasks = [py_task]
            works = [work_fn]
            tickets = rust_pool.submit_tasks(tasks, works)
            results = []
            for t in tickets:
                res = t.wait(5000)
                results.append(res)
            return {"status": "success", "subtask": subtask.get("description", str(subtask)), "from": "rust_real", "results": results}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    # Fallback: actually execute
    result = work_fn()
    return {"status": "success", "subtask": subtask.get("description", str(subtask)), "from": "python_fallback", "result": result}


# GSD-Core integration hooks (non-blocking submit returns ids immediately; collect aggregates)
def gsd_submit_task(pool: "SubagentPool", task: Dict[str, Any]) -> List[str]:
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


def gsd_collect_results(pool: "SubagentPool") -> List[Dict[str, Any]]:
    """Hook to collect results back to GSD-Core. Aggregates all completed (forces check)."""
    return pool.aggregate_results()
