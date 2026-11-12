"""Frozen copy of the stateless `live.json` path of `swarm/telemetry.py` as it was at git 5f49ab0 (before unit E2 of phase 14.5, D-P35).

It is the oracle of `tests/test_telemetry_refresh_cost.py`: the optimised `derive_snapshot` / `refresh_live` must write the same document,
byte for byte, for the same Task Store state. The functions below are VERBATIM copies (do not edit them to follow the live code: a
difference is exactly what the differential test is there to find). Names this source does not define (clip, clip_id, `_read_tail`,
`_parse_ts`, `_live_lock`, `_producer_fresh`, the constants, the schema validator) resolve to the live module, which this unit did not change:
`load(telemetry)` executes the source in a copy of the live module's namespace and returns the frozen functions.
"""
from __future__ import annotations

import types
from typing import Any

FROZEN_NAMES = ("derive_snapshot", "refresh_live", "build_snapshot", "_load_tasks", "_handovers_in", "_write_atomic", "_read_live_at")

OLD_SOURCE = r'''
import uuid  # the live module no longer needs it

def _handovers_in(sdir: Path, corr: str | None, tasks: list[dict[str, Any]], side: str = "to_agent") -> dict[str, str]:
    """`{task_id: handover_id}` for each task whose newest handover is addressed to the agent the task runs on
    (`side="to_agent"`, the in marker) or was written by that agent (`side="from_agent"`, the out marker).

    Read from `<state dir>/handovers/<correlation>/` (swarm.handover's layout; the digest is checked, the full schema
    is not: this is a display projection). A malformed newest file means no marker, as `latest_handover` raises
    rather than skip to an older one; a handover of another agent is passed over. Never raises."""
    out: dict[str, str] = {}
    if not corr:
        return out
    try:
        from . import handover  # lazy: handover imports this module lazily too
        names = os.listdir(sdir / "handovers" / corr)
    except (OSError, ValueError, ImportError):
        return out
    by_task: dict[str, list[tuple[int, str]]] = {}
    for name in names:
        m = _HANDOVER_FILE.match(name)
        if m:
            by_task.setdefault(m["task"], []).append((int(m["seq"]), name))
    try:
        for t in tasks:
            agent = handover.agent_id_of(t.get("agent_id"))
            cands = by_task.get(t["task_id"])
            if not agent or not cands:
                continue
            for _, name in sorted(cands, reverse=True):
                path = sdir / "handovers" / corr / name
                try:
                    if not 0 < path.stat().st_size <= handover.MAX_FILE_BYTES:
                        break
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    valid = (isinstance(doc, dict) and doc.get("task_id") == t["task_id"]
                             and isinstance(doc.get("handover_id"), str) and isinstance(doc.get(side), str)
                             and doc.get("digest") == handover.compute_digest(doc))
                except (OSError, ValueError):
                    break
                if not valid:
                    break
                if handover.agent_id_of(doc[side]) == agent:
                    out[t["task_id"]] = clip_id(doc["handover_id"], 64) or doc["handover_id"][:64]
                    break
    except Exception:  # noqa: BLE001 - a display projection: a handover module that misbehaves hides handover_in only
        log.debug("handover_in derivation failed", exc_info=True)
    return out


def _write_atomic(path: Path, doc: dict[str, Any]) -> None:
    """Write `doc` as JSON to a unique sibling temp file, then `os.replace`: readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        data = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))  # C encoder: much cheaper than dump(fh)
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


#: live.json path -> (inode, size, mtime_ns) of the last file that passed schema validation: an unchanged file is
#: parsed again (cheap C decode) but never re-validated (the Python validator was the cost).
_LIVE_VALID: dict[str, tuple[int, int, int]] = {}


def _remember_valid(path: Path) -> None:
    with contextlib.suppress(OSError):
        st = path.stat()
        _LIVE_VALID[str(path)] = (st.st_ino, st.st_size, st.st_mtime_ns)


def _read_live_at(path: Path) -> dict[str, Any] | None:
    try:
        st = path.stat()
        sig = (st.st_ino, st.st_size, st.st_mtime_ns)
        doc: object = json.loads(path.read_text(encoding="utf-8"))
        if _LIVE_VALID.get(str(path)) == sig and isinstance(doc, dict) and doc.get("schema") == LIVE_SCHEMA:
            return doc
        if not isinstance(doc, dict) or not _valid_live(doc):
            return None
        _LIVE_VALID[str(path)] = sig
        return doc
    except (OSError, ValueError):
        return None


def _valid_live(doc: object) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == LIVE_SCHEMA and not validate(doc, load_schema(LIVE_SCHEMA))


# ---- snapshot assembly (shared by the producer and the stateless path) -----------------------------------------
def _sort_key(row: dict[str, Any], order: int) -> tuple[int, int]:
    rank = _STALLED_RANK if row["stalled"] and row["state"] == RunState.RUNNING.value else _RANK[RunState(row["state"])]
    return rank, order


def build_snapshot(rows: list[dict[str, Any]], *, correlation_id: str | None, seq: int, updated_wall: float, elapsed_s: float,
                   max_parallel: int, effective_parallel: int, degraded: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble a `swarm.live.v1` document from complete run rows (counts cover every row, the list is capped)."""
    counts = {s: 0 for s in ("running", "queued", "waiting", "completed", "failed", "cancelled", "timed_out")}
    stalled = 0
    for r in rows:
        st = RunState(r["state"])
        if st in (RunState.RUNNING, RunState.HANDING_OFF):
            counts["running"] += 1
        elif st in (RunState.QUEUED, RunState.STARTING):
            counts["queued"] += 1
        else:
            counts[st.value] += 1
        stalled += 1 if r["stalled"] and st is RunState.RUNNING else 0
    if not rows:
        state = "idle"
    elif counts["running"] or counts["queued"] or counts["waiting"]:
        state = "running"
    elif counts["failed"] or counts["timed_out"]:
        state = "failed"
    elif counts["cancelled"]:
        state = "cancelled"  # a cancelled swarm must not read as a clean finish (the bar would paint a green check)
    else:
        state = "completed"
    ordered = [r for _, r in sorted(((_sort_key(r, i), r) for i, r in enumerate(rows)), key=lambda p: p[0])]
    return {
        "schema": LIVE_SCHEMA, "correlation_id": correlation_id, "seq": seq, "updated_wall": updated_wall,
        "swarm": {"state": state, "elapsed_s": round(max(0.0, elapsed_s), 3), "max_parallel": max_parallel,
                  "effective_parallel": effective_parallel, **counts, "stalled": stalled, "degraded": degraded},
        "runs": ordered[:MAX_RUNS],
    }


def _load_tasks(db: Path, correlation_id: str | None, state_dir: Path) -> tuple[str | None, list[dict[str, Any]], dict[str, list[tuple[Any, ...]]]]:
    """(correlation, task rows, transitions by task) from a read-only connection; nothing when there is no store."""
    if not db.exists():
        return correlation_id, [], {}
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        if correlation_id is None:
            marker = state_dir / "latest_correlation"
            try:
                correlation_id = marker.read_text(encoding="utf-8").strip() or None
            except OSError:
                correlation_id = None
        rows = []
        if correlation_id is not None:
            rows = conn.execute("SELECT * FROM tasks WHERE correlation_id=? ORDER BY dag_depth, created_at",
                                (correlation_id,)).fetchall()
        if not rows:
            newest = conn.execute("SELECT correlation_id FROM tasks ORDER BY updated_at DESC LIMIT 1").fetchone()
            if newest is None:
                return correlation_id, [], {}
            correlation_id = newest[0]
            rows = conn.execute("SELECT * FROM tasks WHERE correlation_id=? ORDER BY dag_depth, created_at",
                                (correlation_id,)).fetchall()
        ids = [r["task_id"] for r in rows]
        marks = ",".join("?" * len(ids))
        trans: dict[str, list[tuple[Any, ...]]] = {tid: [] for tid in ids}
        for t in conn.execute(f"SELECT task_id, to_state, reason, ts FROM transitions WHERE task_id IN ({marks}) "
                              "ORDER BY id", ids):
            trans[t["task_id"]].append((t["to_state"], t["reason"], t["ts"]))
        return correlation_id, [dict(r) for r in rows], trans
    finally:
        conn.close()


def _loads(text: str | None, default: _T) -> _T:
    try:
        val = json.loads(text) if text else default
    except ValueError:
        return default
    return val if isinstance(val, type(default)) else default


def derive_snapshot(root: str | Path | None = None, *, clock: Clock = SYSTEM_CLOCK, db_path: str | Path | None = None,
                    correlation_id: str | None = None, stall_after_s: float | None = None) -> dict[str, Any]:
    """A `swarm.live.v1` document rebuilt, statelessly, from Task Store rows (mapped with `lifecycle.from_task_state`)
    and the `events.jsonl` tail (activity and heartbeat). Runs without a scheduler process show up too.

    The Task Store keeps wall timestamps only, so elapsed values here are wall differences clamped at zero;
    `Telemetry.snapshot` is the producer-monotonic variant. Writes nothing."""
    from .taskstore import SATISFIED_STATES  # lazy: taskstore imports this module lazily too

    sdir = _state_dir(root, db_path)
    db = Path(db_path).resolve() if db_path is not None else sdir / "tasks.db"
    stall_after = stall_after_s if stall_after_s is not None else stall_after_default()
    corr, tasks, trans = _load_tasks(db, correlation_id, sdir)
    now_wall = clock.wall()

    events = _read_tail(sdir / "events.jsonl")
    last_by_run: dict[str, dict[str, dict[str, Any]]] = {}  # key -> {"activity": rec, "heartbeat": rec, "handover": rec}
    tick: dict[str, Any] | None = None
    degraded: dict[str, Any] | None = None
    finished: dict[str, str] = {}
    for rec in events:
        if corr is not None and rec.get("correlation_id") not in (None, corr):
            continue
        kind, p = rec["type"], rec["payload"]
        if kind == EV_TICK:
            tick = p
        elif kind == EV_DEGRADED:
            if p.get("mode") == RECOVERED_MODE:  # clear_degraded(): the fallback it names (`detail`) is over
                if degraded is not None and p.get("detail") in (None, degraded["mode"]):
                    degraded = None
            else:
                degraded = {"reason": clip(p.get("reason")) or "", "mode": clip(p.get("mode"), 32) or "degraded"}
        elif kind in (EV_ACTIVITY, EV_HEARTBEAT, EV_HANDOVER):
            key = p.get("run_id") or rec.get("task_id")
            if isinstance(key, str):
                last_by_run.setdefault(key, {})[kind] = rec
        elif kind == EV_FINISHED and isinstance(p.get("run_id"), str):
            finished[p["run_id"]] = str(p.get("state"))

    by_id = {t["task_id"]: t for t in tasks}
    handover_in = _handovers_in(sdir, corr, tasks)
    rows: list[dict[str, Any]] = []
    starts: list[float] = []
    ends: list[float] = []
    for t in tasks:
        deps: list[str] = _loads(t.get("depends_on"), [])
        deps_met = all((by_id.get(d) or {}).get("state") in SATISFIED_STATES for d in deps)
        state = from_task_state(t["state"], deps_met=deps_met)
        attempt = max(int(t.get("attempt") or 0), 1)
        run_id = f"{t['task_id']}.a{attempt}"
        if state is RunState.FAILED and finished.get(run_id) == RunState.TIMED_OUT.value:
            state = RunState.TIMED_OUT
        history = trans.get(t["task_id"], [])
        last_ts = history[-1][2] if history else (t.get("updated_at") or now_wall)
        claimed = [ts for to, _, ts in history if to == "CLAIMED"]
        start_ts = claimed[-1] if claimed else (history[0][2] if history else (t.get("created_at") or last_ts))
        settled = is_terminal(state)
        end_ts = last_ts if settled else now_wall
        starts.append(start_ts)
        ends.append(end_ts)
        activity = last_by_run.get(run_id) or last_by_run.get(t["task_id"]) or {}
        act, hb = activity.get(EV_ACTIVITY), activity.get(EV_HEARTBEAT)
        latest = max((r for r in (act, hb) if r), key=lambda r: r.get("ts") or "", default=None)
        last_act_wall: float | None = None
        if act and _parse_ts(act.get("ts")) is not None:
            last_act_wall = _parse_ts(act["ts"])
        hb_wall = _parse_ts(hb.get("ts")) if hb else None
        if hb and hb_wall is not None and isinstance(hb["payload"].get("since_activity_s"), (int, float)):
            cand = hb_wall - float(hb["payload"]["since_activity_s"])
            last_act_wall = cand if last_act_wall is None else max(last_act_wall, cand)
        if last_act_wall is None or last_act_wall < start_ts:
            last_act_wall = last_ts if state is RunState.RUNNING else None
        since = max(0.0, now_wall - last_act_wall) if (last_act_wall is not None and not settled) else None
        lp = latest["payload"] if latest else {}
        ho = (activity.get(EV_HANDOVER) or {}).get("payload", {})
        error = None
        if state in (RunState.FAILED, RunState.TIMED_OUT) and history:
            error = clip(history[-1][1])
        rows.append({
            "run_id": run_id, "task_id": t["task_id"], "agent": clip(t.get("agent_id") or t.get("capability"), 64) or "?",
            "state": state.value, "attempt": attempt, "elapsed_s": round(max(0.0, end_ts - start_ts), 3),
            "state_elapsed_s": round(max(0.0, end_ts - last_ts), 3) if not settled else 0.0,
            "since_activity_s": round(since, 3) if since is not None else None,
            "stalled": bool(state is RunState.RUNNING and since is not None and since > stall_after),
            "activity": clip(lp.get("text") or lp.get("activity")), "tool_calls": int(lp.get("tool_calls") or 0),
            "last_tool": clip(lp.get("tool") or lp.get("last_tool"), 64), "parent": t.get("parent_id"),
            "handover_in": handover_in.get(t["task_id"]), "handover_out": clip_id(ho.get("handover_id"), 64), "error": error,
        })

    max_parallel = int((tick or {}).get("max_parallel") or _env_int("SWARM_MAX_PARALLEL", DEFAULT_MAX_PARALLEL))
    effective = int((tick or {}).get("effective_parallel") or max_parallel)
    elapsed = (max(ends) - min(starts)) if rows else 0.0
    return build_snapshot(rows, correlation_id=corr, seq=0, updated_wall=now_wall, elapsed_s=elapsed,
                          max_parallel=max_parallel, effective_parallel=effective, degraded=degraded)


def refresh_live(root: str | Path | None = None, *, db_path: str | Path | None = None, clock: Clock = SYSTEM_CLOCK,
                 correlation_id: str | None = None) -> bool:
    """Derive the snapshot and write `live.json` atomically. Fail-open: any error is logged and returns False. When a
    `Telemetry` in this process owns the same file it is flushed instead (its in-memory view is richer).

    A derived write never replaces a document another LIVE scheduler process produced (`producer == "scheduler"`, fresh
    within 3 heartbeats, pid alive): that file is richer and monotonic, and the call returns False. The whole
    read-check-derive-write cycle runs under an flock on `live.json.lock`, so two derived writers cannot interleave and
    a stale derivation cannot overwrite a newer one."""
    try:
        target = (_state_dir(root, db_path) / LIVE_FILE)
        owner = _ACTIVE.get(str(target))
        tel = owner() if owner is not None else None
        if tel is not None:
            return tel.flush_live()
        with _live_lock(target) as held:
            if not held and fcntl is not None:
                log.warning("live.json lock busy; skipping derived refresh")
                return False
            prior = _read_live_at(target)
            if _producer_fresh(prior, clock.wall(), owned_here=False):
                return False
            doc = derive_snapshot(root, clock=clock, db_path=db_path, correlation_id=correlation_id)
            doc["seq"] = (prior["seq"] + 1) if prior else 1
            doc["producer"] = "derived"
            doc["pid"] = os.getpid()
            _write_atomic(target, doc)
            _remember_valid(target)  # our own derived document: the next refresh need not re-validate it
        return True
    except Exception:  # noqa: BLE001 - observer path: a display projection must never break a Task Store transition
        log.warning("refresh_live failed", exc_info=True)
        return False
'''


def load(telemetry: types.ModuleType) -> Any:
    """The frozen functions, bound to the namespace of `telemetry` (a module object): `load(T).refresh_live(...)` is the pre-E2 code."""
    namespace = dict(vars(telemetry))
    exec(compile(OLD_SOURCE, "<telemetry.py at 5f49ab0>", "exec"), namespace)  # noqa: S102 - a frozen, committed copy of this repository's own code
    return types.SimpleNamespace(**{name: namespace[name] for name in FROZEN_NAMES}, namespace=namespace)
