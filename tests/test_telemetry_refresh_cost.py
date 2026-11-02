"""Unit E2 (phase 14.5, D-P35): the cost of `telemetry.refresh_live`, the derived `live.json` written after every committed
`TaskStore.transition`, and the proof that making it cheaper changed nothing it writes.

1. Differential: the optimised `derive_snapshot` / `refresh_live` against a FROZEN copy of the pre-E2 code
   (`tests/telemetry_refresh_oracle.py`), byte for byte, over named store states and a seeded fuzz set.
2. The contracts the optimisation had to keep, through the new code: the producer rule, the flock busy-skip, fail-open, the `seq` rule,
   the `_LIVE_VALID` memo, the atomic write (a crash leaves a whole file or none, never a temp file behind).
3. The cost, as operation counts and ratios that a loaded host cannot move (SQL statements, connections, JSON decodes, redactions,
   Python calls per added task, file operations per refresh): never a wall-clock threshold.
"""
import contextlib
import fcntl
import importlib
import json
import os
import random
import secrets
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from telemetry_refresh_oracle import load as load_oracle

T0 = 1_760_000_000.0  # the base of every stored timestamp; the fake clock reads T0 + an offset
STATES = ["CREATED", "VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "DONE", "BLOCKED",
          "FAILED", "RETRY", "ESCALATED", "CANCELLED"]
RUNNING_ISH = ["IN_PROGRESS", "IN_PROGRESS", "CLAIMED", "PLANNED", "PLANNED", "VALIDATED"]
AGENTS = [None, "", "a05-backend", "A05", "a06-frontend", "a08-qa", "sk-onboarding-flow-v2", "x" * 90, "agent with  spaces"]
CAPS = ["code.backend", "code.frontend", "req.spec"]
SECRET = "sk-" + secrets.token_hex(16)  # generated per run, only ever used inside this process
REASONS = [None, "", "boom", "x" * 300, "line one\nline  two\ttabbed", f"call failed with token {SECRET} attached", "tool timeout after 30s"]


@pytest.fixture()
def mods(tmp_path, monkeypatch):
    """Fresh imports per test (other suites purge sys.modules) and the frozen pre-E2 functions bound to the same module."""
    monkeypatch.setenv("SWARM_DIR", str(tmp_path / ".swarm"))
    for name in ("SWARM_STALL_AFTER_S", "SWARM_MAX_PARALLEL", "SWARM_HEARTBEAT_S"):
        monkeypatch.delenv(name, raising=False)

    class M:
        tel = importlib.import_module("swarm.telemetry")
        clock = importlib.import_module("swarm.clock")
        store_mod = importlib.import_module("swarm.taskstore")
        old = load_oracle(importlib.import_module("swarm.telemetry"))
        root = tmp_path

    return M


def encode(doc):
    """The bytes `_write_atomic` puts in the file."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ---- store states --------------------------------------------------------------------------------------------------------------------
def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def make_spec(rnd, n, *, ties=False, other_corr=0, events=True, stalls=True):
    """A deterministic store state: task rows, transition rows (history order = row order), run events, and the clock offset to read them at."""
    tasks, histories = [], {}
    ids = []
    for i in range(n):
        tid = f"T-{i}" if rnd.random() < 0.9 else f"T.{i}-x_y"
        ids.append(tid)
        deps = [d for d in rnd.sample(ids[:-1], k=min(len(ids) - 1, rnd.choice([0, 0, 0, 1, 1, 2]))) if d != tid]
        if rnd.random() < 0.05:
            deps.append("T-ghost")  # a dependency that does not exist is never satisfied
        raw_deps = rnd.choice([None, "", "not json", '{"a": 1}', "[]"]) if rnd.random() < 0.08 else json.dumps(deps)
        state = rnd.choice(STATES if rnd.random() < 0.5 else RUNNING_ISH)
        created = T0 + (i // 2 if ties else i) * 0.5
        path = [rnd.choice(STATES) for _ in range(rnd.choice([0, 1, 2, 3, 4, 6]))]
        if path and rnd.random() < 0.6:
            path.insert(rnd.randrange(len(path) + 1), "CLAIMED")
        if path and rnd.random() < 0.2:
            path.append("CLAIMED")
        histories[tid] = [("CREATED", created)] + [(s, created + 1 + 7 * k) for k, s in enumerate(path)]
        tasks.append({
            "task_id": tid, "correlation_id": "c1", "parent_id": rnd.choice([None, None, ids[0]]), "dag_depth": rnd.choice([0, 0, 1, 2]),
            "capability": rnd.choice(CAPS), "agent_id": rnd.choice(AGENTS), "state": state, "attempt": rnd.choice([0, 1, 1, 2, 3, None]),
            "depends_on": raw_deps, "created_at": created, "updated_at": histories[tid][-1][1] if rnd.random() < 0.9 else None})
    for j in range(other_corr):
        tid = f"O-{j}"
        tasks.append({"task_id": tid, "correlation_id": "c2", "parent_id": None, "dag_depth": 0, "capability": "req.spec", "agent_id": "a08-qa",
                      "state": rnd.choice(STATES), "attempt": 1, "depends_on": "[]", "created_at": T0 + 500 + j, "updated_at": T0 + 500 + j})
        histories[tid] = [("CREATED", T0 + 500 + j), ("VALIDATED", T0 + 501 + j)]
    queues = {t: list(h) for t, h in histories.items()}
    order = []
    while any(queues.values()):  # interleave the tasks' histories at random: only the per-task order is meaningful
        tid = rnd.choice([t for t, q in queues.items() if q])
        order.append((tid, queues[tid].pop(0)))
    transitions, last = [], {}
    for tid, (to_state, ts) in order:
        transitions.append((tid, last.get(tid), to_state, "actor", rnd.choice(REASONS), ts))
        last[tid] = to_state
    horizon = max([ts for _, (_, ts) in order] + [T0])
    recs = []
    if events:
        for t in tasks:
            tid = t["task_id"]
            if t["correlation_id"] != "c1" or rnd.random() > 0.35:
                continue
            run_id = f"{tid}.a{max(int(t['attempt'] or 0), 1)}"
            at = horizon - rnd.choice([1, 20, 400, 3000]) if stalls else horizon
            kind = rnd.choice(["activity", "heartbeat", "both", "handover", "finished_timeout", "task_keyed"])
            if kind in ("activity", "both", "task_keyed"):
                recs.append(_event("swarm.run.activity", at, "c1", tid, {"run_id": run_id if kind != "task_keyed" else None, "tool": "edit" if rnd.random() < 0.5 else None,
                                                                         "text": rnd.choice(["working", f"token {SECRET} here", "y" * 200, None]),
                                                                         "tool_calls": rnd.choice([0, 3, "7", None])}))
            if kind in ("heartbeat", "both"):
                recs.append(_event("swarm.run.heartbeat", at + 5, "c1", tid, {"run_id": run_id, "since_activity_s": rnd.choice([2.5, 300, "x", None]),
                                                                              "text": "beat"}))
            if kind == "handover":
                recs.append(_event("swarm.run.handover", at, "c1", tid, {"run_id": run_id, "handover_id": f"{tid}.1"}))
            if kind == "finished_timeout":
                recs.append(_event("swarm.run.finished", at, "c1", tid, {"run_id": run_id, "state": "timed_out"}))
        recs.append(_event("swarm.scheduler.tick", horizon, "c1", None, {"max_parallel": 6, "effective_parallel": 3}))
        recs.append(_event("swarm.degraded", horizon, None, None, {"reason": "fell back", "mode": "serial"}))
        if rnd.random() < 0.4:
            recs.append(_event("swarm.degraded", horizon, "c1", None, {"mode": "recovered", "detail": "serial"}))
        recs.append(_event("swarm.run.activity", horizon, "other-corr", None, {"run_id": "foreign.a1", "text": "not ours"}))
        rnd.shuffle(recs)
        recs.sort(key=lambda r: r["ts"])
    return {"tasks": tasks, "transitions": transitions, "events": recs, "now": horizon + rnd.choice([0, 3, 90, 200, 4000])}


def _event(kind, ts, corr, task_id, payload):
    return {"type": kind, "ts": _iso(ts), "correlation_id": corr, "task_id": task_id, "payload": payload}


def build_dir(mods, directory, spec, *, marker=None):
    """Write `spec` as a state dir: tasks.db (through TaskStore for the schema), events.jsonl, latest_correlation."""
    directory.mkdir(parents=True, exist_ok=True)
    store = mods.store_mod.TaskStore(directory / "tasks.db")
    cols = ("task_id", "correlation_id", "parent_id", "dag_depth", "capability", "agent_id", "state", "attempt", "depends_on", "created_at", "updated_at")
    store.conn.executemany(f"INSERT INTO tasks ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", [tuple(t[c] for c in cols) for t in spec["tasks"]])
    store.conn.executemany("INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES (?,?,?,?,?,?)", spec["transitions"])
    store.conn.commit()
    store.conn.close()
    if spec["events"]:
        (directory / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in spec["events"]), encoding="utf-8")
    if marker is not None:
        (directory / "latest_correlation").write_text(marker + "\n", encoding="utf-8")


def mutate(db, task_id, to_state, ts):
    """One transition applied the way the store applies it, to the raw tables (no observer, no refresh)."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES (?,?,?,?,?,?)", (task_id, None, to_state, "t", "step", ts))
        conn.execute("UPDATE tasks SET state=?, updated_at=? WHERE task_id=?", (to_state, ts, task_id))
        conn.commit()


def assert_same_everywhere(mods, tmp_path, spec, *, marker=None):
    """Old and new `derive_snapshot` agree (for every way of choosing the correlation, with and without a borrowed connection), and a run of
    `refresh_live` over a few committed changes writes the same live.json bytes, seq included."""
    tel, old = mods.tel, mods.old
    a, b = tmp_path / "old", tmp_path / "new"
    build_dir(mods, a, spec, marker=marker)
    shutil.copytree(a, b)
    clock = mods.clock.FakeClock(start_wall=spec["now"])
    for corr in (None, "c1", "c2", "no-such-correlation"):
        want = encode(old.derive_snapshot(db_path=a / "tasks.db", clock=clock, correlation_id=corr))
        assert encode(tel.derive_snapshot(db_path=a / "tasks.db", clock=clock, correlation_id=corr)) == want, corr
        with contextlib.closing(sqlite3.connect(a / "tasks.db")) as conn:
            conn.row_factory = sqlite3.Row  # the factory the Task Store's own connection has
            assert encode(tel.derive_snapshot(db_path=a / "tasks.db", clock=clock, correlation_id=corr, conn=conn)) == want, corr
    victims = [t["task_id"] for t in spec["tasks"] if t["correlation_id"] == "c1"][:3]
    own = sqlite3.connect(b / "tasks.db")
    own.row_factory = sqlite3.Row
    try:
        for step in range(4):
            assert old.refresh_live(db_path=a / "tasks.db", clock=clock) is True
            assert tel.refresh_live(db_path=b / "tasks.db", clock=clock, conn=own if step % 2 else None) is True
            assert (b / "live.json").read_bytes() == (a / "live.json").read_bytes(), f"step {step}"
            if victims:
                for target in (a, b):
                    mutate(target / "tasks.db", victims[step % len(victims)], STATES[(step * 5) % len(STATES)], spec["now"] - 3 + step)
            clock.advance(11)
    finally:
        own.close()
    assert json.loads((b / "live.json").read_text())["seq"] == 4


NAMED = {}


def named(fn):
    NAMED[fn.__name__] = fn
    return fn


def _simple(states, *, depends=None, corr="c1", attempt=1):
    tasks, transitions = [], []
    for i, state in enumerate(states):
        tid = f"S-{i}"
        tasks.append({"task_id": tid, "correlation_id": corr, "parent_id": None, "dag_depth": 0, "capability": "code.backend", "agent_id": "a05-backend",
                      "state": state, "attempt": attempt, "depends_on": json.dumps((depends or {}).get(tid, [])), "created_at": T0 + i, "updated_at": T0 + 10 + i})
        transitions.append((tid, None, "CREATED", "a", "created", T0 + i))
        transitions.append((tid, "CREATED", state, "a", f"to {state}", T0 + 10 + i))
    return {"tasks": tasks, "transitions": transitions, "events": [], "now": T0 + 100}


@named
def empty_store():
    return {"tasks": [], "transitions": [], "events": [], "now": T0}


@named
def one_task_per_state():
    return _simple(STATES)


@named
def dependencies_met_and_unmet():
    spec = _simple(["DONE", "PLANNED", "PLANNED", "RETRY", "RETRY", "IN_REVIEW", "PLANNED"], depends={"S-1": ["S-0"], "S-2": ["S-3"], "S-3": ["S-4"], "S-4": ["S-5"],
                                                                                                        "S-6": ["S-0", "S-5", "ghost"]})
    return spec


@named
def stalled_running_rows():
    spec = _simple(["IN_PROGRESS"] * 5 + ["CLAIMED", "DONE"])
    for i, (age, kind) in enumerate([(5, "activity"), (500, "activity"), (30, "heartbeat"), (3000, "both"), (2, "none")]):
        tid = f"S-{i}"
        if kind in ("activity", "both"):
            spec["events"].append(_event("swarm.run.activity", T0 + 100 - age, "c1", tid, {"run_id": f"{tid}.a1", "tool": "bash", "text": "go", "tool_calls": 2}))
        if kind in ("heartbeat", "both"):
            spec["events"].append(_event("swarm.run.heartbeat", T0 + 100 - age, "c1", tid, {"run_id": f"{tid}.a1", "since_activity_s": 250.0}))
    spec["events"].sort(key=lambda r: r["ts"])
    return spec


@named
def timed_out_by_event():
    spec = _simple(["FAILED", "FAILED", "CANCELLED"])
    spec["events"].append(_event("swarm.run.finished", T0 + 50, "c1", "S-0", {"run_id": "S-0.a1", "state": "timed_out"}))
    return spec


@named
def more_than_max_runs_all_states():
    return make_spec(random.Random(11), 150)


@named
def more_than_max_runs_all_queued():
    return _simple(["PLANNED"] * 90)


@named
def exactly_max_runs():
    return _simple(["DONE", "IN_PROGRESS", "FAILED", "PLANNED"] * 16)


@named
def correlation_marker_fallback_and_two_correlations():
    return make_spec(random.Random(5), 12, other_corr=5)


@named
def created_at_and_depth_ties():
    return make_spec(random.Random(6), 40, ties=True)


@named
def attempts_zero_none_and_large():
    spec = _simple(["IN_PROGRESS", "FAILED", "DONE", "PLANNED"])
    for t, attempt in zip(spec["tasks"], (0, None, 7, 2)):
        t["attempt"] = attempt
    return spec


@named
def tasks_without_any_transition():
    spec = _simple(["PLANNED", "IN_PROGRESS", "FAILED", "DONE"])
    spec["transitions"] = [t for t in spec["transitions"] if t[0] not in ("S-1", "S-2")]
    spec["tasks"][2]["updated_at"] = None
    return spec


@named
def failed_with_long_and_secret_reasons():
    spec = _simple(["FAILED", "FAILED", "FAILED", "CANCELLED"])
    spec["transitions"] += [("S-0", "FAILED", "FAILED", "a", "x" * 400, T0 + 30), ("S-1", "FAILED", "FAILED", "a", f"token {SECRET} leaked", T0 + 31),
                            ("S-2", "FAILED", "FAILED", "a", "multi\nline\n\nreason", T0 + 32)]
    return spec


@named
def claimed_twice_and_never():
    spec = _simple(["IN_PROGRESS", "IN_PROGRESS", "DONE"])
    spec["transitions"] += [("S-0", "x", "CLAIMED", "a", "first", T0 + 20), ("S-0", "CLAIMED", "IN_PROGRESS", "a", "w", T0 + 21), ("S-0", "IN_PROGRESS", "CLAIMED", "a", "again", T0 + 25),
                            ("S-1", "x", "CLAIMED", "a", "once", T0 + 22)]
    return spec


@named
def activity_events_for_every_key_shape():
    spec = make_spec(random.Random(21), 30, stalls=True)
    spec["events"] += [_event("swarm.run.activity", spec["now"] - 2, "c1", "T-3", {"text": "task keyed"}),
                       _event("swarm.run.handover", spec["now"] - 2, "c1", "T-4", {"handover_id": "T-4.1"})]
    spec["events"].sort(key=lambda r: r["ts"])
    return spec


@pytest.mark.parametrize("name", sorted(NAMED))
def test_named_store_states_derive_and_refresh_byte_for_byte_like_the_frozen_code(mods, tmp_path, name):
    marker = "c1" if name == "correlation_marker_fallback_and_two_correlations" else None
    assert_same_everywhere(mods, tmp_path, NAMED[name](), marker=marker)


@pytest.mark.parametrize("seed", range(30))
def test_fuzzed_store_states_derive_and_refresh_byte_for_byte_like_the_frozen_code(mods, tmp_path, seed):
    rnd = random.Random(seed * 7919 + 13)
    spec = make_spec(rnd, rnd.choice([1, 2, 5, 13, 40, 70, 130]), ties=rnd.random() < 0.3, other_corr=rnd.choice([0, 0, 4]), events=rnd.random() < 0.7)
    assert_same_everywhere(mods, tmp_path, spec, marker=rnd.choice([None, None, "c1", "c2", "gone"]))


def test_a_store_that_does_not_exist_is_an_empty_idle_snapshot_for_both(mods, tmp_path):
    clock = mods.clock.FakeClock(start_wall=T0)
    want = encode(mods.old.derive_snapshot(db_path=tmp_path / "none" / "tasks.db", clock=clock))
    assert encode(mods.tel.derive_snapshot(db_path=tmp_path / "none" / "tasks.db", clock=clock)) == want
    assert json.loads(want)["swarm"]["state"] == "idle"


def test_handover_markers_are_derived_alike(mods, tmp_path, monkeypatch):
    from test_handover import base
    ho = importlib.import_module("swarm.handover")
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SWARM_DIR", str(state_dir))
    spec = _simple(["IN_PROGRESS", "IN_PROGRESS", "PLANNED"])
    spec["tasks"][0].update(task_id="T1-be", agent_id="a08-qa")  # runs on the agent the handover is addressed to: handover_in
    spec["tasks"][1].update(task_id="T2-fe", agent_id="a05-backend")  # runs on the agent that wrote it: only handover_out would apply
    spec["transitions"] = [({"S-0": "T1-be", "S-1": "T2-fe"}.get(t[0], t[0]),) + t[1:] for t in spec["transitions"]]
    build_dir(mods, state_dir, spec)
    ho.write_handover(base(correlation_id="c1", task_id="T1-be"), root=tmp_path, clock=mods.clock.FakeClock(), emit=False)
    ho.write_handover(base(correlation_id="c1", task_id="T2-fe", from_agent="A08", to_agent="A05"), root=tmp_path, clock=mods.clock.FakeClock(), emit=False)
    clock = mods.clock.FakeClock(start_wall=T0 + 100)
    new = mods.tel.derive_snapshot(db_path=state_dir / "tasks.db", clock=clock)
    assert encode(new) == encode(mods.old.derive_snapshot(db_path=state_dir / "tasks.db", clock=clock))
    assert {r["task_id"]: r["handover_in"] for r in new["runs"]} == {"T1-be": "T1-be.1", "T2-fe": "T2-fe.1", "S-2": None}


def test_build_snapshot_orders_counts_and_caps_like_the_frozen_one(mods):
    rnd = random.Random(99)
    states = ["waiting", "queued", "starting", "running", "handing_off", "completed", "failed", "cancelled", "timed_out"]
    for n in (0, 1, 9, 64, 65, 200):
        rows = [{"run_id": f"r{i}", "state": rnd.choice(states), "stalled": rnd.random() < 0.3} for i in range(n)]
        kw = dict(correlation_id="c1", seq=3, updated_wall=T0, elapsed_s=12.3456, max_parallel=4, effective_parallel=2, degraded=None)
        assert encode(mods.tel.build_snapshot(rows, **kw)) == encode(mods.old.build_snapshot(rows, **kw))
    with pytest.raises(ValueError):
        mods.tel.build_snapshot([{"run_id": "r", "state": "bogus", "stalled": False}], **kw)
    with pytest.raises(ValueError):
        mods.old.build_snapshot([{"run_id": "r", "state": "bogus", "stalled": False}], **kw)
    run_state = mods.tel.RunState
    assert encode(mods.tel.build_snapshot([{"run_id": "r", "state": run_state.RUNNING, "stalled": True}], **kw)) == \
        encode(mods.old.build_snapshot([{"run_id": "r", "state": run_state.RUNNING, "stalled": True}], **kw))


# ---- the contracts the optimisation had to keep ----------------------------------------------------------------------------------------
def _state_dir(mods, tmp_path, states=("IN_PROGRESS", "PLANNED", "DONE"), *, marker="c1"):
    sdir = tmp_path / ".swarm"  # SWARM_DIR of the `mods` fixture
    build_dir(mods, sdir, _simple(list(states)), marker=marker)
    return sdir


def _scheduler_doc(mods, *, pid, updated_wall, seq=7):
    doc = mods.tel.build_snapshot([], correlation_id="c1", seq=seq, updated_wall=updated_wall, elapsed_s=0.0, max_parallel=4, effective_parallel=4, degraded=None)
    doc.update(producer="scheduler", pid=pid, producer_updated=updated_wall)
    return doc


def _put_live(sdir, doc):
    """Replace live.json the way a producer does: a new file renamed over the old one."""
    tmp = sdir / "live.json.foreign.tmp"
    tmp.write_bytes(encode(doc))
    os.replace(tmp, sdir / "live.json")
    return (sdir / "live.json").read_bytes()


def _dead_pid():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_a_fresh_foreign_scheduler_document_is_never_replaced_even_with_our_own_document_remembered(mods, tmp_path):
    tel, sdir = mods.tel, _state_dir(mods, tmp_path)
    clock = mods.clock.FakeClock(start_wall=T0 + 100)
    assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is True  # our derived document is on disk and remembered
    text = _put_live(sdir, _scheduler_doc(mods, pid=os.getppid(), updated_wall=clock.wall()))  # the parent process is alive
    assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is False
    assert (sdir / "live.json").read_bytes() == text
    clock.advance(3 * tel.heartbeat_default() + 1)  # the producer went quiet: its document is stale now
    assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is True
    doc = json.loads((sdir / "live.json").read_text())
    assert doc["producer"] == "derived" and doc["seq"] == 8  # the scheduler's seq 7 + 1


@pytest.mark.parametrize("case", ["stale", "dead_pid", "no_producer_fields", "same_pid_no_owner"])
def test_a_producer_document_that_is_not_fresh_is_taken_over(mods, tmp_path, case):
    tel, sdir = mods.tel, _state_dir(mods, tmp_path)
    clock = mods.clock.FakeClock(start_wall=T0 + 100)
    doc = {"stale": lambda: _scheduler_doc(mods, pid=os.getppid(), updated_wall=clock.wall() - 3 * tel.heartbeat_default() - 5),
           "dead_pid": lambda: _scheduler_doc(mods, pid=_dead_pid(), updated_wall=clock.wall()),
           "no_producer_fields": lambda: {k: v for k, v in _scheduler_doc(mods, pid=1, updated_wall=clock.wall()).items() if k not in ("producer", "producer_updated", "pid")},
           "same_pid_no_owner": lambda: _scheduler_doc(mods, pid=os.getpid(), updated_wall=clock.wall())}[case]()
    _put_live(sdir, doc)
    assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is True
    written = json.loads((sdir / "live.json").read_text())
    assert written["producer"] == "derived" and written["seq"] == 8 and written["pid"] == os.getpid() and written["updated_wall"] == clock.wall()


def test_seq_counts_up_from_our_own_document_a_foreign_one_or_nothing(mods, tmp_path):
    tel, sdir = mods.tel, _state_dir(mods, tmp_path)
    clock = mods.clock.FakeClock(start_wall=T0 + 100)
    seqs = []
    for _ in range(3):
        assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is True
        seqs.append(json.loads((sdir / "live.json").read_text())["seq"])
    assert seqs == [1, 2, 3]
    foreign = tel.build_snapshot([], correlation_id="c1", seq=40, updated_wall=T0, elapsed_s=0.0, max_parallel=4, effective_parallel=4, degraded=None)
    foreign.update(producer="derived", pid=1)
    _put_live(sdir, foreign)  # another derived writer got in between: its seq is the one to continue from
    assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is True
    assert json.loads((sdir / "live.json").read_text())["seq"] == 41
    (sdir / "live.json").write_text("{ not json")  # garbage reads as no document
    assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is True
    assert json.loads((sdir / "live.json").read_text())["seq"] == 1
    (sdir / "live.json").unlink()
    assert tel.refresh_live(db_path=sdir / "tasks.db", clock=clock) is True
    assert json.loads((sdir / "live.json").read_text())["seq"] == 1


def test_a_busy_lock_skips_the_refresh_and_changes_nothing(mods, tmp_path, monkeypatch, caplog):
    tel, sdir = mods.tel, _state_dir(mods, tmp_path)
    assert tel.refresh_live(db_path=sdir / "tasks.db") is True
    before = (sdir / "live.json").read_bytes()
    real_lock = tel._live_lock
    monkeypatch.setattr(tel, "_live_lock", lambda target, timeout=2.0: real_lock(target, 0.05))  # the production wait is 2 s
    with open(sdir / "live.json.lock", "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with caplog.at_level("WARNING", logger="swarm.telemetry"):
            assert tel.refresh_live(db_path=sdir / "tasks.db") is False
        assert "lock busy" in caplog.text and (sdir / "live.json").read_bytes() == before
    assert tel.refresh_live(db_path=sdir / "tasks.db") is True
    assert json.loads((sdir / "live.json").read_text())["seq"] == 2


def test_the_flock_is_held_while_the_snapshot_is_derived_and_while_it_is_written(mods, tmp_path, monkeypatch):
    tel, sdir = mods.tel, _state_dir(mods, tmp_path)
    probes = []

    def locked_elsewhere():
        with open(sdir / "live.json.lock", "a") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(other, fcntl.LOCK_UN)
            return False

    real_derive, real_write = tel.derive_snapshot, tel._write_atomic
    monkeypatch.setattr(tel, "derive_snapshot", lambda *a, **k: (probes.append(("derive", locked_elsewhere())), real_derive(*a, **k))[1])
    monkeypatch.setattr(tel, "_write_atomic", lambda *a, **k: (probes.append(("write", locked_elsewhere())), real_write(*a, **k))[1])
    assert tel.refresh_live(db_path=sdir / "tasks.db") is True
    assert probes == [("derive", True), ("write", True)]
    assert not locked_elsewhere()  # released afterwards


def test_a_failing_derivation_or_connection_or_write_returns_false_and_logs(mods, tmp_path, monkeypatch, caplog):
    tel, sdir = mods.tel, _state_dir(mods, tmp_path)
    assert tel.refresh_live(db_path=sdir / "tasks.db") is True
    before = (sdir / "live.json").read_bytes()
    dead = sqlite3.connect(sdir / "tasks.db")
    dead.close()
    with caplog.at_level("WARNING", logger="swarm.telemetry"):
        assert tel.refresh_live(db_path=sdir / "tasks.db", conn=dead) is False  # a closed connection
        assert "refresh_live failed" in caplog.text
        caplog.clear()
        real_replace = os.replace
        monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("disk says no")))
        assert tel.refresh_live(db_path=sdir / "tasks.db") is False  # the rename fails
        assert "refresh_live failed" in caplog.text
        monkeypatch.setattr(os, "replace", real_replace)
        monkeypatch.setattr(tel, "derive_snapshot", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("derive broke")))
        assert tel.refresh_live(db_path=sdir / "tasks.db") is False
    assert (sdir / "live.json").read_bytes() == before  # a whole file, the previous one
    assert sorted(p.name for p in sdir.glob("*.tmp")) == []  # no temp file left behind
    monkeypatch.undo()
    assert mods.tel.refresh_live(db_path=sdir / "tasks.db") is True
    assert json.loads((sdir / "live.json").read_text())["seq"] == 2  # the failed attempts did not use up a seq


def test_write_atomic_returns_the_signature_of_the_file_it_leaves_and_survives_short_writes(mods, tmp_path, monkeypatch):
    tel = mods.tel
    target = tmp_path / "deep" / "er" / "live.json"  # the directory is created on demand
    doc = {"schema": tel.LIVE_SCHEMA, "text": "naïve — ünïcode ✓", "n": list(range(50))}
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, bytes(data)[:7]))  # the kernel may take less than asked
    sig = tel._write_atomic(target, doc)
    monkeypatch.undo()
    st = target.stat()
    assert sig == (st.st_ino, st.st_size, st.st_mtime_ns)
    assert target.read_bytes() == encode(doc) and list(target.parent.iterdir()) == [target]
    with pytest.raises(UnicodeEncodeError):  # an unencodable string fails before any file is created
        tel._write_atomic(target, {"bad": "\ud800"})
    assert target.read_bytes() == encode(doc) and list(target.parent.iterdir()) == [target]
    monkeypatch.setattr(os, "write", lambda fd, data: (_ for _ in ()).throw(OSError("full")))
    with pytest.raises(OSError):
        tel._write_atomic(target, doc)
    monkeypatch.undo()
    assert target.read_bytes() == encode(doc) and list(target.parent.iterdir()) == [target]


def test_our_own_document_is_remembered_as_valid_and_read_back_without_validating(mods, tmp_path, monkeypatch):
    tel, sdir = mods.tel, _state_dir(mods, tmp_path)
    assert tel.refresh_live(db_path=sdir / "tasks.db") is True
    st = (sdir / "live.json").stat()
    assert tel._LIVE_VALID[str(sdir / "live.json")] == (st.st_ino, st.st_size, st.st_mtime_ns)
    monkeypatch.setattr(tel, "_valid_live", lambda doc: (_ for _ in ()).throw(AssertionError("validated an unchanged file")))
    assert tel.read_live(tmp_path)["producer"] == "derived"
    _put_live(sdir, {"schema": tel.LIVE_SCHEMA})  # any other file is validated: the validator patched in above is reached
    with pytest.raises(AssertionError, match="validated an unchanged file"):
        tel.read_live(tmp_path)


def test_the_task_store_observer_hands_its_own_connection_to_the_refresh(mods, tmp_path, monkeypatch):
    tel = mods.tel
    store = mods.store_mod.TaskStore(root=tmp_path)
    seen = []
    real = tel.refresh_live
    monkeypatch.setattr(tel, "refresh_live", lambda *a, **k: (seen.append(k), real(*a, **k))[1])
    store.create(task_id="T-a", correlation_id="c1", capability="code.backend")
    store.transition("T-a", "VALIDATED", actor="t")
    store.transition("T-a", "PLANNED", actor="t")
    assert [k["conn"] is store.conn for k in seen] == [True, True] and all(k["db_path"] == store.path for k in seen)
    doc = tel.read_live(tmp_path)
    assert doc is not None and doc["seq"] == 2 and [r["state"] for r in doc["runs"]] == ["queued"]


# ---- the cost: counts and ratios, never a clock ---------------------------------------------------------------------------------------
def _queue(mods, tmp_path, n):
    sdir = tmp_path / f"q{n}"
    build_dir(mods, sdir, _simple(["PLANNED"] * n), marker="c1")  # no dependencies, no events: the usual in-session store
    return sdir


def _warm_refresh(mods, sdir, conn):
    clock = mods.clock.FakeClock(start_wall=T0 + 100)
    run = (lambda: mods.tel.refresh_live(db_path=sdir / "tasks.db", clock=clock, conn=conn)) if conn is not False else \
        (lambda: mods.old.refresh_live(db_path=sdir / "tasks.db", clock=clock))
    assert run() is True and run() is True  # the first refresh creates the document, the second is the steady state
    return run


class Ops:
    """What one call of `run` did: SQL statements on `conn`, connections opened, JSON decodes, redactions, file operations, Python calls."""

    def __init__(self, mods, run, conn, monkeypatch):
        tel = mods.tel
        self.statements, self.connects, self.decodes, self.files, self.calls = [], [], [], [], 0
        if conn not in (None, False):
            conn.set_trace_callback(self.statements.append)
        real_loads, real_connect = json.loads, sqlite3.connect
        monkeypatch.setattr(json, "loads", lambda *a, **k: (self.decodes.append(1), real_loads(*a, **k))[1])
        monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: (self.connects.append(1), real_connect(*a, **k))[1])
        for name in ("open", "replace", "write", "stat", "fstat", "unlink", "listdir"):
            real = getattr(os, name)
            monkeypatch.setattr(os, name, lambda *a, _n=name, _r=real, **k: (self.files.append(_n), _r(*a, **k))[1])
        real_read = Path.read_text
        monkeypatch.setattr(Path, "read_text", lambda p, *a, **k: (self.files.append(f"read_text:{p.name}"), real_read(p, *a, **k))[1])
        real_open = open
        monkeypatch.setattr("builtins.open", lambda f, *a, **k: (self.files.append(f"open:{Path(str(f)).name}"), real_open(f, *a, **k))[1])
        tel._clip_text.cache_clear()
        counter = [0]

        def prof(frame, event, arg):
            if event in ("call", "c_call"):
                counter[0] += 1

        sys.setprofile(prof)
        try:
            run()
        finally:
            sys.setprofile(None)
            monkeypatch.undo()
            if conn not in (None, False):
                conn.set_trace_callback(None)
        info = tel._clip_text.cache_info()
        self.redactions, self.calls = info.hits + info.misses, counter[0]


def _measure(mods, tmp_path, monkeypatch, n, *, own_conn=True):
    sdir = _queue(mods, tmp_path, n)
    conn = sqlite3.connect(sdir / "tasks.db") if own_conn else None
    try:
        run = _warm_refresh(mods, sdir, conn)
        return Ops(mods, run, conn, monkeypatch)
    finally:
        if conn is not None:
            conn.close()


def test_a_refresh_reads_through_the_store_connection_in_a_constant_number_of_statements(mods, tmp_path, monkeypatch):
    ops = {n: _measure(mods, tmp_path, monkeypatch, n) for n in (13, 60, 200)}
    assert {n: len(o.statements) for n, o in ops.items()} == {13: 2, 60: 2, 200: 2}  # the task rows and the folded transition history
    assert [len(o.connects) for o in ops.values()] == [0, 0, 0]


def test_without_a_connection_one_read_only_connection_is_opened_whatever_the_size(mods, tmp_path, monkeypatch):
    assert [len(_measure(mods, tmp_path, monkeypatch, n, own_conn=False).connects) for n in (13, 200)] == [1, 1]


def test_the_steady_state_refresh_decodes_no_json_and_touches_the_same_files_whatever_the_size(mods, tmp_path, monkeypatch):
    ops = {n: _measure(mods, tmp_path, monkeypatch, n) for n in (13, 60, 200)}
    assert [len(o.decodes) for o in ops.values()] == [0, 0, 0]  # not the previous document, not one depends_on per task
    assert ops[13].files == ops[60].files == ops[200].files
    files = ops[200].files
    assert files.count("replace") == 1 and "read_text:live.json" not in files and "open:live.json" not in files
    assert [f for f in files if f.startswith(("read_text", "open:"))] == ["open:live.json.lock", "read_text:latest_correlation"]
    assert len(files) <= 16  # the lock, the marker, one stat of the previous document, the temp file's create, write, fstat, close, rename


def test_redactions_stop_growing_at_the_listed_runs(mods, tmp_path, monkeypatch):
    """Only the MAX_RUNS runs that are listed get a row (agent, activity, last_tool, ...): task 65 and task 5000 cost the same."""
    counts = {n: _measure(mods, tmp_path, monkeypatch, n).redactions for n in (30, 64, 100, 200)}
    assert counts[100] == counts[200] == counts[64] and counts[30] < counts[64]


def test_python_calls_per_added_task_are_a_fraction_of_the_frozen_code(mods, tmp_path, monkeypatch):
    new = {n: _measure(mods, tmp_path, monkeypatch, n).calls for n in (100, 200)}
    old = {}
    for n in (100, 200):  # the frozen refresh, same stores, same measurement
        sdir = _queue(mods, tmp_path / "old", n)
        run = _warm_refresh(mods, sdir, False)
        old[n] = Ops(mods, run, False, monkeypatch).calls
    slope_new, slope_old = (new[200] - new[100]) / 100, (old[200] - old[100]) / 100
    assert slope_old > 30  # the yardstick is real: the frozen code pays that per task
    assert slope_new * 4 <= slope_old, (slope_new, slope_old)
    assert slope_new <= 16  # and the new code a small constant per task (the dependency lookup, the rank, the counter)
