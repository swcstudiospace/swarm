"""Cells of the `orch_status --transition` parity matrix (Phase 14.5, unit U3.2c).

One cell is one store state plus one `orch_status` argv. The cells come from the committed transition vectors
(`crates/gsd-route/tests/vectors/transition.json`, generated from the real `TaskStore` by `gen_transition_vectors.py`): every
(from, to) pair of the 14 x 14 matrix, the rework stash and cap, the `CLAIMED` attempt cap, gate tasks, state names, an unknown task,
corrupt rows. A few cells are added on top for what the vectors cannot say: `--dry-run`, a bad id, the ids of the run-log record, an
argv outside the grammar the binary serves, and a lease mirrored in `notes.lease` for each terminal state.

This module holds only data and pure helpers (cells, which cells Rust must refuse, the store dump, the masks, a loopback stand-in for
the substrate). The legs that run the cells are in `harness.py`, which imports nothing back from here. Standard library only.
"""
from __future__ import annotations

import http.server
import json
import re
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "transition.json"
Row = dict[str, Any]

STATES = ("CREATED", "VALIDATED", "PLANNED", "CLAIMED", "IN_PROGRESS", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "DONE", "FAILED",
          "BLOCKED", "RETRY", "ESCALATED", "CANCELLED")
I64_MAX = 9_223_372_036_854_775_807
MAX_DEPTH = 128  # the nesting the Rust JSON reader accepts (`gsd_route::parse_json`)
JSON_COLUMNS = ("inputs", "outputs", "acceptance", "budget", "depends_on")
GATES_BY_RISK = ("low", "medium", "high")  # the risk classes `GATES_BY_RISK` of swarm/taskstore.py names (its gates are in gate_facts.rs)

# The masks, each a clock reading that no two runs can share. Stdout, the run-log records and the database dump are masked at the same
# two keys; nothing else of the answer is.
CLOCK_KEYS = re.compile(r'(\\?"(?:updated_at|verdicts_since)\\?"\s*:\s*)-?[0-9][0-9.eE+-]*')
# `substrate_lease._mirror` stamps `notes.lease.at` with `time.time()` in the process that settles; only a cell with a lease is masked at it.
LEASE_AT = re.compile(r'(\\?"at\\?"\s*:\s*)-?[0-9][0-9.eE+-]*')
# live.json keys that are derived from the clock of the process that wrote it (`telemetry.derive_snapshot`), besides `seq` and `pid`.
LIVE_CLOCK_KEYS = frozenset({"updated_wall", "elapsed_s", "state_elapsed_s", "since_activity_s", "producer_updated"})
LIVE_DROPPED_KEYS = frozenset({"seq", "pid"})
RUNLOG_DROPPED_KEYS = frozenset({"ts", "msg_id"})
TABLES = ("tasks", "transitions", "verdicts", "artifacts")

SUBSTRATE_TOKEN = "tok-parity-a05-be"  # a fake token for the fake substrate; the real one is never read
SUBSTRATE_TOKEN_VAR = "SUBSTRATE_TOKEN_SWARM_A05_BE"  # `workspace.server_var("swarm-a05-be")`: where A05's own token is looked up
LEASE_GRAPH = "ut-mabc123-0123abcd"


@dataclass(frozen=True)
class LeaseScript:
    """What the fake substrate answers to the lease call of one cell."""

    complete: Mapping[str, Any]
    release: Mapping[str, Any]


SETTLES = LeaseScript(complete={"completed": True}, release={"released": True, "action": "released"})
REFUSES_RELEASE = LeaseScript(complete={"completed": True}, release={"released": False, "reason": "boom"})


@dataclass(frozen=True)
class Cell:
    """One parity cell: the store before the call, the arguments of the call, and what the vectors say about it."""

    cell_id: str
    group: str
    argv: tuple[str, ...]  # without `--root` and `--json`, which the legs append; `{root}` is the workspace
    row: Optional[Row]  # the task row to insert; None leaves the store empty
    fallback: Optional[str]  # why the binary must refuse with write_state not_started; None when it must answer
    expect: Optional[Row]  # the vector's recorded outcome, when it applies to the command line (manual, no injected approval facts)
    strict: bool  # the vector's actor and reason are the command line's (A01, not empty): audit rows and notes can be compared
    lease: Optional[LeaseScript] = None  # substrate on, answering this
    sample: bool = False  # also run the default selector (GSD_ENGINE unset) on this cell
    known: Optional[str] = None  # a key of KNOWN_DIVERGENCES: the Rust path is known to differ from Python on this cell, in that way only
    settle: Optional[str] = None  # the substrate tool a lease cell's settlement must call (`graph_release`, `graph_complete`); None: no call

    @property
    def task_id(self) -> Optional[str]:
        return None if self.row is None else str(self.row["task_id"])

    @property
    def masks(self) -> tuple[re.Pattern[str], ...]:
        return (CLOCK_KEYS, LEASE_AT) if self.lease is not None else (CLOCK_KEYS,)


def mask(text: str, *patterns: re.Pattern[str]) -> str:
    """`text` with the value of every key the patterns name replaced by `<clock>`."""
    for pattern in patterns:
        text = pattern.sub(r"\g<1><clock>", text)
    return text


# --- which cells Rust must refuse ---------------------------------------------------------------------------------------------------


def _json_readable(text: str) -> bool:
    """Whether `gsd_route::parse_json` reads `text`: JSON Python's `json` reads, with integers in i64, no NaN and at most 128 levels."""
    def reject(_name: str) -> object:
        raise ValueError("not a JSON number")

    def whole(number: str) -> int:
        value = int(number)
        if not -I64_MAX - 1 <= value <= I64_MAX:
            raise ValueError("integer beyond i64")
        return value

    depth = most = 0
    in_text = escaped = False
    for char in text:
        if in_text:
            escaped = not escaped and char == "\\"
            in_text = in_text and (escaped or char != '"')
        elif char == '"':
            in_text = True
        elif char in "[{":
            depth += 1
            most = max(most, depth)
        elif char in "]}":
            depth -= 1
    if most > MAX_DEPTH:
        return False
    try:
        json.loads(text, parse_int=whole, parse_constant=reject)
    except ValueError:
        return False
    return True


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def refusal(row: Optional[Row], to: str) -> Optional[str]:
    """Why the host route refuses `orch_status --transition <row's task> <to>` before the first write, or None when it answers.

    Mirrors `decide_transition` (`crates/gsd-route/src/transition_plan.rs`) and the route's own refusals, in the same order, for the
    command line (a manual request, A01, no key facts): the vectors' `rust_not_started` flags and `exception` outcomes agree with it on
    every manual cell (`cross_check` proves that), so a cell Rust refuses for another reason, or answers when it should refuse, fails.
    """
    if not to or to.startswith("-"):
        return "argv outside the grammar the binary serves (the state name is empty or starts with '-')"
    if row is None:
        return None  # Python's E-INPUT `unknown task`, which Rust prints as it is (the unknown task is decided before the row is read)
    for column in JSON_COLUMNS:
        text = row.get(column)
        if isinstance(text, str) and text and not _json_readable(text):
            return f"column {column} is not JSON Rust reads (Python raises while shaping the row)"
        if column == "depends_on" and isinstance(text, str) and text and not isinstance(json.loads(text), list):
            return "depends_on is not a list"
    notes_text = row.get("notes")
    if isinstance(notes_text, str) and notes_text and not _json_readable(notes_text):
        return "notes is JSON Rust cannot read (corrupt, an integer beyond i64 or nesting beyond 128)"
    notes: Any = json.loads(notes_text) if isinstance(notes_text, str) and notes_text else {}
    if to == "IN_REVIEW" and isinstance(notes, dict) and notes.get("gate"):
        return None  # a gate task: Python's E-POLICY, decided before the state names are looked at
    if to not in STATES:
        return "the target is not a task state: Python raises ValueError and prints a traceback"
    state = row["state"]
    if state not in STATES:
        return "the row's state is not a task state: Python raises ValueError and prints a traceback"
    legal = _legal()[state]
    if to not in legal:
        return None  # E-CONTRACT illegal transition
    if to == "CHANGES_REQUESTED" and not _is_int(row.get("rework_loops")):
        return "rework_loops is not an integer"
    if to == "CLAIMED" and state in ("PLANNED", "RETRY", "BLOCKED"):
        if not (_is_int(row.get("attempt")) and _is_int(row.get("max_attempts"))):
            return "attempt or max_attempts is not an integer"
        if row["attempt"] + 1 > row["max_attempts"]:
            return None  # E-TIMEOUT max_attempts exceeded
        if row["attempt"] + 1 > I64_MAX:
            return "the new attempt is beyond i64"
    if to == "APPROVED":
        return _approval_refusal(row, notes)
    return None


def _float_text_ok(text: str) -> bool:
    """Whether the gate facts read `text` as Python's `float()` does: ASCII, no underscore, and `float` accepts it."""
    try:
        float(text)
    except ValueError:
        return False
    return text.isascii() and "_" not in text


def _approval_refusal(row: Row, notes: Any) -> Optional[str]:
    """Why the host cannot read the gate facts of a legal APPROVED (`TaskFacts::from_row` and `approval::gather` of `gsd-taskwriter`
    refuse what Python may read differently), else None: the route answers, with the facts of the harness's key and no verdict row."""
    if not isinstance(row.get("task_id"), str) or not isinstance(row.get("correlation_id"), str):
        return "the task row has no text task_id or correlation_id"
    if row.get("risk_class") is not None and not isinstance(row.get("risk_class"), str):
        return "risk_class is not text"
    fields: dict[str, Any] = notes if isinstance(notes, dict) else {}
    gates = fields.get("gates")
    if gates is None:
        if row.get("risk_class") not in GATES_BY_RISK:
            return "the risk class is outside GATES_BY_RISK and notes.gates does not name the gates"
    elif not (isinstance(gates, list) and all(isinstance(gate, str) for gate in gates)):
        return "notes.gates is not a list of strings"
    since = fields.get("verdicts_since")
    if isinstance(since, str) and since and not _float_text_ok(since):
        return "notes.verdicts_since is text that float() may read in a way Rust does not reproduce"
    if isinstance(since, (list, dict)) and since:
        return "notes.verdicts_since is not a number"
    return None


_LEGAL: dict[str, list[str]] = {}


def _legal() -> dict[str, list[str]]:
    if not _LEGAL:
        _LEGAL.update(json.loads(VECTOR_PATH.read_text(encoding="utf-8"))["constants"]["legal"])
    return _LEGAL


def cross_check(cases: list[Row]) -> list[str]:
    """The manual vector cases the derived `refusal` disagrees with the vectors on. Empty means the two derivations agree."""
    problems: list[str] = []
    for case in cases:
        request = case["request"]
        if not request["manual"]:
            continue
        if request["to"] == "APPROVED" and case["task"] is not None and case["task"]["state"] == "IN_REVIEW":
            continue  # the vector carries injected key facts; the route reads the key and gate facts itself now (the signed cells prove it)
        flagged = bool(case["rust_not_started"]) or case["expect"]["outcome"] == "exception"
        derived = refusal(case["task"], request["to"]) is not None
        if derived != flagged:
            problems.append(f"{case['id']}: vectors say {'refused' if flagged else 'answered'}, the derivation says "
                            f"{refusal(case['task'], request['to']) or 'answered'}")
    return problems


# --- the cells ---------------------------------------------------------------------------------------------------------------------


def _argv(case: Row) -> tuple[str, ...]:
    request = case["request"]
    return ("--transition", request["task_id"], request["to"], "--reason", request["reason"])


def _vector_cells(cases: list[Row]) -> list[Cell]:
    cells: list[Cell] = []
    seen_groups: set[str] = set()
    for case in cases:
        request = case["request"]
        applies = bool(request["manual"]) and case["approval"] is None
        strict = request["actor"] == "A01" and request["reason"] != ""
        cells.append(Cell(f"vec-{case['id']}", case["group"], _argv(case), case["task"], refusal(case["task"], request["to"]),
                          case["expect"] if applies else None, strict, sample=case["group"] not in seen_groups))
        seen_groups.add(case["group"])
    return cells


def _held_row(base: Row, task_id: str, state: str, **notes: Any) -> Row:
    """`base` as a task in `state` holding a lease the runner mirrored in `notes.lease`."""
    lease = {"state": "held", "lease_id": "L-parity-1", "agent": "A05", "surface": "swarm-a05-be", "session_id": f"A05@r1:{LEASE_GRAPH}",
             "graph_id": LEASE_GRAPH, "ttl_s": 900, "at": 1.0}
    return {**base, "task_id": task_id, "title": task_id, "state": state, "notes": json.dumps({"lease": lease, **notes})}


def _extra_cells(base: Row) -> list[Cell]:
    """The cells the vectors cannot be: flags, ids, the grammar's edge, and leases."""
    def row(state: str, task_id: str) -> Row:
        return {**base, "task_id": task_id, "title": task_id, "state": state, "notes": None}

    def cell(cell_id: str, group: str, argv: tuple[str, ...], task: Optional[Row], fallback: Optional[str] = None,
             lease: Optional[LeaseScript] = None, sample: bool = True, known: Optional[str] = None,
             settle: Optional[str] = None) -> Cell:
        return Cell(f"extra-{cell_id}", group, argv, task, fallback, None, False, lease, sample, known, settle)

    created, review = row("CREATED", "T-x-created"), row("IN_REVIEW", "T-x-review")
    outside = "argv outside the grammar the binary serves"
    cells = [
        cell("dry-run-legal", "dry-run", ("--transition", "T-x-created", "CANCELLED", "--dry-run"), created),
        cell("dry-run-illegal", "dry-run", ("--transition", "T-x-created", "DONE", "--dry-run"), created),
        cell("dry-run-unknown-task", "dry-run", ("--transition", "T-x-gone", "CANCELLED", "--dry-run"), None),
        cell("dry-run-approved", "dry-run", ("--transition", "T-x-review", "APPROVED", "--dry-run"), review),
        cell("dry-run-bad-state", "dry-run", ("--transition", "T-x-created", "BOGUS", "--dry-run"), created),
        cell("bad-task-id", "ids", ("--transition", "T-x-created", "CANCELLED", "--task-id", "bad..id"), created),
        cell("bad-correlation-id", "ids", ("--transition", "T-x-created", "CANCELLED", "--correlation-id", "bad..id"), created),
        cell("ids-reach-the-run-log", "ids", ("--transition", "T-x-created", "CANCELLED", "--task-id", "T-ctx", "--correlation-id", "C-ctx"),
             created, known="D-2"),
        cell("no-reason-flag", "reason", ("--transition", "T-x-created", "CANCELLED"), created),
        cell("reason-inline-form", "reason", ("--transition", "T-x-created", "CANCELLED", "--reason=inline form, with = and spaces"), created),
        cell("reason-empty", "reason", ("--transition", "T-x-created", "CANCELLED", "--reason", ""), created),
        cell("empty-state-name", "grammar", ("--transition", "T-x-created", ""), created, outside),
        cell("advisory-flag", "grammar", ("--transition", "T-x-created", "CANCELLED", "--advisory"), created, outside),
        cell("reason-starts-with-dash", "grammar", ("--transition", "T-x-created", "CANCELLED", "--reason", "-dash"), created, outside),
        cell("repo-flag-beside-root", "grammar", ("--transition", "T-x-created", "CANCELLED", "--repo", "{root}"), created, outside),
        cell("lease-cancelled-releases", "lease", ("--transition", "T-x-lease-cancel", "CANCELLED", "--reason", "stop"),
             _held_row(base, "T-x-lease-cancel", "IN_PROGRESS"), lease=SETTLES, settle="graph_release"),
        cell("lease-failed-releases", "lease", ("--transition", "T-x-lease-fail", "FAILED", "--reason", "broke"),
             _held_row(base, "T-x-lease-fail", "IN_PROGRESS"), lease=SETTLES, settle="graph_release"),
        cell("lease-done-completes", "lease", ("--transition", "T-x-lease-done", "DONE", "--reason", "shipped"),
             _held_row(base, "T-x-lease-done", "APPROVED"), lease=SETTLES, settle="graph_complete"),
        cell("lease-rework-releases", "lease", ("--transition", "T-x-lease-rework", "CHANGES_REQUESTED", "--reason", "again"),
             _held_row(base, "T-x-lease-rework", "IN_REVIEW", result={"summary": "s"}), lease=SETTLES, settle="graph_release"),
        cell("lease-release-refused", "lease", ("--transition", "T-x-lease-refused", "CANCELLED", "--reason", "stop"),
             _held_row(base, "T-x-lease-refused", "IN_PROGRESS"), lease=REFUSES_RELEASE, settle="graph_release"),
        cell("lease-running-defers", "lease", ("--transition", "T-x-lease-running", "FAILED", "--reason", "runner owns it"),
             _held_row(base, "T-x-lease-running", "IN_PROGRESS", running={"run_id": "R-1"}), lease=SETTLES),
        cell("lease-kept-while-holding", "lease", ("--transition", "T-x-lease-kept", "IN_PROGRESS", "--reason", "go"),
             _held_row(base, "T-x-lease-kept", "CLAIMED"), lease=SETTLES),
        cell("lease-no-lease-on-task", "lease", ("--transition", "T-x-lease-none", "CANCELLED", "--reason", "stop"),
             row("IN_PROGRESS", "T-x-lease-none"), lease=SETTLES),
        cell("lease-illegal-settles-nothing", "lease", ("--transition", "T-x-lease-illegal", "DONE", "--reason", "no"),
             _held_row(base, "T-x-lease-illegal", "IN_PROGRESS"), lease=SETTLES),
    ]
    return cells


def load_cells() -> list[Cell]:
    """Every cell of the matrix, in a fixed order: the vector cells, then the added ones."""
    cases: list[Row] = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))["cases"]
    base = next(case["task"] for case in cases if case["id"] == "matrix-IN_PROGRESS-CANCELLED")
    return _vector_cells(cases) + _extra_cells(base)


def vector_cases() -> list[Row]:
    """The committed vector cases (for `cross_check`)."""
    return list(json.loads(VECTOR_PATH.read_text(encoding="utf-8"))["cases"])


# --- the store -------------------------------------------------------------------------------------------------------------------


def insert_row(db: Path, row: Row) -> None:
    """Inserts `row` into the `tasks` table of the store at `db`, as the vectors recorded it (every column, raw)."""
    conn = sqlite3.connect(db)
    try:
        names = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO tasks ({names}) VALUES ({marks})", list(row.values()))  # noqa: S608 - column names of a committed file
        conn.commit()
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> list[str]:
    found = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [table for table in TABLES if table in found]


def dump_store(db: Path, *patterns: re.Pattern[str]) -> str:
    """The `tasks`, `transitions`, `verdicts` and `artifacts` tables as sorted rows, one JSON document. `ts` and `updated_at` are the
    clock columns; `notes.verdicts_since` (and, for a lease cell, `notes.lease.at`) is masked in the text."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        dumped: dict[str, Any] = {}
        for table in _tables(conn):
            cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 - names from the constant TABLES
            names = [column[0] for column in cursor.description]
            rows = [{name: ("<clock>" if name in ("ts", "updated_at") else value) for name, value in zip(names, values)}
                    for values in cursor.fetchall()]
            dumped[table] = sorted(rows, key=lambda one: json.dumps(one, sort_keys=True, default=repr))
        return mask(json.dumps(dumped, sort_keys=True, ensure_ascii=False, indent=1, default=repr), *patterns)
    finally:
        conn.close()


def task_state(db: Path, task_id: str) -> tuple[Optional[Row], list[Row]]:
    """The task row and its audit rows (oldest first), or (None, [])."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        audit = conn.execute("SELECT from_state, to_state, actor, reason FROM transitions WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return (None if found is None else dict(found)), [dict(one) for one in audit]
    finally:
        conn.close()


# --- the effects -----------------------------------------------------------------------------------------------------------------


# The divergences between the Rust path and Python this matrix found and did not fix (Rust is not edited by this unit). A cell that names
# one passes only while its Rust-path answer differs from Python's in exactly that way (the scrub below removes just that difference) and
# FAILS the day the answers are equal, so a fix has to delete the entry.
KNOWN_DIVERGENCES = {
    "D-2": "with `--task-id X` the replayed `task.transition` record carries the target's task id (the manifest names it); Python's "
           "`Ctx.emit` gives the context's id X precedence over the payload's",
}


def scrub(known: Optional[str], value: Any) -> Any:
    """`value` without the one difference `known` names (the value when `known` is None)."""
    if known is None:
        return value
    if isinstance(value, dict):
        if known == "D-2" and value.get("type") == "task.transition":
            value = {key: item for key, item in value.items() if key != "task_id"}
        return {key: scrub(known, item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(known, item) for item in value]
    return value


def scrubbed_stdout(known: Optional[str], text: str) -> str:
    """A reply as text with `scrub` applied (the text itself when it is not one JSON document or nothing is scrubbed)."""
    if known is None:
        return text
    try:
        return json.dumps(scrub(known, json.loads(text)), indent=2)
    except ValueError:
        return text


def _drop(value: Any, dropped: frozenset[str], clocks: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {key: ("<clock>" if key in clocks else _drop(item, dropped, clocks)) for key, item in value.items() if key not in dropped}
    if isinstance(value, list):
        return [_drop(item, dropped, clocks) for item in value]
    return value


def effects_of(swarm_dir: Path, calls: list[Row], *patterns: re.Pattern[str], known: Optional[str] = None) -> str:
    """What one call left besides the store: the run-log records without `ts` and `msg_id`, `live.json` without `seq` and `pid` (and its
    clock-derived readings), the names of the files in the state directory, and the calls the fake substrate saw. With `known`, the
    difference that divergence names is scrubbed from the records."""
    log = swarm_dir / "events.jsonl"
    records = [scrub(known, _drop(json.loads(line), RUNLOG_DROPPED_KEYS, frozenset())) for line in log.read_text(encoding="utf-8").splitlines()
               if line.strip()] if log.exists() else []
    live_path = swarm_dir / "live.json"
    live = _drop(json.loads(live_path.read_text(encoding="utf-8")), LIVE_DROPPED_KEYS, LIVE_CLOCK_KEYS) if live_path.exists() else None
    names = sorted(path.name for path in swarm_dir.iterdir() if not path.name.startswith("tasks.db"))
    document = {"run_log": records, "live": live, "files": names, "substrate": calls}
    return mask(json.dumps(document, sort_keys=True, ensure_ascii=False, indent=1), *patterns)


class FakeSubstrate:
    """A loopback stand-in for the substrate's MCP endpoint: answers `graph_complete` and `graph_release` from a script and records every
    request. One per leg, so the legs' records are comparable. No other host is ever contacted."""

    def __init__(self, script: LeaseScript) -> None:
        self.script = script
        self.calls: list[Row] = []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - the http.server hook name
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    body = {}
                params = body.get("params") if isinstance(body, dict) else None
                tool = params.get("name") if isinstance(params, dict) else None
                arguments = params.get("arguments") if isinstance(params, dict) else None
                owner.calls.append({"path": self.path, "tool": tool, "arguments": arguments,
                                    "bearer_ok": self.headers.get("Authorization") == f"Bearer {SUBSTRATE_TOKEN}"})
                answers = {"graph_complete": owner.script.complete, "graph_release": owner.script.release}
                reply = {"jsonrpc": "2.0", "id": body.get("id") if isinstance(body, dict) else None,
                         "result": {"content": [{"type": "text", "text": json.dumps(answers.get(str(tool), {}))}]}}
                payload = json.dumps(reply).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the http.server hook signature
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeSubstrate:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def env(self) -> dict[str, str]:
        """The environment that turns the substrate on for a leg, with A05's own token (the agent the leases belong to)."""
        return {"SUBSTRATE_URL": f"http://127.0.0.1:{self._server.server_address[1]}", SUBSTRATE_TOKEN_VAR: SUBSTRATE_TOKEN}


# --- the signed cells (Phase 14.5, unit U3.4d) --------------------------------------------------------------------------------------
#
# A transition to APPROVED reads the signing keys and the verdict rows. These cells run the REAL Python `TaskStore.transition` (through the
# script) and the binary (through the selector and alone) on identical copies of a store whose verdict rows were signed by the REAL
# `swarm.gates.make_verdict` under a key profile of the cell. Keys are `secrets.token_hex(32)` made when a cell runs; they are in no file
# and in no argument, only in the environment of the processes of that cell. `harness.py` does not run them (its sandbox environment
# fixes one HMAC key); `test_write_parity.py` does.

SIGNING_ENV = ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_REQUIRE_KEY")
TARGET, ISSUER, CORRELATION = "T-signed", "T-gate", "C-signed"

# Key profiles: variable -> `@slot` (a secret made per cell and slot at run time) or a literal (`{root}` is a directory of the cell).
PROFILES: dict[str, Mapping[str, str]] = {
    "none": {},
    "hmac": {"SWARM_SIGNING_KEY": "@hmac"},
    "hmac-other": {"SWARM_SIGNING_KEY": "@other"},
    "ed25519": {"SWARM_ED25519_KEY": "@ed"},
    "both": {"SWARM_SIGNING_KEY": "@hmac", "SWARM_ED25519_KEY": "@ed"},
    "dev": {"SWARM_ALLOW_INSECURE_DEV_KEY": "1"},
    "require": {"SWARM_REQUIRE_KEY": "1"},
    "require-hmac": {"SWARM_REQUIRE_KEY": "1", "SWARM_SIGNING_KEY": "@hmac"},
    "dev-require": {"SWARM_ALLOW_INSECURE_DEV_KEY": "1", "SWARM_REQUIRE_KEY": "1"},
    "ed25519-unloadable": {"SWARM_ED25519_KEY": "zz"},
    "ed25519-unloadable-hmac": {"SWARM_ED25519_KEY": "zz", "SWARM_SIGNING_KEY": "@hmac"},
    "key-file": {"SWARM_ED25519_KEY_FILE": "{root}/no-such-key.pem"},
    # a conventional key file (`$XDG_CONFIG_HOME/agent-swarm/signing-key.hex`, run-time random content) beside the dev opt-in: Python loads it
    "dev-xdg-key-file": {"SWARM_ALLOW_INSECURE_DEV_KEY": "1", "XDG_CONFIG_HOME": "{root}/xdg"},
}


@dataclass(frozen=True)
class Verdict:
    """One verdict row of a scenario. Times are seconds after the store was built."""

    gate: str
    verdict: str = "pass"
    issued: float = 0.0  # the signed `issued_at`
    expires_s: int = 86400
    correlation: str = CORRELATION
    row_ts: float = 0.0  # `verdicts.ts`
    extra: Mapping[str, Any] = field(default_factory=dict)  # more payload (`dry_run`, `gate_task`, `waived_by`)


REVIEW, QUALITY = Verdict("review"), Verdict("quality")


@dataclass(frozen=True)
class Scenario:
    """The store of a signed cell: a medium-risk task in IN_REVIEW (gates review and quality) and the rows recorded on it, in order."""

    verdicts: tuple[Verdict, ...] = (REVIEW, QUALITY)
    notes: Mapping[str, Any] = field(default_factory=dict)
    since: Optional[float] = None  # `notes.verdicts_since`
    issuer_state: Optional[str] = None  # the state of the gate task ISSUER; None leaves no such task
    after: tuple[str, ...] = ()  # edits to the stored rows: `tamper-first-signature`, `unsign-first`


def _issued_by(gate_task: str) -> Mapping[str, Any]:
    return {"gate_task": gate_task}


SCENARIOS: dict[str, Scenario] = {
    "pass": Scenario(),
    "quality-fails": Scenario((REVIEW, Verdict("quality", "fail"))),
    "quality-missing": Scenario((REVIEW,)),
    "quality-expired": Scenario((REVIEW, Verdict("quality", issued=-1000.0, expires_s=60))),
    "issuer-failed": Scenario((Verdict("review", extra=_issued_by(ISSUER)), Verdict("quality", extra=_issued_by(ISSUER))), issuer_state="FAILED"),
    "issuer-accepted": Scenario((Verdict("review", extra=_issued_by(ISSUER)), Verdict("quality", extra=_issued_by(ISSUER))),
                                issuer_state="IN_REVIEW"),
    "issuer-unknown": Scenario((Verdict("review", extra=_issued_by("T-nope")), QUALITY)),
    "wrong-correlation": Scenario((Verdict("review", correlation="C-other"), QUALITY)),
    "rows-before-since": Scenario(since=3600.0),
    "signed-before-since": Scenario((Verdict("review", issued=-100.0), Verdict("quality", issued=-100.0)), since=-50.0),
    "tampered-signature": Scenario(after=("tamper-first-signature",)),
    "unsigned-row": Scenario(after=("unsign-first",)),
    "dry-run-rows": Scenario((Verdict("review", extra={"dry_run": True}), Verdict("quality", extra={"dry_run": True}))),
    "dry-run-task": Scenario((Verdict("review", extra={"dry_run": True}), QUALITY), notes={"dry_run": True}),
    "no-gates": Scenario((), notes={"gates": []}),
    "gates-override": Scenario((Verdict("security"),), notes={"gates": ["security"]}),
    "latest-pass-wins": Scenario((Verdict("review", "fail"), REVIEW, QUALITY)),
    "latest-fail-wins": Scenario((REVIEW, Verdict("review", "fail"), QUALITY)),
    "waived": Scenario((Verdict("review", "waive", extra={"waived_by": "human"}), QUALITY)),
    # Python's `_latest_rows` runs `json.loads(findings)` on every row of the task with `ts >= since`, whatever its gate
    "findings-invalid-required": Scenario(after=("corrupt-findings",)),
    "findings-null-required": Scenario(after=("null-findings",)),
    "findings-invalid-other-gate": Scenario(after=("extra-row-other-gate-corrupt",)),
    "findings-invalid-null-gate": Scenario(after=("extra-row-null-gate-corrupt",)),
    "findings-valid-other-gate": Scenario(after=("extra-row-other-gate-valid",)),
    # a corrupt row of a required gate that Python's query never returns (older than `verdicts_since`) shadows nothing
    "findings-invalid-before-since": Scenario(since=-500.0, after=("extra-row-before-since-corrupt",)),
}


@dataclass(frozen=True)
class SignedCell:
    """One signed cell: the rows are signed under the `sign` profile, the call runs under the `call` profile."""

    cell_id: str
    scenario: str
    sign: str
    call: str
    message: Optional[str]  # the E-POLICY text Python prints; None when Python approves (the task ends APPROVED)
    fallback: Optional[str] = None  # why the binary must refuse with write_state not_started (Python answers); None when it answers
    code: str = "E-POLICY"  # the error code Python prints for a refusal
    key_file: bool = False  # the call profile names `$XDG_CONFIG_HOME`: a conventional key file with run-time content is made there

    @property
    def masks(self) -> tuple[re.Pattern[str], ...]:
        return (CLOCK_KEYS,)


def _approves(cell_id: str, scenario: str, sign: str, call: str) -> SignedCell:
    return SignedCell(cell_id, scenario, sign, call, None)


NO_KEY = "fail-closed: no signing key configured"
REQUIRE = "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"
BOTH_GATES = "lacks passing gates ['review', 'quality']"
QUALITY_GATE = "lacks passing gates ['quality']"
REVIEW_GATE = "lacks passing gates ['review']"
FINDINGS_INVALID = "JSONDecodeError"
FINDINGS_NULL = "TypeError"
FINDINGS_FALLBACK = "a findings column Python's json.loads cannot read: Python's own refusal stands"
SIGNED_CELLS: tuple[SignedCell, ...] = (
    _approves("pass-hmac", "pass", "hmac", "hmac"),
    _approves("pass-ed25519", "pass", "ed25519", "ed25519"),
    _approves("pass-both-keys", "pass", "both", "both"),
    _approves("pass-hmac-signed-under-both-keys", "pass", "hmac", "both"),
    _approves("pass-ed25519-signed-under-both-keys", "pass", "ed25519", "both"),
    _approves("pass-dev-key-opt-in", "pass", "dev", "dev"),
    _approves("pass-require-key-with-hmac", "pass", "hmac", "require-hmac"),
    _approves("pass-hmac-beside-an-unloadable-ed25519-seed", "pass", "hmac", "ed25519-unloadable-hmac"),
    _approves("pass-no-gates-required", "no-gates", "hmac", "hmac"),
    _approves("pass-gates-override", "gates-override", "hmac", "hmac"),
    _approves("pass-latest-row-wins", "latest-pass-wins", "hmac", "hmac"),
    _approves("pass-waived", "waived", "hmac", "hmac"),
    _approves("pass-issuer-accepted", "issuer-accepted", "hmac", "hmac"),
    _approves("pass-dry-run-row-on-a-dry-run-task", "dry-run-task", "hmac", "hmac"),
    SignedCell("refused-failing-verdict", "quality-fails", "hmac", "hmac", QUALITY_GATE),
    SignedCell("refused-missing-verdict", "quality-missing", "hmac", "hmac", QUALITY_GATE),
    SignedCell("refused-expired-verdict", "quality-expired", "hmac", "hmac", QUALITY_GATE),
    SignedCell("refused-unaccepted-gate-task", "issuer-failed", "hmac", "hmac", "has unaccepted gates ['quality', 'review']"),
    SignedCell("refused-unknown-gate-task", "issuer-unknown", "hmac", "hmac", "has unaccepted gates ['review']"),
    SignedCell("refused-wrong-correlation", "wrong-correlation", "hmac", "hmac", REVIEW_GATE),
    SignedCell("refused-rows-before-verdicts-since", "rows-before-since", "hmac", "hmac", BOTH_GATES),
    SignedCell("refused-signed-before-verdicts-since", "signed-before-since", "hmac", "hmac", BOTH_GATES),
    SignedCell("refused-tampered-signature", "tampered-signature", "hmac", "hmac", REVIEW_GATE),
    SignedCell("refused-tampered-ed25519-signature", "tampered-signature", "ed25519", "ed25519", REVIEW_GATE),
    SignedCell("refused-unsigned-row", "unsigned-row", "hmac", "hmac", REVIEW_GATE),
    SignedCell("refused-dry-run-rows", "dry-run-rows", "hmac", "hmac", BOTH_GATES),
    SignedCell("refused-latest-row-fails", "latest-fail-wins", "hmac", "hmac", REVIEW_GATE),
    SignedCell("refused-other-hmac-key", "pass", "hmac", "hmac-other", BOTH_GATES),
    SignedCell("refused-hmac-rows-under-ed25519-only", "pass", "hmac", "ed25519", BOTH_GATES),
    SignedCell("refused-ed25519-rows-under-hmac-only", "pass", "ed25519", "hmac", BOTH_GATES),
    SignedCell("refused-real-key-rows-under-dev-key", "pass", "hmac", "dev", BOTH_GATES),
    SignedCell("refused-no-key", "pass", "hmac", "none", NO_KEY),
    SignedCell("refused-require-key-without-a-key", "pass", "hmac", "require", REQUIRE),
    SignedCell("refused-require-key-beats-the-dev-key", "pass", "dev", "dev-require", REQUIRE),
    SignedCell("refused-unloadable-ed25519-seed", "pass", "hmac", "ed25519-unloadable", NO_KEY),
    SignedCell("key-file-reference-goes-to-python", "pass", "hmac", "key-file", NO_KEY,
               "SWARM_ED25519_KEY_FILE names a key file: only Python opens key files"),
    # F-3: the parent has a conventional key file and the dev opt-in, the rows are dev-signed. Python loads the key (the dev key is off), so
    # the rows are bad-sig and it refuses. The binary must see XDG_CONFIG_HOME to know the file exists; without it, it would approve.
    SignedCell("refused-dev-rows-beside-a-conventional-key-file", "pass", "dev", "dev-xdg-key-file", BOTH_GATES,
               "a conventional key file exists: only Python opens key files", key_file=True),
    # F-2: a `findings` column Python's `json.loads` cannot read refuses the transition, with intact signed envelopes
    SignedCell("refused-invalid-findings-on-a-required-row", "findings-invalid-required", "hmac", "hmac", FINDINGS_INVALID,
               FINDINGS_FALLBACK, code="E-INPUT"),
    SignedCell("refused-null-findings-on-a-required-row", "findings-null-required", "hmac", "hmac", FINDINGS_NULL, FINDINGS_FALLBACK,
               code="E-INTERNAL"),
    SignedCell("refused-invalid-findings-on-a-gate-that-is-not-required", "findings-invalid-other-gate", "hmac", "hmac", FINDINGS_INVALID,
               FINDINGS_FALLBACK, code="E-INPUT"),
    SignedCell("refused-invalid-findings-on-a-row-with-a-null-gate", "findings-invalid-null-gate", "hmac", "hmac", FINDINGS_INVALID,
               FINDINGS_FALLBACK, code="E-INPUT"),
    _approves("pass-valid-findings-on-a-gate-that-is-not-required", "findings-valid-other-gate", "hmac", "hmac"),
    _approves("pass-invalid-findings-on-a-row-before-verdicts-since", "findings-invalid-before-since", "hmac", "hmac"),
)

# The script that builds a signed cell's store, run as `python -c` from the repository root with the SIGN profile in its environment.
# It reads the scenario as JSON on stdin and prints nothing.
SEED_SCRIPT = """
import json, sys, time
from swarm.gates import make_verdict
from swarm.taskstore import TaskStore

spec = json.load(sys.stdin)
base = time.time()
store = TaskStore(spec["db"])
notes = dict(spec["notes"])
if spec["since"] is not None:
    notes["verdicts_since"] = base + spec["since"]
store.create(task_id=spec["target"], correlation_id=spec["correlation"], capability="code.backend", risk_class="medium", notes=notes)
store.conn.execute("UPDATE tasks SET state='IN_REVIEW' WHERE task_id=?", (spec["target"],))
if spec["issuer_state"] is not None:
    store.create(task_id=spec["issuer"], correlation_id=spec["correlation"], capability="qa.gate")
    store.conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (spec["issuer_state"], spec["issuer"]))
store.conn.commit()
for item in spec["verdicts"]:
    extra = dict(item["extra"])
    extra["issued_at"] = base + item["issued"]
    envelope = make_verdict(gate=item["gate"], task_id=spec["target"], agent_id="A09", verdict=item["verdict"],
                            expires_s=item["expires_s"], correlation_id=item["correlation"], extra=extra, root=spec["scratch"])
    store.record_verdict(spec["target"], envelope)
    store.conn.execute("UPDATE verdicts SET ts=? WHERE id=(SELECT max(id) FROM verdicts)", (base + item["row_ts"],))
store.conn.commit()
# step -> (gate, findings, ts offset): rows with no envelope that only `_latest_rows` can trip over (a NULL gate is a NULL column)
EXTRA_ROWS = {
    "extra-row-other-gate-corrupt": ("security", "{oops", 1.0),
    "extra-row-null-gate-corrupt": (None, "{oops", 1.0),
    "extra-row-other-gate-valid": ("security", "[]", 1.0),
    "extra-row-before-since-corrupt": ("review", "{oops", -1000.0),
}
for step in spec["after"]:
    first = store.conn.execute("SELECT id, envelope_json FROM verdicts ORDER BY id LIMIT 1").fetchone()
    if step == "tamper-first-signature":
        envelope = json.loads(first["envelope_json"])
        chars = list(envelope["sig"])
        chars[20] = "A" if chars[20] != "A" else "B"
        envelope["sig"] = "".join(chars)
        store.conn.execute("UPDATE verdicts SET envelope_json=?, sig=? WHERE id=?", (json.dumps(envelope), envelope["sig"], first["id"]))
    elif step == "unsign-first":
        store.conn.execute("UPDATE verdicts SET envelope_json=NULL, sig=NULL WHERE id=?", (first["id"],))
    elif step == "corrupt-findings":
        store.conn.execute("UPDATE verdicts SET findings='{oops' WHERE id=?", (first["id"],))
    elif step == "null-findings":
        store.conn.execute("UPDATE verdicts SET findings=NULL WHERE id=?", (first["id"],))
    elif step in EXTRA_ROWS:
        gate, findings, offset = EXTRA_ROWS[step]
        store.conn.execute("INSERT INTO verdicts (task_id, gate, verdict, agent_id, findings, expires_at, ts, envelope_json, sig)"
                           " VALUES (?,?,?,?,?,?,?,NULL,NULL)", (spec["target"], gate, "pass", "A09", findings, base + 86400, base + offset))
    else:
        raise SystemExit("unknown step " + step)
store.conn.commit()
store.conn.close()
"""


def seed_spec(scenario: Scenario, db: Path, scratch: Path) -> dict[str, Any]:
    """The JSON `SEED_SCRIPT` reads for `scenario`."""
    return {"db": str(db), "scratch": str(scratch), "target": TARGET, "issuer": ISSUER, "correlation": CORRELATION,
            "notes": dict(scenario.notes), "since": scenario.since, "issuer_state": scenario.issuer_state, "after": list(scenario.after),
            "verdicts": [{"gate": item.gate, "verdict": item.verdict, "issued": item.issued, "expires_s": item.expires_s,
                          "correlation": item.correlation, "row_ts": item.row_ts, "extra": dict(item.extra)} for item in scenario.verdicts]}


def profile_env(name: str, secrets: Mapping[str, str], root: Path) -> dict[str, str]:
    """The key variables of profile `name` with its secrets filled in. `SWARM_SIGNING_KEY` is always set (empty when the profile has no
    HMAC key) so the harness sandbox's own key never leaks into a cell."""
    env = {"SWARM_SIGNING_KEY": ""}
    for variable, value in PROFILES[name].items():
        env[variable] = secrets[value[1:]] if value.startswith("@") else value.replace("{root}", str(root))
    return env


def write_conventional_key(config_home: Path, seed_hex: str) -> None:
    """Makes `<config_home>/agent-swarm/signing-key.hex` the way Python's `_private_seed` accepts it (owner-only directory and file, 64 hex
    characters): the file holds `seed_hex`, which a cell makes at run time and never writes anywhere else."""
    folder = config_home / "agent-swarm"
    folder.mkdir(parents=True, mode=0o700)
    folder.chmod(0o700)
    path = folder / "signing-key.hex"
    path.write_text(seed_hex + "\n", encoding="ascii")
    path.chmod(0o600)
