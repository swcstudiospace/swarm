# Agent Swarm Usage (n7 Wave 4 SOTA Integration)

This documents the integrated Python orchestration layers + Rust concurrency primitives (via PyO3 binding) for concurrent sub-task execution.

Refs: ultrathink n7, plan/execution-waves.md, crates/concurrency, python/orchestrator/{pool,decomposer}.py, pyo3/bindings.

## Setup

Python:
```python
from python.orchestrator.decomposer import TaskDecomposer, StructuredTask
from python.orchestrator.pool import OrchestrationPool, PoolConfig, TaskPhase
import swarm_pyo3_pool as rust_pool  # built via maturin or cargo + .so in path (see tests)
```

Rust side available through binding for SOTA performance/observability.

Run with `SWARM_PURE_PYTHON=1` for pure oracles (conftest default).

## Example Usage

### Decompose then concurrent execute (cross layer)

```python
decomp = TaskDecomposer()
task = {"id": "T-42", "description": "implement feature with parallel subtasks and ordered steps", "deps": []}
subs = decomp.decompose(task)  # produces SubTask with priority, decomp_mode=independent/ordered

p = OrchestrationPool(PoolConfig(max_threads=8))
tid = await p.submit(task)
# internal uses decomp + async tasks for concurrency

# Rust SOTA layer (binding)
cfg = rust_pool.ConcurrencyConfig.default()
pool = rust_pool.ThreadPool.create_pool(cfg)
print("pool status:", pool.status())  # observable: active_threads, queue_depth, state
stasks = [rust_pool.SubTask(f"sub-{s.id}", 10000, 0) for s in subs[:4]]
# works are python callables executed by rust workers (bidir)
def work_for(sub): 
    def w(): 
        # do work, may raise for failure
        return f"completed {sub.id}"
    return w
tickets = pool.submit_tasks(stasks, [work_for(s) for s in subs[:4]])
results = [t.wait(5000) for t in tickets]
print([(r.kind, r.value, r.error) for r in results])
pool.shutdown(100)
```

Observable concurrent: starts of work overlap (measured spread << sum sleeps).

### Query/aggregate/cancel

```python
st = await p.query(tid)
agg = await p.aggregate()
await p.cancel(tid)
stats = p.get_stats()  # by_state, active etc
```

### Rust direct (for perf critical)

```python
from python.orchestrator.pool import classify_error
# ...
```

## Workflows

1. Intake -> Decomposer (keyword independent vs ordered) -> Pool submit (nonblock) -> async runners or rust dispatch.
2. Parallel fan-out for independent subs; topo for ordered.
3. GSD hooks for lifecycle (submit/complete/state).
4. Metrics/observability via pool.status + rust metrics.
5. Aggregation collects partials + successes.

Full flow exercised in integration_test.py and python/orchestrator/pool tests.

## Failure Modes + Error Propagation

- Invalid config: E-CAPACITY / ValueError on validate (workers=0, >30).
- Timeout (per sub or overall): E-TIMEOUT, SubResult partial/failure; worker sets Partial on deadline.
- Cancel: cooperative via token/shutdown; yields CANCELLED or Partial Cancelled. E-TIMEOUT alias for some.
- Work error (exception in callable): caught, SubResult::Failure, mapped via classify_error to E- code (E_INPUT for Value/Key, E_TIMEOUT for Cancel/Timeout, E_DEP for OS, E_INTERNAL else). Retries use backoff if max_retries >0.
- Partial results on batch error: successes continue; failed marked; join returns mixed.
- Queue full: E-CAPACITY.
- Panic in work: WorkerPanic wrapped to Failure.
- Python <-> Rust: SwarmError carries code + details; rust TaskError -> Py with E- prefix.
- Capacity: hard 30 threads; submit tracks but may queue.
- State seal: terminal states (SUCCEEDED/FAILED/CANCELLED) reject transitions -> SwarmError("illegal state transition").

See also swarm/errors.py classify, CentralErrorAggregator for partials.

Error always propagates: rust failure -> slot/ticket -> py SubResult -> classify -> SwarmError.

## Verification Script + Acceptance

Run:
```bash
cargo test -p concurrency -p pyo3-bindings -- --test-threads=1
SWARM_PURE_PYTHON=1 python -m pytest tests/integration_test.py -q --tb=line -rA
python scripts/verify_n7_wave4.py   # runs suites + greps for concurrent evidence + error paths
```

Acceptance (exercised):
- Full pytest suite passes (pure + integration).
- Full cargo test passes (concurrency 6+ tests, binding).
- Observable concurrent sub-task: multiple starts within << sequential time; active_threads>1 or enqueue nonblock + progress.
- Correct error propagation: failing work -> Failure kind + error str, classified E-*, no silent loss, partials on batch.

See tests/integration_test.py for concrete cases + timing asserts.

## Related

- python/orchestrator/pool.py (states, hooks, caps)
- crates/concurrency/src/execution.rs (ThreadPool, SubResult, TaskError, parallel_progress test)
- swarm/errors.py
