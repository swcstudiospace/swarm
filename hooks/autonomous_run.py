#!/usr/bin/env python3
"""Detached AgentSwarm runner used after the all-in-one Prompt Uplift hook.

Fail-open. Dedupes concurrent kicks with an exclusive lock held for the whole run, plus a
120s mtime debounce after it finishes. An accepted kick creates $SWARM_DIR/autonomous.log before the
run starts and overwrites it with the result.

The kick gate is swarm.signal_detector.classify: a brief starts a plan and a run only when it returns
"sdlc". Questions, trivia, slash and system-reminder prompts, opt-out tokens and the dispatch marker exit
0 without starting anything. An exact gsd-autonomous command gets no special case.

The run step is capped (SWARM_AUTONOMOUS_RUN_CAP_S, default 3600). On expiry the runner is
SIGTERMed so it can end its sessions, and SIGKILLed only after SWARM_AUTONOMOUS_RUN_GRACE_S
(default 15). subprocess.run's timeout SIGKILLs immediately, which orphans those sessions.
The hook exits 0; the runner's returncode is recorded as run_rc in autonomous.log. SIGTERM to the hook stops the runner's process group within the grace and exits 143. A cap or grace above 2,000,000 s takes its default.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.runlog import log_phase
from swarm.paths import logging_paths
from swarm.signal_detector import classify as sd_classify


def classify(prompt: str) -> bool:
    """True only when swarm.signal_detector.classify marks the prompt "sdlc"; its skip rules and original-text
    extraction apply unchanged. A classifier fault starts nothing."""
    try:
        return sd_classify(prompt) == "sdlc"
    except Exception:
        return False

def pattern_for(brief: str) -> str:
    low = brief.lower()
    if "hotfix" in low or "cve" in low or "incident" in low:
        return "hotfix"
    if "dependenc" in low or "bump" in low:
        return "dependency"
    return "feature"


# The selectors' poll takes milliseconds in a C int, so about 2,147,483 s is its ceiling. Keep a margin below it.
_MAX_WAIT_S = 2_000_000.0


def _env_seconds(name: str, default: float) -> float:
    """A positive finite number of seconds from the environment, else `default`. Blank, non-numeric, non-finite,
    non-positive and above _MAX_WAIT_S values are the default: a typo must not disable the cap or the grace, and a
    value the OS poll cannot wait on must not overflow it."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) and 0 < value <= _MAX_WAIT_S else default


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


class _Terminated(BaseException):
    """Raised by the SIGTERM handler; run_capped's BaseException path stops the runner group before this reaches main."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


def _on_sigterm(signum: int, frame: object) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)  # one SIGTERM is enough: a repeat must not interrupt the cleanup
    raise _Terminated(signum)


def _stop_group(proc: subprocess.Popen, grace: float) -> None:
    """Terminate the child's process group, wait `grace` seconds for the leader, SIGKILL the group, then reap the leader.
    run_capped starts the child as its own session and group leader, so its pid is the group id."""
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc.pid, signal.SIGKILL)
    proc.wait()
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()


def run_capped(cmd: list[str], *, cwd: str, env: dict, timeout: float, grace: float,
               pass_fds: tuple[int, ...] = ()) -> subprocess.CompletedProcess:
    """Run `cmd`, capturing stdout and stderr. `subprocess.run(timeout=)` SIGKILLs on expiry, which orphans the
    runner's sessions: they sit in their own OS session, so the kill never reaches them and their tasks stay
    IN_PROGRESS (T-06-26). Ask the runner to stop with SIGTERM first — its handler ends those sessions — and
    SIGKILL only what ignores that for `grace` seconds. `Popen.communicate` does not kill on timeout; `run` does,
    which is why this does not call `run`. timeout and grace are clamped to _MAX_WAIT_S, the OS poll range. The child
    leads its own process group, and any BaseException (KeyboardInterrupt included) stops that group before it re-raises,
    so an error never leaves a running child. pass_fds are inherited by the child only."""
    timeout = min(timeout, _MAX_WAIT_S)
    grace = min(grace, _MAX_WAIT_S)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True, pass_fds=pass_fds)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _signal_pid(proc.pid, signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=grace)
            except subprocess.TimeoutExpired:
                _signal_pid(proc.pid, signal.SIGKILL)
                try:
                    out, err = proc.communicate(timeout=grace)
                except subprocess.TimeoutExpired:
                    # A grandchild that inherited the pipes can outlive the SIGKILL of its parent and hold them open, so
                    # communicate() would never return. Close the pipes, reap the killed direct child and abandon its output.
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe is not None:
                            pipe.close()
                    proc.wait()
                    out, err = None, None
    except BaseException:
        _stop_group(proc, grace)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode if proc.returncode is not None else 1, out or "", err or "")


def _stamp_lock(fd: int) -> None:
    """Rewrite the lock so its mtime is now. acquire stamps acquisition; main stamps release."""
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    os.write(fd, str(time.time()).encode())


def acquire(lock: Path) -> int | None:
    """An fd holding the exclusive kickoff lock, or None when this brief must not start.

    The fd stays open for the whole run, so a second process cannot pass the check while the first
    is still planning or running (T-06-18). The stamp written here is the acquisition time. main
    rewrites the fd on release, and the 120s debounce is measured from that release mtime. A crash
    that skips finally keeps this acquisition mtime."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    now = time.time()
    info = os.fstat(fd)
    if info.st_size > 0 and now - info.st_mtime < 120:
        os.close(fd)
        return None
    _stamp_lock(fd)
    return fd


def main() -> int:
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)  # SIGTERM stops the runner's group before the hook exits (H-03)
        ap = argparse.ArgumentParser(description=__doc__)
        ap.add_argument("--cwd", default=".", help="target application repo")
        ap.add_argument("--brief", required=True, help="the user's original prompt (classification + dedupe key)")
        ap.add_argument("--spec", default="", help="uplifted XML spec file; when readable, A01 plans from it instead of --brief")
        ap.add_argument("--runtime", default="auto", choices=["auto", "claude", "grok", "omp"])
        ap.add_argument("--swarm-root", default=os.environ.get("SWARM_ROOT", str(Path(__file__).resolve().parent.parent)))
        ap.add_argument("--dry-run", action="store_true")
        ap.add_argument("--wait", action="store_true", help="run in-process (tests); default is already in-process for this script")
        args = ap.parse_args()
        if os.environ.get("SWARM_DISABLED") == "1":
            return 0  # operator kill switch: never plan or dispatch while the swarm is disabled
        if not classify(args.brief):
            return 0
        log_phase("hook.main", {"brief_excerpt": args.brief[:80], "cwd": args.cwd, "paths_spec": logging_paths(args.cwd) if hasattr(logging_paths, "__call__") else {}}, source="autonomous_run", tee=True)
        root = Path(args.swarm_root).resolve()
        repo = Path(args.cwd).resolve()
        sys.path.insert(0, str(root))
        from swarm.paths import swarm_dir as resolve_swarm_dir
        swarm_dir = resolve_swarm_dir(repo, create=True)  # absolute; exported to both children
        digest = hashlib.sha256(f"{repo}|{args.brief}".encode()).hexdigest()[:16]
        lock = swarm_dir / "kickoffs" / f"{digest}.lock"
        log = swarm_dir / "autonomous.log"
        held = acquire(lock)
        if held is None:
            log.write_text((log.read_text() if log.exists() else "") + f"skip duplicate {digest}\n")
            return 0
        log.touch()  # created before the run starts writing it; the run overwrites it with the result
        env = {**os.environ, "SWARM_DIR": str(swarm_dir), "SWARM_CHILD": "1", "AIO_UPLIFT": "0", "AIO_SWARM": "0"}
        key_ref = os.environ.get("SWARM_ED25519_KEY_FILE", "")
        if key_ref and not os.path.isabs(key_ref):
            # A relative reference names the kickoff workspace's file; the signer refuses relative refs, so export it absolute.
            env["SWARM_ED25519_KEY_FILE"] = os.path.normpath(os.path.join(str(repo), key_ref))
        spec = Path(args.spec) if args.spec and Path(args.spec).is_file() else None
        plan = [sys.executable, str(root / "scripts" / "orch_plan.py"), "--pattern", pattern_for(args.brief), "--json"]
        plan += ["--brief", str(spec)] if spec else ["--brief-text", args.brief]
        run = [
            sys.executable, str(root / "scripts" / "swarm_run.py"),
            "--repo", str(repo), "--runtime", args.runtime, "--json",
        ]
        if args.dry_run:
            run.append("--dry-run")
        try:
            p1 = subprocess.run(plan, cwd=str(root), env=env, capture_output=True, text=True, timeout=120)
            try:
                plan_json = json.loads(p1.stdout)
            except ValueError:
                plan_json = {}
            if p1.returncode != 0 or plan_json.get("status") != "ok" or not plan_json.get("correlation_id"):
                err = plan_json.get("error") if isinstance(plan_json.get("error"), dict) else {}
                log.write_text(json.dumps({
                    "brief": args.brief[:500],
                    "repo": str(repo),
                    "spec": str(spec) if spec else None,
                    "plan_rc": p1.returncode,
                    "plan_out": p1.stdout[-4000:],
                    "plan_err": p1.stderr[-2000:],
                    "skipped_run": True,
                    "error": {"code": err.get("code"), "message": err.get("message")},
                }, indent=2))
                return 0
            run += ["--correlation-id", plan_json["correlation_id"]]
            # SWARM_AUTONOMOUS_RUN_CAP_S bounds the run (default 3600). The grace is how long the runner has to end
            # its sessions after SIGTERM before this process SIGKILLs it (T-06-26). The runner fits its whole shutdown
            # (cooperative wait, session kill, final records) inside it: SWARM_SHUTDOWN_BUDGET_S.
            grace = _env_seconds("SWARM_AUTONOMOUS_RUN_GRACE_S", 15)
            env["SWARM_SHUTDOWN_BUDGET_S"] = f"{grace:g}"
            p2 = run_capped(
                run, cwd=str(root), env=env,
                timeout=_env_seconds("SWARM_AUTONOMOUS_RUN_CAP_S", 3600),
                grace=grace, pass_fds=(held,),  # the runner keeps the kickoff flock, so it outlives a SIGKILLed hook
            )
            log.write_text(
                json.dumps({
                    "brief": args.brief[:500],
                    "repo": str(repo),
                    "spec": str(spec) if spec else None,
                    "plan_rc": p1.returncode,
                    "run_rc": p2.returncode,
                    "plan_out": p1.stdout[-4000:],
                    "run_out": p2.stdout[-8000:],
                    "run_err": p2.stderr[-2000:],
                }, indent=2)
            )
            # The runner's returncode is in autonomous.log as run_rc; the hook's own exit status is always 0.
            return 0
        except Exception as exc:
            log.write_text(json.dumps({"error": str(exc)}))
            return 0
        finally:
            # Debounce from release. A crash that skips this finally keeps the acquisition mtime.
            _stamp_lock(held)
            os.close(held)
    except _Terminated as term:
        return 128 + term.signum
    except Exception:
        # Fail open: whatever broke (unwritable state dir, lock, import), the hook prints {} and exits 0.
        print("{}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
