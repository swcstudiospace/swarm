"""Shared helpers for lane and pool handlers. Not a registered tool."""
from __future__ import annotations

import contextlib
import contextvars
import importlib.util
import threading
import time
from pathlib import Path
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any, TypeVar

_Arg = TypeVar("_Arg")
_Result = TypeVar("_Result")

ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS: dict[str, ModuleType] = {}
_SCRIPTS_LOCK = threading.RLock()
_running: set[int] = set()
_lane = threading.Condition()
_cancel: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar("swarm_call_cancel", default=None)
_caller: contextvars.ContextVar[str | None] = contextvars.ContextVar("swarm_call_caller", default=None)


def current_caller() -> str | None:
    """Agent id (for example "A05") that the registry authorized for the running handler call, else None."""
    return _caller.get()


@contextlib.contextmanager
def caller_scope(caller: str | None) -> Iterator[None]:
    """Bind ``caller`` for the duration of the block and restore the previous value on any exit."""
    token = _caller.set(caller)
    try:
        yield
    finally:
        _caller.reset(token)


class StillRunning(Exception):
    """This call started, exceeded its limit, and the worker has not finished.

    The worker is not killed. The cancel flag is set, and the function stays
    occupied until the worker returns so a later call cannot overlap it.
    """


class Waiting(Exception):
    """This call never started: the previous one still holds the function.

    Independent callers wait for that worker. Timing out in the queue is not a
    failure of a peer that did not receive this request.
    """


class Busy(Exception):
    """A previous call of this function is still running."""


def target_root() -> Path:
    """Workspace the swarm is operating on. Script modules still load from ROOT."""
    return Path.cwd().resolve()


def sleep_until_cancelled(seconds: float) -> bool:
    """Sleep up to ``seconds``. Return True when the caller has given up."""
    flag = _cancel.get()
    if flag is None:
        time.sleep(seconds)
        return False
    return flag.wait(seconds)


def invoke_bounded(fn: Callable[[_Arg], _Result], arg: _Arg, timeout_s: float) -> _Result:
    """Run ``fn(arg)`` until ``timeout_s``.

    One function runs at a time. A later independent call waits for the worker
    that is already in progress, and raises Waiting if that wait exceeds its own
    limit. A call that did start raises StillRunning, sets the cancel flag, and
    keeps the function occupied until the worker returns.
    """
    key = id(fn)
    deadline = time.monotonic() + float(timeout_s)
    with _lane:
        while key in _running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Waiting(getattr(fn, "__name__", "handler"))
            _lane.wait(remaining)
        # The slot can free as the wait expires. Recheck before starting the handler.
        if time.monotonic() >= deadline:
            raise Waiting(getattr(fn, "__name__", "handler"))
        _running.add(key)
    done = threading.Event()
    outcome: dict[str, Any] = {}
    cancel = threading.Event()
    caller = _caller.get()

    def target() -> None:
        token = _cancel.set(cancel)
        caller_token = _caller.set(caller)
        try:
            outcome["value"] = fn(arg)
        except Exception as exc:
            outcome["error"] = exc
        finally:
            _cancel.reset(token)
            _caller.reset(caller_token)
            with _lane:
                _running.discard(key)
                _lane.notify_all()
            done.set()

    threading.Thread(target=target, daemon=True).start()
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not done.wait(remaining):
        cancel.set()
        raise StillRunning(f"exceeded {timeout_s}s")
    if "error" in outcome:
        raise outcome["error"]
    value: _Result = outcome["value"]  # outcome is written only by target(), which stores the fn result under "value"
    return value


def script(stem: str) -> ModuleType:
    """Load scripts/<stem>.py once. Scripts are not a package and only run main under __main__.

    The return is ModuleType: each stem is a loose script loaded from its file. Attribute access on a module is Any,
    so callers reach functions that exist on that file; a static checker cannot see their signatures.
    """
    cached = _SCRIPTS.get(stem)
    if cached is not None:
        return cached
    with _SCRIPTS_LOCK:
        cached = _SCRIPTS.get(stem)
        if cached is not None:
            return cached
        path = ROOT / "scripts" / f"{stem}.py"
        spec = importlib.util.spec_from_file_location(f"_swarm_lane_{stem}", path)
        if spec is None or spec.loader is None:
            raise OSError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SCRIPTS[stem] = module
        return module


def respond(payload: dict[str, Any], work: Callable[[], _Result]) -> dict[str, Any] | _Result:
    """Contract-test probe, then the real call. Probe values never reach the lane function."""
    probe = payload.get("probe")
    if probe == "sleep":
        sleep_until_cancelled(0.2)
        return {"state": "TIMEOUT", "message": "probe sleep"}
    if probe == "dependency":
        raise OSError("declared dependency is unavailable")
    if probe == "partial":
        return {"state": "PARTIAL_SUCCESS", "completed_fraction": 0.5}
    return work()
