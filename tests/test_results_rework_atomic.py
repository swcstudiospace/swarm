"""P1-03 / P1-04: reconcile's paired transitions commit together. A failure on the second one must not strand the task
in CHANGES_REQUESTED or APPROVED, because reconcile only revisits IN_REVIEW."""
import pytest

_STEPS = ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW")


class _Boom(RuntimeError):
    pass


def _in_review(ts, tid: str, corr: str) -> None:
    ts.create(task_id=tid, correlation_id=corr, capability="code.backend", notes={})
    for step in _STEPS:
        ts.transition(tid, step)


def _fail_on(ts, target: str) -> None:
    """The store raises when asked to enter `target`; every other transition runs for real."""
    real = ts.transition

    def transition(task_id, to_state, **kwargs):
        if getattr(to_state, "value", to_state) == target:
            raise _Boom(target)
        return real(task_id, to_state, **kwargs)

    ts.transition = transition


def _no_gates(ts) -> None:
    """Nothing is required and nothing is missing, so the approve branch runs."""
    ts.required_gates = lambda task_id: []
    ts.missing_gate_reasons = lambda task_id: {}
    ts.missing_gates = lambda task_id: []
    ts.unaccepted_gates = lambda *a, **k: {}


def _failing_gate(ts) -> None:
    """A required gate that failed: reconcile takes the rework branch."""
    ts.required_gates = lambda task_id: ["review"]
    ts.latest_verdicts = lambda task_id, **k: {"review": {"verdict": "fail"}}


def _emit(*_a, **_k) -> None:
    return None


def test_rework_failure_on_second_transition_keeps_task_in_review(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _in_review(ts, "R-be", "rw")
    _failing_gate(ts)
    _fail_on(ts, S.IN_PROGRESS.value)
    with pytest.raises(_Boom):
        reconcile(ts, "rw", _emit)
    task = ts.get("R-be")
    assert task["state"] == S.IN_REVIEW.value
    assert task["rework_loops"] == 0


def test_rework_commits_changes_requested_then_in_progress(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _in_review(ts, "R-ok", "rw")
    _failing_gate(ts)
    reconcile(ts, "rw", _emit)
    task = ts.get("R-ok")
    assert task["state"] == S.IN_PROGRESS.value
    assert task["rework_loops"] == 1


def test_approve_failure_on_done_keeps_task_in_review(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _in_review(ts, "A-be", "ap")
    _no_gates(ts)
    _fail_on(ts, S.DONE.value)
    with pytest.raises(_Boom):
        reconcile(ts, "ap", _emit)
    assert ts.get("A-be")["state"] == S.IN_REVIEW.value


def test_approve_commits_approved_and_done(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _in_review(ts, "A-ok", "ap")
    _no_gates(ts)
    reconcile(ts, "ap", _emit)
    assert ts.get("A-ok")["state"] == S.DONE.value
