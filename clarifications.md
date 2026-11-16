# Blocking Clarifications for Multithreading Subagent Module (v2.4)

From the ultrathink synthesized plan (graph ut-9a65b6c900-8ba8ba75).

## q1 Pool Size
Recommended: Configurable max default 30
Rationale: Supports dynamic sizing up to limit with sensible default, enables scaling states without hardcoding.

## q2 GSD Interface
Recommended: Abstract JSON functions
Rationale: Loose coupling via dict/JSON-like payloads, easiest to adapt without knowing internals.

## q3 Subagent Model
Recommended: Dynamically instantiated
Rationale: Pool creates on demand from templates, supports scaling and pluggable nature.

## q4 Error Recovery (non-blocking)
Recommended: Bounded retries + timeouts
Rationale: Default 3 retries, per-task timeout, cancel support, isolate single failures.

These are recorded as settled for the implementation.

## Resolution from Repository Evidence (q1-q3 only; appended per assignment)

**Evidence files read (explicit paths):**
- clarifications.md (current)
- crates/concurrency/src/lib.rs
- crates/concurrency/src/execution.rs (ConcurrencyConfig, ThreadPool)
- crates/concurrency/Cargo.toml
- crates/gsd-pool/src/lib.rs (PoolConfig)
- crates/gsd-sched-core/src/pool.rs (Pool)
- swarm/_native.pyi (ChatPool, run_scheduler)
- swarm/tool_registry.py (call_tool, payloads)
- swarm/subagent_lifecycle.py (defaults, SubagentLifecycleManager)
- swarm/AGENTS.md (30 agents)
- python/orchestrator/pool.py (PoolConfig, SubagentPool)
- python_tools/orchestration.py (stubs), python_tools/gsd_integration.py
- python_tools/decomposition.py
- docs/assumptions.md
- scripts/orch_assign.py, scripts/orch_plan.py, omp/src/tools.ts (for interfaces)
- swarm/lifecycle.py, etc. (via grep for 30, pool, assignment)

**q1 Pool Size**
Recommended default from code: configurable; workers default 4 (ConcurrencyConfig), max_threads default 30 (hard cap in gsd-pool/PoolConfig/ChatPool/python side), swarm global default_max_parallel=4 / ceiling=32, per-agent max_parallel from agents.json.
Evidence quotes (no invention):
- crates/concurrency/src/execution.rs:237: `workers: 4,`
- crates/concurrency/src/execution.rs:226: `pub struct ConcurrencyConfig { pub workers: usize, ... }`
- swarm/_native.pyi:193: `workers: int = 4,`
- swarm/_native.pyi:196: `max_threads: int = 30,`
- crates/gsd-pool/src/lib.rs:86: `max_threads: 30,`
- python/orchestrator/pool.py:79: `max_threads: int = 30`
- swarm/subagent_lifecycle.py:42: `DEFAULT_MAX_PARALLEL = 4`
- swarm/subagent_lifecycle.py:43: `DEFAULT_MAX_PARALLEL_CEILING = 32`
- AGENTS.md:36: "The global cap is `--max-parallel` (default `SWARM_MAX_PARALLEL`, else 4), at most `SWARM_MAX_PARALLEL_CEILING` (32)"
- docs/assumptions.md:41: "Pool size 30 = enforced configurable maximum (session cap 30; `--max-parallel` default 4, ceiling 32 in Python port; per-agent max_parallel; PoolConfig{max_threads:30}"
- AGENTS.md:7: "Design spec for a 30-agent SDLC swarm, A01 ORCH through A30 CUSTODY"
Pool size param: ConcurrencyConfig.workers (or equiv max_threads) is the configurable param.

**q2 GSD Interface**
Recommended: Abstract JSON functions (loose coupling via dict/JSON-like payloads).
Evidence quotes:
- swarm/tool_registry.py:280: `if not isinstance(payload, dict): return {"state": "INVALID_INPUT", ... "payload must be an object"}`
- swarm/tool_registry.py:225: `def call_tool( name: str, caller: str, payload: object, ... ) -> dict[str, Any]:`
- omp/src/tools.ts:103: `export type ToolCallParams = { name: string; input: Record<string, unknown> };`
- omp/src/tools.ts:89: `export type AssignmentParams = { task_id: string; correlation_id: string };`
- scripts/orch_assign.py:64: checks `envelope.get("schema") != "swarm.v1.task.assign"`
- swarm/envelope.py (via refs): JSON envelopes for task.assign
- python_tools/gsd_integration.py:16: `task: Dict[str, Any]`
- python_tools/orchestration.py:54: `subtask: Dict[str, Any]`
- docs/assumptions.md:17: "Contracts frozen per n4/n6: argv/exit/JSON shapes identical;"
- AGENTS.md:140: "swarm_tool_call {name, input} (one registry tool, result object returned unchanged"
GSD uses existing swarm_tool / swarm_assignment (JSON dict payloads for assignments, tool calls, results).

**q3 Subagent Model**
Recommended: Dynamically instantiated (from templates/slugs).
Evidence quotes:
- AGENTS.md:7: "30-agent SDLC swarm, A01 ORCH through A30 CUSTODY"
- swarm/_native.pyi:206: `def open_thread(self, thread_id: str, subagent_type: str) -> None:`
- python/orchestrator/pool.py:27: `def __init__(self, size: int = 30):`
- python/orchestrator/pool.py:54: `def submit(self, subtask: Dict[str, Any]) -> str:`
- crates/gsd-pool/src/lib.rs:101: `threads: DashMap<String, SubagentType>,`
- swarm/subagent_lifecycle.py:143: `class SubagentLifecycleManager:`
- AGENTS.md:92: "A01 then dispatches `a02`–`a30` by slug."
- concurrency execution: workers created in loop `for i in 0..config.workers` on demand.
30 fixed agent types (templates by slug in agents.json/swarm/tools/aNN_tools.py), instantiated dynamically by pools/threads/lifecycle on dispatch/assignment. Matches existing swarm_tool/assignment flow.

These resolutions recorded from direct code reads/greps (no invention). Update plan defaults if needed. Paths listed above.
 (end append)

## q2 Pool Resize (from blocking clarifications in ultrathink spec)
Recommended: Fixed at creation
Rationale from repo evidence: Pool size (workers) is set at ThreadPool::new(ConcurrencyConfig) and PoolConfig; no resize method or runtime capacity change API exists. Scaling state is internal (for batch/queue), not pool worker count change. Matches swarm max_parallel at start, gsd-pool warm_pool_size fixed in config.
Evidence quotes:
- crates/concurrency/src/execution.rs:363: `pub fn new(config: ConcurrencyConfig) -> Result<Self, TaskError> { ... workers: Mutex::new(Vec::with_capacity(config.workers)) ... }`
- crates/concurrency/src/execution.rs:318: `pub workers: usize` in ConcurrencyConfig (no resize)
- crates/gsd-pool/src/lib.rs:106: `pub warm_pool_size: usize` (fixed in config)
- swarm/_native.pyi:193: `workers: int = 4` (param at creation)
- No resize, scale, or set_workers in execution.rs, lib.rs, gsd-pool, or pyo3 bindings.
Pool size fixed at creation time (configurable at init, up to 30 cap). Scaling is for internal state, not dynamic worker resize.

Recorded as settled. No invention; derived from repo code.
(end append for q2)
