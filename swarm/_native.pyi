"""Type stubs for the PyO3 extension ``swarm._native`` (crates/gsd-python/src/lib.rs).

The stub declares exactly the names the extension exports: the error-code, retry, ``orch_status`` and redaction functions, and the
scheduler primitives and runtime of Phase 14.4 (``sched_*``, ``ClaimTable``, ``AimdLimiter``, ``run_scheduler``; logic in
crates/gsd-sched-core, wrappers in crates/gsd-python/src/{sched,sched_run}.rs). ``tests/typecheck`` and stubtest against the installed wheel
keep it in step. Every scheduler call that is given an invalid value raises ``ValueError`` whose message starts with the wire code
(``E-INPUT: <operation> failed: ...``); a Rust panic is ``E-INTERNAL`` and never a ``PanicException``.
"""

from typing import Self, final

__all__ = [
    "wire_codes",
    "code_from_wire",
    "retry_policy",
    "orch_status_invoke",
    "decode_reply_wire",
    "redact_spans",
    "sched_order_ready",
    "sched_normalize_claim",
    "sched_claims_overlap",
    "sched_backoff_s",
    "sched_drain_plan",
    "ClaimTable",
    "AimdLimiter",
    "run_scheduler",
    "NativeCleanupFence",
]

def wire_codes() -> list[str]: ...
def code_from_wire(code: str) -> str: ...
def retry_policy() -> dict[str, object]: ...
def orch_status_invoke(request: bytes) -> bytes: ...
def decode_reply_wire(reply: bytes) -> dict[str, object]: ...
def redact_spans(text: str) -> list[tuple[int, int]]: ...
def sched_order_ready(rows: list[tuple[str | None, int | None, float | None]]) -> list[int]:
    """Indices of ``rows`` (priority, dag_depth, created_at) in dispatch order: a stable permutation. A missing or unknown priority is ``P2``."""
    ...
def sched_normalize_claim(pattern: str) -> str:
    """The canonical form of a claim pattern; ``ValueError`` for an empty, absolute, ``~`` or ``..`` pattern."""
    ...
def sched_claims_overlap(a: str, b: str) -> bool:
    """Whether two claim patterns can match a common path, whatever their modes; ``ValueError`` for an invalid pattern."""
    ...
def sched_backoff_s(attempt: int, base_s: float, cap_s: float, jitter: float, unit: float) -> float:
    """Delay before retry ``attempt``; ``unit`` is the caller's jitter sample in [-1, 1]. ``ValueError`` for a jitter outside [0, 1] or a base or cap <= 0."""
    ...
def sched_drain_plan(
    cancel_grace_s: float, shutdown_budget_s: float, kill_grace_max_s: float, stop_grace_s: float, kill_settle_s: float
) -> tuple[float, float]:
    """The cancel drain schedule ``(grace_s, kill_grace_s)``; ``ValueError`` for a NaN argument."""
    ...

def run_scheduler(
    ports: object, *, cleanup_fence: NativeCleanupFence, max_concurrent: int, barrier: bool, once: bool, budget: int, max_rounds: int,
    fail_streak: int, cooldown_s: float, retry_cap_s: float, backoff_base_s: float, backoff_cap_s: float,
    wait_tick_s: float, internal_failure_limit: int, cancel_grace_s: float, shutdown_budget_s: float,
    kill_grace_s: float, kill_settle_s: float,
) -> dict[str, object]:
    """Run the existing native loop/pool with sixteen required resolved options, sixteen host callbacks and one fresh
    ``NativeCleanupFence``.

    Returns exactly rounds, dispatched, effective_parallel, closed, extension_down, abandoned, stopped and waiting.
    Loop callback exceptions retain their original object/type/traceback; worker faults are isolated via internal_failure.
    New native summaries are fixed safe constants, not exception text. Native panic hook support requires cooperative
    quiescent host hook setup at first entry; later/concurrent host hook replacement is unsupported.
    A loop exception stops dispatch but does not join in-flight native workers. ``cleanup_fence`` must be fresh: a claimed
    fence raises ``ValueError``. Wait on it until the run's native references are released before finalizing the interpreter.
    This does not add a worker join, hard preemption, or a guarantee for interpreter exit with stuck callbacks.

    Host records. ``begin_pass`` rows carry ``created_at`` and ``retry_after`` as ``None`` or real numbers. NaN is ``E-INPUT``:
    ``created_at`` has no ordering meaning for NaN. For ``retry_after`` the oracle would not hold a task back on NaN (``until > now``
    is false), but the binding refuses NaN so that a host never sends one by accident. ``+inf`` holds a task out of every pass;
    in continuous mode the loop then waits until cancel. ``-inf`` is accepted, as the oracle orders it. ``observe_pass`` receives
    a dict with exactly the keys ``round``, ``ready``, ``submitted``, ``effective_parallel``, ``free``, ``in_flight``, ``skipped``
    and ``unsubmitted``, in that order. ``skipped`` lists ``(task_id, reason)`` pairs in first-skipped order. ``unsubmitted`` lists
    ``(task_id, agent_id)`` leases granted in selection order whose dispatch is not in flight after a stop or a port error; the
    host must settle each pair, and a dispatch already in flight is never listed.
    """
    ...

@final
class NativeCleanupFence:
    """Single-use per-run native cleanup observer. ``wait`` is False while native references remain and True once they are
    released; True is terminal and repeatable, and a claimed fence is never reused. Thread-safe."""

    def __new__(cls) -> Self: ...
    def wait(self, timeout_s: float) -> bool:
        """Wait at most ``timeout_s`` seconds (zero polls for 0), releasing the GIL. ``ValueError`` (``E-INPUT``) for a fresh
        fence or an invalid timeout."""
        ...

@final
class ClaimTable:
    """Claims per owner; ``acquire`` is all-or-nothing and an owner never conflicts with itself. Thread-safe."""

    def __new__(cls) -> Self: ...
    def acquire(self, owner: str, claims: list[tuple[str, str]]) -> tuple[bool, list[tuple[str, str, str]]]:
        """Claims are ``(pattern, mode)``, mode ``"exclusive"`` or ``"shared"``. Returns ``(True, [])`` or ``(False, conflicts)``, each conflict
        ``(holder, held_pattern, requested_pattern)``. ``ValueError`` for an invalid pattern or mode, before anything changes."""
        ...
    def release(self, owner: str) -> int:
        """Drops everything ``owner`` holds and returns how many claims that was; idempotent."""
        ...
    def held(self) -> dict[str, list[tuple[str, str]]]:
        """Snapshot ``{owner: [(pattern, mode), ...]}`` in acquisition order."""
        ...

@final
class AimdLimiter:
    """Effective-parallelism limiter with the once-per-burst gate and the failure-streak circuit, on the caller's clock.

    ``now`` and ``submitted_at`` are monotonic seconds, finite and in [0, 1e9]. ``ValueError`` unless ``1 <= min_n <= max_n``,
    ``cooldown_s`` is finite and >= 0 and ``retry_cap_s`` is finite and > 0. ``streak_threshold`` <= 0 turns the circuit off. Thread-safe."""

    def __new__(cls, max_n: int, min_n: int, cooldown_s: float, retry_cap_s: float, streak_threshold: int) -> Self: ...
    def current(self) -> int: ...
    def on_success(self, now: float) -> bool:
        """True when the limit grew (it never does during a cooldown)."""
        ...
    def on_rate_limit(self, now: float, retry_after_s: float | None = None) -> bool:
        """A bare signal: halves on every call, no burst dedupe. True when the limit dropped."""
        ...
    def on_failure(self, now: float, task_id: str) -> bool:
        """A failure that is not a rate limit. True when it tripped the streak circuit and halved the limit."""
        ...
    def on_burst_hit(self, submitted_at: float, now: float, retry_after_s: float | None = None) -> bool:
        """A rate-limit hit through the once-per-burst gate. True when the limit dropped; False for the same burst and for a hit at the floor."""
        ...
