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
import logging
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.script_base import AgentScript, Ctx  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402
from swarm.runlog import read_events  # noqa: E402
from swarm.paths import latest_correlation, swarm_dir  # noqa: E402
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


def finish_transition(store: TaskStore, ctx: Ctx, tid: str, *, before: str, reason: str,
                      events: Callable[[dict[str, Any]], list[tuple[str, dict[str, Any], str | None]]]) -> dict[str, Any]:
    """What follows a committed manual transition, in one order for the Python path and for the replay of a Rust answer.

    A lease the runner mirrored in notes.lease is released (CANCELLED, FAILED, …) or completed (DONE) now, by its lease_id and
    with the agent's token for this workspace (--root, the runner's --repo), rather than left to lapse; a no-op when the
    substrate is off or nothing is held (LEASE-07). While a runner still works the task (notes.running) it is left to that
    runner, which stops the session first. The task returned carries the mirrored lease_id; runlog.emit drops it from the event
    (no lease_id in the log). Then the run events that are the in-session counterpart of Telemetry's: `swarm.run.state` and
    `swarm.run.finished` for the move from `before`, and the `events` for the task row after the move, each as
    (type, payload, task_id or None). TaskStore's observer has refreshed live.json by then (the replay does that itself).
    """
    substrate_lease.settle_mirrored(store, tid, emit=ctx.emit, workspace=ctx.root)
    t = store.get(tid)
    emit_inline_transition(store, t, before, t["state"], reason=reason, root=ctx.root)
    for event_type, payload, task_id in events(t):
        # The closed set of this route: a manifest cannot make the selector write another type into the run log, and the literal
        # name keeps the emit site visible to the substrate-tee scanner (tests/test_substrate_tee.py).
        if event_type != "task.transition":
            raise SwarmError(ErrorCode.E_CONTRACT, "effects manifest names an event type this route does not emit")
        ctx.emit("task.transition", payload, task_id=task_id)
    return t


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
        t = finish_transition(store, ctx, tid, before=before, reason=args.reason or "manual",
                              events=lambda row: [("task.transition", {"task_id": tid, "state": row["state"], "reason": args.reason}, None)])
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
# The `--transition` route is a write (Phase 14.5 U3.2b). It resolves in the same order, but an unset or empty GSD_ENGINE means
# DEFAULT_ENGINE_WRITE, and a default-resolved call reaches the binary only when rust_serves_write(argv) holds. A write is
# never run twice: run_rust_write answers from a table (see its docstring) in which Python answers only when the binary
# says that nothing was written, and any answer that leaves the outcome unknown is refused with a typed E-INTERNAL.
# The binary is started with an allowlisted environment (BINARY_ENV_NAMES, the GSD_* and SWARM_* variables, no signing key).
DEFAULT_ENGINE = "python"  # the engine an unset or empty GSD_ENGINE resolves to for the read routes; "python" or "rust"
RUST_CALL_TIMEOUT_S = 660
RUST_FALLBACK_CODES = frozenset({"E-INTERNAL", "E-DEP", "E-CAPACITY", "E-CONTRACT", "E-INPUT"})
RUST_ENV_KEYS = ("SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID")
RELEASE_BINARY = Path(__file__).resolve().parent.parent / "target" / "release" / "gsd_engine"
# flags of the read grammar that take a value (crates/gsd-route/src/lib.rs `parse_orch_status_read`)
READ_VALUE_FLAGS = frozenset({"--root", "--repo", "--correlation-id", "--task-id", "--history"})
DEFAULT_ENGINE_WRITE = "python"  # the engine an unset or empty GSD_ENGINE resolves to for the --transition route; "python" or "rust"
# flags of the --transition write grammar that take a value; --repo is --root's alias, so a call names the pair once
WRITE_VALUE_FLAGS = frozenset({"--root", "--repo", "--correlation-id", "--task-id", "--reason"})
# The environment the binary is started with (security review F-2): what a process needs to run, and the GSD_* and SWARM_*
# variables, minus the signing key material, which only a route that signs or verifies is given.
BINARY_ENV_NAMES = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"})
BINARY_ENV_PREFIXES = ("GSD_", "SWARM_")
SIGNING_KEY_ENV = frozenset({"SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY",
                             "SWARM_REQUIRE_KEY"})
POST_KINDS = frozenset({"live_refresh", "lease_settle", "inline_transition"})

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


class WriteCall(NamedTuple):
    """A `--transition` call inside the grammar the binary serves, as `parse_write_argv` read it (argparse is the oracle in tests)."""
    target: str
    state: str
    reason: str
    root: str
    dry_run: bool
    task_id: str | None
    correlation_id: str | None


def parse_write_argv(argv: list[str]) -> WriteCall | None:
    """The call `argv` makes when it is exactly the `--transition` grammar the gsd_engine binary serves, else None.

    The grammar: `--transition TASK_ID STATE` once (both non-empty and not starting with `-`, no inline value), `--json` at least
    once, `--dry-run` (flags without an inline value) and each of `--root`/`--repo` (one slot), `--correlation-id`, `--task-id`,
    `--reason` at most once, as `--flag=value` or `--flag value` (a separate value must not start with `-`; it may be empty).
    Everything else is refused: `--ingest`, `--history`, `--advisory`, an unknown or abbreviated flag, a positional, a repeated
    value flag, a missing value. A refused argv under the default engine never starts the binary. Written by hand, not with
    argparse: argparse is the oracle the tests compare it with. SWARM_TASK_ID and SWARM_CORRELATION_ID are the defaults of
    the id flags, as in AgentScript.parser.
    """
    json_seen = dry_run = False
    transition: tuple[str, str] | None = None
    values: dict[str, str] = {}
    index = 0
    while index < len(argv):
        head, equals, inline = argv[index].partition("=")
        has_inline = bool(equals) and head.startswith("--")  # `--flag=value`; any other `=` belongs to an unknown token
        name = head if has_inline else argv[index]
        if name in ("--json", "--dry-run"):
            if has_inline:
                return None
            json_seen = json_seen or name == "--json"
            dry_run = dry_run or name == "--dry-run"
        elif name == "--transition":
            pair = argv[index + 1:index + 3]
            if has_inline or transition is not None or len(pair) != 2 or any(not part or part.startswith("-") for part in pair):
                return None
            transition = (pair[0], pair[1])
            index += 2
        elif name in WRITE_VALUE_FLAGS:
            slot = "--root" if name == "--repo" else name
            if slot in values:
                return None
            if has_inline:
                values[slot] = inline
            else:
                index += 1
                if index >= len(argv) or argv[index].startswith("-"):
                    return None
                values[slot] = argv[index]
        else:
            return None
        index += 1
    if not json_seen or transition is None:
        return None
    return WriteCall(transition[0], transition[1], values.get("--reason", ""), values.get("--root", "."), dry_run,
                     values["--task-id"] if "--task-id" in values else os.environ.get("SWARM_TASK_ID") or None,
                     values["--correlation-id"] if "--correlation-id" in values else os.environ.get("SWARM_CORRELATION_ID") or None)


def rust_serves_write(argv: list[str]) -> bool:
    """True exactly for the argv `parse_write_argv` accepts: the `--transition` grammar the gsd_engine binary serves."""
    return parse_write_argv(argv) is not None


def _binary_env() -> dict[str, str]:
    """The environment of the gsd_engine process: BINARY_ENV_NAMES and the GSD_* and SWARM_* variables, never SIGNING_KEY_ENV."""
    return {key: value for key, value in os.environ.items()
            if (key in BINARY_ENV_NAMES or key.startswith(BINARY_ENV_PREFIXES)) and key not in SIGNING_KEY_ENV}


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
        done = subprocess.run([binary], input=payload, stdout=subprocess.PIPE, check=False, timeout=_rust_timeout_s(), env=_binary_env())
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


class Manifest(NamedTuple):
    """The effects manifest a binary writes after its COMMIT, checked: the run events and the post-commit steps of one transition."""
    effects: tuple[tuple[str, dict[str, Any], str | None], ...]
    inline_from: str
    inline_reason: str


def _read_manifest(path: str, target: str) -> Manifest | None:
    """The manifest at `path`, or None when it is missing, empty, not JSON or not of the shape of Design 8: `effects` entries with a
    type, a payload object and a task_id or null; `post` with each of live_refresh, lease_settle and inline_transition once, the
    last two naming `target`. A step the Python path always runs and the manifest omits is a broken manifest, not a smaller one."""
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("effects"), list) or not isinstance(body.get("post"), list):
        return None
    effects = []
    for item in body["effects"]:
        if not (isinstance(item, dict) and isinstance(item.get("type"), str) and item["type"] and isinstance(item.get("payload"), dict)
                and isinstance(item.get("task_id"), (str, type(None)))):
            return None
        effects.append((item["type"], item["payload"], item.get("task_id")))
    post: dict[str, dict[str, Any]] = {}
    for item in body["post"]:
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind not in POST_KINDS or kind in post:
            return None
        post[kind] = item
    if set(post) != POST_KINDS or post["lease_settle"].get("task_id") != target or post["inline_transition"].get("task_id") != target:
        return None
    inline = post["inline_transition"]
    if not isinstance(inline.get("from"), str) or not isinstance(inline.get("reason"), str):
        return None
    return Manifest(tuple(effects), inline["from"], inline["reason"])


def _state_before(call: WriteCall) -> str | None:
    """The state of the task before the call, as the Python path reads it first (`store.get(tid)["state"]`), or None when there is no
    readable row. A read-only connection, so the selector creates and migrates nothing: the binary checks the schema itself."""
    try:
        db = (swarm_dir(Path(call.root).resolve()) / "tasks.db").resolve()
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            row = conn.execute("SELECT state FROM tasks WHERE task_id=?", (call.target,)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return None
    return row[0] if row is not None and isinstance(row[0], str) else None


def _call_ctx(call: WriteCall) -> Ctx:
    return Ctx("A01", "orch_status", call.task_id, call.correlation_id, call.dry_run, Path(call.root).resolve())


def _replay(call: WriteCall, manifest: Manifest, before: str | None) -> dict[str, Any]:
    """Runs the effects of a transition the binary committed, in the order of the Python path: the live.json refresh that
    TaskStore's observer makes inside `transition`, then `finish_transition` (lease settlement, inline run events, the manifest's
    events). `before` is the state read before the binary started; the manifest's own `from` stands in when none could be read.
    Returns the task row after the lease settlement, the row Python's own answer carries."""
    ctx = _call_ctx(call)
    store = TaskStore(root=ctx.root)
    try:
        from swarm import telemetry  # lazy, as TaskStore._notify_committed does
        telemetry.refresh_live(db_path=store.path)
    except Exception:  # noqa: BLE001 - the observer never fails a committed transition
        logging.getLogger("swarm.telemetry").warning("live.json refresh failed", exc_info=True)
    return finish_transition(store, ctx, call.target, before=manifest.inline_from if before is None else before,
                             reason=manifest.inline_reason, events=lambda _row: list(manifest.effects))


def _run_effects(call: WriteCall | None, before: str | None, effects_path: str) -> tuple[str | None, dict[str, Any] | None]:
    """Runs the effects of a committed write from the manifest at `effects_path`. Returns why they were not all run (else None) and
    the task row after the lease settlement (None when no replay ran to its end).
    A dry run has none; an argv outside the write grammar has no task to replay them for."""
    if call is None:
        return "argv outside the write grammar", None
    if call.dry_run:
        return None, None
    manifest = _read_manifest(effects_path, call.target)
    if manifest is None:
        return "effects manifest missing or unparsable", None
    try:
        return None, _replay(call, manifest, before)
    except Exception as exc:  # noqa: BLE001 - the transition is committed: say what was lost, never run it again
        return f"replay failed: {type(exc).__name__}", None


def _warn(message: str) -> None:
    print(f"orch_status: {message}", file=sys.stderr)


def _unknown_outcome(argv: list[str], call: WriteCall | None, why: str) -> int:
    """Prints the typed E-INTERNAL for a write whose outcome is unknown, records it, and returns exit 2. The call is not repeated."""
    target = call.target if call is not None else "<task>"
    body = {"agent": "A01", "script": "orch_status", "status": "error",
            "error": {"code": ErrorCode.E_INTERNAL.value,
                      "message": f"outcome unknown: {why}; the call was NOT repeated. "
                                 f"Run `orch_status.py --history {shlex.quote(target)}` before retrying",
                      "details": {"write_state": "unknown", **({"task_id": target} if call is not None else {})},
                      "handling": ErrorCode.E_INTERNAL.standard_handling}}
    try:
        SCRIPT.record_external_reply(argv, body, AgentScript.EXIT_ERROR)
    except Exception as exc:  # noqa: BLE001 - the refusal is printed whether or not the run log takes it
        _warn(f"the unknown-outcome refusal could not be recorded ({type(exc).__name__})")
    print(json.dumps(body, indent=2))
    return AgentScript.EXIT_ERROR


def _record_effects_lost(call: WriteCall | None, why: str) -> None:
    """Writes the run-log event `orch_status.effects_lost` for a transition that committed but whose effects were not all run."""
    try:
        ctx = _call_ctx(call) if call is not None else Ctx("A01", "orch_status", None, None, False, Path.cwd())
        ctx.emit("orch_status.effects_lost", {"task_id": call.target if call is not None else None, "reason": why})
    except Exception as exc:  # noqa: BLE001 - nothing more can be done for a lost event than to say so
        _warn(f"effects of a committed transition were lost: {why}; the event could not be recorded ({type(exc).__name__})")


def _settled_reply(reply: dict[str, Any], stdout: bytes, row: dict[str, Any] | None) -> tuple[dict[str, Any], bytes]:
    """The reply to record and the bytes to print once the lease settlement has run. Python answers with the row it read after
    `settle_mirrored` (its `task.notes` and `notes_json.lease` say released, completed or unsettled), while the binary answered with
    the row it committed, before. When `row` (what `_replay` read) differs from the reply's `task`, that `task` is replaced in place,
    so the reply keeps the key order the binary printed, and the bytes are rendered as `AgentScript._print` renders `--json`. When
    the rows are equal, or there is no row (a dry run, a lost manifest, a failed replay) or no `task` object, the binary's `stdout`
    is returned untouched."""
    if row is None or not isinstance(reply.get("task"), dict):
        return reply, stdout
    settled = json.loads(json.dumps(row, default=str))  # the row as the reply carries it: JSON values, as `_print` would write them
    if settled == reply["task"]:
        return reply, stdout
    reply["task"] = settled
    return reply, (json.dumps(reply, indent=2, default=str) + "\n").encode("utf-8")


def _error_details(reply: dict[str, Any]) -> dict[str, Any]:
    error = reply.get("error")
    details = error.get("details") if isinstance(error, dict) else None
    return details if isinstance(details, dict) else {}


def run_rust_write(argv: list[str], binary: str, *, explicit: bool) -> int | None:
    """Answers a `--transition` call on the Rust engine at `binary`. None hands the call to the Python body, once.

    The request is the read request plus `effects_path`, a temp file this selector owns and removes; the binary fills it after
    its COMMIT. Outcome table (Design 7), by what the binary did:

    * exit 0 or 1 with a JSON object: committed (or a dry run). The manifest is replayed in Python's order, the answer is
      recorded (D1) and printed, with the task row read after the lease settlement in place of the binary's own `task` when the two
      differ (`_settled_reply`: Python prints that row). A missing or broken manifest, or a replay that fails, writes the run-log
      event `orch_status.effects_lost`; the answer is still printed as the binary wrote it and nothing is run again.
    * exit 2 with `error.details.write_state == "not_started"`: nothing was written; the Python body answers.
    * exit 2 with write_state `final`, or absent on any code but E-INTERNAL (E-CONTRACT, E-POLICY, E-INPUT of an unknown task or
      of an invalid id, Python's `max_attempts exceeded` E-TIMEOUT that names its task, ...): the binary's own refusal, with
      Python's text; recorded and printed as it is.
    * everything else is an unknown outcome, answered with a typed E-INTERNAL (`_unknown_outcome`), no Python run, no fallback:
      the call deadline (the subprocess deadline, or an E-TIMEOUT that names no task), a signal or any exit code outside 0, 1, 2,
      an unparsable reply, E-INTERNAL without a write_state, an unrecognised write_state.
    * not started: a binary that cannot be started is Python's answer under the default engine and E-DEP under `explicit`,
      as for reads; so is an argument the binary cannot take (not UTF-8 encodable).
    """
    call = parse_write_argv(argv)
    before = _state_before(call) if call is not None and not call.dry_run else None
    try:
        handle, effects_path = tempfile.mkstemp(prefix="gsd-effects-", suffix=".json")
    except OSError:
        return None if not explicit else _refuse(ErrorCode.E_DEP, "the effects file for the gsd_engine binary could not be created")
    os.close(handle)
    try:
        return _answer_write(argv, binary, explicit, call, before, effects_path)
    finally:
        try:
            os.unlink(effects_path)
        except OSError:
            pass


def _answer_write(argv: list[str], binary: str, explicit: bool, call: WriteCall | None, before: str | None,
                  effects_path: str) -> int | None:
    request = {"agent": "A01", "script": "orch_status", "argv": argv, "root": os.getcwd(),
               "env": {key: os.environ[key] for key in RUST_ENV_KEYS if os.environ.get(key)}, "effects_path": effects_path}
    try:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return None  # fallback: an argument the binary cannot take
    try:
        done = subprocess.run([binary], input=payload, stdout=subprocess.PIPE, check=False, timeout=_rust_timeout_s(), env=_binary_env())
    except subprocess.TimeoutExpired:
        return _unknown_outcome(argv, call, "the gsd_engine binary did not answer within the call deadline")
    except OSError:
        if not explicit:
            return None  # fallback: nothing was started
        return _refuse(ErrorCode.E_DEP, "the gsd_engine binary named by GSD_ENGINE_BIN could not be started")
    code = done.returncode
    if code not in (AgentScript.EXIT_OK, AgentScript.EXIT_FAIL, AgentScript.EXIT_ERROR):
        how = f"killed by signal {-code}" if code < 0 else f"exit status {code}"
        return _unknown_outcome(argv, call, f"the gsd_engine binary ended with {how}")
    reply = _reply_object(done.stdout)
    if reply is None:
        return _unknown_outcome(argv, call, "the gsd_engine binary printed no JSON object")
    stdout = done.stdout
    if code == AgentScript.EXIT_ERROR:
        error_code, details = _error_code(reply), _error_details(reply)
        state = details.get("write_state")
        # Python's own `max_attempts exceeded` refusal is E-TIMEOUT too, and names its task in details; the call deadline's does not
        if error_code == ErrorCode.E_TIMEOUT.value and not isinstance(details.get("task_id"), str):
            return _unknown_outcome(argv, call, "the gsd_engine binary reported E-TIMEOUT")
        if state == "not_started":
            return None  # Python answers: the binary wrote nothing
        if error_code is None or state not in (None, "final") or (state is None and error_code == ErrorCode.E_INTERNAL.value):
            return _unknown_outcome(argv, call, "the gsd_engine binary refused without saying that nothing was written")
    else:
        lost, row = _run_effects(call, before, effects_path)
        if lost is not None:
            _record_effects_lost(call, lost)
        reply, stdout = _settled_reply(reply, stdout, row)
    try:
        SCRIPT.record_external_reply(argv, reply, code)
    except Exception as exc:  # noqa: BLE001 - a write is never handed to Python once the binary has answered
        _warn(f"the run-log record of the binary's answer failed ({type(exc).__name__}); the answer stands and was not repeated")
    sys.stdout.buffer.write(stdout)
    sys.stdout.flush()
    return code


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
    write = any(token == "--transition" or token.startswith("--transition=") for token in argv)
    if not explicit and (DEFAULT_ENGINE_WRITE if write else DEFAULT_ENGINE) != "rust":
        return None
    binary = os.environ.get("GSD_ENGINE_BIN", "")
    if not binary and RELEASE_BINARY.is_file() and os.access(RELEASE_BINARY, os.X_OK):
        binary = str(RELEASE_BINARY)
    if not explicit:
        serves = rust_serves_write(argv) if write else rust_serves(argv)
        if not serves or not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
            return None
    if os.environ.get("GSD_RUST_BACKEND", "") not in ("", "host", "sandbox"):
        return _refuse(ErrorCode.E_INPUT, "GSD_RUST_BACKEND must be 'host' or 'sandbox' when set")
    if not binary:
        return _refuse(ErrorCode.E_DEP, "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary")
    return (run_rust_write if write else run_rust)(argv, binary, explicit=explicit)


if __name__ == "__main__":
    answer = select_engine(sys.argv[1:])
    sys.exit(answer if answer is not None else SCRIPT.main())
