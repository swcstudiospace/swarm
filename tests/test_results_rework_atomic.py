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


def _review_lineage(ts, target: str, corr: str) -> str:
    """A target failing its review gate: the gate task is leased on it (its rerun is what a rework reopens) and one
    consumer waits for the gate. Returns the gate's id."""
    _in_review(ts, target, corr)
    gate = f"G-{target}"
    ts.create(task_id=gate, correlation_id=corr, capability="code.backend", notes={"gate": "review", "gate_for": [target]})
    for step in ("VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS"):
        ts.transition(gate, step)
    ts.create(task_id=f"C-{target}", correlation_id=corr, capability="code.backend", depends_on=[gate])
    return gate


def _two_failing_targets(ts) -> None:
    for target in ("R-a", "R-b"):
        _review_lineage(ts, target, "rw")
    _failing_gate(ts)


@pytest.mark.parametrize("stop", [None, lambda: False], ids=["omitted", "never-true"])
def test_omitted_or_never_true_stop_reworks_every_target(swarm_dir, stop):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _two_failing_targets(ts)
    notes = reconcile(ts, "rw", _emit) if stop is None else reconcile(ts, "rw", _emit, stop_requested=stop)
    assert len(notes) == 2 and all("CHANGES_REQUESTED" in n for n in notes)
    for target in ("R-a", "R-b"):
        assert ts.get(target)["state"] == S.IN_PROGRESS.value and ts.get(target)["rework_loops"] == 1
        assert ts.get(f"G-{target}.r1")["state"] == S.PLANNED.value
        assert f"G-{target}.r1" in ts.get(f"C-{target}")["depends_on"]


def test_a_stop_before_the_first_target_acts_on_nothing(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _two_failing_targets(ts)
    assert reconcile(ts, "rw", _emit, stop_requested=lambda: True) == []
    for target in ("R-a", "R-b"):
        assert ts.get(target)["state"] == S.IN_REVIEW.value and ts.get(target)["rework_loops"] == 0
    assert not [t for t in ts.list(correlation_id="rw") if ".r1" in t["task_id"]]


def test_a_stop_raised_inside_an_admitted_rework_completes_its_lineage_then_stops(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _two_failing_targets(ts)
    raised: list[str] = []
    real = ts.transition

    def transition(task_id, to_state, **kwargs):
        out = real(task_id, to_state, **kwargs)
        # the stop arrives while the first rework's paired transaction is open
        if getattr(to_state, "value", to_state) == S.IN_PROGRESS.value and task_id.startswith("R-") and not raised:
            raised.append(task_id)
        return out

    ts.transition = transition
    notes = reconcile(ts, "rw", _emit, stop_requested=lambda: bool(raised))
    (done,) = raised
    other = "R-b" if done == "R-a" else "R-a"
    assert len(notes) == 1 and notes[0].startswith(f"{done}: CHANGES_REQUESTED")
    assert ts.get(done)["state"] == S.IN_PROGRESS.value and ts.get(done)["rework_loops"] == 1
    assert ts.get(f"G-{done}.r1")["state"] == S.PLANNED.value  # the admitted target's lineage is complete
    assert f"G-{done}.r1" in ts.get(f"C-{done}")["depends_on"]
    assert ts.get(other)["state"] == S.IN_REVIEW.value and ts.get(other)["rework_loops"] == 0
    assert not [t for t in ts.list(correlation_id="rw") if t["task_id"] == f"G-{other}.r1"]


def test_an_injected_failure_inside_an_admitted_rework_still_rolls_back_under_a_predicate(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _two_failing_targets(ts)
    _fail_on(ts, S.IN_PROGRESS.value)
    with pytest.raises(_Boom):
        reconcile(ts, "rw", _emit, stop_requested=lambda: False)
    for target in ("R-a", "R-b"):
        assert ts.get(target)["state"] == S.IN_REVIEW.value and ts.get(target)["rework_loops"] == 0
    assert not [t for t in ts.list(correlation_id="rw") if ".r1" in t["task_id"]]


def test_an_injected_failure_inside_an_admitted_approval_still_rolls_back_under_a_predicate(swarm_dir):
    from swarm.results import reconcile
    from swarm.taskstore import TaskStore, TaskState as S
    ts = TaskStore()
    _in_review(ts, "A-be", "ap")
    _no_gates(ts)
    _fail_on(ts, S.DONE.value)
    with pytest.raises(_Boom):
        reconcile(ts, "ap", _emit, stop_requested=lambda: False)
    assert ts.get("A-be")["state"] == S.IN_REVIEW.value
