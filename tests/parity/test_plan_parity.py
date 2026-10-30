"""Parity of `orch_plan` on Python and on the host engine (phase 14.5, unit U3.7b).

Each case runs `scripts/orch_plan.py` in two scratch roots that start from the same store: once as Python, and once as the Rust
binary called the way the selector calls it (a JSON request on stdin with an `effects_path`; the exit code and the reply on stdout).

A served case must be answered by the binary and match Python: the reply (the signed kickoff's ids, clock and signature masked),
the exit code, the `tasks` and `transitions` rows (the clock columns masked), the plan file bytes and `latest_correlation`. A
declined case must come back as `E-DEP` with `write_state = "not_started"` and leave the store unchanged, so the selector hands the
call to Python. The selector's own output for every case is compared with Python's in the last test, which stays an expected failure
until `scripts/orch_plan.py` carries its selector block (orchestrator-owned). Unset `GSD_ENGINE_BIN` skips.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "orch_plan.py"
SEED = "11" * 32
CORR = "c0ffee01-aaaa-4bbb-8ccc-0000000000a1"
OTHER = "c0ffee01-aaaa-4bbb-8ccc-0000000000b2"
BRIEF = "Build the orders service.\nAcceptance: p95 under 200 ms."
CLOCK = {"created_at", "updated_at", "ts"}
MASKED_ASSIGNMENT = {"msg_id", "trace_id", "ts", "sig"}
REQUEST_ENV = ("SWARM_DIR", "SWARM_AGENT_SESSION", "SWARM_CHILD", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT")
TEMPLATE = "import sys\nfrom swarm.taskstore import TaskStore\nTaskStore(sys.argv[1]).conn.close()\n"


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        pytest.skip("GSD_ENGINE_BIN is not set")
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is not executable: {value}")
    return value


@dataclass
class Case:
    name: str
    argv: list[str]
    served: bool
    env: dict[str, str] = field(default_factory=dict)
    seed: list[list[str]] = field(default_factory=list)
    brief_crlf: bool = False
    fresh_store: bool = False  # no tasks.db: the route declines, Python creates the store


CASES = [
    Case("feature-low-brief-text", ["--correlation-id", CORR, "--brief-text", BRIEF, "--risk-class", "low"], True),
    Case("feature-default-risk-and-acceptance", ["--correlation-id", CORR, "--brief-text", BRIEF,
                                                  "--acceptance", "p95 under 200 ms", "--acceptance", "no 500s"], True),
    Case("hotfix-high-p0-explicit-prefix", ["--correlation-id", CORR, "--pattern", "hotfix", "--risk-class", "high",
                                             "--priority", "P0", "--prefix", "HF", "--brief-text", "Fix the login."], True),
    Case("dependency-medium", ["--correlation-id", CORR, "--pattern", "dependency", "--brief-text", "Bump deps."], True),
    Case("cloud-native-low", ["--correlation-id", CORR, "--pattern", "cloud-native", "--risk-class", "low",
                               "--brief-text", "Move to the cloud."], True),
    Case("cluster-high-rows", ["--correlation-id", CORR, "--pattern", "cluster", "--brief-text", "Roll out the cluster."],
         True),
    Case("web3-high-rows", ["--correlation-id", CORR, "--pattern", "web3", "--brief-text", "Ship the vault."], True),
    Case("brief-file-crlf", ["--correlation-id", CORR, "--brief", "@BRIEF"], True, brief_crlf=True),
    Case("dry-run-feature", ["--correlation-id", CORR, "--brief-text", BRIEF, "--dry-run"], True),
    Case("dry-run-without-brief-placeholder", ["--correlation-id", CORR, "--dry-run"], True),
    Case("reuse-of-the-same-plan", ["--correlation-id", CORR, "--brief-text", BRIEF], True,
         seed=[["--correlation-id", CORR, "--brief-text", BRIEF]]),
    Case("prefix-collision-refused", ["--correlation-id", CORR, "--prefix", "PC", "--brief-text", "A different brief."],
         True, seed=[["--correlation-id", OTHER, "--prefix", "PC", "--brief-text", BRIEF]]),
    Case("six-character-prefix-fallback", ["--correlation-id", CORR, "--brief-text", BRIEF], True,
         seed=[["--correlation-id", "c0ffee01-0000-4000-8000-0000000000cc", "--brief-text", "Other plan."]]),
    Case("missing-brief-refused", ["--correlation-id", CORR], True),
    Case("bad-graph-id-refused", ["--correlation-id", CORR, "--brief-text", BRIEF, "--graph-id", "nope"], True),
    Case("bad-correlation-id-refused", ["--correlation-id", "../escape", "--brief-text", BRIEF], True),
    Case("kickoff-with-real-key", ["--correlation-id", CORR, "--brief-text", BRIEF], True,
         env={"SWARM_AGENT_SESSION": "", "SWARM_ED25519_KEY": SEED}),
    Case("declined-custom-plan", ["--correlation-id", CORR, "--pattern", "custom", "--plan", "/nonexistent.json"], False),
    Case("declined-slices-json", ["--correlation-id", CORR, "--brief-text", BRIEF,
                                   "--slices-json", "[{\"name\": \"a\", \"paths\": [\"src/a\"]}]"], False),
    Case("declined-abbreviated-flag", ["--correlation-id", CORR, "--brief-text", BRIEF, "--dr"], False),
    Case("declined-bad-pattern-choice", ["--correlation-id", CORR, "--pattern", "bogus", "--brief-text", BRIEF], False),
    Case("declined-option-like-value", ["--correlation-id", CORR, "--brief-text", "-x"], False),
    Case("declined-no-store-yet", ["--correlation-id", CORR, "--brief-text", BRIEF], False, fresh_store=True),
    Case("declined-substrate-url", ["--correlation-id", CORR, "--brief-text", BRIEF], False,
         env={"SUBSTRATE_DISABLED": "", "SUBSTRATE_URL": "http://127.0.0.1:9"}),
]


def _base_env(root: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", ""), "PYTHONDONTWRITEBYTECODE": "1",
           "SWARM_DIR": str(root / ".swarm"), "SWARM_GSD_STATE_FILE": "", "SWARM_CLAUDE_STATE_DIR": str(root / "claude"),
           "SWARM_AGENT_SESSION": "1", "SUBSTRATE_DISABLED": "1"}
    env.update(extra)
    return env


def _python(root: Path, argv: list[str], extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *argv, "--json"], capture_output=True,
                          text=True, env=_base_env(root, extra), cwd=ROOT, timeout=120)


def _binary_call(binary: str, root: Path, argv: list[str], extra: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    """The request the selector sends: the same argv, the call's root as cwd, the forwarded session env, an effects path."""
    env = _base_env(root, extra)
    request = {"agent": "A01", "script": "orch_plan", "argv": [*argv, "--json"], "root": str(root),
               "env": {key: env[key] for key in REQUEST_ENV if key in env},
               "effects_path": str(root / "effects.json")}
    process = {key: value for key, value in env.items() if key != "SWARM_DIR"}
    return subprocess.run([binary], input=json.dumps(request).encode("utf-8"), capture_output=True, env=process,
                          cwd=root, timeout=120)


def _store(root: Path) -> dict[str, list[dict]]:
    db = root / ".swarm" / "tasks.db"
    if not db.exists():
        return {"tasks": [], "transitions": []}
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    out: dict[str, list[dict]] = {}
    for table, order in (("tasks", "task_id"), ("transitions", "id")):
        rows = con.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
        out[table] = [{key: ("CLOCK" if key in CLOCK else row[key]) for key in row.keys()} for row in rows]
    con.close()
    return out


def _files(root: Path) -> dict[str, object]:
    plans = root / ".swarm" / "plans"
    latest = root / ".swarm" / "latest_correlation"
    return {
        "latest": latest.read_text() if latest.exists() else None,
        "plans": {p.name: p.read_text() for p in sorted(plans.glob("*.json"))} if plans.exists() else {},
    }


def _normal(value, root: Path):
    """The reply with the scratch root replaced and the signed kickoff's ids, clock and signature masked."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "execution_assignment" and isinstance(item, dict):
                out[key] = {k: ("MASKED" if k in MASKED_ASSIGNMENT else _normal(v, root)) for k, v in item.items()}
            else:
                out[key] = _normal(item, root)
        return out
    if isinstance(value, list):
        return [_normal(item, root) for item in value]
    if isinstance(value, str):
        return value.replace(str(root), "<ROOT>")
    return value


def _seed(root: Path, case: Case, extra: dict[str, str]) -> None:
    """Both roots start from the same state: an empty Task Store made by Python's `TaskStore` (what a Python run leaves), then the
    case's own seed runs through Python. A `fresh_store` case has no store, so the route declines and Python creates it."""
    root.mkdir(parents=True, exist_ok=True)
    if case.brief_crlf:
        (root / "brief.md").write_bytes(BRIEF.replace("\n", "\r\n").encode("utf-8"))
    if case.fresh_store:
        return
    (root / ".swarm").mkdir(exist_ok=True)
    made = subprocess.run([sys.executable, "-c", TEMPLATE, str(root / ".swarm" / "tasks.db")], capture_output=True,
                          text=True, env=_base_env(root, {}), cwd=ROOT, timeout=120)
    assert made.returncode == 0, made.stderr
    for argv in case.seed:
        done = _python(root, argv, extra)
        assert done.returncode == 0, done.stdout + done.stderr


def _argv(case: Case, root: Path) -> list[str]:
    return [str(root / "brief.md") if token == "@BRIEF" else token for token in case.argv]


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_each_case_matches_python(case: Case, tmp_path: Path) -> None:
    binary = _binary()
    py_root, rs_root = tmp_path / "py", tmp_path / "rs"
    _seed(py_root, case, case.env)
    _seed(rs_root, case, case.env)
    before = _store(rs_root)
    rs = _binary_call(binary, rs_root, _argv(case, rs_root), case.env)
    if not case.served:
        assert rs.returncode == 2, rs.stdout + rs.stderr
        reply = json.loads(rs.stdout)
        assert reply["error"]["code"] == "E-DEP", reply
        assert reply["error"]["details"]["write_state"] == "not_started", reply
        assert _store(rs_root) == before, "a declined call writes nothing"
        return
    py = _python(py_root, _argv(case, py_root), case.env)
    assert rs.returncode == py.returncode, (rs.stdout + rs.stderr, py.stdout + py.stderr)
    rs_reply, py_reply = json.loads(rs.stdout), json.loads(py.stdout)
    assert rs_reply.get("error", {}).get("details", {}).get("write_state") != "not_started", rs_reply
    assert _normal(rs_reply, rs_root) == _normal(py_reply, py_root), "the reply"
    if "execution_assignment" in py_reply:
        assert rs_reply["execution_assignment_verified"] is True
        assert rs_reply["execution_assignment"]["payload"] == py_reply["execution_assignment"]["payload"]
    assert _store(rs_root) == _store(py_root), "the task and audit rows"
    assert _files(rs_root) == _files(py_root), "the plan file and latest_correlation"
    manifest = json.loads((rs_root / "effects.json").read_text())
    if rs_reply.get("status") == "ok" and "tasks" in rs_reply and not rs_reply.get("reused") and not rs_reply.get("dry_run"):
        # A created plan: `ctx.emit("plan.updated", ...)` once, then the live refresh (every plan transitions its tasks).
        assert manifest["effects"] == [{"type": "plan.updated", "payload": {"pattern": rs_reply["pattern"],
                                                                            "task_count": len(rs_reply["tasks"])},
                                        "task_id": None}], "the effects to replay"
        assert manifest["live_refresh"] is True
    else:
        assert manifest["effects"] == [] and manifest["live_refresh"] is False, "nothing to replay"


@pytest.mark.xfail(strict=True, reason="needs scripts/orch_plan.py's selector block (orchestrator-owned); remove the marker then")
def test_the_selector_output_equals_python_for_every_case(tmp_path: Path) -> None:
    binary = _binary()
    for case in CASES:
        py_root, sel_root = tmp_path / f"{case.name}-py", tmp_path / f"{case.name}-sel"
        _seed(py_root, case, case.env)
        _seed(sel_root, case, case.env)
        py = _python(py_root, _argv(case, py_root), case.env)
        env = _base_env(sel_root, {**case.env, "GSD_ENGINE": "rust", "GSD_ENGINE_BIN": binary, "GSD_RUST_BACKEND": "host"})
        sel = subprocess.run([sys.executable, str(SCRIPT), "--root", str(sel_root), *_argv(case, sel_root), "--json"],
                             capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
        assert sel.returncode == py.returncode, case.name
        assert _normal(json.loads(sel.stdout), sel_root) == _normal(json.loads(py.stdout), py_root), case.name
        assert _store(sel_root) == _store(py_root), case.name
