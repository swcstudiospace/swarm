"""``swarm.rust_scheduler`` driven through the real native binding with a small fake controller.

The native loop and its worker pool are real. The controller, store, leases, claims and telemetry are fakes that record what
the facade does to them. The swarm-package helpers ``reconcile`` and ``log_phase`` are replaced by no-ops because they write
the task store and the run logs. The runner-local helpers are passed in as fakes, as the facade expects.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

native = pytest.importorskip("swarm._native")
if not hasattr(native, "run_scheduler"):  # pragma: no cover - the extension always exports it
    pytest.skip("swarm._native does not export run_scheduler", allow_module_level=True)

from swarm import rust_scheduler  # noqa: E402
from swarm.rust_scheduler import RunnerHelpers, RunSettings, RustScheduler  # noqa: E402
from swarm import workspace  # noqa: E402
from swarm.envelope import build_envelope, sign_envelope, verify_envelope  # noqa: E402

EIGHT_KEYS = {"rounds", "dispatched", "effective_parallel", "closed", "extension_down", "abandoned", "stopped", "waiting"}
SEVENTEEN_KEYWORDS = {
    "cleanup_fence", "max_concurrent", "barrier", "once", "budget", "max_rounds", "fail_streak", "cooldown_s",
    "retry_cap_s", "backoff_base_s", "backoff_cap_s", "wait_tick_s", "internal_failure_limit", "cancel_grace_s",
    "shutdown_budget_s", "kill_grace_s", "kill_settle_s",
}
AGENT = {"id": "a1", "slug": "agent-one", "max_parallel": 1}
SETTINGS = RunSettings(
    cooldown_s=0.0,
    retry_cap_s=120.0,
    backoff_base_s=0.1,
    backoff_cap_s=5.0,
    wait_tick_s=0.05,
    internal_failure_limit=3,
    kill_grace_s=0.5,
    kill_settle_s=0.5,
)


def make_task(tid: str) -> dict[str, Any]:
    return {
        "task_id": tid,
        "state": "PLANNED",
        "priority": "P2",
        "dag_depth": 0,
        "created_at": 1.0,
        "attempt": 0,
        "max_attempts": 1,
        "notes_json": {},
        "agent_id": "a1",
        "capability": "docs",
    }


class FakeStore:
    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = {task["task_id"]: task for task in tasks}

    def list(self, *, correlation_id: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        return list(self.tasks.values())

    def get(self, task_id: str) -> dict[str, Any]:
        return self.tasks[task_id]

    def transition(self, task_id: str, to_state: object, *, actor: str = "A01", reason: str = "") -> None:
        self.tasks[task_id]["state"] = getattr(to_state, "value", to_state)

    def set_notes(self, task_id: str, **kv: object) -> None:
        self.tasks[task_id]["notes_json"].update(kv)


class FakeManager:
    max_concurrent = 2

    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, sid: str) -> None:
        self.released.append(sid)


class FakeClaims:
    def __init__(self) -> None:
        self.released: list[str] = []

    def release(self, owner: str) -> int:
        self.released.append(owner)
        return 0


class FakeLeases:
    def __init__(self) -> None:
        self.settled: list[str] = []

    def acquire(self, task: dict[str, Any], agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(dispatch=True, status="held", holder=None)

    def settle(self, task_id: str) -> str:
        self.settled.append(task_id)
        return "settled"


class FakeTelemetry:
    def degraded(self, *args: object, **kwargs: object) -> None:
        return None

    def clear_degraded(self, *args: object, **kwargs: object) -> None:
        return None


class FakeBook:
    def seen(self, task: dict[str, Any], *, waiting: bool, queue_depth: int) -> str:
        return f"run-{task['task_id']}"


class FakeController:
    """The Scheduler attributes and methods the facade uses. The methods follow the controller's contracts."""

    def __init__(
        self,
        root: Path,
        tasks: list[dict[str, Any]],
        *,
        leases: FakeLeases | None = None,
        once: bool = False,
        barrier: bool = False,
        max_rounds: int = 5,
    ) -> None:
        self.corr = "corr-1"
        self.repo = root
        self.sdir = root / "swarm"
        self.store = FakeStore(tasks)
        self.ctx = SimpleNamespace(emit=lambda *args, **kwargs: None)
        self.mgr = FakeManager()
        self.tel = FakeTelemetry()
        self.book = FakeBook()
        self.leases = leases
        self.handoffs = None
        self.claims = FakeClaims()
        self.cancel = SimpleNamespace(event=threading.Event(), force=threading.Event(), reason=None, at=None)
        self.barrier = barrier
        self.once = once
        self.max_rounds = max_rounds
        self.budget = max_rounds * self.mgr.max_concurrent
        self.fail_streak = 3
        self.cancel_grace = 0.0
        self.shutdown_budget = 2.0
        self.kill_reason = ""
        self.rounds = 0
        self.dispatched = 0
        self.closed = False
        self.extension_down = False
        self.t0 = 0.0
        self.log: list[str] = []
        self.inflight: dict[str, Any] = {}
        self.monitor_starts = 0
        self.workers: list[str] = []
        self.finished: list[tuple[str, str]] = []
        self.ticks: list[tuple[int, int, int, int, dict[str, str]]] = []
        self.open_error: BaseException | None = None
        self.open_as_failed = False
        self.finish_failed_override: object | None = None

    def _start_monitor(self) -> None:
        self.monitor_starts += 1

    def _open_run(self, task: dict[str, Any], agent: dict[str, Any]) -> SimpleNamespace:
        if self.open_error is not None:
            raise self.open_error
        tid = task["task_id"]
        item = SimpleNamespace(task=task, agent=agent, sid=f"sid-{tid}", run=SimpleNamespace(rate_limited=None))
        self.inflight[tid] = item
        self.dispatched += 1
        failure = None
        if self.open_as_failed:
            failure = SimpleNamespace(task_id=tid, agent_id=agent["id"], outcome="FAILED", error="E-INTERNAL: open failed")
        return SimpleNamespace(item=item, failure=failure)

    def _worker(self, task: dict[str, Any], agent: dict[str, Any], sid: str, run: object) -> SimpleNamespace:
        self.workers.append(task["task_id"])
        return SimpleNamespace(task_id=task["task_id"], agent_id=agent["id"], outcome="DONE", error=None)

    def _internal_failure(self, task: dict[str, Any], agent: dict[str, Any], exc: Exception) -> SimpleNamespace:
        return SimpleNamespace(task_id=task["task_id"], agent_id=agent["id"], outcome="FAILED", error=str(exc))

    def _finish_run(self, item: object, done: Any) -> SimpleNamespace:
        self.finished.append((done.task_id, done.outcome))
        failed: object = done.error is not None or done.outcome == "FAILED"
        if self.finish_failed_override is not None:
            failed = self.finish_failed_override
        return SimpleNamespace(failed=failed, reason=done.error or "")

    def _tick(self, ready: int, submitted: int, eff: int, free: int, skipped: dict[str, str]) -> None:
        self.ticks.append((ready, submitted, eff, free, dict(skipped)))


def helpers_for(
    tasks: list[dict[str, Any]],
    cancel_flags: list[tuple[Path, str, str, float]] | None = None,
    kills: list[dict[str, object]] | None = None,
) -> RunnerHelpers:
    flags = cancel_flags if cancel_flags is not None else []
    kill_calls = kills if kills is not None else []

    def dispatchable(store: object, corr: str, emit: object, handoffs: object = None, **kwargs: Any) -> list[dict[str, Any]]:
        skip = kwargs.get("skip", frozenset())
        return [task for task in tasks if task["task_id"] not in skip]

    def write_cancel_flag(sdir: Path, corr: str, reason: str, grace_s: float) -> Path:
        flags.append((sdir, corr, reason, grace_s))
        return sdir

    def kill_sessions(grace: float = 5, reason: str = "") -> None:
        kill_calls.append({"grace": grace, "reason": reason})

    def safe(fn: Any, *args: Any, **kwargs: Any) -> object:
        return fn(*args, **kwargs)

    return RunnerHelpers(
        dispatchable=dispatchable,
        resolve_agent=lambda task: AGENT,
        extension_failed=lambda store, tid: False,
        kill_sessions=kill_sessions,
        write_cancel_flag=write_cancel_flag,
        safe=safe,
    )


@pytest.fixture(autouse=True)
def no_store_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rust_scheduler, "reconcile", lambda *args, **kwargs: [])
    monkeypatch.setattr(rust_scheduler, "log_phase", lambda *args, **kwargs: {})


def run_with(controller: FakeController, helpers: RunnerHelpers, calls: list[dict[str, object]] | None = None) -> dict[str, object]:
    recorded = calls if calls is not None else []

    def spy(ports: object, **kwargs: object) -> dict[str, object]:
        recorded.append(kwargs)
        return native.run_scheduler(ports, **kwargs)

    facade = RustScheduler(
        controller,
        helpers=helpers,
        settings=SETTINGS,
        run_native=spy,
        fence_factory=native.NativeCleanupFence,
    )
    return facade.run()


def test_nothing_ready_passes_seventeen_keywords_and_maps_the_summary(tmp_path: Path) -> None:
    controller = FakeController(tmp_path, tasks=[])
    calls: list[dict[str, object]] = []
    summary = run_with(controller, helpers_for([]), calls)

    assert set(summary) == EIGHT_KEYS
    assert summary["stopped"] == "nothing-ready"
    assert summary["rounds"] == 1
    assert summary["dispatched"] == 0
    assert summary["closed"] is False
    assert summary["extension_down"] is False
    assert summary["abandoned"] == []
    assert summary["waiting"] == []
    assert isinstance(summary["effective_parallel"], int) and summary["effective_parallel"] >= 1

    assert len(calls) == 1
    assert set(calls[0]) == SEVENTEEN_KEYWORDS
    assert isinstance(calls[0]["cleanup_fence"], native.NativeCleanupFence)

    assert controller.rounds == 1
    assert controller.dispatched == 0
    assert controller.closed is False
    assert controller.monitor_starts == 1
    assert len(controller.ticks) == 1 and controller.ticks[0][0] == 0
    assert controller.log == []


def test_once_dispatches_settles_the_task_and_closes(tmp_path: Path) -> None:
    task = make_task("t1")
    controller = FakeController(tmp_path, tasks=[task], once=True)
    summary = run_with(controller, helpers_for([task]))

    assert set(summary) == EIGHT_KEYS
    assert summary["stopped"] == "closed"
    assert summary["dispatched"] == 1
    assert summary["closed"] is True
    assert controller.workers == ["t1"]
    assert controller.finished == [("t1", "DONE")]
    assert controller.inflight == {}
    assert controller.mgr.released == ["sid-t1"]
    assert "t1" in controller.claims.released
    assert controller.dispatched == 1
    assert controller.closed is True


def test_barrier_round_limit_closes_after_one_pass(tmp_path: Path) -> None:
    task = make_task("t1")
    controller = FakeController(tmp_path, tasks=[task], barrier=True, max_rounds=1)
    summary = run_with(controller, helpers_for([task]))

    assert summary["stopped"] == "closed"
    assert summary["rounds"] == 1
    assert summary["dispatched"] == 1
    assert summary["closed"] is True
    assert controller.workers == ["t1"]
    assert controller.inflight == {}
    assert controller.closed is True


def test_cancel_before_start_drains_without_dispatch(tmp_path: Path) -> None:
    task = make_task("t1")
    controller = FakeController(tmp_path, tasks=[task])
    controller.cancel.reason = "signal SIGTERM"
    controller.cancel.at = time.monotonic()
    controller.cancel.event.set()
    flags: list[tuple[Path, str, str, float]] = []
    kills: list[dict[str, object]] = []
    summary = run_with(controller, helpers_for([task], flags, kills))

    assert summary["stopped"] == "cancelled"
    assert summary["dispatched"] == 0
    assert summary["abandoned"] == []
    assert controller.workers == []
    assert kills == []
    assert controller.log and controller.log[0].startswith("cancelling: signal SIGTERM; 0 session(s)")
    assert [flag[2] for flag in flags] == ["signal SIGTERM"]
    assert flags[0][0] == controller.sdir


def test_granted_lease_is_settled_when_its_submission_fails(tmp_path: Path) -> None:
    task = make_task("t1")
    leases = FakeLeases()
    controller = FakeController(tmp_path, tasks=[task], leases=leases, once=True)
    controller.open_error = RuntimeError("submit boom")

    with pytest.raises(RuntimeError, match="submit boom"):
        run_with(controller, helpers_for([task]))

    assert leases.settled == ["t1"]
    assert "t1" in controller.claims.released
    assert controller.workers == []
    assert controller.inflight == {}


def test_failed_submission_counts_once_and_never_runs_the_worker(tmp_path: Path) -> None:
    task = make_task("t1")
    controller = FakeController(tmp_path, tasks=[task], once=True)
    controller.open_as_failed = True
    summary = run_with(controller, helpers_for([task]))

    assert summary["dispatched"] == 1
    assert summary["stopped"] == "closed"
    assert controller.workers == []
    assert controller.finished == [("t1", "FAILED")]
    assert controller.inflight == {}


def test_malformed_completion_return_raises_value_error(tmp_path: Path) -> None:
    task = make_task("t1")
    controller = FakeController(tmp_path, tasks=[task], once=True)
    controller.finish_failed_override = 1  # an int where complete must return a bool

    with pytest.raises(ValueError, match="expected bool"):
        run_with(controller, helpers_for([task]))


# ---- native security consumers (NATSEC) -------------------------------------------------------------------------------
# Every secret below is an inert canary string, never a key, token or credential. The headless child is sys.executable
# running a fixed probe script. No real key, token or credential file is read or written.

ROOT = Path(__file__).resolve().parents[1]
SIGNING_CANARY = "CANARY-SIGNING-KEY-7f3a"
ED25519_CANARY = "CANARY-ED25519-SEED-9b2c"
ED25519_REFERENCE = "/canary/ed25519-reference.key"  # a path that is never opened
PRIOR_DENY = "/canary/prior-deny.key"
PARENT_TOKEN_CANARY = "CANARY-PARENT-SUBSTRATE-TOKEN"
OTHER_TOKEN_CANARY = "CANARY-OTHER-AGENT-TOKEN"
OPERATOR_TOKENS_CANARY = "CANARY-OPERATOR-TOKENS"
OWN_TOKEN = "canary-own-token-a05"
CANARY_VALUES = (SIGNING_CANARY, ED25519_CANARY, PARENT_TOKEN_CANARY, OTHER_TOKEN_CANARY, OPERATOR_TOKENS_CANARY)
WORKER_FAILED = "E-INTERNAL: scheduler worker callback failed"
CALLBACKS = (
    "begin_pass", "agent_for", "claims_for", "block", "lease", "observe_pass", "submit", "worker",
    "internal_failure", "complete", "set_retry_after", "random_unit", "cancel_state", "cancel_begin", "kill", "log",
)
A05 = {"id": "A05", "slug": "a05-backend", "max_parallel": 1}
PROBE = (
    "import json, os, sys\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as out:\n"
    "    json.dump(dict(os.environ), out)\n"
)


class Tap:
    """Records every argument and return value that crosses the native callback boundary, then forwards the call."""

    def __init__(self, inner: Any) -> None:
        self.calls: dict[str, list[tuple[Any, ...]]] = {name: [] for name in CALLBACKS}
        self.returns: dict[str, list[Any]] = {name: [] for name in CALLBACKS}
        for name in CALLBACKS:
            setattr(self, name, self._forward(name, getattr(inner, name)))

    def _forward(self, name: str, target: Any) -> Any:
        def call(*args: Any) -> Any:
            self.calls[name].append(args)
            result = target(*args)
            self.returns[name].append(result)
            return result

        return call


def run_tapped(controller: FakeController, helpers: RunnerHelpers) -> tuple[dict[str, object], Tap]:
    """Runs the facade through the native binding with every callback tapped; returns the summary and the tap."""
    taps: list[Tap] = []

    def spy(ports: object, **kwargs: object) -> dict[str, object]:
        tap = Tap(ports)
        taps.append(tap)
        return native.run_scheduler(tap, **kwargs)

    facade = RustScheduler(controller, helpers=helpers, settings=SETTINGS, run_native=spy,
                           fence_factory=native.NativeCleanupFence)
    summary = facade.run()
    return summary, taps[0]


def load_swarm_run() -> Any:
    spec = importlib.util.spec_from_file_location("swarm_run_natsec_under_test", ROOT / "scripts" / "swarm_run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ProbeController(FakeController):
    """Stands in for execute_one. Its session env is the real headless_command env, run as an inert probe that records it."""

    def __init__(self, root: Path, tasks: list[dict[str, Any]], *, swarm_run: Any, repo: Path, args: SimpleNamespace,
                 out: Path) -> None:
        super().__init__(root, tasks, once=True)
        self.repo = repo
        self.swarm_run = swarm_run
        self.args = args
        self.out = out
        self.child_returncode: int | None = None

    def _worker(self, task: dict[str, Any], agent: dict[str, Any], sid: str, run: object) -> SimpleNamespace:
        self.workers.append(task["task_id"])
        _argv, env, cwd = self.swarm_run.headless_command("omp", agent, self.repo, self.sdir, self.args)
        probe = subprocess.run([sys.executable, "-c", PROBE, str(self.out)], env=env, cwd=cwd, capture_output=True,
                               text=True, timeout=60, check=False)
        self.child_returncode = probe.returncode
        outcome = "DONE" if probe.returncode == 0 else "FAILED"
        return SimpleNamespace(task_id=task["task_id"], agent_id=agent["id"], outcome=outcome, error=None)


def dispatch_headless_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Dispatches one A05 task through the native binding; returns the environment the headless child really received."""
    repo = tmp_path / "workspace"
    repo.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workspace.write_token(workspace.env_file(repo, "a05-backend"), OWN_TOKEN)
    args = SimpleNamespace(approval_mode="write", task_timeout=600, model=None, omp_bin="omp")
    out = tmp_path / "child-env.json"
    task = make_task("t1")
    controller = ProbeController(tmp_path, [task], swarm_run=load_swarm_run(), repo=repo, args=args, out=out)
    summary, _tap = run_tapped(controller, replace(helpers_for([task]), resolve_agent=lambda _task: A05))
    assert summary["stopped"] == "closed"
    assert controller.workers == ["t1"]
    assert controller.child_returncode == 0
    assert controller.finished == [("t1", "DONE")]
    return json.loads(out.read_text(encoding="utf-8"))


def test_headless_child_gets_only_its_own_identity_and_the_deny_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_SIGNING_KEY", SIGNING_CANARY)
    monkeypatch.setenv("SWARM_ED25519_KEY", ED25519_CANARY)
    monkeypatch.setenv("SWARM_ED25519_KEY_FILE", ED25519_REFERENCE)
    monkeypatch.setenv("SWARM_SIGNER_DENY_PATHS", json.dumps([PRIOR_DENY]))
    monkeypatch.setenv("SWARM_REQUIRE_KEY", "1")
    monkeypatch.setenv("SWARM_ALLOW_INSECURE_DEV_KEY", "1")
    monkeypatch.setenv("SWARM_TASK_ID", "parent-task")
    monkeypatch.setenv("SWARM_CORRELATION_ID", "parent-corr")
    monkeypatch.setenv("SUBSTRATE_TOKEN", PARENT_TOKEN_CANARY)
    monkeypatch.setenv("SUBSTRATE_TOKEN_SWARM_A01_ORCH", OTHER_TOKEN_CANARY)
    monkeypatch.setenv("SUBSTRATE_OPERATOR_TOKENS", OPERATOR_TOKENS_CANARY)
    child = dispatch_headless_probe(tmp_path, monkeypatch)

    # The session's own identity: its slug, its session markers and its own token from its own env file.
    assert child["SWARM_AGENT"] == "a05-backend"
    assert child["SWARM_AGENT_SESSION"] == "1"
    assert child["SWARM_CHILD"] == "1"
    assert child["SUBSTRATE_TOKEN"] == OWN_TOKEN
    assert [name for name in child if workspace.carries_token(name)] == ["SUBSTRATE_TOKEN"]
    # Signing material, key references and the runner's own task markers are not inherited.
    for name in ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY",
                 "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_TASK_ID", "SWARM_CORRELATION_ID"):
        assert name not in child
    # The deny-only policy keeps the key reference's path as a denial, ahead of the prior denial. It is not a loadable reference.
    assert json.loads(child["SWARM_SIGNER_DENY_PATHS"]) == [ED25519_REFERENCE, PRIOR_DENY]
    leaked = [name for name, value in child.items()
              if name != "SWARM_SIGNER_DENY_PATHS" and any(canary in value for canary in CANARY_VALUES)]
    assert leaked == []


def test_headless_child_without_a_key_reference_gets_no_deny_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SWARM_ED25519_KEY_FILE", raising=False)
    monkeypatch.delenv("SWARM_SIGNER_DENY_PATHS", raising=False)
    child = dispatch_headless_probe(tmp_path, monkeypatch)

    assert child["SWARM_AGENT"] == "a05-backend"
    assert child["SUBSTRATE_TOKEN"] == OWN_TOKEN
    assert "SWARM_SIGNER_DENY_PATHS" not in child
    assert "SWARM_ED25519_KEY_FILE" not in child


class LeakyWorkerController(FakeController):
    """A worker whose failure text carries the signing canary, as an unguarded spawn error would."""

    def __init__(self, root: Path, tasks: list[dict[str, Any]]) -> None:
        super().__init__(root, tasks, once=True)
        self.finish_errors: list[str | None] = []

    def _worker(self, task: dict[str, Any], agent: dict[str, Any], sid: str, run: object) -> SimpleNamespace:
        self.workers.append(task["task_id"])
        raise RuntimeError(f"headless spawn failed with key {os.environ['SWARM_SIGNING_KEY']}")

    def _finish_run(self, item: object, done: Any) -> SimpleNamespace:
        self.finish_errors.append(done.error)
        return super()._finish_run(item, done)


def test_worker_failure_carrying_key_canary_never_reaches_the_observer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_SIGNING_KEY", SIGNING_CANARY)
    task = make_task("t1")
    controller = LeakyWorkerController(tmp_path, [task])
    summary, tap = run_tapped(controller, helpers_for([task]))

    assert summary["stopped"] == "closed"
    assert summary["dispatched"] == 1
    assert controller.workers == ["t1"]
    assert controller.finish_errors == [WORKER_FAILED]
    assert controller.finished == [("t1", "FAILED")]
    assert tap.calls["internal_failure"] == [("t1", "a1", WORKER_FAILED)]
    observed = repr((summary, tap.calls, tap.returns, controller.log, controller.ticks, controller.finish_errors))
    assert SIGNING_CANARY not in observed


class SigningController(FakeController):
    """The Python completion authority. It signs its own record of each completion; the Rust side never signs anything."""

    def __init__(self, root: Path, tasks: list[dict[str, Any]]) -> None:
        super().__init__(root, tasks, once=True)
        self.records: list[tuple[bool, bool, dict[str, object]]] = []

    def _finish_run(self, item: object, done: Any) -> SimpleNamespace:
        result = super()._finish_run(item, done)
        record = build_envelope(source="A01@runner", target=done.agent_id, msg_type="task.result",
                                payload={"task_id": done.task_id, "outcome": done.outcome, "error": done.error},
                                correlation_id=self.corr)
        unsigned_verifies = verify_envelope(record)  # the bare completion Rust hands over carries no signature
        signed = sign_envelope(record, root=self.repo, audit=False)
        self.records.append((unsigned_verifies, verify_envelope(signed), signed))
        return result


def test_unsigned_worker_completion_is_signed_only_by_python(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_SIGNING_KEY", "fixed-inert-hmac-key-for-natsec")
    task = make_task("t1")
    controller = SigningController(tmp_path, [task])
    summary, tap = run_tapped(controller, helpers_for([task]))

    assert summary["stopped"] == "closed"
    assert controller.finished == [("t1", "DONE")]
    # Rust forwards the worker's bare Done unchanged and adds no signature to it.
    assert tap.calls["complete"] == [(("t1", "a1", "DONE", None),)]
    assert tap.returns["complete"] == [(False, None, None, False, False)]
    # Python's record verifies only after Python signs it, under the controller's own key.
    assert len(controller.records) == 1
    unsigned_verifies, signed_verifies, signed = controller.records[0]
    assert unsigned_verifies is False
    assert signed_verifies is True
    assert str(signed["sig"]).startswith("hmac:")
    assert signed["payload"] == {"task_id": "t1", "outcome": "DONE", "error": None}


def test_complete_reports_the_python_verdict_not_the_worker_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SWARM_SIGNING_KEY", "fixed-inert-hmac-key-for-natsec")
    task = make_task("t1")
    controller = SigningController(tmp_path, [task])
    controller.finish_failed_override = True  # Python decides this completion failed, though the worker said DONE
    _summary, tap = run_tapped(controller, helpers_for([task]))

    assert tap.calls["complete"] == [(("t1", "a1", "DONE", None),)]
    assert tap.returns["complete"][0][0] is True
    assert tap.returns["complete"][0][4] is False
    assert controller.finished == [("t1", "DONE")]
