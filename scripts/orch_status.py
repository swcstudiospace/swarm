#!/usr/bin/env python3
"""A01 — swarm status, task transitions and task.status ingestion.

  python3 scripts/orch_status.py                              # table for latest correlation
  python3 scripts/orch_status.py --correlation-id <id> --json
  python3 scripts/orch_status.py --transition T7f3a-be IN_PROGRESS --reason "lease granted"
  python3 scripts/orch_status.py --ingest result.json         # task.result payload from an agent
  python3 scripts/orch_status.py --history T7f3a-be
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.script_base import AgentScript  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402
from swarm.runlog import read_events  # noqa: E402
from swarm.paths import latest_correlation  # noqa: E402
from swarm.errors import SwarmError, ErrorCode  # noqa: E402
from swarm.results import parse_result, validate_result, apply_result, reconcile, reject, check_manual_transition  # noqa: E402
from swarm.observability import emit_inline_transition  # noqa: E402
from swarm import substrate_lease  # noqa: E402


def ingest(store: TaskStore, path: Path, ctx, *, approve: bool = True) -> dict:
    """task.result file → shared swarm.results path (same as the headless runner).

    approve=False (--advisory) saves the result and skips the APPROVED/DONE step. A keyless
    Cursor session uses it so a task with no required gates stays IN_REVIEW instead of
    raising E-POLICY after the result is already stored.
    """
    result = parse_result(path.read_text(encoding="utf-8"))
    tid = ctx.task_id or (result or {}).get("task_id")
    if not isinstance(tid, str):
        ctx.emit("task.result.rejected", {"task_id": None, "mode": "ingest", "reason": "no task_id in result"})
        raise SwarmError(ErrorCode.E_CONTRACT, "task.result has no task_id (use --task-id or include task_id)")
    try:
        task = store.get(tid)
    except SwarmError as e:
        raise SwarmError(ErrorCode.E_INPUT, f"unknown task {tid}", task_id=tid) from e
    try:
        result = validate_result(result, task_id=tid)
        state = apply_result(store, task, agent_id=result.get("agent_id") or task["agent_id"] or "agent", result=result,
                             meta={"ingest": str(path)}, emit=ctx.emit, mode="ingest")
    except SwarmError as e:
        if e.code is ErrorCode.E_CONTRACT:
            reject(store, tid, attempt=task["attempt"], reason=str(e), mode="ingest", emit=ctx.emit)
        raise
    log = reconcile(store, task["correlation_id"], ctx.emit, approve=approve)
    t = store.get(tid)
    emit_inline_transition(store, t, task["state"], t["state"], reason=f"ingest {path.name}", root=ctx.root)
    return {"status": "ok", "task": t, "reconcile": log, "summary": f"ingested task.result for {tid} → {state} (now {t['state']})"}


def run(args, ctx) -> dict:
    store = TaskStore(root=ctx.root)
    corr = ctx.correlation_id or latest_correlation(ctx.root)

    if args.history:
        try:
            store.get(args.history)
        except SwarmError as exc:
            # Named without the id, as the Rust wire answers (review F17): the caller has the id.
            raise SwarmError(ErrorCode.E_INPUT, "unknown task for history.task_id") from exc
        return {"status": "ok", "task_id": args.history, "history": store.history(args.history),
                "verdicts": store.latest_verdicts(args.history),
                "missing_gate_reasons": store.missing_gate_reasons(args.history), "summary": f"history for {args.history}"}

    if args.transition:
        tid, state = args.transition
        if ctx.dry_run:
            return {"status": "ok", "summary": f"dry-run: would transition {tid} → {state}"}
        check_manual_transition(store, tid, state)  # T-05-31: a gate task reaches IN_REVIEW only through an ingest
        before = store.get(tid)["state"]
        store.transition(tid, state, actor="A01", reason=args.reason or "manual")
        # a lease the runner mirrored in notes.lease is released (CANCELLED, FAILED, …) or completed (DONE) now, by its
        # lease_id and with the agent's token for this workspace (--root, the runner's --repo), rather than left to
        # lapse; a no-op when the substrate is off or nothing is held (LEASE-07). While a runner still works the task
        # (notes.running) it is left to that runner, which stops the session first.
        # The task printed below carries the mirrored lease_id; runlog.emit drops it from the event (no lease_id in the log).
        substrate_lease.settle_mirrored(store, tid, emit=ctx.emit, workspace=ctx.root)
        t = store.get(tid)
        # TaskStore's observer refreshed live.json; the run events are the in-session counterpart of Telemetry's
        emit_inline_transition(store, t, before, t["state"], reason=args.reason or "manual", root=ctx.root)
        ctx.emit("task.transition", {"task_id": tid, "state": t["state"], "reason": args.reason})
        return {"status": "ok", "task": t, "summary": f"{tid} → {t['state']}"}

    if args.ingest:
        return ingest(store, Path(args.ingest), ctx, approve=not args.advisory)

    tasks = store.list(correlation_id=corr)
    where = {"db": str(store.path), "swarm_dir": str(store.path.parent)}  # D-04: absolute, cwd-independent
    if not tasks:
        return {"status": "ok", "correlation_id": corr, "tasks": [], **where, "summary": "no tasks (run orch_plan.py first)"}
    ready = {t["task_id"] for t in store.ready(corr)}
    rows, counts = [], {}
    for t in tasks:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
        verdicts = {g: v["verdict"] for g, v in store.latest_verdicts(t["task_id"]).items()}
        rows.append({"task_id": t["task_id"], "agent": t["agent_id"], "state": t["state"], "depth": t["dag_depth"],
                     "attempt": t["attempt"], "rework": t["rework_loops"], "ready": t["task_id"] in ready,
                     "gates_required": store.required_gates(t["task_id"]), "verdicts": verdicts,
                     "missing_gates": store.missing_gates(t["task_id"]) if t["state"] == "IN_REVIEW" else [],
                     "depends_on": t["depends_on"], "outputs": len(t["outputs"])})
    escalated = [r["task_id"] for r in rows if r["state"] == "ESCALATED"]
    done = all(r["state"] in ("DONE", "CANCELLED") for r in rows)
    lines = [f"{'TASK':<14}{'AGENT':<7}{'STATE':<19}{'ATT':<4}{'RW':<3}{'READY':<6}{'GATES'}"]
    for r in rows:
        gates = ",".join(f"{g}={r['verdicts'].get(g, '·')}" for g in r["gates_required"]) or "-"
        lines.append(f"{r['task_id']:<14}{r['agent'] or '?':<7}{r['state']:<19}{r['attempt']:<4}{r['rework']:<3}"
                     f"{'yes' if r['ready'] else '':<6}{gates}")
    lines.append(f"\ncounts: {counts}   escalated: {escalated or 'none'}   complete: {done}")
    return {"status": "ok", "correlation_id": corr, "tasks": rows, "counts": counts, "escalated": escalated,
            "complete": done, "events": len(read_events(correlation_id=corr, root=ctx.root)), **where,
            "summary": "\n".join(lines)}


def add_args(p):
    p.add_argument("--repo", dest="root", default=argparse.SUPPRESS,
                   help="alias of --root (same flag as swarm_run.py --repo)")
    p.add_argument("--transition", nargs=2, metavar=("TASK_ID", "STATE"), help="A01-only legal transition")
    p.add_argument("--reason", default="")
    p.add_argument("--ingest", help="path to a task.result JSON (swarm/schemas/task.result.v1.json) from an agent")
    p.add_argument("--advisory", action="store_true",
                   help="with --ingest, save the result and do not transition it to APPROVED or DONE")
    p.add_argument("--history", metavar="TASK_ID")


# GSD_ENGINE selector (U12.6, OD-1). Unset, empty or `python` runs the body unchanged. `rust` asks the
# gsd_engine binary at GSD_ENGINE_BIN first; the body answers when run_rust returns None (see its docstring).
# E-TIMEOUT is never retried and never falls back (D-P5-21).
# RUST_CALL_TIMEOUT_S bounds the whole call. The binary has its own 600 s deadline, which does not cover its
# start-up or its stdin read, so this outer bound is 60 s larger. GSD_RUST_TIMEOUT_S, when set to a positive
# integer (seconds), overrides it; tests use that to keep the timeout short.
RUST_CALL_TIMEOUT_S = 660
RUST_FALLBACK_CODES = frozenset({"E-INTERNAL", "E-DEP", "E-CAPACITY", "E-CONTRACT", "E-INPUT"})
RUST_ENV_KEYS = ("SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID")


def _refuse(code: ErrorCode, message: str) -> int:
    """Prints an error reply shaped like the Rust engine's and returns exit 2."""
    body = {"agent": "A01", "script": "orch_status", "status": "error",
            "error": {"code": code.value, "message": message, "details": {}, "handling": code.standard_handling}}
    print(json.dumps(body, indent=2))
    return AgentScript.EXIT_ERROR


def _reply_object(stdout: bytes) -> dict | None:
    try:
        body = json.loads(stdout.decode("utf-8"))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error_code(reply: dict) -> str | None:
    error = reply.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _rust_timeout_s() -> int:
    """GSD_RUST_TIMEOUT_S when it is a positive integer, otherwise RUST_CALL_TIMEOUT_S."""
    raw = os.environ.get("GSD_RUST_TIMEOUT_S", "")
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return RUST_CALL_TIMEOUT_S


def run_rust(argv: list[str]) -> int | None:
    """Answers the call on the Rust engine. None hands the call to the Python body.

    None (fallback) is returned exactly for these triggers: a reply whose error.code is in RUST_FALLBACK_CODES;
    an argument the binary cannot take (the request is not UTF-8 encodable); an exit-2 reply without a typed
    error.code; a reply that is not a JSON object; an exit code other than 0, 1 or 2.
    Refused with exit 2 and never a fallback: GSD_ENGINE_BIN unset or not startable (E-DEP), and a binary that
    did not answer within the call deadline (E-TIMEOUT).
    """
    binary = os.environ.get("GSD_ENGINE_BIN", "")
    if not binary:
        return _refuse(ErrorCode.E_DEP, "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary")
    request = {"agent": "A01", "script": "orch_status", "argv": argv, "root": os.getcwd(),
               "env": {key: os.environ[key] for key in RUST_ENV_KEYS if os.environ.get(key)}}
    try:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return None  # fallback: an argument the binary cannot take
    try:
        done = subprocess.run([binary], input=payload, stdout=subprocess.PIPE, check=False, timeout=_rust_timeout_s())
    except subprocess.TimeoutExpired:
        return _refuse(ErrorCode.E_TIMEOUT, "the gsd_engine binary did not answer within the call deadline")
    except OSError:
        return _refuse(ErrorCode.E_DEP, "the gsd_engine binary named by GSD_ENGINE_BIN could not be started")
    reply = _reply_object(done.stdout)
    if reply is None:
        return None
    if done.returncode not in (AgentScript.EXIT_OK, AgentScript.EXIT_FAIL, AgentScript.EXIT_ERROR):
        return None
    if done.returncode == AgentScript.EXIT_ERROR:
        code = _error_code(reply)
        # fallback: an exit-2 reply without a typed error.code, or one whose code the Python body serves
        if code is None or code in RUST_FALLBACK_CODES:
            return None
    sys.stdout.buffer.write(done.stdout)
    sys.stdout.flush()
    return done.returncode


if __name__ == "__main__":
    engine = os.environ.get("GSD_ENGINE", "")
    if engine == "rust":
        backend = os.environ.get("GSD_RUST_BACKEND", "")
        if backend not in ("", "host", "sandbox"):
            sys.exit(_refuse(ErrorCode.E_INPUT, "GSD_RUST_BACKEND must be 'host' or 'sandbox' when set"))
        answer = run_rust(sys.argv[1:])
        if answer is not None:
            sys.exit(answer)
    elif engine not in ("", "python"):
        sys.exit(_refuse(ErrorCode.E_INPUT, "GSD_ENGINE must be 'python' or 'rust'"))
    sys.exit(AgentScript("A01", "orch_status", run, description=__doc__, add_args=add_args).main())
