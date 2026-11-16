#!/usr/bin/env python3
"""Vectors for the pure Rust plan derivation (`crates/gsd-route/src/plan_derive.rs`), generated from the REAL `scripts/orch_plan.py`.

Every case runs `AgentScript("A01", "orch_plan", ...).main(argv)`, the real entry point of `orch_plan.py` (argv parsing, `run()`, the printed
reply), over an in-memory `TaskStore` seeded through the real API (earlier plans are created by `orch_plan` itself). The case records:

* `request`: the arguments the derivation reads, taken from the namespace the real argparse parser produced; files named by `--brief`, `--plan`
  and `--slices` are read here, as the host will read them (`{"text": ...}`, or `{"unreadable": true}`);
* `facts`: the host's snapshot: the candidate-prefix rows, the rows of every involved correlation (`ORDER BY dag_depth, created_at`), the plan
  files, the signing state, the uuid the run draws (pinned) and the roster (top level `agents`);
* `expect`: what Python did: `stdout` and `exit` (reply bytes), the tasks and `transitions` rows it wrote, the plan file and
  `latest_correlation` it published, the events it emitted (`plan.updated`), the call of `_bind_graph`, the `build_envelope` call of the
  controller kickoff, and the error of a refusal. `rust` is `plan` (the derivation must reproduce all of it) or `not_started` (it must refuse
  before any write; `why` names the decline class and the module's documentation lists it).

Units under `units`: the tables the derivation's Python-compatibility helpers are checked against (`str.isspace`, `repr`, `==`, `sha256`, the
`.rN` regular expression, the blast-radii fence, the Graph ID pattern).

Determinism: the clocks of `swarm.taskstore`, `swarm.gates` and `swarm.envelope` are one fake clock that stands still at `NOW` during a call,
except that `TaskStore.create` moves it to `NOW + i * CREATED_AT_STEP` for the `i`-th task (the stamp the derivation gives each new task), so
`ORDER BY dag_depth, created_at` is deterministic. `uuid.uuid4` returns the case's pinned uuid. The state directory is a temporary directory
whose path is replaced by `FIXED_ROOT` in everything recorded. Signing uses a throwaway HMAC key derived at run time from a seed phrase; the
file holds payloads, never a key.

`python3 tests/parity/gen_plan_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SWARM_PURE_PYTHON", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_reconcile_vectors as base  # noqa: E402  (the fake clock and the seeded environment)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


orch_plan = _load("_orch_plan_for_vectors", REPO / "scripts" / "orch_plan.py")

from swarm import blast_radius, envelope, manifest, script_base, substrate_tee, taskstore  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "plan_derive.json"
SCHEMA = "gsd-route.plan-derive-vectors.v1"
FIXED_ROOT = "/fixture/repo"
NOW = base.NOW
EARLIER = 2000.0
CREATED_AT_STEP = 1e-6

Row = dict[str, Any]

CORR_A = "c0ffee01-aaaa-4bbb-8ccc-000000000001"
CORR_B = "c0ffee02-aaaa-4bbb-8ccc-000000000002"
CORR_SAME4_A = "abcd0001-aaaa-4bbb-8ccc-000000000003"
CORR_SAME4_B = "abcd3001-bbbb-4bbb-8ccc-000000000004"
CORR_SAME6_C = "abcd2201-cccc-4bbb-8ccc-000000000005"
CORR_SAME6_D = "abcd2202-dddd-4bbb-8ccc-000000000006"
GRAPH_ID = "ut-abc123-0123abcd"
BRIEF = "Build the orders service.\nAcceptance: p95 under 200 ms."
RISKS = ("low", "medium", "high")
PRIORITIES = ("P0", "P1", "P2", "P3")
HOT = ["--pattern", "hotfix"]


def derived(corr: str, n: int) -> str:
    return orch_plan._derived_prefix(corr, n)  # type: ignore[no-any-return]


class _Counter:
    """`uuid.uuid4` for one call: the case's uuid first, then a fixed sequence (envelope trace ids)."""

    def __init__(self, first: str) -> None:
        self.first, self.count = first, 0

    def uuid4(self) -> uuid.UUID:
        self.count += 1
        if self.count == 1:
            return uuid.UUID(self.first)
        return uuid.UUID(int=int.from_bytes(hashlib.sha256(f"plan-vector-uuid-{self.count}".encode("ascii")).digest()[:16], "big"))


def isolate(text: str, root: Path) -> str:
    return text.replace(str(root), FIXED_ROOT)


def normalise(value: Any, root: Path) -> Any:
    """`value` with the temporary root replaced by `FIXED_ROOT` in every string."""
    if isinstance(value, str):
        return isolate(value, root)
    if isinstance(value, list):
        return [normalise(item, root) for item in value]
    if isinstance(value, dict):
        return {normalise(key, root): normalise(item, root) for key, item in value.items()}
    return value


class World:
    """One state directory, one in-memory store and the clock that drives them; `call` runs the real script."""

    def __init__(self, clock: base._Clock) -> None:
        self.clock = clock
        self.root = Path(tempfile.mkdtemp(prefix="plan-vectors-")).resolve()
        self.store = TaskStore(":memory:")
        (self.root / ".swarm").mkdir()

    def close(self) -> None:
        self.store.conn.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- files ------------------------------------------------------------------------------------------------------------
    def file(self, name: str, content: str | bytes) -> str:
        path = self.root / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return str(path)

    def plan_files(self) -> dict[str, tuple[int, int]]:
        plans = self.root / ".swarm" / "plans"
        return {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in plans.glob("*.json")} if plans.exists() else {}

    def plan_text(self, corr: str) -> str | None:
        try:
            return (self.root / ".swarm" / "plans" / f"{corr}.json").read_text(encoding="utf-8")
        except OSError:
            return None

    def rows(self) -> list[Row]:
        return [dict(r) for r in self.store.conn.execute("SELECT * FROM tasks ORDER BY rowid")]

    # ---- the call ---------------------------------------------------------------------------------------------------------
    def _request(self, args: Any) -> Row:
        def read(path: str | None, encoding: str | None, must_exist: bool) -> Row | None:
            if not path:
                return None
            if must_exist and not Path(path).exists():
                return None
            try:
                return {"text": Path(path).read_text(encoding=encoding)}
            except (OSError, ValueError):
                return {"unreadable": True}

        return {"pattern": args.pattern, "risk_class": args.risk_class, "priority": args.priority, "prefix": args.prefix,
                "acceptance": list(args.acceptance), "graph_id": args.graph_id,
                "brief_file": read(args.brief, "utf-8", True), "brief_text": args.brief_text,
                "plan_file": read(args.plan, None, False), "slices_json": args.slices_json,
                "slices_file": read(args.slices, "utf-8", False),
                "correlation_id": args.correlation_id, "dry_run": bool(args.dry_run)}

    def _facts(self, args: Any, corr_uuid: str) -> Row:
        corr = args.correlation_id or corr_uuid
        prefixes = [args.prefix] if args.prefix is not None else [derived(corr, 4), derived(corr, 6)]
        rows = self.rows()
        prefix_rows = [r for r in rows if any(r["task_id"].startswith(f"{p}-") for p in prefixes)]
        corrs = sorted({corr, *(r["correlation_id"] for r in prefix_rows)})
        marks = ",".join("?" for _ in corrs)
        by_corr = [dict(r) for r in self.store.conn.execute(
            f"SELECT * FROM tasks WHERE correlation_id IN ({marks}) ORDER BY dag_depth, created_at, rowid", corrs)]
        return {"new_correlation_id": corr_uuid, "plans_dir": f"{FIXED_ROOT}/.swarm/plans",
                "signing": {"agent_session": any(os.environ.get(n) == "1" for n in ("SWARM_AGENT_SESSION", "SWARM_CHILD")),
                            "config_error": envelope.signing_config_error(), "real_key": envelope.real_key_configured()},
                "prefix_tasks": prefix_rows, "correlation_tasks": by_corr,
                "plan_files": [{"correlation_id": c, "text": self.plan_text(c)} for c in corrs]}

    def call(self, name: str, group: str, argv: list[str], *, corr: str = CORR_A, at: float = NOW, keys: str = "none",
             env: dict[str, str] | None = None, record: bool = True, rust: str = "plan", why: str | None = None,
             alias: bool = False) -> Row:
        """Runs `orch_plan.py` once at `at`; with `record` returns the case, without it only drives the store (a seed)."""
        argv = ["--json", "--repo" if alias else "--root", str(self.root), *argv]
        values, removed = self._env(keys, env or {})
        events: list[Row] = []
        bound: list[Row] = []
        envelopes: list[Row] = []
        results: list[Row] = []
        counter = [0]

        def emit(event_type: str, payload: dict[str, Any], **kw: Any) -> dict[str, Any]:
            events.append({"type": event_type, "payload": normalise(payload, self.root), "correlation_id": kw.get("correlation_id"),
                           "task_id": kw.get("task_id")})
            return {}

        def bind(args: Any, ctx: Any, corr_id: str, brief: str, tasks: list[Row]) -> None:
            bound.append({"explicit_graph_id": args.graph_id, "correlation_id": corr_id, "brief": brief,
                          "task_ids": [t["task_id"] for t in tasks]})

        real_envelope = envelope.build_envelope

        def build(**kw: Any) -> Row:
            envelopes.append({"correlation_id": kw["correlation_id"], "risk_class": kw["risk_class"], "payload": copy.deepcopy(kw["payload"])})
            return real_envelope(**kw)

        real_run = orch_plan.run

        def run(args: Any, ctx: Any) -> Row:
            result: Row = real_run(args, ctx)
            results.append(copy.deepcopy(result))
            return result

        real_create = TaskStore.create

        def create(store: TaskStore, **kw: Any) -> Any:
            self.clock.now = at + counter[0] * CREATED_AT_STEP
            counter[0] += 1
            return real_create(store, **kw)

        script = script_base.AgentScript("A01", "orch_plan", run, description=orch_plan.__doc__ or "", add_args=orch_plan.add_args)
        with mock.patch.dict(os.environ, values):
            for variable in removed:
                os.environ.pop(variable, None)
            args = script.parser().parse_args(argv)
            request = self._request(args)
            facts = self._facts(args, corr)
            rows_before = self.rows()
            tmax = self.store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM transitions").fetchone()[0]
            files_before = self.plan_files()
            (self.root / ".swarm" / "latest_correlation").unlink(missing_ok=True)
            self.clock.now = at
            out = io.StringIO()
            with (mock.patch.object(orch_plan, "TaskStore", lambda root=None: self.store),
                  mock.patch.object(orch_plan, "_bind_graph", bind),
                  mock.patch.object(orch_plan, "build_envelope", build),
                  mock.patch.object(script_base, "emit", emit),
                  mock.patch.object(TaskStore, "create", create),
                  mock.patch.object(uuid, "uuid4", _Counter(corr).uuid4),
                  contextlib.redirect_stdout(out)):
                exit_code = script.main(argv)
        if not record:
            return {}
        doc = json.loads(out.getvalue())
        error = doc.get("error")
        traced = isinstance(error, dict) and "trace" in error
        stdout = None if (traced or envelopes) else isolate(out.getvalue(), self.root)
        reply: Row | None = None
        if results:
            reply = normalise(results[0], self.root)
            for key in ("execution_assignment", "execution_assignment_verified"):
                reply.pop(key, None)
        files_after = self.plan_files()
        written = sorted(n for n, stamp in files_after.items() if files_before.get(n) != stamp)
        latest = self.root / ".swarm" / "latest_correlation"
        new_tasks = [r for r in self.rows() if r not in rows_before]
        expect: Row = {
            "exit": exit_code, "stdout": stdout, "reply": reply if stdout is None else None,
            "error": normalise({k: error[k] for k in ("code", "message", "details")}, self.root) if isinstance(error, dict) and not traced else None,
            "python_error": error["message"].split(":")[0] if traced else None,
            "tasks": [normalise(r, self.root) for r in new_tasks],
            "tasks_unchanged": self.rows() == rows_before or None,
            "transitions": [dict(r) for r in self.store.conn.execute("SELECT * FROM transitions WHERE id > ? ORDER BY id", (tmax,))],
            "plan_file": {"name": written[0], "text": self.plan_text(written[0][:-5])} if written else None,
            "latest_correlation": latest.read_text() if latest.exists() else None,
            "plan_updated": [e for e in events if e["type"] == "plan.updated"],
            "exit_record_correlation": next((e["correlation_id"] for e in events if e["type"] == "script.orch_plan"), None),
            "graph_bind": bound[0] if bound else None,
            "kickoff": normalise(envelopes[0], self.root) if envelopes else None,
            "kickoff_verified": bool(doc.get("execution_assignment_verified")) if envelopes else None,
        }
        assert len(written) <= 1, written
        if rust == "plan":
            assert expect["error"] is not None or expect["python_error"] is None, f"{name}: Python crashed but Rust must answer"
            if expect["error"] is not None:
                assert expect["tasks_unchanged"], f"{name}: a refusal changed the store"
        return {"name": name, "group": group, "rust": rust, "why": why, "now": at, "request": normalise(request, self.root),
                "facts": normalise(facts, self.root), "expect": expect}

    @staticmethod
    def _env(keys: str, extra: dict[str, str]) -> tuple[dict[str, str], list[str]]:
        """The variables to set and the names to remove for one call. `keys`: `none` (no signing key), `hmac` (a real key), `require`
        (`SWARM_REQUIRE_KEY=1` without a key) or `session` (a child session with `SWARM_REQUIRE_KEY=1`)."""
        values = {"SWARM_SIGNING_KEY": "" if keys in ("none", "require", "session") else base.derive_key(base.KEY_SEED)}
        if keys in ("require", "session"):
            values["SWARM_REQUIRE_KEY"] = "1"
        if keys == "session":
            values["SWARM_AGENT_SESSION"] = "1"
        values.update(extra)
        clean = (*base.KEY_ENV, "SWARM_CHILD", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SUBSTRATE_URL", "SUBSTRATE_GRAPH_ID", "HOME",
                 "XDG_CONFIG_HOME")
        return values, [name for name in clean if name not in values]


Scenario = Callable[[base._Clock], Iterator[Row]]


@contextlib.contextmanager
def world(clock: base._Clock) -> Iterator[World]:
    w = World(clock)
    with mock.patch.dict(os.environ, {"SWARM_DIR": str(w.root / ".swarm")}):
        try:
            yield w
        finally:
            w.close()


def brief_args(text: str = BRIEF) -> list[str]:
    return ["--brief-text", text]


# ---- groups -----------------------------------------------------------------------------------------------------------------
def _patterns(clock: base._Clock) -> Iterator[Row]:
    """Every pattern at every risk class, with the priorities and acceptance lines rotating."""
    for p_index, pattern in enumerate(orch_plan.PATTERNS):
        for r_index, risk in enumerate(RISKS):
            extra: list[str] = ["--priority", PRIORITIES[(p_index + r_index) % 4]]
            if (p_index + r_index) % 2:
                extra += ["--acceptance", "p95 under 200 ms", "--acceptance", "audit log kept"]
            with world(clock) as w:
                yield w.call(f"pattern-{pattern}-{risk}", "pattern", ["--pattern", pattern, "--risk-class", risk, *extra, *brief_args()])
    for priority in PRIORITIES:
        with world(clock) as w:
            yield w.call(f"priority-{priority}", "pattern", [*HOT, "--priority", priority, *brief_args()])
    for label, lines in {"one": ["x"], "unicode": ["caf\u00e9 \u2603", "\U0001f680 launch"], "quote-and-backslash": ['say "hi"', "a\\b"],
                         "empty-line": [""]}.items():
        with world(clock) as w:
            args = [part for line in lines for part in ("--acceptance", line)]
            yield w.call(f"acceptance-{label}", "pattern", [*HOT, *args, *brief_args()])
    one = json.dumps({"tasks": [{"id": "a", "capability": "req.spec"}]})
    long_brief = "\U0001f680" * 999 + "ab" + "x" * 50
    briefs = {"multiline-quotes": 'line one\nline "two"\n\ttabbed \\ back', "unicode": "caf\u00e9 \u4e2d\u6587 \U0001f680",
              "exactly-2000": "y" * 2000, "2001": "y" * 2001, "astral-2000-boundary": long_brief, "whitespace": "   ",
              "fence-like": "text ```json\n{}\n``` more"}
    for label, text in briefs.items():
        with world(clock) as w:
            yield w.call(f"brief-text-{label}", "brief", ["--plan", w.file("p.json", one), *brief_args(text)])


def _briefs(clock: base._Clock) -> Iterator[Row]:
    with world(clock) as w:
        yield w.call("brief-empty-refused", "brief", [])
    with world(clock) as w:
        yield w.call("brief-empty-text-refused", "brief", ["--brief-text", ""])
    with world(clock) as w:
        yield w.call("brief-empty-dry-run-placeholder", "brief", ["--dry-run"])
    with world(clock) as w:
        path = w.file("brief.md", "From a file.\nSecond line \u00e9.\n")
        yield w.call("brief-file", "brief", ["--brief", path])
    with world(clock) as w:
        path = w.file("crlf.md", b"line one\r\nline two\rline three\n")
        yield w.call("brief-file-universal-newlines", "brief", [*HOT, "--brief", path])
    with world(clock) as w:
        yield w.call("brief-text-keeps-carriage-returns", "brief", [*HOT, "--brief-text", "a\r\nb\rc"])
    with world(clock) as w:
        path = w.file("brief.md", "File wins.")
        yield w.call("brief-file-wins-over-text", "brief", ["--brief", path, "--brief-text", "ignored text"])
    with world(clock) as w:
        yield w.call("brief-missing-file-falls-to-text", "brief", ["--brief", str(w.root / "nope.md"), "--brief-text", "the text"])
    with world(clock) as w:
        yield w.call("brief-missing-file-no-text-refused", "brief", ["--brief", str(w.root / "nope.md")])
    with world(clock) as w:
        path = w.file("empty.md", "")
        yield w.call("brief-empty-file-refused", "brief", ["--brief", path, "--brief-text", "text is not read"])
    with world(clock) as w:
        path = w.file("empty.md", "")
        yield w.call("brief-empty-file-dry-run", "brief", ["--brief", path, "--dry-run"])
    with world(clock) as w:
        (w.root / "adir").mkdir()
        yield w.call("brief-directory-unreadable", "brief", ["--brief", str(w.root / "adir")], rust="not_started", why="unreadable-file")
    with world(clock) as w:
        path = w.file("bad.md", b"\xff\xfe not utf-8")
        yield w.call("brief-invalid-utf8-unreadable", "brief", ["--brief", path], rust="not_started", why="unreadable-file")
    with world(clock) as w:
        yield w.call("brief-empty-text-with-plan-ok", "brief", ["--plan", w.file("p.json", json.dumps({"tasks": [
            {"id": "a", "capability": "code.backend"}]}))])
    with world(clock) as w:
        yield w.call("repo-alias-for-root", "brief", ["--pattern", "dependency", *brief_args()], alias=True)


def _dry_runs(clock: base._Clock) -> Iterator[Row]:
    for pattern in orch_plan.PATTERNS:
        with world(clock) as w:
            yield w.call(f"dry-run-{pattern}", "dry-run", ["--dry-run", "--pattern", pattern, *brief_args()])
        with world(clock) as w:
            yield w.call(f"dry-run-{pattern}-prefix", "dry-run", ["--dry-run", "--pattern", pattern, "--prefix", "Tdry", *brief_args()])
    with world(clock) as w:
        yield w.call("dry-run-correlation-pinned", "dry-run", ["--dry-run", "--correlation-id", "pinned-corr-id", *brief_args()])
    with world(clock) as w:
        yield w.call("dry-run-empty-prefix-is-derived", "dry-run", ["--dry-run", "--prefix", "", *brief_args()])
    with world(clock) as w:
        yield w.call("dry-run-ignores-signing-config-error", "dry-run", ["--dry-run", *brief_args()], keys="require")
    with world(clock) as w:
        yield w.call("dry-run-writes-nothing-when-tasks-exist", "dry-run", ["--dry-run", "--prefix", "Tx", *brief_args()])
    with world(clock) as w:
        yield w.call("dry-run-invalid-graph-id-refused", "dry-run", ["--dry-run", "--graph-id", "nope", *brief_args()])
    with world(clock) as w:
        plan = {"tasks": [{"id": "a", "capability": "code.backend", "depends_on": "not-a-list"},
                          {"id": "b", "capability": "req.spec", "depends_on": ["zzz", 5, None]}]}
        yield w.call("dry-run-custom-weird-dependencies-pass-through", "dry-run", ["--dry-run", "--plan", w.file("p.json", json.dumps(plan))])
    with world(clock) as w:
        yield w.call("dry-run-custom-empty-plan", "dry-run", ["--dry-run", "--plan", w.file("p.json", '{"tasks": []}')])
    with world(clock) as w:
        yield w.call("dry-run-parallel", "dry-run", ["--dry-run", *brief_args(fence(S2))])
    with world(clock) as w:
        yield w.call("dry-run-parallel-high", "dry-run", ["--dry-run", "--risk-class", "high", *brief_args(fence(S3))])
    with world(clock) as w:
        yield w.call("dry-run-pattern-custom-without-plan", "dry-run", ["--dry-run", "--pattern", "custom", *brief_args()],
                     rust="not_started", why="python-trace")
    with world(clock) as w:
        yield w.call("dry-run-pattern-parallel-without-slices", "dry-run", ["--dry-run", "--pattern", "parallel", *brief_args()])


def _prefixes(clock: base._Clock) -> Iterator[Row]:
    with world(clock) as w:
        yield w.call("prefix-derived-four", "prefix", brief_args())
    with world(clock) as w:
        yield w.call("prefix-explicit", "prefix", ["--prefix", "PROJ", *brief_args()])
    with world(clock) as w:
        yield w.call("prefix-explicit-empty", "prefix", ["--prefix", "", *brief_args()])
    with world(clock) as w:
        yield w.call("prefix-correlation-pinned", "prefix", ["--correlation-id", "my-corr.1", *brief_args()])
    # another correlation holds the 4 hex prefix: fall back to 6
    with world(clock) as w:
        w.call("seed", "prefix", HOT + brief_args(), corr=CORR_SAME4_A, at=EARLIER, record=False)
        yield w.call("prefix-four-taken-falls-back-to-six", "prefix", HOT + brief_args("a different brief"), corr=CORR_SAME4_B)
    with world(clock) as w:
        w.call("seed", "prefix", HOT + brief_args(), corr=CORR_SAME4_A, at=EARLIER, record=False)
        yield w.call("prefix-four-taken-same-brief-still-falls-back", "prefix", HOT + brief_args(), corr=CORR_SAME4_B)
    # the 6 hex prefix is taken too: conflict naming prefix6
    with world(clock) as w:
        w.call("seed", "prefix", HOT + brief_args(), corr=CORR_SAME6_C, at=EARLIER, record=False)
        w.call("seed", "prefix", HOT + brief_args("two"), corr=CORR_SAME6_D, at=EARLIER, record=False)
        yield w.call("prefix-six-also-taken-conflict", "prefix", HOT + brief_args("three"), corr="abcd2203-eeee-4bbb-8ccc-000000000007")
    # the 4 hex prefix is this correlation's own plan: reuse or conflict, never the fallback
    with world(clock) as w:
        w.call("seed", "prefix", HOT + brief_args(), corr=CORR_A, at=EARLIER, record=False)
        yield w.call("prefix-own-four-reused", "prefix", ["--correlation-id", CORR_A, *HOT, *brief_args()], corr=CORR_A)
    with world(clock) as w:
        w.call("seed", "prefix", HOT + brief_args(), corr=CORR_A, at=EARLIER, record=False)
        yield w.call("prefix-own-four-different-brief-conflict", "prefix", ["--correlation-id", CORR_A, *HOT, *brief_args("changed")], corr=CORR_A)
    # a second plan under the same correlation (different prefix): the plan file holds both
    with world(clock) as w:
        w.call("seed", "prefix", ["--correlation-id", "shared-corr", "--prefix", "TA", "--pattern", "hotfix", *brief_args()],
               at=EARLIER, record=False)
        yield w.call("prefix-second-plan-same-correlation", "prefix", ["--correlation-id", "shared-corr", "--prefix", "TB", "--pattern", "dependency",
                                                                        *brief_args("second")])
    with world(clock) as w:
        w.call("seed", "prefix", ["--correlation-id", "shared-corr", "--prefix", "TA", "--pattern", "hotfix", *brief_args()],
               at=EARLIER, record=False)
        yield w.call("prefix-second-plan-interleaves-by-depth-and-time", "prefix",
                     ["--correlation-id", "shared-corr", "--prefix", "TB", "--pattern", "hotfix", *brief_args("second")])


def _reuse(clock: base._Clock) -> Iterator[Row]:
    seed_args = ["--pattern", "hotfix", "--prefix", "TR", "--acceptance", "keep it fast", *brief_args()]
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        yield w.call("reuse-same-arguments-explicit-prefix", "reuse", seed_args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", ["--correlation-id", "pinned-1", *seed_args], at=EARLIER, record=False)
        yield w.call("reuse-same-arguments-pinned-correlation", "reuse", ["--correlation-id", "pinned-1", *seed_args])
    with world(clock) as w:
        plan = json.dumps({"tasks": [{"id": "a", "capability": "req.spec"}, {"id": "b", "capability": "code.backend", "depends_on": ["a"]}]})
        args = ["--prefix", "TC", "--plan", w.file("p.json", plan)]
        w.call("seed", "reuse", args, at=EARLIER, record=False)
        yield w.call("reuse-custom-plan", "reuse", args, corr=CORR_B)
    with world(clock) as w:
        args = ["--prefix", "TP", *brief_args(fence(S2))]
        w.call("seed", "reuse", args, at=EARLIER, record=False)
        yield w.call("reuse-parallel-plan", "reuse", args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        (w.root / ".swarm" / "plans" / f"{CORR_A}.json").unlink()
        yield w.call("reuse-plan-file-missing-reports-null", "reuse", seed_args, corr=CORR_B)
    for label, text in {"blank": "  \n", "not-json": "{not json", "not-an-object": "[1, 2]", "no-tasks": '{"correlation_id": "x"}',
                        "tasks-not-a-list": '{"tasks": 5}'}.items():
        with world(clock) as w:
            w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
            (w.root / ".swarm" / "plans" / f"{CORR_A}.json").write_text(text, encoding="utf-8")
            if label == "not-json":
                yield w.call(f"reuse-plan-file-{label}", "reuse", seed_args, corr=CORR_B, rust="not_started", why="json-not-reproduced")
            else:
                yield w.call(f"reuse-plan-file-{label}", "reuse", seed_args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", ["--prefix", "TQ", "--correlation-id", "shared", "--pattern", "hotfix", *brief_args()], at=EARLIER, record=False)
        w.call("seed", "reuse", ["--prefix", "TS", "--correlation-id", "shared", "--pattern", "dependency", *brief_args("other")],
               at=EARLIER + 10, record=False)
        yield w.call("reuse-lists-every-task-of-the-correlation", "reuse",
                     ["--prefix", "TQ", "--pattern", "hotfix", *brief_args()], corr=CORR_B)
    # legacy rows: no brief_sha256 and no plan_sha256
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        legacy(w, drop=("brief_sha256", "plan_sha256"))
        yield w.call("reuse-legacy-rows-brief-excerpt-pattern-plan", "reuse", seed_args, corr=CORR_B)
    with world(clock) as w:
        plan = json.dumps({"tasks": [{"id": "a", "capability": "req.spec"}]})
        args = ["--prefix", "TL", "--plan", w.file("p.json", plan)]
        w.call("seed", "reuse", args, at=EARLIER, record=False)
        legacy(w, drop=("plan_sha256",))
        yield w.call("reuse-legacy-custom-fails-closed", "reuse", args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        legacy(w, drop=("brief_sha256", "plan_sha256"))
        yield w.call("reuse-legacy-different-excerpt-conflict", "reuse", ["--pattern", "hotfix", "--prefix", "TR", "--acceptance", "keep it fast", *brief_args("not it")],
                     corr=CORR_B)
    # conflicts
    changes: dict[str, list[str]] = {
        "brief": ["--pattern", "hotfix", "--prefix", "TR", "--acceptance", "keep it fast", *brief_args("other brief")],
        "pattern": ["--pattern", "dependency", "--prefix", "TR", "--acceptance", "keep it fast", *brief_args()],
        "risk-class": ["--pattern", "hotfix", "--prefix", "TR", "--risk-class", "high", "--acceptance", "keep it fast", *brief_args()],
        "priority": ["--pattern", "hotfix", "--prefix", "TR", "--priority", "P0", "--acceptance", "keep it fast", *brief_args()],
        "acceptance": ["--pattern", "hotfix", "--prefix", "TR", "--acceptance", "other", *brief_args()],
        "no-acceptance": ["--pattern", "hotfix", "--prefix", "TR", *brief_args()],
    }
    for label, args in changes.items():
        with world(clock) as w:
            w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
            yield w.call(f"conflict-{label}", "reuse", args, corr=CORR_B)
    with world(clock) as w:
        args = ["--prefix", "TD", "--plan", w.file("p.json", json.dumps({"tasks": [{"id": "a", "capability": "req.spec"}]}))]
        w.call("seed", "reuse", args, at=EARLIER, record=False)
        other = ["--prefix", "TD", "--plan", w.file("q.json", json.dumps({"tasks": [{"id": "a", "capability": "req.spec", "title": "changed"}]}))]
        yield w.call("conflict-custom-plan-changed", "reuse", other, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.conn.execute("DELETE FROM tasks WHERE task_id='TR-retro'")
        w.store.conn.commit()
        yield w.call("conflict-partial-prefix", "reuse", seed_args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET correlation_id='other-owner' WHERE task_id='TR-retro'")
        w.store.conn.commit()
        yield w.call("conflict-two-owners-sorted", "reuse", seed_args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", ["--correlation-id", "owner-1", *seed_args], at=EARLIER, record=False)
        yield w.call("conflict-other-correlation-requested", "reuse", ["--correlation-id", "owner-2", *seed_args])
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        yield w.call("conflict-hint-explicit-prefix", "reuse", ["--pattern", "hotfix", "--prefix", "TR", "--priority", "P3", *brief_args()], corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET risk_class=NULL WHERE task_id='TR-rca'")
        w.store.conn.commit()
        yield w.call("conflict-stored-null-risk", "reuse", seed_args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET acceptance='not json' WHERE task_id='TR-rca'")
        w.store.conn.commit()
        yield w.call("reuse-stored-acceptance-not-json", "reuse", seed_args, corr=CORR_B, rust="not_started", why="json-not-reproduced")
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET notes='not json' WHERE task_id='TR-rca'")
        w.store.conn.commit()
        yield w.call("reuse-stored-notes-not-json", "reuse", seed_args, corr=CORR_B, rust="not_started", why="json-not-reproduced")
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET notes='[1]' WHERE task_id='TR-rca'")
        w.store.conn.commit()
        yield w.call("conflict-stored-notes-not-an-object", "reuse", seed_args, corr=CORR_B)
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.create(task_id="TR-extra", correlation_id=CORR_A, capability="x", dag_depth=0, risk_class="low",
                       notes={"gates": "abc"})
        yield w.call("reuse-stored-gates-not-a-list", "reuse", seed_args, corr=CORR_B, rust="not_started", why="stored-row")
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET dag_depth=NULL WHERE task_id='TR-rca'")
        w.store.conn.commit()
        yield w.call("reuse-stored-dag-depth-null", "reuse", seed_args, corr=CORR_B, rust="not_started", why="stored-row")
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.create(task_id="TR-extra", correlation_id=CORR_A, capability="x", dag_depth=0, risk_class="bogus")
        yield w.call("reuse-stored-unknown-risk-class", "reuse", seed_args, corr=CORR_B, rust="not_started", why="stored-row")
    with world(clock) as w:
        w.call("seed", "reuse", seed_args, at=EARLIER, record=False)
        w.store.create(task_id="TR-extra", correlation_id="bad corr id", capability="x", dag_depth=0, risk_class="low")
        w.store.conn.execute("UPDATE tasks SET correlation_id='bad corr id' WHERE task_id LIKE 'TR-%'")
        w.store.conn.commit()
        yield w.call("reuse-stored-correlation-not-a-safe-id", "reuse", seed_args, corr=CORR_B)


def legacy(w: World, *, drop: tuple[str, ...]) -> None:
    """Removes `notes` keys from every stored task, as plans of older versions lack them."""
    for row in w.rows():
        notes = json.loads(row["notes"])
        for key in drop:
            notes.pop(key, None)
        w.store.conn.execute("UPDATE tasks SET notes=? WHERE task_id=?", (json.dumps(notes), row["task_id"]))
    w.store.conn.commit()


def _graph(clock: base._Clock) -> Iterator[Row]:
    with world(clock) as w:
        yield w.call("graph-id-valid-is-passed-to-the-binding", "graph", ["--graph-id", GRAPH_ID, *brief_args()])
    for label, bad in {"plain": "nope", "empty": "", "uppercase": "ut-ABC-0123abcd", "short-hex": "ut-abc-0123abc", "long-hex": "ut-abc-0123abcde",
                       "no-base": "ut--0123abcd", "single-quote": "it's", "both-quotes": "a'b\"c", "backslash": "a\\b", "control": "a\nb\u007f",
                       "trailing-newline": GRAPH_ID + "\n", "dash-in-base": "ut-a-b-0123abcd"}.items():
        with world(clock) as w:
            yield w.call(f"graph-id-invalid-{label}", "graph", ["--graph-id", bad, *brief_args()])
    with world(clock) as w:
        yield w.call("graph-id-invalid-non-ascii", "graph", ["--graph-id", "ut-\u00e9-0123abcd", *brief_args()], rust="not_started",
                     why="non-ascii-repr")
    with world(clock) as w:
        yield w.call("graph-id-in-brief-is-not-the-planners-business", "graph", brief_args(f"Graph ID: {GRAPH_ID}"))


def _signing(clock: base._Clock) -> Iterator[Row]:
    with world(clock) as w:
        yield w.call("signing-require-key-without-key-refused", "signing", brief_args(), keys="require")
    with world(clock) as w:
        yield w.call("signing-agent-session-skips-the-check", "signing", brief_args(), keys="session")
    with world(clock) as w:
        yield w.call("signing-child-env-skips-the-check", "signing", brief_args(), keys="require", env={"SWARM_CHILD": "1"})
    with world(clock) as w:
        yield w.call("kickoff-feature-medium", "kickoff", brief_args(), keys="hmac")
    for pattern in ("hotfix", "web3", "cluster"):
        with world(clock) as w:
            yield w.call(f"kickoff-{pattern}-risk-high", "kickoff", ["--pattern", pattern, "--risk-class", "high", *brief_args()], keys="hmac")
    for pattern, risk in (("web3", "low"), ("cluster", "medium")):
        with world(clock) as w:
            yield w.call(f"kickoff-{pattern}-pinned-high-rows-over-{risk}", "kickoff", ["--pattern", pattern, "--risk-class", risk, *brief_args()],
                         keys="hmac")
    with world(clock) as w:
        yield w.call("kickoff-with-acceptance-lines", "kickoff", ["--acceptance", "a", "--acceptance", "a", "--acceptance", "b", *brief_args()], keys="hmac")
    with world(clock) as w:
        plan = {"tasks": [{"id": "a", "capability": "req.spec", "acceptance": [1.0, "x"]}]}
        yield w.call("kickoff-custom-acceptance-not-text", "kickoff", ["--plan", w.file("p.json", json.dumps(plan))], keys="hmac",
                     rust="not_started", why="kickoff-input")
    with world(clock) as w:
        plan = {"tasks": [{"id": "a", "capability": "req.spec", "acceptance": "plain text"}]}
        yield w.call("kickoff-custom-acceptance-a-string-iterates", "kickoff", ["--plan", w.file("p.json", json.dumps(plan))], keys="hmac",
                     rust="not_started", why="kickoff-input")
    with world(clock) as w:
        plan = {"tasks": [{"id": "a", "capability": "req.spec", "gates": [], "acceptance": [["nested"]]}]}
        yield w.call("kickoff-custom-acceptance-unhashable", "kickoff", ["--plan", w.file("p.json", json.dumps(plan))], keys="hmac",
                     rust="not_started", why="kickoff-input")
    with world(clock) as w:
        plan = {"tasks": [{"id": "a", "capability": "req.spec", "gates": ["review", 100.0, {"k": 2.5}]}]}
        yield w.call("kickoff-whole-floats-become-ints", "kickoff", ["--plan", w.file("p.json", json.dumps(plan))], keys="hmac")
    with world(clock) as w:
        raw = '{"tasks": [{"id": "a", "capability": "req.spec", "gates": [1e300]}]}'
        yield w.call("kickoff-float-beyond-js-after-write", "kickoff", ["--plan", w.file("p.json", raw)], keys="hmac",
                     rust="not_started", why="after-write")
    for label, value, answer in (("int-at-limit", "9007199254740991", "plan"), ("negative-int-at-limit", "-9007199254740991", "plan"),
                                 ("float-at-limit", "9007199254740991.0", "plan"), ("float-just-beyond", "9007199254740992.0", "not_started"),
                                 ("negative-int-beyond", "-9007199254740993", "not_started"), ("float-integral-1e15", "1e15", "plan"),
                                 ("negative-zero-float", "-0.0", "plan")):
        with world(clock) as w:
            raw = '{"tasks": [{"id": "a", "capability": "req.spec", "gates": [' + value + ']}]}'
            yield w.call(f"kickoff-js-limit-{label}", "kickoff", ["--plan", w.file("p.json", raw)], keys="hmac", rust=answer,
                         why="after-write" if answer == "not_started" else None)
    with world(clock) as w:
        raw = '{"tasks": [{"id": "a", "capability": "req.spec", "gates": [9007199254740993]}]}'
        yield w.call("kickoff-int-beyond-js-after-write", "kickoff", ["--plan", w.file("p.json", raw)], keys="hmac",
                     rust="not_started", why="after-write")
    with world(clock) as w:
        yield w.call("kickoff-empty-plan-after-write", "kickoff", ["--plan", w.file("p.json", '{"tasks": []}')], keys="hmac",
                     rust="not_started", why="after-write")
    with world(clock) as w:
        yield w.call("signing-empty-plan-without-key", "signing", ["--plan", w.file("p.json", '{"tasks": []}')])
    seed = ["--pattern", "hotfix", "--prefix", "TK", *brief_args()]
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        yield w.call("kickoff-reuse", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        (w.root / ".swarm" / "plans" / f"{CORR_A}.json").unlink()
        yield w.call("kickoff-reuse-plan-file-missing", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        text = json.loads((w.root / ".swarm" / "plans" / f"{CORR_A}.json").read_text())
        text["correlation_id"] = "someone-else"
        (w.root / ".swarm" / "plans" / f"{CORR_A}.json").write_text(json.dumps(text), encoding="utf-8")
        yield w.call("kickoff-reuse-plan-correlation-mismatch", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        (w.root / ".swarm" / "plans" / f"{CORR_A}.json").write_text('{"tasks": [], "extra": {"n": 1.5, "m": 3.0}}', encoding="utf-8")
        yield w.call("kickoff-reuse-plan-without-correlation", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        text = json.loads((w.root / ".swarm" / "plans" / f"{CORR_A}.json").read_text())
        text["extra"] = {"n": 1.5, "m": 3.0, "big": 4503599627370496.0, "keep": [True, None]}
        (w.root / ".swarm" / "plans" / f"{CORR_A}.json").write_text(json.dumps(text), encoding="utf-8")
        yield w.call("kickoff-reuse-plan-keeps-extra-keys", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        text = json.loads((w.root / ".swarm" / "plans" / f"{CORR_A}.json").read_text())
        text["extra"] = 1e300
        (w.root / ".swarm" / "plans" / f"{CORR_A}.json").write_text(json.dumps(text), encoding="utf-8")
        yield w.call("kickoff-reuse-plan-number-not-signable", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET state='DONE' WHERE task_id IN ('TK-rca')")
        w.store.conn.commit()
        yield w.call("kickoff-reuse-ready-tasks-follow-dependencies", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET budget='{\"max_tokens\": 5}' WHERE task_id='TK-rca'")
        w.store.conn.commit()
        yield w.call("kickoff-reuse-budget-missing-keys-count-zero", "kickoff", seed, corr=CORR_B, keys="hmac")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        w.store.conn.execute("UPDATE tasks SET budget='{\"max_tokens\": 1.5}' WHERE task_id='TK-rca'")
        w.store.conn.commit()
        yield w.call("kickoff-reuse-budget-not-an-integer", "kickoff", seed, corr=CORR_B, keys="hmac", rust="not_started", why="kickoff-input")
    with world(clock) as w:
        text = json.dumps({"tasks": [{"id": "a", "capability": "req.spec", "acceptance": ["only", 3]}]})
        args = ["--prefix", "TK", "--plan", w.file("p.json", text)]
        w.call("seed", "kickoff", args, at=EARLIER, record=False)
        yield w.call("kickoff-reuse-acceptance-item-not-text", "kickoff", args, corr=CORR_B, keys="hmac", rust="not_started", why="kickoff-input")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        yield w.call("kickoff-reuse-signing-config-error", "kickoff", seed, corr=CORR_B, keys="require")
    with world(clock) as w:
        w.call("seed", "kickoff", seed, at=EARLIER, record=False)
        yield w.call("kickoff-reuse-agent-session-skips", "kickoff", seed, corr=CORR_B, keys="session")


# ---- custom plans -----------------------------------------------------------------------------------------------------------
def custom(w: World, plan: Any, name: str, *extra: str, rust: str = "plan", why: str | None = None, raw: str | None = None,
           corr: str = CORR_A, keys: str = "none") -> Row:
    text = raw if raw is not None else json.dumps(plan)
    return w.call(name, "custom", ["--plan", w.file("plan.json", text), *extra], rust=rust, why=why, corr=corr, keys=keys)


def _custom(clock: base._Clock) -> Iterator[Row]:
    task = {"id": "a", "capability": "req.spec"}
    valid: dict[str, Any] = {
        "single": {"tasks": [task]},
        "empty-plan": {"tasks": []},
        "diamond": {"tasks": [task, {"id": "b", "capability": "design.blueprint", "depends_on": ["a"]},
                              {"id": "c", "capability": "ux.spec", "depends_on": ["a"]},
                              {"id": "d", "capability": "code.backend", "depends_on": ["b", "c"], "title": "the join"}]},
        "agent-and-capability": {"tasks": [{"id": "a", "capability": "code.backend", "agent": "A05"}]},
        "agent-by-slug-and-code": {"tasks": [{"id": "a", "capability": "code.backend", "agent": "a05-backend"},
                                             {"id": "b", "capability": "code.backend", "agent": "BE"}]},
        "agent-case-insensitive": {"tasks": [{"id": "a", "capability": "code.backend", "agent": "a05"}]},
        "capability-picks-first-agent": {"tasks": [{"id": "a", "capability": "gate.quality"}, {"id": "b", "capability": "code.patch"}]},
        "title-null": {"tasks": [{"id": "a", "capability": "req.spec", "title": None}]},
        "title-present": {"tasks": [{"id": "a", "capability": "req.spec", "title": "Spec \u00e9\u2603 \U0001f680"}]},
        "gates-list": {"tasks": [{"id": "a", "capability": "code.backend", "gates": ["review", "quality"]}]},
        "gates-empty-list": {"tasks": [{"id": "a", "capability": "code.backend", "gates": []}]},
        "gates-null": {"tasks": [{"id": "a", "capability": "code.backend", "gates": None}]},
        "gates-object": {"tasks": [task, {"id": "g", "capability": "gate.quality", "depends_on": ["a"], "gates": {"gate": "quality", "for": ["a"]}}]},
        "gates-object-gate-null": {"tasks": [task, {"id": "g", "capability": "gate.quality", "gates": {"gate": None, "for": []}}]},
        "risk-override": {"tasks": [{"id": "a", "capability": "code.backend", "risk_class": "high"}, {"id": "b", "capability": "req.spec", "risk_class": "low"}]},
        "risk-falsy-uses-default": {"tasks": [{"id": "a", "capability": "req.spec", "risk_class": ""}, {"id": "b", "capability": "req.spec", "risk_class": None},
                                              {"id": "c", "capability": "req.spec", "risk_class": 0}]},
        "acceptance-own": {"tasks": [{"id": "a", "capability": "req.spec", "acceptance": ["one", "two"]}, {"id": "b", "capability": "req.spec"}]},
        "acceptance-json-variety": {"tasks": [{"id": "a", "capability": "req.spec", "acceptance": [1, 2.5, True, None, {"k": [1.0, "x"]}, "\u00e9"]}]},
        "rn-lookalike-ids": {"tasks": [{"id": "a.r", "capability": "req.spec"}, {"id": "a.rx", "capability": "req.spec"},
                                       {"id": "r1", "capability": "req.spec"}, {"id": "a.R1", "capability": "req.spec"}, {"id": "a.r1x", "capability": "req.spec"}]},
        "ids-with-unicode-and-symbols": {"tasks": [{"id": "caf\u00e9 task", "capability": "req.spec"}, {"id": "x/y z", "capability": "req.spec"}]},
        "deep-chain": {"tasks": [{"id": f"t{i}", "capability": "req.spec", **({"depends_on": [f"t{i - 1}"]} if i else {})} for i in range(8)]},
        "duplicate-dependency": {"tasks": [task, {"id": "b", "capability": "req.spec", "depends_on": ["a", "a"]}]},
        "extra-keys-ignored": {"tasks": [{"id": "a", "capability": "req.spec", "owner": "me", "inputs": [1]}], "meta": 1},
    }
    for label, plan in valid.items():
        with world(clock) as w:
            yield custom(w, plan, f"custom-valid-{label}", "--priority", "P1")
    with world(clock) as w:
        yield custom(w, valid["diamond"], "custom-valid-diamond-medium-with-acceptance", "--acceptance", "fallback", "--risk-class", "medium",
                     *brief_args("with a brief"))
    refusals: dict[str, Any] = {
        "unknown-agent": {"tasks": [{"id": "a", "capability": "req.spec", "agent": "nobody"}]},
        "unknown-agent-quote": {"tasks": [{"id": "a", "capability": "req.spec", "agent": "it's"}]},
        "unknown-agent-both-quotes": {"tasks": [{"id": "a", "capability": "req.spec", "agent": "it's \"x\""}]},
        "unknown-agent-backslash-and-newline": {"tasks": [{"id": "a", "capability": "req.spec", "agent": "a\\b\nc"}]},
        "agent-not-text-number": {"tasks": [{"id": "a", "capability": "req.spec", "agent": 7}]},
        "agent-not-text-list": {"tasks": [{"id": "a", "capability": "req.spec", "agent": ["A05"]}]},
        "agent-not-text-object": {"tasks": [{"id": "a", "capability": "req.spec", "agent": {"id": "A05"}}]},
        "agent-true": {"tasks": [{"id": "a", "capability": "req.spec", "agent": True}]},
        "agent-float": {"tasks": [{"id": "a", "capability": "req.spec", "agent": 1.5}]},
        "agent-does-not-offer": {"tasks": [{"id": "a", "capability": "req.spec", "agent": "A05"}]},
        "agent-offer-capability-not-text": {"tasks": [{"id": "a", "capability": 5, "agent": "A05"}]},
        "no-agent-offers": {"tasks": [{"id": "a", "capability": "no.such.capability"}]},
        "no-agent-offers-number": {"tasks": [{"id": "a", "capability": 5}]},
        "no-agent-offers-list": {"tasks": [{"id": "a", "capability": ["code.backend"]}]},
        "no-agent-offers-null": {"tasks": [{"id": "a", "capability": None}]},
        "no-agent-offers-float": {"tasks": [{"id": "a", "capability": 1.5}]},
        "no-agent-offers-true": {"tasks": [{"id": "a", "capability": True}]},
        "no-agent-offers-object": {"tasks": [{"id": "a", "capability": {"k": "v"}}]},
        "no-agent-offers-unicode-text": {"tasks": [{"id": "a", "capability": "caf\u00e9 \u4e2d"}]},
        "unknown-agent-unicode-capability-is-not-reached": {"tasks": [{"id": "a", "capability": "caf\u00e9", "agent": "A05"}]},
        "rework-id": {"tasks": [{"id": "a.r1", "capability": "req.spec"}]},
        "rework-id-multi-digit": {"tasks": [{"id": "task.r12", "capability": "req.spec"}]},
        "rework-id-trailing-newline": {"tasks": [{"id": "a.r2\n", "capability": "req.spec"}]},
        "rework-id-with-quote": {"tasks": [{"id": "it's.r3", "capability": "req.spec"}]},
        "rework-id-second-task": {"tasks": [task, {"id": "b.r9", "capability": "req.spec"}]},
        "rework-id-before-unknown-agent": {"tasks": [{"id": "a.r1", "capability": "req.spec", "agent": "nobody"}]},
        "unknown-agent-before-missing-capability": {"tasks": [{"id": "a", "agent": "nobody"}]},
        "duplicate-id": {"tasks": [task, {"id": "a", "capability": "design.blueprint"}]},
        "duplicate-id-after-bad-dependency-order": {"tasks": [task, {"id": "a", "capability": "req.spec", "depends_on": ["a"]}]},
    }
    for label, plan in refusals.items():
        with world(clock) as w:
            yield custom(w, plan, f"custom-refused-{label}")
    declines: dict[str, tuple[Any, str]] = {
        "tasks-missing": ({"nope": []}, "python-trace"),
        "root-is-a-list": ([task], "python-trace"),
        "tasks-is-an-object": ({"tasks": {"a": 1}}, "python-trace"),
        "tasks-is-null": ({"tasks": None}, "python-trace"),
        "task-is-a-string": ({"tasks": ["a"]}, "python-trace"),
        "task-is-a-list": ({"tasks": [[1]]}, "python-trace"),
        "id-missing": ({"tasks": [{"capability": "req.spec"}]}, "python-trace"),
        "id-is-a-number": ({"tasks": [{"id": 5, "capability": "req.spec"}]}, "python-trace"),
        "id-is-null": ({"tasks": [{"id": None, "capability": "req.spec"}]}, "python-trace"),
        "capability-missing": ({"tasks": [{"id": "a"}]}, "python-trace"),
        "capability-missing-with-agent": ({"tasks": [{"id": "a", "agent": "A05"}]}, "python-trace"),
        "depends-on-unknown": ({"tasks": [{"id": "a", "capability": "req.spec", "depends_on": ["zzz"]}]}, "python-trace"),
        "depends-on-forward": ({"tasks": [{"id": "a", "capability": "req.spec", "depends_on": ["b"]}, {"id": "b", "capability": "req.spec"}]}, "python-trace"),
        "depends-on-cycle": ({"tasks": [{"id": "a", "capability": "req.spec", "depends_on": ["b"]},
                                         {"id": "b", "capability": "req.spec", "depends_on": ["a"]}]}, "python-trace"),
        "depends-on-self": ({"tasks": [{"id": "a", "capability": "req.spec", "depends_on": ["a"]}]}, "python-trace"),
        "depends-on-string": ({"tasks": [task, {"id": "b", "capability": "req.spec", "depends_on": "a"}]}, "value-shape"),
        "depends-on-null": ({"tasks": [{"id": "a", "capability": "req.spec", "depends_on": None}]}, "value-shape"),
        "depends-on-number-element": ({"tasks": [{"id": "a", "capability": "req.spec", "depends_on": [1]}]}, "python-trace"),
        "depends-on-list-element": ({"tasks": [task, {"id": "b", "capability": "req.spec", "depends_on": [["a"]]}]}, "python-trace"),
        "depends-on-object": ({"tasks": [task, {"id": "b", "capability": "req.spec", "depends_on": {"a": 1}}]}, "value-shape"),
        "risk-class-unknown": ({"tasks": [{"id": "a", "capability": "req.spec", "risk_class": "extreme"}]}, "python-trace"),
        "risk-class-number": ({"tasks": [{"id": "a", "capability": "req.spec", "risk_class": 3}]}, "python-trace"),
        "risk-class-list": ({"tasks": [{"id": "a", "capability": "req.spec", "risk_class": ["low"]}]}, "python-trace"),
        "gates-string": ({"tasks": [{"id": "a", "capability": "req.spec", "gates": "review"}]}, "value-shape"),
        "gates-number": ({"tasks": [{"id": "a", "capability": "req.spec", "gates": 5}]}, "value-shape"),
        "gates-object-without-gate": ({"tasks": [task, {"id": "g", "capability": "gate.quality", "gates": {"for": ["a"]}}]}, "python-trace"),
        "gates-object-without-for": ({"tasks": [task, {"id": "g", "capability": "gate.quality", "gates": {"gate": "quality"}}]}, "python-trace"),
        "gates-object-empty": ({"tasks": [{"id": "g", "capability": "gate.quality", "gates": {}}]}, "python-trace"),
        "gates-for-string": ({"tasks": [task, {"id": "g", "capability": "gate.quality", "gates": {"gate": "quality", "for": "a"}}]}, "value-shape"),
        "gates-for-number-element": ({"tasks": [task, {"id": "g", "capability": "gate.quality", "gates": {"gate": "quality", "for": [1]}}]}, "value-shape"),
        "title-number": ({"tasks": [{"id": "a", "capability": "req.spec", "title": 5}]}, "value-shape"),
        "title-list": ({"tasks": [{"id": "a", "capability": "req.spec", "title": ["x"]}]}, "value-shape"),
        "title-object": ({"tasks": [{"id": "a", "capability": "req.spec", "title": {"x": 1}}]}, "value-shape"),
        "rework-id-unicode-digit": ({"tasks": [{"id": "a.r\u0663", "capability": "req.spec"}]}, "unicode-digit"),
        "rework-id-ascii-then-unicode-digit": ({"tasks": [{"id": "a.r\u06631", "capability": "req.spec"}]}, "unicode-digit"),
        "agent-not-ascii": ({"tasks": [{"id": "a", "capability": "req.spec", "agent": "\u212a"}]}, "non-ascii-repr"),
        "agent-not-ascii-unknown": ({"tasks": [{"id": "a", "capability": "req.spec", "agent": "caf\u00e9"}]}, "non-ascii-repr"),
        "rework-id-not-ascii-repr": ({"tasks": [{"id": "caf\u00e9.r1", "capability": "req.spec"}]}, "non-ascii-repr"),
        "no-agent-offers-list-not-ascii": ({"tasks": [{"id": "a", "capability": ["\u00e9"]}]}, "non-ascii-repr"),
    }
    for label, (plan, why) in declines.items():
        with world(clock) as w:
            yield custom(w, plan, f"custom-declined-{label}", rust="not_started", why=why)
    raws: dict[str, str] = {
        "not-json": "{nope",
        "empty-file": "",
        "nan-literal": '{"tasks": [{"id": "a", "capability": "req.spec", "title": NaN}]}',
        "bigint": '{"tasks": [{"id": "a", "capability": "req.spec", "acceptance": [123456789012345678901234567890]}]}',
        "lone-surrogate": '{"tasks": [{"id": "a", "capability": "req.spec", "title": "\\ud800"}]}',
        "trailing-comma": '{"tasks": [],}',
    }
    for label, raw in raws.items():
        with world(clock) as w:
            yield custom(w, None, f"custom-declined-json-{label}", raw=raw, rust="not_started", why="json-not-reproduced")
    with world(clock) as w:
        yield w.call("custom-declined-plan-file-missing", "custom", ["--plan", str(w.root / "absent.json")], rust="not_started", why="unreadable-file")
    with world(clock) as w:
        yield custom(w, valid["single"], "custom-refused-with-slices-json", "--slices-json", json.dumps(S2))
    with world(clock) as w:
        yield custom(w, valid["single"], "custom-refused-with-slices-file", "--slices", w.file("s.json", json.dumps(S2)))
    with world(clock) as w:
        yield custom(w, valid["single"], "custom-refused-with-brief-fence", *brief_args(fence(S2)))
    with world(clock) as w:
        yield custom(w, valid["single"], "custom-refused-with-fence-text-only", *brief_args("```blast-radii nothing"))
    with world(clock) as w:
        yield custom(w, valid["single"], "custom-pattern-flag-is-overridden", "--pattern", "hotfix")
    with world(clock) as w:
        yield w.call("custom-declined-pattern-custom-without-plan", "custom", ["--pattern", "custom", *brief_args()], rust="not_started",
                     why="python-trace")
    with world(clock) as w:
        yield w.call("custom-empty-plan-flag-is-no-plan", "custom", ["--plan", "", *brief_args()])
    with world(clock) as w:
        yield custom(w, {"tasks": [task, {"id": "a2", "capability": "req.spec"}]}, "custom-explicit-prefix-and-correlation",
                     "--prefix", "PX", "--correlation-id", "fixed-corr")
    with world(clock) as w:
        yield custom(w, valid["single"], "custom-valid-risk-high", "--risk-class", "high")


# ---- blast radii ------------------------------------------------------------------------------------------------------------
def sl(slice_id: Any, paths: Any, **extra: Any) -> dict[str, Any]:
    return {"id": slice_id, "paths": paths, **extra}


S2 = [sl("api", ["src/api"], agent="A05"), sl("web", ["src/web"], agent="A06")]
S3 = [sl("api", ["src/api", "tests/api"], capability="code.backend"), sl("web", ["src/web"], agent="a06-frontend", title="  The web lane  "),
      sl("docs", ["docs"], agent="A15", acceptance=["docs build", "links ok"])]


def fence(slices: Any, info: str = "") -> str:
    return f"Plan\n```blast-radii{info}\n{json.dumps(slices)}\n```\nend"


def _parallel(clock: base._Clock) -> Iterator[Row]:
    for risk in RISKS:
        for label, slices in (("two", S2), ("three", S3)):
            with world(clock) as w:
                yield w.call(f"parallel-brief-fence-{label}-{risk}", "parallel", ["--risk-class", risk, "--priority", "P3", *brief_args(fence(slices))])
    with world(clock) as w:
        yield w.call("parallel-slices-json", "parallel", ["--slices-json", json.dumps(S2), *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-slices-file", "parallel", ["--slices", w.file("s.json", json.dumps(S3)), *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-slices-file-only-no-brief", "parallel", ["--slices", w.file("s.json", json.dumps(S2))])
    with world(clock) as w:
        yield w.call("parallel-pattern-flag-feature-is-overridden", "parallel", ["--pattern", "feature", "--slices-json", json.dumps(S2), *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-pattern-flag-parallel-with-slices", "parallel", ["--pattern", "parallel", "--slices-json", json.dumps(S2), *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-without-slices-refused", "parallel", ["--pattern", "parallel", *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-explicit-prefix-branch-names", "parallel", ["--prefix", "PAR", "--slices-json", json.dumps(S2), *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-fence-with-info-string", "parallel", brief_args(fence(S2, " json extra words")))
    with world(clock) as w:
        yield w.call("parallel-fence-crlf-info-line", "parallel", brief_args(f"```blast-radii \r\n{json.dumps(S2)}\n```"))
    with world(clock) as w:
        yield w.call("parallel-fence-second-unclosed-ignored", "parallel", brief_args(fence(S2) + "\n```blast-radii\n[1]"))
    with world(clock) as w:
        yield w.call("parallel-fence-null-block-is-no-block", "parallel", ["--slices-json", json.dumps(S2), *brief_args("```blast-radii\nnull\n```")])
    with world(clock) as w:
        yield w.call("parallel-fence-null-block-alone-is-no-slices", "parallel", brief_args("```blast-radii\nnull\n```"))
    with world(clock) as w:
        yield w.call("parallel-fence-unclosed-is-no-block", "parallel", brief_args("```blast-radii\n[]"))
    with world(clock) as w:
        yield w.call("parallel-fence-without-newline-is-no-block", "parallel", brief_args("```blast-radii[]```"))
    with world(clock) as w:
        yield w.call("parallel-fence-two-blocks-refused", "parallel", brief_args(fence(S2) + "\n" + fence(S3)))
    with world(clock) as w:
        yield w.call("parallel-fence-then-slices-json-refused", "parallel", ["--slices-json", json.dumps(S2), *brief_args(fence(S2))])
    with world(clock) as w:
        yield w.call("parallel-fence-then-slices-file-refused", "parallel", ["--slices", w.file("s.json", json.dumps(S2)), *brief_args(fence(S2))])
    with world(clock) as w:
        yield w.call("parallel-slices-json-and-file-refused", "parallel",
                     ["--slices-json", json.dumps(S2), "--slices", w.file("s.json", json.dumps(S2)), *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-all-three-refused", "parallel",
                     ["--slices-json", json.dumps(S2), "--slices", w.file("s.json", json.dumps(S2)), *brief_args(fence(S2))])
    with world(clock) as w:
        yield w.call("parallel-slices-json-null-is-not-an-array", "parallel", ["--slices-json", "null", *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-slices-json-empty-text-is-no-slices", "parallel", ["--slices-json", "", *brief_args()])
    with world(clock) as w:
        yield w.call("parallel-slices-file-unreadable", "parallel", ["--slices", str(w.root / "nope.json"), *brief_args()], rust="not_started",
                     why="unreadable-file")
    for label, text in {"not-json": "{nope", "nan": "[NaN]", "bigint": '[{"id": "a", "paths": ["x"], "n": 99999999999999999999999}]'}.items():
        with world(clock) as w:
            yield w.call(f"parallel-declined-slices-json-{label}", "parallel", ["--slices-json", text, *brief_args()], rust="not_started",
                         why="json-not-reproduced")
    with world(clock) as w:
        yield w.call("parallel-declined-fence-not-json", "parallel", brief_args("```blast-radii\n{nope\n```"), rust="not_started",
                     why="json-not-reproduced")
    with world(clock) as w:
        yield w.call("parallel-declined-slices-file-not-json", "parallel", ["--slices", w.file("s.json", "{nope"), *brief_args()],
                     rust="not_started", why="json-not-reproduced")
    errors: dict[str, Any] = {
        "not-an-array": {"id": "a"},
        "one-slice": [sl("a", ["x"], agent="A05")],
        "thirty-three": [sl(f"s{i}", [f"p{i}"], agent="A05") for i in range(33)],
        "thirty-two-ok": [sl(f"s{i}", [f"p{i}"], agent="A05") for i in range(32)],
        "item-not-object": [sl("a", ["x"], agent="A05"), "b"],
        "id-missing": [sl("a", ["x"], agent="A05"), {"paths": ["y"], "agent": "A05"}],
        "id-number": [sl("a", ["x"], agent="A05"), sl(5, ["y"], agent="A05")],
        "id-null": [sl("a", ["x"], agent="A05"), sl(None, ["y"], agent="A05")],
        "id-list": [sl("a", ["x"], agent="A05"), sl(["z"], ["y"], agent="A05")],
        "id-object": [sl("a", ["x"], agent="A05"), sl({"k": 1.5}, ["y"], agent="A05")],
        "id-bool": [sl("a", ["x"], agent="A05"), sl(True, ["y"], agent="A05")],
        "id-float": [sl("a", ["x"], agent="A05"), sl(2.5, ["y"], agent="A05")],
        "id-uppercase": [sl("a", ["x"], agent="A05"), sl("Bad", ["y"], agent="A05")],
        "id-starts-with-digit": [sl("a", ["x"], agent="A05"), sl("1b", ["y"], agent="A05")],
        "id-33-chars": [sl("a", ["x"], agent="A05"), sl("b" * 33, ["y"], agent="A05")],
        "id-32-chars": [sl("a", ["x"], agent="A05"), sl("b" * 32, ["y"], agent="A05")],
        "id-with-dash": [sl("a", ["x"], agent="A05"), sl("b-c-1", ["y"], agent="A05")],
        "id-with-quote": [sl("a", ["x"], agent="A05"), sl("it's", ["y"], agent="A05")],
        "id-trailing-newline": [sl("a", ["x"], agent="A05"), sl("b\n", ["y"], agent="A05")],
        "id-empty": [sl("a", ["x"], agent="A05"), sl("", ["y"], agent="A05")],
        "id-reserved-join": [sl("join", ["x"], agent="A05"), sl("b", ["y"], agent="A05")],
        "id-reserved-rev": [sl("b", ["x"], agent="A05"), sl("rev", ["y"], agent="A05")],
        "id-reserved-qa-sec-rel": [sl("qa", ["x"], agent="A05"), sl("sec", ["y"], agent="A05")],
        "id-repeated": [sl("a", ["x"], agent="A05"), sl("a", ["y"], agent="A05")],
        "paths-missing": [sl("a", ["x"], agent="A05"), {"id": "b", "agent": "A05"}],
        "paths-empty": [sl("a", ["x"], agent="A05"), sl("b", [], agent="A05")],
        "paths-not-a-list": [sl("a", ["x"], agent="A05"), sl("b", "y", agent="A05")],
        "path-not-text": [sl("a", ["x"], agent="A05"), sl("b", [5], agent="A05")],
        "path-blank": [sl("a", ["x"], agent="A05"), sl("b", ["  \t"], agent="A05")],
        "path-absolute": [sl("a", ["x"], agent="A05"), sl("b", ["/etc"], agent="A05")],
        "path-home": [sl("a", ["x"], agent="A05"), sl("b", ["~/x"], agent="A05")],
        "path-home-after-strip": [sl("a", ["x"], agent="A05"), sl("b", ["  ~x"], agent="A05")],
        "path-backslash-absolute": [sl("a", ["x"], agent="A05"), sl("b", ["\\root"], agent="A05")],
        "path-dotdot": [sl("a", ["x"], agent="A05"), sl("b", ["a/../b"], agent="A05")],
        "path-only-dots": [sl("a", ["x"], agent="A05"), sl("b", ["./."], agent="A05")],
        "path-only-slashes-is-absolute": [sl("a", ["x"], agent="A05"), sl("b", ["//"], agent="A05")],
        "path-quote-in-message": [sl("a", ["x"], agent="A05"), sl("b", ["/it's"], agent="A05")],
        "path-normalised": [sl("a", [" .\\src\\\\a/./b// "], agent="A05"), sl("b", ["src/ab", "src/a/c"], agent="A05")],
        "path-repeated-after-normalising": [sl("a", ["src/a", "src//a/"], agent="A05"), sl("b", ["y"], agent="A05")],
        "overlap-equal": [sl("a", ["src"], agent="A05"), sl("b", ["src"], agent="A05")],
        "overlap-prefix-directory": [sl("a", ["src"], agent="A05"), sl("b", ["src/deep/file.py"], agent="A05")],
        "overlap-reverse-order": [sl("a", ["src/deep"], agent="A05"), sl("b", ["src"], agent="A05")],
        "overlap-not-a-prefix-directory": [sl("a", ["src/a"], agent="A05"), sl("b", ["src/ab"], agent="A05")],
        "overlap-first-found-in-order": [sl("a", ["x", "y"], agent="A05"), sl("b", ["z"], agent="A05"), sl("c", ["z/q", "x/w"], agent="A05")],
        "agent-and-capability-missing": [sl("a", ["x"], agent="A05"), sl("b", ["y"])],
        "agent-unknown": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="nobody")],
        "agent-unknown-quote": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="it's")],
        "agent-unknown-both-quotes": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="it's \"q\"")],
        "agent-unknown-control-chars": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="a\tb\x01")],
        "agent-not-text-number": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent=9)],
        "agent-not-text-list": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent=["A05"])],
        "agent-falsy-falls-to-capability": [sl("a", ["x"], agent="", capability="code.backend"), sl("b", ["y"], agent=0, capability="code.frontend")],
        "agent-is-control": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A01")],
        "agent-is-gate": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A08")],
        "agent-is-release": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A12")],
        "capability-only-gate-agent": [sl("a", ["x"], agent="A05"), sl("b", ["y"], capability="gate.quality")],
        "capability-unknown": [sl("a", ["x"], agent="A05"), sl("b", ["y"], capability="no.such")],
        "capability-not-offered-by-agent": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A05", capability="req.spec")],
        "capability-number-with-agent-uses-default": [sl("a", ["x"], agent="A05", capability=5), sl("b", ["y"], agent="A06", capability="")],
        "capability-number-alone": [sl("a", ["x"], agent="A05"), sl("b", ["y"], capability=5)],
        "default-capability-every-lane-agent": [sl(f"a{i}", [f"p{i}"], agent=f"A{i:02d}") for i in
                                                 (2, 3, 4, 5, 6, 7, 11, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30)],
        "agent-by-code-and-slug": [sl("a", ["x"], agent="BE"), sl("b", ["y"], agent="a06-frontend"), sl("c", ["z"], agent="a15")],
        "title-not-text": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A05", title=5)],
        "title-blank": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A05", title="   ")],
        "title-falsy-uses-default": [sl("a", ["x"], agent="A05", title=""), sl("b", ["y"], agent="A05", title=None), sl("c", ["z"], agent="A05", title=0)],
        "title-stripped": [sl("a", ["x"], agent="A05", title="\u00a0\u3000padded\x1f\x1c "), sl("b", ["y"], agent="A05", title="\u200bkept\u200b")],
        "title-list": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A05", title=["t"])],
        "acceptance-not-a-list": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A05", acceptance="text")],
        "acceptance-item-not-text": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A05", acceptance=["ok", 5])],
        "acceptance-item-empty": [sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="A05", acceptance=["ok", ""])],
        "acceptance-falsy-uses-default": [sl("a", ["x"], agent="A05", acceptance=[]), sl("b", ["y"], agent="A05", acceptance=None)],
        "acceptance-own": [sl("a", ["x"], agent="A05", acceptance=["one", "caf\u00e9"]), sl("b", ["y"], agent="A05")],
        "extra-keys-ignored": [sl("a", ["x"], agent="A05", owner="me"), sl("b", ["y"], agent="A05")],
        "non-ascii-paths-and-title": [sl("a", ["src/caf\u00e9"], agent="A05", title="d\u00e9j\u00e0 vu"), sl("b", ["\u4e2d\u6587/x"], agent="A05")],
        "unicode-whitespace-edge-path": [sl("a", ["\u00a0src/a\u3000"], agent="A05"), sl("b", ["\x1fsrc/b\x1c"], agent="A05")],
    }
    for label, slices in errors.items():
        with world(clock) as w:
            yield w.call(f"parallel-slices-{label}", "parallel-slices", ["--slices-json", json.dumps(slices), *brief_args()])
    declines: dict[str, tuple[Any, str]] = {
        "id-not-ascii": ([sl("a", ["x"], agent="A05"), sl("caf\u00e9", ["y"], agent="A05")], "non-ascii-repr"),
        "path-absolute-not-ascii": ([sl("a", ["x"], agent="A05"), sl("b", ["/caf\u00e9"], agent="A05")], "non-ascii-repr"),
        "path-dotdot-not-ascii": ([sl("a", ["x"], agent="A05"), sl("b", ["\u00e9/.."], agent="A05")], "non-ascii-repr"),
        "agent-not-ascii-kelvin": ([sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="\u212a")], "non-ascii-repr"),
        "agent-not-ascii-unknown": ([sl("a", ["x"], agent="A05"), sl("b", ["y"], agent="caf\u00e9")], "non-ascii-repr"),
        "id-list-not-ascii": ([sl("a", ["x"], agent="A05"), sl(["\u00e9"], ["y"], agent="A05")], "non-ascii-repr"),
    }
    for label, (slices, why) in declines.items():
        with world(clock) as w:
            yield w.call(f"parallel-slices-declined-{label}", "parallel-declined", ["--slices-json", json.dumps(slices), *brief_args()],
                         rust="not_started", why=why)


def _probe(clock: base._Clock) -> Iterator[Row]:
    """A few small cases that touch every table the planner reads; the freshness test regenerates only these to prove a rule change shows."""
    with world(clock) as w:
        yield w.call("probe-hotfix-medium", "probe", [*HOT, "--priority", "P0", "--acceptance", "fast", *brief_args()])
    with world(clock) as w:
        plan = {"tasks": [{"id": "a", "capability": "req.spec"}, {"id": "g", "capability": "gate.review", "depends_on": ["a"],
                                                                   "gates": {"gate": "review", "for": ["a"]}}]}
        yield w.call("probe-custom-gates", "probe", ["--plan", w.file("p.json", json.dumps(plan)), "--risk-class", "high"])
    with world(clock) as w:
        yield w.call("probe-parallel-high", "probe", ["--risk-class", "high", *brief_args(fence(S3))])
    with world(clock) as w:
        yield w.call("probe-overlap-refused", "probe", ["--slices-json", json.dumps([sl("a", ["src"], agent="A05"), sl("b", ["src/x"], agent="A05")]),
                                                         *brief_args()])
    with world(clock) as w:
        args = [*HOT, "--prefix", "TP", *brief_args()]
        w.call("seed", "probe", args, at=EARLIER, record=False)
        yield w.call("probe-reuse", "probe", args, corr=CORR_B)
    with world(clock) as w:
        yield w.call("probe-dry-run-web3", "probe", ["--dry-run", "--pattern", "web3", *brief_args()])


# ---- units ------------------------------------------------------------------------------------------------------------------
def units() -> Row:
    """Tables for the Python-compatibility helpers of the Rust module, from the real Python."""
    space = [c for c in range(0x110000) if not 0xD800 <= c < 0xE000 and chr(c).isspace()]
    sha_inputs = [b"", b"abc", b"a" * 55, b"a" * 56, b"a" * 57, b"a" * 63, b"a" * 64, b"a" * 65, b"a" * 119, b"a" * 120, b"a" * 121,
                  b"x" * 1000, "caf\u00e9 \u4e2d\u6587 \U0001f680".encode(), bytes(range(256)),
                  b"The quick brown fox jumps over the lazy dog", b"\x00" * 64]
    ascii_all = "".join(chr(c) for c in range(128))
    reprs = ["", "plain", "it's", 'say "hi"', "it's \"both\"", "back\\slash", "tab\tnew\nline\rret", ascii_all, "\x7f", "\x00", "caf\u00e9",
             "\u2603", "\U0001f680", "a'b", "'", '"', "'\"", "\\'", "ends\\"]
    json_values = ["null", "true", "false", "0", "-5", "12345678901234", "1.5", "-0.0", "1e22", "1e-7", "0.1", "100.0", "1e16", "123456789.123456789",
                   "[]", "{}", '[1, "a", null, [true, {"k": 1.5}]]', '{"b": 1, "a": [1, 2]}', '{"it\'s": "x"}', '"a\\"b"', "1e400", "-1e400",
                   '[{"k": "\\u00e9"}]', '{"a": {"b": {"c": []}}}']
    eq_pairs = [("1", "1.0"), ("true", "1"), ("false", "0"), ("true", "1.0"), ("1", "true"), ("2", "true"), ("0", "null"), ("null", "null"),
                ('"a"', '"a"'), ('"a"', '"b"'), ('"1"', "1"), ("[1]", "[1.0]"), ("[true]", "[1]"), ("[1, 2]", "[2, 1]"), ("[]", "[]"),
                ("[]", "{}"), ('{"a": 1, "b": 2}', '{"b": 2, "a": 1}'), ('{"a": 1}', '{"a": 1.0}'), ('{"a": 1}', '{"a": 1, "b": 2}'),
                ('{"a": 1}', '{"b": 1}'), ("9007199254740993", "9007199254740992.0"), ("9007199254740992", "9007199254740992.0"),
                ("1e300", "1e300"), ("0.5", "0.5"), ("-0.0", "0"), ("0.0", "false"), ("null", "false"), ('""', "null"),
                ("9223372036854775807", "9.223372036854775807e18"), ("-9223372036854775808", "-9.223372036854775808e18"),
                ("[[1]]", "[[1.0]]"), ('{"a": [true]}', '{"a": [1]}')]
    rework_ids = ["a.r1", "a.r12", "a.r", "a.rx", "a.R1", "r1", ".r1", "a.r1x", "a.r1\n", "a.r\n", "a.r1\n\n", "a.r1 ", "a..r1", "a.r0",
                  "a.r\u0663", "a.r\u06631", "a.r1\u0663", "x\u00e9.r1", "a.r\u00b2", "a.r\u2160", "", "\n", ".r", "a.r1.r2", "a.r2.x", "ar1", "a.r\uff11"]
    fences = ["", "no fence", "```blast-radii\n[1]\n```", "a```blast-radii x\n[1]```b", "```blast-radii\n[1]", "```blast-radii[1]```",
              "```blast-radii\n```", "```blast-radii\n[1]\n```\n```blast-radii\n[2]\n```", "```blast-radii\r\n[1]\r\n```",
              "```blast-radii\n[1]\n```blast-radii\n[2]\n```", "```blast-radii\n```blast-radii\n[2]\n```", "```blast-radii```\n[1]\n```",
              "```blast-radii ```blast-radii\nX\n```", "```json\n[1]\n```", "```blast-radiiX\n[1]\n````", "```blast-radii\n[1]\n``` ```blast-radii\n[2]\n```",
              "```blast-radii\n\n\n```x```", "x```blast-radii\n```blast-radii\n```", "``blast-radii\n[1]\n```", "```blast-radii\n\u00e9\u4e2d\n```",
              "```blast-radii\n[1]\n``````blast-radii\n[2]\n```"]
    graph_ids = [GRAPH_ID, "ut-a-00000000", "ut-0-ffffffff", "ut-abc-0123abcg", "ut-abc-0123ABCD", "ut-ABC-0123abcd", "ut--0123abcd", "ut-a-b-0123abcd",
                 "ut-abc-0123abc", "ut-abc-0123abcde", "", "ut-", "ut-abc", "xut-abc-0123abcd", GRAPH_ID + "\n", "ut-ab_c-0123abcd", "ut-\u00e9-0123abcd"]
    strips = ["", "  a  ", "\t\n a b \r\x0b\x0c", "\x1c\x1d\x1e\x1f", "\x1ca\x1f", "\u00a0\u2003a\u3000", "\u200ba\u200b", "\u0085a\u0085", "\u180ea\u180e",
              "\u2028x\u2029", "\ufeffa", "a b", "\u00e9 ", "\u1680x\u202f\u205f"]
    return {
        "isspace": space,
        "sha256": [{"hex": data.hex(), "digest": hashlib.sha256(data).hexdigest()} for data in sha_inputs],
        "repr_strings": [{"text": text, "repr": repr(text)} for text in reprs],
        "repr_values": [{"json": text, "repr": repr(json.loads(text))} for text in json_values],
        "eq": [{"left": a, "right": b, "equal": json.loads(a) == json.loads(b)} for a, b in eq_pairs],
        "rework_suffix": [{"id": i, "matches": re.search(r"\.r\d+$", i) is not None} for i in rework_ids],
        "fence": [{"brief": b, "blocks": blast_radius._FENCE.findall(b)} for b in fences],
        "graph_id": [{"text": t, "valid": substrate_tee.is_graph_id(t)} for t in graph_ids],
        "strip": [{"text": t, "stripped": t.strip()} for t in strips],
        "derived_prefix": [{"corr": c, "four": derived(c, 4), "six": derived(c, 6)}
                           for c in (CORR_A, "ab-cd-ef-gh-ij", "---", "a", "", "caf\u00e9-\u4e2d\u6587-xyz12345", "pinned-1")],
    }


def constants() -> Row:
    """Python's tables the Rust module must equal."""
    patterns: Row = {}
    for name, rows in orch_plan.PATTERNS.items():
        patterns[name] = [list(orch_plan._as_row(row))[:8] for row in rows]
    return {
        "now": NOW, "created_at_step": CREATED_AT_STEP, "brief_excerpt_chars": 2000, "max_slices": blast_radius._MAX_SLICES,
        "patterns": patterns, "pattern_names": list(orch_plan.PATTERNS),
        "budgets": {risk: orch_plan._budget(risk) for risk in taskstore.GATES_BY_RISK}, "gates_by_risk": taskstore.GATES_BY_RISK,
        "lane_agents": sorted(blast_radius._LANE_AGENTS), "default_capability": dict(sorted(blast_radius._DEFAULT_CAPABILITY.items())),
        "reserved": sorted(blast_radius._RESERVED),
        "gate_rows": {name: [list(row[:4]), row[4]] for name, row in blast_radius._GATE_ROWS.items()},
        "js_safe_int": orch_plan._JS_SAFE_INT, "max_rework_loops": taskstore.MAX_REWORK_LOOPS,
        "satisfied_states": sorted(taskstore.SATISFIED_STATES),
    }


def roster() -> list[Row]:
    return [{"id": a["id"], "code": a["code"], "slug": a["slug"], "capabilities": list(a["capabilities"])} for a in manifest.load_manifest()]


def build_cases(only: tuple[str, ...] | None = None) -> list[Row]:
    """Runs every case against the real script and returns the recorded cases in a fixed order; `only` names the groups to run."""
    clock = base._Clock()
    groups: tuple[Scenario, ...] = (_patterns, _briefs, _dry_runs, _prefixes, _reuse, _graph, _signing, _custom, _parallel, _probe)
    cases: list[Row] = []
    with base.seeded(clock):
        for group in groups:
            if only is None or group.__name__ in only:
                cases.extend(group(clock))
    names = [case["name"] for case in cases]
    repeated = sorted({name for name in names if names.count(name) > 1})
    assert not repeated, f"duplicate case names: {repeated}"
    return cases


def render() -> str:
    """The committed file text: constants, units and the roster first, then one case per line."""
    lines = ",\n".join("  " + json.dumps(case) for case in build_cases())
    head = json.dumps({"schema": SCHEMA, "constants": constants(), "units": units(), "agents": roster()})
    return head[:-1] + ',\n "cases": [\n' + lines + "\n ]\n}\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = VECTOR_PATH.read_text(encoding="utf-8") if VECTOR_PATH.exists() else ""
        print("up to date" if current == text else "STALE: rerun without --check")
        return 0 if current == text else 1
    VECTOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTOR_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {VECTOR_PATH.relative_to(REPO)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
