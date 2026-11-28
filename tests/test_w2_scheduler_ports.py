"""scheduler_ports: transition table, frontier aging, dead-end defence (happy)."""
from swarm.scheduler_ports import (
    FrontierEntry, dead_end_blocked, is_legal_transition, order_frontier,
    LEGAL_TRANSITIONS,
)


def test_legal_transitions_are_exhaustive_and_sane():
    assert is_legal_transition("undiscussed", "discussing")
    assert is_legal_transition("planning", "planned")
    assert is_legal_transition("verification", "passed")
    # forbidden (B3-style): no path to passed without verification
    assert not is_legal_transition("executing", "passed")
    assert not is_legal_transition("undiscussed", "done-hold")
    assert len(LEGAL_TRANSITIONS) > 20


def test_frontier_excludes_held_done_and_downstream():
    entries = [
        FrontierEntry("9", priority=5),
        FrontierEntry("14.4", priority=9, holds=("held",)),
        FrontierEntry("14.7", priority=9, upstream_done=False),
        FrontierEntry("8", verdict="passed"),
        FrontierEntry("6", stage="executing", priority=1),
    ]
    got = [e.phase_id for e in order_frontier(entries)]
    assert got == ["9", "6"]


def test_frontier_zero_slots_defers_never_drops():
    assert order_frontier([FrontierEntry("9")], free_slots=0) == []


def test_starvation_aging_promotes_waiting_phase():
    entries = [FrontierEntry("new", priority=99, waves_waited=0),
               FrontierEntry("old", priority=1, waves_waited=3)]
    assert [e.phase_id for e in order_frontier(entries)] == ["old", "new"]


def test_dead_end_blocked_requires_plan_or_ruling():
    assert dead_end_blocked("failed", has_recovery_plan=False) is True
    assert dead_end_blocked("failed", has_recovery_plan=True) is False
    assert dead_end_blocked("failed", has_recovery_plan=False, superseded_by="r1") is False
    assert dead_end_blocked("gaps_found", has_recovery_plan=False) is False
