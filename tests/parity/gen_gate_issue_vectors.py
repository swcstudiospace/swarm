"""Oracle vectors for the gate-issue tail (`swarm/verdicts.py::issue_gate`), replayed by `crates/gsd-taskwriter/tests/gate_issue.rs`.

Each case runs the REAL `issue_gate` (with `swarm.gates.make_verdict`, `swarm.envelope.sign_envelope`, `TaskStore.record_verdict` and
`TaskStore.append_feedback`) over a Task Store seeded through the real `TaskStore`, under one fake clock and a pinned id source, and records
what Python did: the verdict file, the returned envelopes, the events (`ctx.emit` and the `security.dev_key` audit event), the rows of
`tasks`, `transitions` and `verdicts` afterwards, and the exception when it raised. The Rust derivation and applier must produce the same
bytes: identical canonical bytes give an identical `sig` for both HMAC and Ed25519, so the whole envelope is compared, not a masked copy.

Keys. The throwaway keys are made at run time from fixed ASCII phrases below. They are public test data, not secrets: no deployment ever
holds them, and the committed file carries the placeholders `@hmac` and `@ed25519` where an environment value is a key, never the key or
the seed. The signatures in the file reveal nothing about either. The Rust test re-derives the same keys from the same phrases, which is
what lets it compare `sig` values. (The Ed25519 seed is the 32 ASCII bytes of its phrase, so neither language needs a hash to derive it.)

`python3 tests/parity/gen_gate_issue_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from time import struct_time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import swarm.runlog  # noqa: E402
from swarm.paths import swarm_dir  # noqa: E402
from swarm.errors import SwarmError  # noqa: E402
from swarm.script_base import Ctx  # noqa: E402
from swarm.taskstore import TaskStore  # noqa: E402
from swarm.verdicts import issue_gate  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-taskwriter" / "tests" / "vectors" / "gate_issue.json"
SCHEMA = "gsd-taskwriter.gate-issue-vectors.v1"
CREATE_AT = 1_700_000_000.5
NOW = 1_700_000_123.456789

# Throwaway test data (see the module docstring). The seed phrase is exactly 32 ASCII characters.
HMAC_PHRASE = "gate-issue-vectors-throwaway-hmac"
ED25519_SEED_PHRASE = "gate-issue-vectors-throwaway-key"
assert len(ED25519_SEED_PHRASE) == 32
PLACEHOLDERS = {"@hmac": HMAC_PHRASE, "@ed25519": ED25519_SEED_PHRASE.encode().hex()}

ENV_NAMES = (
    "SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_REQUIRE_KEY",
    "SWARM_AGENT_SESSION", "SWARM_DRYRUN_FAIL", "SWARM_DIR", "SWARM_TASKSTORE_WAL", "SWARM_CHILD",
)
KEY_SETS: dict[str, dict[str, str]] = {
    "none": {},
    "hmac": {"SWARM_SIGNING_KEY": "@hmac"},
    "ed25519": {"SWARM_ED25519_KEY": "@ed25519"},
    "both": {"SWARM_SIGNING_KEY": "@hmac", "SWARM_ED25519_KEY": "@ed25519"},
    "dev": {"SWARM_ALLOW_INSECURE_DEV_KEY": "1"},
    "hmac_dev": {"SWARM_SIGNING_KEY": "@hmac", "SWARM_ALLOW_INSECURE_DEV_KEY": "1"},
    "dev_literal": {"SWARM_SIGNING_KEY": "dev-insecure-key", "SWARM_ALLOW_INSECURE_DEV_KEY": "1"},
    "bad_ed25519": {"SWARM_ED25519_KEY": "not-a-seed-throwaway"},
    "require_none": {"SWARM_REQUIRE_KEY": "1"},
    "require_hmac": {"SWARM_REQUIRE_KEY": "1", "SWARM_SIGNING_KEY": "@hmac"},
}

Row = dict[str, Any]


@dataclass
class Seed:
    """One task row, made through `TaskStore.create` and then put in `state`."""

    task_id: str
    correlation_id: str
    state: str = "IN_PROGRESS"
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class Spec:
    """One case: the seeded tasks, the environment and the `issue_gate` arguments."""

    case_id: str
    group: str
    keys: str = "hmac"
    env: dict[str, str] = field(default_factory=dict)
    seeds: list[Seed] = field(default_factory=list)
    gate: str = "quality"
    agent_id: str = "A08@local"
    findings: list[Any] = field(default_factory=list)
    runs: dict[str, Any] = field(default_factory=dict)
    expires_s: int = 86400
    extra: dict[str, Any] | None = None
    simulate: bool = False
    per_target: dict[str, list[Any]] | None = None
    task_id: str | None = None
    correlation_id: str | None = None
    rust_declines: bool = False  # Python answers; Rust must refuse before the first write


def finding(fid: str, severity: str, summary: str = "s", **more: Any) -> Row:
    """A finding as `make_finding` builds it."""
    base: Row = {"id": fid, "severity": severity, "kind": "functional", "summary": summary, "evidence": "", "ac_ref": None,
                 "owner_suggestion": None, "location": None}
    return {**base, **more}


def gate_task(task_id: str, corr: str, targets: list[str] | None, gate: str = "quality", **notes: Any) -> Seed:
    """A running gate task: `notes.gate`, `notes.gate_for`."""
    body: dict[str, Any] = {"gate": gate}
    if targets is not None:
        body["gate_for"] = targets
    return Seed(task_id, corr, "IN_PROGRESS", {**body, **notes})


def target(task_id: str, corr: str | None = None, **notes: Any) -> Seed:
    """A task under review."""
    return Seed(task_id, corr or f"corr-{task_id}", "IN_REVIEW", dict(notes))


MINOR = finding("F-1", "minor", "naming")
MAJOR = finding("F-2", "major", "missing test")
BLOCKER = finding("F-3", "blocker", "secret in diff", evidence="x = 1", location="a.py:3")


class _Clock:
    """Stand-in for `time.time` and `time.gmtime()` with no argument."""

    def __init__(self) -> None:
        self.now = CREATE_AT
        self._gmtime = time.gmtime

    def time(self) -> float:
        return self.now

    def gmtime(self, seconds: float | None = None) -> struct_time:
        return self._gmtime(self.now if seconds is None else seconds)


class _Ids:
    """Pinned `_uuid7()` and `uuid.uuid4()`; records what was drawn, in order."""

    def __init__(self) -> None:
        self.msg: list[str] = []
        self.uuid4: list[str] = []

    def uuid7(self) -> str:
        text = f"0000{len(self.msg):04x}-0000-7000-8000-00000000{len(self.msg) + 0xa1:04x}"
        self.msg.append(text)
        return text

    def draw4(self) -> uuid.UUID:
        value = uuid.UUID(int=(0x4 << 76) | (0x2 << 62) | (0xC0FFEE << 32) | len(self.uuid4) + 1)
        self.uuid4.append(value.hex)
        return value


@contextmanager
def _patched(clock: _Clock, ids: _Ids, events: list[Row]) -> Iterator[None]:
    import swarm.envelope as envelope

    saved = (time.time, time.gmtime, envelope._uuid7, uuid.uuid4, swarm.runlog.emit)

    def emit(event_type: str, payload: Any, *, source: str, correlation_id: str | None = None, task_id: str | None = None,
             root: Any = None, tee: bool = True) -> Row:
        events.append({"name": event_type, "payload": payload, "origin": "envelope" if source == "swarm.envelope" else source,
                       "correlation_id": correlation_id, "task_id": task_id})
        return {}

    time.time, time.gmtime = clock.time, clock.gmtime  # type: ignore[assignment]
    envelope._uuid7, uuid.uuid4, swarm.runlog.emit = ids.uuid7, ids.draw4, emit
    try:
        yield
    finally:
        time.time, time.gmtime, envelope._uuid7, uuid.uuid4, swarm.runlog.emit = saved


def _rows(conn: sqlite3.Connection, table: str) -> list[Row]:
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def _dump(root: Path) -> Row:
    conn = sqlite3.connect(swarm_dir(root) / "tasks.db")
    try:
        return {name: _rows(conn, name) for name in ("tasks", "transitions", "verdicts")}
    finally:
        conn.close()


def _resolve(env: dict[str, str]) -> dict[str, str]:
    return {name: PLACEHOLDERS.get(value, value) for name, value in env.items()}


def run(spec: Spec) -> Row:
    """Runs one case against a fresh store and returns the vector record."""
    env = {**KEY_SETS[spec.keys], **spec.env}
    saved_env = {name: os.environ.get(name) for name in (*ENV_NAMES, "HOME", "XDG_CONFIG_HOME")}
    clock, ids, events = _Clock(), _Ids(), []  # type: ignore[var-annotated]
    with tempfile.TemporaryDirectory(prefix="gate-issue-") as scratch:
        root = Path(scratch) / "repo"
        root.mkdir()
        for name in ENV_NAMES:
            os.environ.pop(name, None)
        os.environ["HOME"] = str(Path(scratch) / "home")
        os.environ.pop("XDG_CONFIG_HOME", None)
        try:
            with _patched(clock, ids, events):
                store = TaskStore(root=root)
                for seed in spec.seeds:
                    store.create(task_id=seed.task_id, correlation_id=seed.correlation_id, capability="cap", notes=seed.notes)
                    store.conn.execute("UPDATE tasks SET state=? WHERE task_id=?", (seed.state, seed.task_id))
                store.conn.commit()
                store.conn.close()
                initial = _dump(root)
                os.environ.update(_resolve(env))
                clock.now = NOW
                ctx = Ctx("A08", "gate", spec.task_id, spec.correlation_id, True, root)

                def ctx_emit(event_type: str, payload: Any, *, task_id: str | None = None) -> Row:
                    events.append({"name": event_type, "payload": payload, "origin": "script", "correlation_id": ctx.correlation_id,
                                   "task_id": task_id or ctx.task_id or payload.get("task_id")})
                    return {}

                ctx.emit = ctx_emit  # type: ignore[method-assign]
                python: Row = {}
                try:
                    envelope, recorded = issue_gate(ctx, gate=spec.gate, agent_id=spec.agent_id, findings=spec.findings, runs=spec.runs,
                                                    expires_s=spec.expires_s, extra=spec.extra, simulate=spec.simulate,
                                                    per_target=spec.per_target)
                    python["outcome"] = "ok"
                    python["envelope"] = envelope
                    python["recorded"] = [[name, env_] for name, env_ in recorded.items()]
                except Exception as exc:  # noqa: BLE001 - the oracle's own refusals are the data
                    python["outcome"] = "error"
                    python["error"] = ({"code": exc.code.value, "message": str(exc), "details": exc.details}
                                       if isinstance(exc, SwarmError) else {"type": type(exc).__name__})
                out_dir = swarm_dir(root) / "verdicts"
                files = sorted(out_dir.glob("*.json")) if out_dir.exists() else []
                assert len(files) <= 1, [f.name for f in files]
                python["file"] = {"name": files[0].name, "text": files[0].read_text()} if files else None
                python["verdicts_dir"] = out_dir.is_dir()
                python["events"] = events
                python["after"] = _dump(root)
        finally:
            for name, value in saved_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    request: Row = {"gate": spec.gate, "agent_id": spec.agent_id, "findings": spec.findings, "runs": spec.runs, "expires_s": spec.expires_s,
                    "extra": spec.extra, "simulate": spec.simulate, "per_target": spec.per_target, "task_id": spec.task_id,
                    "correlation_id": spec.correlation_id}
    return {"id": spec.case_id, "group": spec.group, "env": env, "request": request, "now": NOW, "initial": initial,
            "ids": {"msg": ids.msg, "uuid4": ids.uuid4}, "python": python,
            "rust": {"outcome": "not_started" if spec.rust_declines else "plan"}}


def specs() -> list[Spec]:
    """The corpus."""
    out: list[Spec] = []
    two = [gate_task("G1", "corr-g1", ["T-a", "T-b"]), target("T-a"), target("T-b")]

    def add(spec: Spec) -> None:
        out.append(spec)

    # --- signed verdicts, pass and fail, both signature kinds
    add(Spec("hmac-pass", "signed", seeds=list(two), task_id="G1", findings=[MINOR], runs={"pytest": {"passed": 3, "failed": 0}}))
    add(Spec("hmac-fail-feedback", "signed", seeds=list(two), task_id="G1", findings=[MAJOR], runs={"pytest": {"passed": 1, "failed": 2}}))
    add(Spec("ed25519-pass", "signed", keys="ed25519", seeds=list(two), task_id="G1", findings=[], runs={}))
    add(Spec("ed25519-fail-feedback", "signed", keys="ed25519", seeds=list(two), task_id="G1", findings=[BLOCKER, MINOR]))
    add(Spec("both-keys-ed25519-wins", "signed", keys="both", seeds=list(two), task_id="G1", findings=[MAJOR]))
    add(Spec("hmac-correlation-given-and-own", "signed", seeds=list(two), task_id="G1", correlation_id="corr-g1", findings=[]))
    add(Spec("rework-suffix-targets", "signed", seeds=[gate_task("G1", "corr-g1", ["T-a.r1", "T-b.r2"]), target("T-a.r1"), target("T-b.r2")],
             task_id="G1", findings=[MAJOR]))
    # --- per-target findings: some targets omitted, every severity
    add(Spec("per-target-one-omitted", "per-target", seeds=list(two), task_id="G1", findings=[MINOR], per_target={"T-a": [BLOCKER]}))
    add(Spec("per-target-all-clear-file-fails", "per-target", seeds=list(two), task_id="G1", findings=[MAJOR],
             per_target={"T-a": [], "T-b": [MINOR]}))
    five = [f"T-{n}" for n in range(1, 6)]
    add(Spec("per-target-every-severity", "per-target", keys="ed25519", seeds=[gate_task("G1", "corr-g1", five), *map(target, five)],
             task_id="G1", per_target={t: [finding(f"S-{t}", sev)] for t, sev in zip(five, ("info", "minor", "major", "critical", "blocker"),
                                                                                    strict=True)}))
    add(Spec("per-target-key-order-is-kept", "per-target", seeds=list(two), task_id="G1",
             per_target={"T-b": [MAJOR], "T-a": [MINOR]}, extra={"zeta": 1, "alpha": [1, 2, {"b": 1, "a": 2}]}))
    # --- extra fields, runs, text, expiry
    add(Spec("extra-and-runs", "payload", seeds=list(two), task_id="G1", findings=[MINOR], agent_id="A09@local", gate="review",
             extra={"mode": "rules-only", "diff_base": "HEAD~1", "stats": {"files": 3, "ratio": 0.5, "big": 1e22}},
             runs={"git": {"cmd": ["git", "diff"], "rc": 0}}))
    out[-1].seeds = [gate_task("G1", "corr-g1", ["T-a", "T-b"], gate="review"), target("T-a"), target("T-b")]
    add(Spec("non-ascii-text", "payload", seeds=list(two), task_id="G1", agent_id="A08@ünï",
             findings=[finding("U-1", "major", "naïve ✓ 😀 \"quoted\" \\ back\nline", evidence="tab\there")], runs={"note": "é"}))
    for name, expires in (("zero", 0), ("negative", -1), ("two-days", 172_800), ("long-lived", 10**12), ("huge", 2**62)):
        add(Spec(f"expires-{name}", "expiry", seeds=list(two), task_id="G1", findings=[MAJOR], expires_s=expires))
    for gate in ("review", "security", "release"):
        add(Spec(f"gate-{gate}", "gates", seeds=[gate_task("G1", "corr-g1", ["T-a"], gate=gate), target("T-a")], task_id="G1", gate=gate,
                 findings=[MAJOR], agent_id="A12@local"))
    # --- no store, non-gate tasks, empty gate_for
    add(Spec("no-task-id", "no-store", findings=[MINOR], correlation_id="corr-given"))
    add(Spec("no-task-id-no-correlation", "no-store", findings=[]))
    add(Spec("non-gate-task", "not-a-gate-task", seeds=[target("T-a")], task_id="T-a", findings=[MAJOR]))
    add(Spec("unknown-task", "not-a-gate-task", task_id="T-none", findings=[]))
    add(Spec("empty-gate-for", "not-a-gate-task", seeds=[gate_task("G1", "corr-g1", [])], task_id="G1", findings=[MAJOR]))
    add(Spec("no-gate-for", "not-a-gate-task", seeds=[gate_task("G1", "corr-g1", None)], task_id="G1", findings=[]))
    add(Spec("gate-task-without-targets-not-leased", "not-a-gate-task", seeds=[Seed("G1", "corr-g1", "PLANNED", {"gate": "quality", "gate_for": []})],
             task_id="G1"))
    # --- refusals Python raises as SwarmError
    add(Spec("foreign-correlation", "refusal", seeds=list(two), task_id="G1", correlation_id="corr-other", findings=[MAJOR]))
    add(Spec("foreign-correlation-quote", "refusal", seeds=[gate_task("G1", "it's-own", ["T-a"]), target("T-a")], task_id="G1",
             correlation_id="corr-other"))
    add(Spec("foreign-correlation-non-ascii", "refusal", seeds=[gate_task("G1", "côrr", ["T-a"]), target("T-a")], task_id="G1",
             correlation_id="corr-other", rust_declines=True))
    add(Spec("wrong-gate-task", "refusal", seeds=[gate_task("G1", "corr-g1", ["T-a"], gate="review"), target("T-a")], task_id="G1"))
    add(Spec("gate-task-not-running", "refusal", seeds=[Seed("G1", "corr-g1", "CLAIMED", {"gate": "quality", "gate_for": ["T-a"]}), target("T-a")],
             task_id="G1"))
    add(Spec("unknown-target", "refusal", keys="dev", seeds=[gate_task("G1", "corr-g1", ["T-a", "T-missing"]), target("T-a")], task_id="G1",
             findings=[MAJOR]))
    add(Spec("unknown-target-first", "refusal", seeds=[gate_task("G1", "corr-g1", ["T-missing", "T-a"]), target("T-a")], task_id="G1"))
    # --- signing configuration
    add(Spec("config-bad-ed25519", "keys", keys="bad_ed25519", seeds=list(two), task_id="G1", findings=[MAJOR]))
    add(Spec("config-require-no-key", "keys", keys="require_none", seeds=list(two), task_id="G1"))
    add(Spec("config-bad-ed25519-no-task", "keys", keys="bad_ed25519"))
    add(Spec("config-session-key-file", "keys", keys="none", env={"SWARM_AGENT_SESSION": "1", "SWARM_ED25519_KEY_FILE": "/nonexistent/throwaway"},
             seeds=list(two), task_id="G1"))
    add(Spec("require-with-hmac-signs", "keys", keys="require_hmac", seeds=list(two), task_id="G1", findings=[MAJOR]))
    # --- the implicit dev key (security.dev_key events)
    add(Spec("dev-key-signs-and-audits", "dev-key", keys="dev", seeds=list(two), task_id="G1", findings=[MAJOR]))
    add(Spec("dev-key-no-store", "dev-key", keys="dev", findings=[]))
    add(Spec("dev-key-session-no-audit", "dev-key", keys="dev", env={"SWARM_AGENT_SESSION": "1"}, seeds=list(two), task_id="G1"))
    add(Spec("hmac-beside-dev-opt-in", "dev-key", keys="hmac_dev", seeds=list(two), task_id="G1", findings=[MAJOR]))
    add(Spec("dev-literal-as-signing-key", "dev-key", keys="dev_literal", seeds=list(two), task_id="G1", findings=[]))
    # --- keyless advisory
    add(Spec("advisory-pass", "advisory", keys="none", seeds=list(two), task_id="G1", findings=[MINOR]))
    add(Spec("advisory-fail", "advisory", keys="none", seeds=list(two), task_id="G1", findings=[MAJOR]))
    add(Spec("advisory-per-target-blocks-file", "advisory", keys="none", seeds=list(two), task_id="G1", findings=[],
             per_target={"T-a": [BLOCKER], "T-b": []}, extra={"mode": "rules-only"}))
    add(Spec("advisory-per-target-unknown-uses-file-correlation", "advisory", keys="none", seeds=list(two), task_id="G1", correlation_id="corr-g1",
             per_target={"T-zz": [MAJOR]}))
    add(Spec("advisory-per-target-own-correlation", "advisory", keys="none", seeds=[gate_task("G1", "corr-g1", ["T-a"]), target("T-a", "corr-ta")],
             task_id="G1", per_target={"T-a": [MINOR]}))
    add(Spec("advisory-no-task-id", "advisory", keys="none", findings=[MAJOR], per_target={"T-a": [MINOR]}))
    add(Spec("advisory-session", "advisory", keys="none", env={"SWARM_AGENT_SESSION": "1"}, seeds=list(two), task_id="G1", findings=[],
             per_target={"T-a": [MAJOR]}))
    add(Spec("advisory-simulate", "advisory", keys="none", seeds=list(two), task_id="G1", simulate=True, findings=[]))
    add(Spec("advisory-foreign-correlation", "advisory", keys="none", seeds=list(two), task_id="G1", correlation_id="corr-other"))
    # --- agent session
    add(Spec("session-pass", "session", env={"SWARM_AGENT_SESSION": "1"}, seeds=list(two), task_id="G1", findings=[MINOR]))
    add(Spec("session-per-target-forces-fail", "session", keys="ed25519", env={"SWARM_AGENT_SESSION": "1"}, seeds=list(two), task_id="G1",
             findings=[], per_target={"T-a": [MAJOR]}))
    add(Spec("session-no-task", "session", env={"SWARM_AGENT_SESSION": "1"}, findings=[]))
    # --- simulate / dry-run
    runner = [gate_task("G1", "corr-g1", ["T-a", "T-b"], dry_run=True), target("T-a"), target("T-b")]
    add(Spec("simulate-runner-dry-run-pass", "simulate", seeds=list(runner), task_id="G1", simulate=True, findings=[], agent_id="A08@dry"))
    add(Spec("simulate-runner-dry-run-sim-fail", "simulate", seeds=list(runner), task_id="G1", simulate=True, findings=[],
             env={"SWARM_DRYRUN_FAIL": "T-a:quality"}, agent_id="A08@dry"))
    add(Spec("simulate-fail-list-spaces-and-other-gates", "simulate", keys="ed25519", seeds=list(runner), task_id="G1", simulate=True,
             findings=[MINOR], env={"SWARM_DRYRUN_FAIL": " T-b:review , T-b:quality ,, junk "}))
    add(Spec("simulate-caller-findings-fail", "simulate", seeds=list(runner), task_id="G1", simulate=True, findings=[MAJOR],
             env={"SWARM_DRYRUN_FAIL": "T-a:quality"}))
    add(Spec("simulate-not-a-runner-dry-run", "simulate", seeds=list(two), task_id="G1", simulate=True, findings=[]))
    add(Spec("simulate-no-task-id", "simulate", simulate=True, findings=[], agent_id="A08@dry"))
    add(Spec("simulate-non-gate-task", "simulate", seeds=[target("T-a")], task_id="T-a", simulate=True, findings=[]))
    add(Spec("simulate-empty-gate-for", "simulate", seeds=[gate_task("G1", "corr-g1", [])], task_id="G1", simulate=True))
    add(Spec("simulate-extra-dry-run-key-overridden", "simulate", seeds=list(runner), task_id="G1", simulate=True,
             extra={"dry_run": False, "k": 1}))
    add(Spec("not-simulate-extra-dry-run-kept", "simulate", seeds=list(two), task_id="G1", extra={"dry_run": "yes"}))
    add(Spec("simulate-dev-key", "simulate", keys="dev", seeds=list(runner), task_id="G1", simulate=True, env={"SWARM_DRYRUN_FAIL": "T-b:quality"}))
    # --- notes and rows
    add(Spec("existing-feedback-and-notes-order", "rows", seeds=[gate_task("G1", "corr-g1", ["T-a"]),
                                                              target("T-a", z=1, feedback=[{"gate": "review", "source": "human"}], a={"k": [1]})],
             task_id="G1", findings=[MAJOR]))
    add(Spec("duplicate-target", "rows", seeds=[gate_task("G1", "corr-g1", ["T-a", "T-a"]), target("T-a")], task_id="G1", findings=[MAJOR]))
    add(Spec("gate-task-is-its-own-target", "rows", seeds=[gate_task("G1", "corr-g1", ["G1"])], task_id="G1", findings=[MAJOR]))
    add(Spec("target-without-correlation-draws-uuid", "rows", seeds=[gate_task("G1", "corr-g1", ["T-a"]), target("T-a", "")], task_id="G1"))
    add(Spec("gate-task-without-correlation-draws-uuid", "rows", seeds=[gate_task("G1", "", ["T-a"]), target("T-a")], task_id="G1"))
    # --- per-target findings that no row records: the signed file verdict ignores them (only a session or the advisory path forces fail)
    add(Spec("no-task-id-per-target-blocks-signed-file-passes", "per-target", task_id=None, findings=[], per_target={"T-a": [BLOCKER]}))
    add(Spec("non-gate-task-per-target-blocks-signed-file-passes", "per-target", seeds=[target("T-a")], task_id="T-a", findings=[],
             per_target={"T-a": [BLOCKER]}))
    add(Spec("per-target-key-outside-gate-for-is-ignored", "per-target", seeds=list(two), task_id="G1", findings=[],
             per_target={"T-a": [MINOR], "T-zz": [BLOCKER]}))
    # --- what Rust declines (Python answers)
    add(Spec("decline-feedback-not-a-list", "decline", seeds=[gate_task("G1", "corr-g1", ["T-a"]), target("T-a", feedback="text")], task_id="G1",
             findings=[MAJOR], rust_declines=True))
    add(Spec("decline-finding-without-severity", "decline", seeds=list(two), task_id="G1", findings=[{"id": "X"}], rust_declines=True))
    add(Spec("decline-finding-bad-severity", "decline", seeds=list(two), task_id="G1", findings=[finding("X", "huge")], rust_declines=True))
    add(Spec("decline-reserved-extra-verdict", "decline", seeds=list(two), task_id="G1", extra={"verdict": "pass"}, rust_declines=True))
    add(Spec("decline-reserved-extra-task-id", "decline", seeds=list(two), task_id="G1", extra={"task_id": "other"}, rust_declines=True))
    add(Spec("decline-unknown-gate", "decline", seeds=list(two), task_id="G1", gate="nope", rust_declines=True))
    add(Spec("decline-notes-gate-not-text", "decline", seeds=[Seed("G1", "corr-g1", "IN_PROGRESS", {"gate": 7, "gate_for": ["T-a"]}), target("T-a")],
             task_id="G1", rust_declines=True))
    add(Spec("decline-gate-for-not-a-list", "decline", seeds=[Seed("G1", "corr-g1", "IN_PROGRESS", {"gate": "quality", "gate_for": "T-a"}), target("T-a")],
             task_id="G1", rust_declines=True))
    add(Spec("decline-per-target-finding-without-severity", "decline", seeds=list(two), task_id="G1", per_target={"T-a": [{"id": "X"}]},
             rust_declines=True))
    return out


def build_cases() -> list[Row]:
    """Every case, run against the oracle."""
    return [run(spec) for spec in specs()]


def render() -> str:
    """The vector file text: one case per line, member order kept (it is part of the bytes under test)."""
    lines = ",\n".join("  " + json.dumps(case) for case in build_cases())
    head = json.dumps({"schema": SCHEMA, "now": NOW, "create_at": CREATE_AT, "placeholders": sorted(PLACEHOLDERS)})
    return f'{{"head": {head}, "cases": [\n{lines}\n]}}\n'


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
