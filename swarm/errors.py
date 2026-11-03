"""Shared error taxonomy — every agent MUST map exceptions onto one of these codes."""
from __future__ import annotations
from enum import Enum
import random
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .taskstore import TaskStore


class ErrorCode(str, Enum):
    E_INPUT = "E-INPUT"          # invalid/missing input artifact
    E_TIMEOUT = "E-TIMEOUT"      # budget/lease/SLA exceeded
    E_DEP = "E-DEP"              # upstream dependency unavailable
    E_CAPACITY = "E-CAPACITY"    # overload / WIP cap hit
    E_CONTRACT = "E-CONTRACT"    # output rejected by consumer/schema
    E_POLICY = "E-POLICY"        # blocked by security/compliance policy (fail-closed)
    E_INTERNAL = "E-INTERNAL"    # unexpected agent fault

    @property
    def standard_handling(self) -> str:
        return _HANDLING[self]


_HANDLING = {
    ErrorCode.E_INPUT: "Nack + task.status FAILED; notify artifact producer",
    ErrorCode.E_TIMEOUT: "Checkpoint, release lease, requeue once, then ESCALATED",
    ErrorCode.E_DEP: "Exponential backoff 1s→60s (jitter, max 5), then degraded mode",
    ErrorCode.E_CAPACITY: "Refuse bid; backpressure in next heartbeat",
    ErrorCode.E_CONTRACT: "Bounded fix loop per agent spec",
    ErrorCode.E_POLICY: "Fail-closed; escalate to A10/A01",
    ErrorCode.E_INTERNAL: "Checkpoint + crash; A01 requeue; 3 strikes ⇒ ESCALATED",
}


class SwarmError(Exception):
    """Exception carrying a taxonomy code so scripts can report it uniformly."""

    def __init__(self, code: ErrorCode | str, message: str, **details: object):
        self.code = ErrorCode(code)
        self.details = details
        super().__init__(f"{self.code.value}: {message}")

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code.value, "message": str(self), "details": self.details,
                "handling": self.code.standard_handling}


def classify(exc: BaseException) -> ErrorCode:
    """Best-effort mapping of arbitrary exceptions onto the taxonomy."""
    if isinstance(exc, SwarmError):
        return exc.code
    if isinstance(exc, (FileNotFoundError, KeyError, ValueError)):
        return ErrorCode.E_INPUT
    if isinstance(exc, TimeoutError):
        return ErrorCode.E_TIMEOUT
    if isinstance(exc, PermissionError):  # before OSError: PermissionError subclasses it
        return ErrorCode.E_POLICY
    if isinstance(exc, (ConnectionError, OSError)):
        return ErrorCode.E_DEP
    return ErrorCode.E_INTERNAL


def _exp_backoff_err(attempt: int, base: float = 1.0) -> float:
    """1s base + jitter for ErrorHandler 5 att retries."""
    sleep = min(base * 2.0 ** attempt, 30.0)
    jitter = random.uniform(0, 0.3)
    return sleep + jitter


class ErrorHandler:
    """Error handler unit for swarm lifecycle.

    Responsibilities (per assignment):
    - 5 retries (DEFAULT_MAX_ATTEMPTS) w/ 1s + jit backoff
    - timeout handling (maps to E-TIMEOUT, FAILED path)
    - parallelism detection (via batch/max_parallel)
    - degrade to sequential (effective max=1) only when a caller latches it (should_degrade / force_degrade); the
      scheduler no longer does for an ordinary FAILED: it isolates the failure and uses a consecutive-failure circuit
    - integrate with lifecycle (TaskStore transitions, dispatchable retry ladder)
    - output .spec() describing all error paths
    - CentralErrorAggregator per ID for details/partials/retry surfaced

    Acceptance: retries=5, timeout paths, parallel flag, degrade observable.
    """
    DEFAULT_MAX_ATTEMPTS = 5

    def __init__(self, max_attempts: int | None = None):
        try:
            self.max_attempts = max_attempts or self.DEFAULT_MAX_ATTEMPTS
            self._parallel_detected = False
            self._degraded = False
            self._errors_seen: list[str] = []
            self._lock = threading.Lock()
            self.aggregator = CentralErrorAggregator()
        except Exception:
            self.max_attempts = 5
            self._parallel_detected = False
            self._degraded = False
            self._errors_seen = []
            self._lock = threading.Lock()
            self.aggregator = CentralErrorAggregator()

    def detect_parallel(self, batch_size: int, max_parallel: int) -> bool:
        """Record and return whether this dispatch round uses parallelism."""
        is_parallel = bool(batch_size > 1 or max_parallel > 1)
        if is_parallel:
            with self._lock:
                self._parallel_detected = True
        return is_parallel

    def should_degrade(self, code: ErrorCode | None = None, exc: BaseException | None = None) -> bool:
        """Decide and latch degrade to sequential mode.
        Any qualifying error while parallel_detected latches degraded=True.
        """
        if self._degraded:
            return True
        if not self._parallel_detected:
            return False
        if code is None and exc is not None:
            try:
                code = classify(exc)
            except Exception:
                code = ErrorCode.E_INTERNAL
        if code in (ErrorCode.E_TIMEOUT, ErrorCode.E_INTERNAL, ErrorCode.E_CONTRACT, ErrorCode.E_DEP):
            with self._lock:
                self._degraded = True
                self._errors_seen.append(f"{code.value if code else 'exc'}")
            return True
        return False

    def force_degrade(self, reason: str = "error in parallel execution") -> bool:
        """Explicitly latch degrade to sequential (for runner-detected error paths like outcome FAILED)."""
        if not self._degraded:
            with self._lock:
                self._degraded = True
                self._errors_seen.append(reason)
            return True
        return False

    def effective_parallel(self, requested: int) -> int:
        """The worker count to use for ThreadPool; 1 when degraded."""
        if self._degraded:
            return 1
        return max(1, int(requested or self.DEFAULT_MAX_ATTEMPTS))

    def _backoff(self, attempt: int) -> float:
        """Support 1s + jit backoff for extended 5 att."""
        try:
            return _exp_backoff_err(attempt)
        except Exception:
            return 1.0 + (attempt * 0.1)

    def handle_timeout(self, store: "TaskStore", tid: str, reason: str = "E-TIMEOUT: task_timeout exceeded") -> str:
        """Timeout specific path integrated to lifecycle. Record to agg per ID."""
        from .taskstore import TaskState as S
        try:
            store.transition(tid, S.FAILED, reason=reason)
            self.aggregator.record(tid, {"reason": reason, "code": "E-TIMEOUT"}, is_partial=True)
        except Exception:
            pass  # isolate
        return "FAILED"

    def handle_error(self, store: "TaskStore", tid: str, exc: BaseException, prefix: str = "") -> str:
        """Unified error path: classify, check degrade, transition FAILED (retry ladder upstream).
        Use Central agg per ID, support partial/retry surfaced.
        """
        from .taskstore import TaskState as S
        try:
            code = classify(exc)
            self.should_degrade(code=code)
            reason = (prefix + f"{code.value}: {str(exc)[:300]}").strip()
            store.transition(tid, S.FAILED, reason=reason)
            with self._lock:
                self._errors_seen.append(reason)
            # per ID agg for details/partials/retry
            is_partial = "PARTIAL" in reason.upper() or code in (ErrorCode.E_TIMEOUT, ErrorCode.E_DEP)
            self.aggregator.record(tid, {"code": code.value, "msg": str(exc)[:200], "prefix": prefix}, is_partial=is_partial, retry=True)
            return "FAILED"
        except Exception:
            # isolate error handling
            try:
                store.transition(tid, S.FAILED, reason="E-INTERNAL: handler isolate")
            except Exception:
                pass
            return "FAILED"

    def spec(self) -> dict[str, Any]:
        """Static contract for this handler unit: ladder, degrade paths, version."""
        return {
            "retries": self.max_attempts,
            "retry_ladder": "dispatchable: FAILED (attempt < max_attempts) -> RETRY -> re-dispatch; max reached -> ESCALATED",
            "timeout_paths": [
                "run_agent_headless raises AgentTimeout -> _execute_one catches: evidence + FAILED(E-TIMEOUT)",
                "gate communicate TimeoutExpired -> FAILED(E-TIMEOUT)",
                "recover_stranded detects past running_deadline -> FAILED(E-TIMEOUT: stranded)",
                "E_TIMEOUT handling per ErrorCode: checkpoint/release/requeue once then escalate"
            ],
            "parallel_detection": "detect_parallel(len(batch), max_p) sets _parallel_detected when >1 concurrent",
            "degrade_paths": [
                "if parallel_detected and error code in {E_TIMEOUT, E_INTERNAL, E_CONTRACT, E_DEP} -> latch _degraded (callers of should_degrade/handle_error)",
                "effective_parallel() returns 1 while degraded (sticky for the handler's life)",
                "scripts/swarm_run.py does NOT call force_degrade for an ordinary FAILED task: a failure is isolated (retry/escalation ladder only) and parallelism is unchanged",
                "systemic trouble is a circuit in the runner: SWARM_FAIL_STREAK (default 3, 0 = off) consecutive failures of distinct tasks halve the AIMD limiter once per streak (swarm.degraded reason consecutive_failures); a success resets it"
            ],
            "lifecycle_integration": [
                "TaskStore: LEGAL_TRANSITIONS allow CLAIMED/IN_PROGRESS -> FAILED/RETRY; FAILED -> RETRY/ESCALATED",
                "execute_one/_execute_one catch blocks use handle_* to ensure uniform FAILED",
                "dispatchable + reconcile close the loop for retries and gate errors"
            ],
            "classification": "classify() + SwarmError.code map all to ErrorCode; handler uses it for decide",
            "error_codes": [e.value for e in ErrorCode],
            "standard_handling": _HANDLING,
            "acceptance": "retries=5, timeout paths exercised, parallel detected, degrade on error, agg perID",
            "version": "1.0-error-handler-unit-resilience",
            "backoff": "1s base + jitter for 5 attempts"
        }

    def status(self) -> dict[str, Any]:
        """Current runtime state of the handler unit. Surface agg info."""
        with self._lock:
            agg_status: dict[str, Any] = {}
            try:
                agg_status = self.aggregator.status() if hasattr(self.aggregator, 'status') else {}
            except Exception:
                pass
            return {
                "degraded": self._degraded,
                "parallel_detected": self._parallel_detected,
                "recent_errors": self._errors_seen[-3:],
                "effective_max_parallel_hint": 1 if self._degraded else "requested",
                "aggregator": agg_status,
            }


class CentralErrorAggregator:
    """Central per-ID aggregator for error details, partials, retry surfaced.
    Used by ErrorHandler, wired to obs/state for gsd perID partial on FAILED/ERROR.
    """
    def __init__(self) -> None:
        self._per_id: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def record(self, sub_id: str, error: dict[str, Any] | str, is_partial: bool = False, retry: bool = False, **kw: object) -> None:
        """Record error detail per ID, mark partial/retry for surfacing."""
        try:
            with self._lock:
                if sub_id not in self._per_id:
                    self._per_id[sub_id] = {"errors": [], "partials": [], "retries": 0, "last": None, "details": {}}
                entry = {"ts": time.time(), "error": error, **kw}
                self._per_id[sub_id]["errors"].append(entry)
                if is_partial:
                    self._per_id[sub_id]["partials"].append(entry)
                if retry:
                    self._per_id[sub_id]["retries"] += 1
                self._per_id[sub_id]["last"] = entry
                self._per_id[sub_id]["details"].update(kw)
        except Exception:
            pass  # isolate

    def get(self, sub_id: str) -> dict[str, Any]:
        """Get per ID agg for emit in obs/state."""
        with self._lock:
            return dict(self._per_id.get(sub_id, {"errors": [], "partials": [], "retries": 0}))

    def surfaced(self, sub_id: str) -> dict[str, Any]:
        """Surfaced view for gsd/obs: details + partials + retry count."""
        d = self.get(sub_id)
        return {
            "subagent_id": sub_id,
            "error_details": d.get("errors", [])[-5:],
            "partials": d.get("partials", []),
            "retry_count": d.get("retries", 0),
            "has_errors": len(d.get("errors", [])) > 0,
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ids_tracked": len(self._per_id),
                "sample": list(self._per_id.keys())[:3],
            }
