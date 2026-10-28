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


# GSD_ENGINE selector (U12.6, OD-1; Phase 14.3 R5). Resolution order: SWARM_PURE_PYTHON=1 is Python; GSD_ENGINE=python is
# Python; `rust` is explicit (every call goes to the gsd_engine binary, which is required: E-DEP exit 2 when none is found);
# unset or empty is DEFAULT_ENGINE; any other value is E-INPUT. A call resolved through DEFAULT_ENGINE reaches the binary only
# when a binary is found and rust_serves(argv) holds; otherwise Python answers, silently. The body answers whenever
# run_rust returns None (see its docstring). E-TIMEOUT is never retried and never falls back (D-P5-21).
# RUST_CALL_TIMEOUT_S bounds the whole call. The binary has its own 600 s deadline, which does not cover its
# start-up or its stdin read, so this outer bound is 60 s larger. GSD_RUST_TIMEOUT_S, when set to a positive
# integer (seconds), overrides it; tests use that to keep the timeout short.
DEFAULT_ENGINE = "python"  # the engine an unset or empty GSD_ENGINE resolves to for the read routes; "python" or "rust"
RUST_CALL_TIMEOUT_S = 660
RUST_FALLBACK_CODES = frozenset({"E-INTERNAL", "E-DEP", "E-CAPACITY", "E-CONTRACT", "E-INPUT"})
RUST_ENV_KEYS = ("SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID")
RELEASE_BINARY = Path(__file__).resolve().parent.parent / "target" / "release" / "gsd_engine"
# flags of the read grammar that take a value (crates/gsd-route/src/lib.rs `parse_orch_status_read`)
READ_VALUE_FLAGS = frozenset({"--root", "--repo", "--correlation-id", "--task-id", "--history"})

SCRIPT = AgentScript("A01", "orch_status", run, description=__doc__, add_args=add_args)


def rust_serves(argv: list[str]) -> bool:
    """True exactly for the argv the gsd_engine binary serves: the read-only `--json` grammar of `parse_orch_status_read`.

    `argv` is the full argv the selector forwards. Accepted: `--json` (no inline value), `--dry-run` (no inline value) and
    the value flags of READ_VALUE_FLAGS with either `--flag=value` or `--flag value` (a separate value must not start with
    `-`), and `--json` at least once. Everything else is refused: a positional, an unknown or abbreviated flag, `--transition`,
    `--ingest`, `--advisory`, `--reason`, a missing value. The binary answers those with exit 2 E-DEP.
    """
    json_seen = False
    index = 0
    while index < len(argv):
        head, equals, _ = argv[index].partition("=")
        has_inline = bool(equals) and head.startswith("--")  # `--flag=value`; any other `=` belongs to an unknown token
        name = head if has_inline else argv[index]
        if name in ("--json", "--dry-run"):
            if has_inline:
                return False
            json_seen = json_seen or name == "--json"
        elif name in READ_VALUE_FLAGS:
            if not has_inline:
                index += 1
                if index >= len(argv) or argv[index].startswith("-"):
                    return False
        else:
            return False
        index += 1
    return json_seen


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


def run_rust(argv: list[str], binary: str, *, explicit: bool) -> int | None:
    """Answers the call on the Rust engine at `binary`. None hands the call to the Python body.

    None (fallback) is returned exactly for these triggers: a reply whose error.code is in RUST_FALLBACK_CODES;
    an argument the binary cannot take (the request is not UTF-8 encodable); an exit-2 reply without a typed
    error.code; a reply that is not a JSON object; an exit code other than 0, 1 or 2; a binary that cannot be
    started when the call did not ask for `rust` explicitly (`explicit` False); and a run-log record that cannot be
    written (AgentScript.record_external_reply raised, so the Python body answers and applies its own failure rules).
    A Rust answer is recorded in the run log before it is printed, as the Python path's own `main` does (D1).
    Refused with exit 2 and never a fallback: a binary that cannot be started when `explicit`, and a binary that
    did not answer within the call deadline (E-TIMEOUT), explicit or not.
    """
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
        if not explicit:
            return None  # fallback: the default engine never turns an unusable binary into an error
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
    try:
        SCRIPT.record_external_reply(argv, reply, done.returncode)
    except Exception:  # noqa: BLE001 - any failure to record hands the call to the Python body, which answers on its own rules
        return None
    sys.stdout.buffer.write(done.stdout)
    sys.stdout.flush()
    return done.returncode


def select_engine(argv: list[str]) -> int | None:
    """Resolves the engine for one call. An int is the final exit code (the selector already printed the answer); None runs the Python body."""
    if os.environ.get("SWARM_PURE_PYTHON") == "1":
        return None
    requested = os.environ.get("GSD_ENGINE", "")
    if requested == "python":
        return None
    if requested not in ("", "rust"):
        return _refuse(ErrorCode.E_INPUT, "GSD_ENGINE must be 'python' or 'rust'")
    explicit = requested == "rust"
    if not explicit and DEFAULT_ENGINE != "rust":
        return None
    binary = os.environ.get("GSD_ENGINE_BIN", "")
    if not binary and RELEASE_BINARY.is_file() and os.access(RELEASE_BINARY, os.X_OK):
        binary = str(RELEASE_BINARY)
    if not explicit:
        if not rust_serves(argv) or not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
            return None
    if os.environ.get("GSD_RUST_BACKEND", "") not in ("", "host", "sandbox"):
        return _refuse(ErrorCode.E_INPUT, "GSD_RUST_BACKEND must be 'host' or 'sandbox' when set")
    if not binary:
        return _refuse(ErrorCode.E_DEP, "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary")
    return run_rust(argv, binary, explicit=explicit)


if __name__ == "__main__":
    answer = select_engine(sys.argv[1:])
    sys.exit(answer if answer is not None else SCRIPT.main())
