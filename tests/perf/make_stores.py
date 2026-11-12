#!/usr/bin/env python3
"""Builds the three Task Stores the A2 engine legs read (tests/perf/run.sh, A2_ENGINE_LEGS=1).

  python3 tests/perf/make_stores.py [--out DIR] [--key-from-env] [--replace]

Each store is a SWARM_DIR holding one `tasks.db`, created through the product's TaskStore (swarm.taskstore) so the schema
is the product's. Every id and timestamp is fixed, so two runs build stores whose rows are equal except for the signed
verdicts' `envelope_json` and `sig` columns, which depend on the signing key.

  empty         no rows                                   workload: orch_status --json
  snapshot500   500 tasks, the shape and counts of `snapshot()` in crates/gsd-sandbox/tests/read_route_budget.rs:
                a binary-tree DAG (task i depends on task (i - 1) // 2), every third task IN_REVIEW with one unsigned
                passing `review` verdict and the rest DONE                              workload: orch_status --json
  signed        50 IN_REVIEW tasks, each with one signed `review` verdict (the history path; the Rust engine refuses
                signed rows with E-DEP and the selector then falls back to Python: the D2 fallback)
                                                            workload: orch_status --history T-S-0000 --json

Signing uses a throwaway HMAC key made at run time with `secrets.token_hex`. It is held in this process only: never
written to disk, never printed. With --key-from-env the key is the one already in SWARM_SIGNING_KEY instead (run.sh makes
a throwaway one for the whole run so the timed Python answers can verify the rows); the value is still never printed.

Stdout is one JSON object: the output directory and, per store, its `swarm_dir`, row counts and `argv` (the workload).
Everything is written under --out (default: a fresh directory under the system temp dir); a store directory that already
exists is refused unless --replace, and a path that resolves outside --out is refused.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from swarm.envelope import sign_envelope  # noqa: E402
from swarm.gates import make_verdict  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402

CORRELATION = "C-A2"
FIXED_TS = 1_790_000_000.0  # 2026-09-21 UTC; every created_at, updated_at, transition ts and verdict ts is this plus an offset
SNAPSHOT_TASKS = 500  # TASKS in read_route_budget.rs
SIGNED_TASKS = 50
HISTORY_TASK = "T-S-0000"
VERDICT_LIFETIME_S = 10**9  # a signed verdict stays unexpired until 2057 whatever day the run is
# signing-key variables that would take precedence over (or alter the use of) the throwaway HMAC key
KEY_VARIABLES = ("SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION")


def counts(db: Path) -> dict[str, int]:
    """Row counts of one store, read back from the file."""
    with closing(sqlite3.connect(db)) as conn:
        return {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
            "transitions": conn.execute("SELECT COUNT(*) FROM transitions").fetchone()[0],
            "verdicts": conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0],
            "signed_verdicts": conn.execute("SELECT COUNT(*) FROM verdicts WHERE envelope_json IS NOT NULL").fetchone()[0],
        }


def fix_timestamps(store: TaskStore) -> None:
    """Replaces the wall-clock values the TaskStore wrote with fixed ones (row order is insertion order)."""
    with store.transaction():
        for index, (task_id,) in enumerate(store.conn.execute("SELECT task_id FROM tasks ORDER BY rowid").fetchall()):
            store.conn.execute("UPDATE tasks SET created_at=?, updated_at=? WHERE task_id=?",
                               (FIXED_TS + index, FIXED_TS + index, task_id))
        store.conn.execute("UPDATE transitions SET ts = ? + id", (FIXED_TS,))
        store.conn.execute("UPDATE verdicts SET ts = ? + id, expires_at = ? + id + ? WHERE envelope_json IS NOT NULL",
                           (FIXED_TS, FIXED_TS, VERDICT_LIFETIME_S))


def build_snapshot(path: Path) -> TaskStore:
    store = TaskStore(path)
    with store.transaction():
        for index in range(SNAPSHOT_TASKS):
            task_id = f"T-{index:04d}"
            in_review = index % 3 == 0
            store.create(task_id=task_id, correlation_id=CORRELATION, capability="code.backend", title=f"a2 snapshot task {index}",
                         dag_depth=(index + 1).bit_length() - 1, risk_class="low", agent_id="A05",
                         depends_on=[] if index == 0 else [f"T-{(index - 1) // 2:04d}"])
            store.update(task_id, state="IN_REVIEW" if in_review else "DONE")
            if in_review:
                # an unsigned legacy-shaped row, as in the Rust snapshot: id index + 1, gate review, pass, no envelope
                store.conn.execute("INSERT INTO verdicts (id, task_id, gate, verdict, findings, ts) VALUES (?,?,?,?,?,?)",
                                   (index + 1, task_id, "review", "pass", "[]", 1.5))
    fix_timestamps(store)  # leaves the unsigned verdict rows at ts 1.5, as in the Rust snapshot
    return store


def build_signed(path: Path) -> TaskStore:
    store = TaskStore(path)
    with store.transaction():
        for index in range(SIGNED_TASKS):
            task_id = f"T-S-{index:04d}"
            store.create(task_id=task_id, correlation_id=CORRELATION, capability="code.backend", title=f"a2 signed task {index}",
                         risk_class="low", agent_id="A05")
            store.update(task_id, state="IN_REVIEW")
            envelope = make_verdict(gate="review", task_id=task_id, agent_id="A09", correlation_id=CORRELATION,
                                    expires_s=VERDICT_LIFETIME_S, audit=False)
            envelope["msg_id"] = f"a2-msg-{index:04d}"
            envelope["trace_id"] = f"a2-trace-{index:04d}"
            envelope["ts"] = "2026-09-21T00:00:00Z"
            envelope["payload"]["issued_at"] = FIXED_TS + index  # type: ignore[index]
            sign_envelope(envelope, audit=False)
            store.record_verdict(task_id, envelope)
        # a transition trail for the history task (the TaskStore's own transition() also refreshes live.json)
        trail = ["CREATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW"]
        for before, after in zip(trail, trail[1:]):
            store.conn.execute("INSERT INTO transitions (task_id, from_state, to_state, actor, reason, ts) VALUES (?,?,?,?,?,?)",
                               (HISTORY_TASK, before, after, "A01", "a2 fixture", 0.0))
    fix_timestamps(store)
    return store


STORES = {
    "empty": (TaskStore, ["--json"]),
    "snapshot500": (build_snapshot, ["--json"]),
    "signed": (build_signed, ["--history", HISTORY_TASK, "--json"]),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", help="directory the stores are created in (default: a fresh directory under the system temp dir)")
    parser.add_argument("--key-from-env", action="store_true", help="sign with the SWARM_SIGNING_KEY already in the environment")
    parser.add_argument("--replace", action="store_true", help="replace store directories that already exist under --out")
    args = parser.parse_args()

    out = Path(args.out).resolve() if args.out else Path(tempfile.mkdtemp(prefix="a2-stores-")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    targets = {name: (out / name) for name in STORES}
    for name, target in targets.items():
        if not target.resolve().is_relative_to(out) or target.resolve().parent != out:
            print(f"error: {name} resolves outside {out}; refusing to write it", file=sys.stderr)
            return 1
        if os.path.lexists(target):
            if not args.replace:
                print(f"error: {target} already exists (use a new --out or --replace)", file=sys.stderr)
                return 1
            if target.is_symlink() or not target.is_dir():
                print(f"error: {target} is not a plain directory; refusing to replace it", file=sys.stderr)
                return 1

    for variable in KEY_VARIABLES:
        os.environ.pop(variable, None)
    if args.key_from_env:
        if not os.environ.get("SWARM_SIGNING_KEY"):
            print("error: --key-from-env needs SWARM_SIGNING_KEY set", file=sys.stderr)
            return 1
    else:
        os.environ["SWARM_SIGNING_KEY"] = secrets.token_hex(32)

    report: dict[str, object] = {"out": str(out), "stores": {}}
    for name, (build, argv) in STORES.items():
        if os.path.lexists(targets[name]):
            shutil.rmtree(targets[name])
        db = targets[name] / "tasks.db"
        store = build(db)
        store.conn.close()
        report["stores"][name] = {"swarm_dir": str(targets[name]), "argv": argv, **counts(db)}  # type: ignore[index]
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
