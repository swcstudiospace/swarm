"""Scheduler ports (W2.1) — interfaces + pure scheduling rules.

Design: docs/design-orchestration-SPE-38340.md §§3-4, 10.
Layer: api/service boundary (data modules implement these Protocols).

Reuse / mapping to existing code (no duplication):
- Time: ``swarm.clock.Clock`` / ``SYSTEM_CLOCK`` / ``FakeClock`` — this module
  re-exports the names so callers depend only on the port.
- Errors: ``swarm.errors.SwarmError`` / ``ErrorCode``.
- Task DAG validation: ``swarm.graph_planner.plan_graph`` remains the
  plan-time cycle/dangling authority; this module does not reimplement it.
- Rust scheduler binding (``swarm.rust_scheduler``) is a *runtime* binding,
  not a port: ports stay pure Python so A05/A06 implementations are
  substitutable without native code.

GAPS recorded (new code needed beyond existing modules):
- G-SCHED-1: No existing port/protocol for phase-state / tracker / sync.
  Provided here as ``Protocol`` classes.
- G-SCHED-2: ``graph_planner`` priority sort has no aging (graphcheck §5
  starvation risk). ``order_frontier`` adds bounded aging.
- G-SCHED-3: ``TaskNode`` has no tool field (graphcheck gap); ports carry
  no per-node tool field either — tool range stays per-agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from .clock import SYSTEM_CLOCK, Clock, FakeClock  # noqa: F401  (re-exported port)

__all__ = [
    "SYSTEM_CLOCK", "Clock", "FakeClock",
    "WorkflowStage", "PhaseVerdict", "HoldKind", "AwaitingKind",
    "PhaseStatePort", "TrackerPort", "SyncPort", "StateFilePort",
    "LEGAL_TRANSITIONS", "is_legal_transition", "is_done_eligible",
    "order_frontier", "FrontierEntry", "dead_end_blocked",
]


class WorkflowStage(str, Enum):
    UNDISCUSSED = "undiscussed"
    DISCUSSING = "discussing"
    DISCUSSED = "discussed"
    PLANNING = "planning"
    PLANNED = "planned"
    EXECUTING = "executing"
    VERIFICATION = "verification"
    DONE_HOLD = "done-hold"


class PhaseVerdict(str, Enum):
    UNVERIFIED = "unverified"
    PASSED = "passed"
    PASSED_WITH_DEBT = "passed_with_recorded_debt"
    GAPS_FOUND = "gaps_found"
    HUMAN_NEEDED = "human_needed"
    FAILED = "failed"
    NOT_RUN = "not_run"


DONE_ELIGIBLE: frozenset[PhaseVerdict] = frozenset(
    {PhaseVerdict.PASSED, PhaseVerdict.PASSED_WITH_DEBT}
)


class HoldKind(str, Enum):
    HELD = "held"
    BLOCKED_HOLD = "blocked_hold"
    CONFLICT_HOLD = "conflict_hold"
    PAUSED_USER_CHOICE = "paused_user_choice"


class AwaitingKind(str, Enum):
    VERIFIER_RERUN = "verifier_rerun"
    CI_EVIDENCE = "ci_evidence"
    USER_RULING = "user_ruling"
    USER_CONFIRMATION = "user_confirmation"
    EXTERNAL_APPROVAL = "external_approval"


# §4.3 normative transition table (stage transitions only; verdict guards
# are enforced by phase_state_store). "*" expansion is explicit below.
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset({
    ("undiscussed", "discussing"),
    ("discussing", "discussed"),
    ("discussing", "human_needed"),
    ("discussed", "planning"),
    ("planning", "planned"),
    ("planned", "executing"),
    ("executing", "verification"),
    ("verification", "passed"),
    ("verification", "passed_with_recorded_debt"),
    ("verification", "gaps_found"),
    ("gaps_found", "verification"),
    ("human_needed", "verification"),
    ("blocked_hold", "verification"),
    ("failed", "executing"),
    # "* -> gaps_found / human_needed / blocked_hold" (§4.3):
    ("undiscussed", "gaps_found"), ("undiscussed", "human_needed"),
    ("undiscussed", "blocked_hold"),
    ("discussing", "gaps_found"), ("discussing", "blocked_hold"),
    ("discussed", "gaps_found"), ("discussed", "human_needed"),
    ("discussed", "blocked_hold"),
    ("planning", "gaps_found"), ("planning", "human_needed"),
    ("planning", "blocked_hold"),
    ("planned", "gaps_found"), ("planned", "human_needed"),
    ("planned", "blocked_hold"),
    ("executing", "gaps_found"), ("executing", "human_needed"),
    ("executing", "blocked_hold"),
    ("verification", "human_needed"), ("verification", "blocked_hold"),
    ("done-hold", "gaps_found"), ("done-hold", "human_needed"),
    ("done-hold", "blocked_hold"),
    # Verdict-rest states park back to verification via human_needed loop:
    ("gaps_found", "human_needed"), ("gaps_found", "blocked_hold"),
    ("human_needed", "blocked_hold"), ("failed", "gaps_found"),
    ("failed", "human_needed"), ("failed", "blocked_hold"),
})


def is_legal_transition(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in LEGAL_TRANSITIONS


def is_done_eligible(verdict: str | PhaseVerdict) -> bool:
    v = verdict if isinstance(verdict, PhaseVerdict) else PhaseVerdict(verdict)
    return v in DONE_ELIGIBLE


@runtime_checkable
class PhaseStatePort(Protocol):
    def get_phase_state(self, phase_id: str) -> dict[str, Any]: ...
    def transition_phase(self, phase_id: str, to_stage: str, **kw: Any) -> dict[str, Any]: ...


@runtime_checkable
class TrackerPort(Protocol):
    def create_or_return(self, *, idempotency_key: str, fake_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def update(self, *, idempotency_key: str, fake_id: str, body: dict[str, Any]) -> dict[str, Any]: ...
    def get(self, fake_id: str) -> dict[str, Any] | None: ...
    def list_all(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class SyncPort(Protocol):
    def enqueue(self, intent: dict[str, Any]) -> dict[str, Any]: ...
    def deliver_all(self) -> list[dict[str, Any]]: ...
    def list_dead_letter(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class StateFilePort(Protocol):
    def read_state(self) -> dict[str, Any]: ...
    def write_state(self, state: dict[str, Any]) -> None: ...


@dataclass(frozen=True)
class FrontierEntry:
    phase_id: str
    priority: float = 0.0
    waves_waited: int = 0
    upstream_done: bool = True
    stage: str = "planned"
    verdict: str = "unverified"
    holds: tuple[str, ...] = ()


def dead_end_blocked(verdict: str | PhaseVerdict, *, has_recovery_plan: bool,
                     superseded_by: str | None = None) -> bool:
    """§4.4: dependents of a failed phase are blocked unless the failed phase
    has a recovery plan (failed->executing) or a superseded_by ruling."""
    v = verdict if isinstance(verdict, PhaseVerdict) else PhaseVerdict(verdict)
    if v is not PhaseVerdict.FAILED:
        return False
    return not (has_recovery_plan or bool(superseded_by))


def order_frontier(entries: list[FrontierEntry], *, starve_threshold: int = 3,
                   free_slots: int | None = None) -> list[FrontierEntry]:
    """Runnable set + (priority desc, phase_id asc) with starvation aging.

    - Held / upstream-not-done / non-runnable-stage / done-eligible verdicts
      are excluded (never runnable).
    - ``free_slots == 0`` defers (returns []) instead of dropping work.
    - Entries waiting >= ``starve_threshold`` waves are promoted ahead of
      newly-runnable higher-priority entries (aging, graphcheck §5 fix).
    """
    if free_slots is not None and free_slots <= 0:
        return []
    runnable = [
        e for e in entries
        if e.upstream_done
        and e.stage in ("planned", "executing")
        and not e.holds
        and not is_done_eligible(e.verdict)
    ]
    starved = sorted((e for e in runnable if e.waves_waited >= starve_threshold),
                     key=lambda e: (-e.waves_waited, e.phase_id))
    fresh = sorted((e for e in runnable if e.waves_waited < starve_threshold),
                   key=lambda e: (-e.priority, e.phase_id))
    return starved + fresh
