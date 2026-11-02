"""U5 parity: the error, retry and wire vectors through swarm._native (Phase 11).

Each vector family runs twice. The pure-Python leg runs in a child process with
SWARM_PURE_PYTHON=1, so the extension is never loaded there. The native leg runs in a second child
process that imports swarm._native. Each leg is its own process, so neither can see the other's
module state, and the two outputs are compared.

Scope is the bound surface only (11-CONTEXT.md): the error codes, the retry policy and the
orch_status wire. The envelope, gates and clock vectors move to Phase 12.

The retry contract is the Rust default (gsd-failure), by the user's decision on 2026-10-25: the
Python retry module follows it, so the retryable set and the delays must match exactly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]

ERROR_INPUTS = [
    "E-INPUT",
    "E-TIMEOUT",
    "E-DEP",
    "E-CAPACITY",
    "E-CONTRACT",
    "E-POLICY",
    "E-INTERNAL",
    "e-input",
    "E-UNKNOWN",
    "",
    " E-INPUT",
    "E-INPUT\n",
]

# name, orch_status argv. Both legs run the same three vectors on the same empty store.
WIRE_CASES = [
    ["list", ["--json"]],
    ["history_unknown_task", ["--history", "T-9999", "--json"]],
    ["history_malformed_id", ["--history", "../escape", "--json"]],
]

# The task id each refused vector names. Neither side may echo it (decision 2026-10-25, F17).
REFUSED_IDS = {"history_unknown_task": "T-9999", "history_malformed_id": "../escape"}

_ERROR_CHILD = r"""
import json, sys
repo, leg, inputs = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.insert(0, repo)
if leg == "pure":
    from swarm.errors import ErrorCode
    wire = [code.value for code in ErrorCode]

    def code_from_wire(text):
        return ErrorCode(text).value
else:
    from swarm._native_loader import load_native
    native = load_native()
    wire = list(native.wire_codes())
    code_from_wire = native.code_from_wire
results = []
for text in inputs:
    try:
        results.append([text, code_from_wire(text)])
    except ValueError:
        results.append([text, "ValueError"])
print(json.dumps({"native_loaded": "swarm._native" in sys.modules, "wire_codes": wire,
                  "code_from_wire": results}))
"""

_RETRY_CHILD = r"""
import json, sys
repo, leg = sys.argv[1], sys.argv[2]
sys.path.insert(0, repo)
if leg == "pure":
    from swarm import retry
    view = {"retryable": sorted(retry.TRANSIENT_CODES),
            "base_delay_ms": int(retry.DEFAULT_BASE_S * 1000),
            "max_delay_ms": int(retry.DEFAULT_CAP_S * 1000)}
else:
    from swarm._native_loader import load_native
    policy = load_native().retry_policy()
    view = {"retryable": sorted(policy["retryable_error_codes"]),
            "base_delay_ms": policy["base_delay_ms"],
            "max_delay_ms": policy["max_delay_ms"],
            "max_attempts": policy["max_attempts"]}
print(json.dumps({"native_loaded": "swarm._native" in sys.modules, "view": view}))
"""

_PURE_WIRE_CHILD = r"""
import json, os, subprocess, sys
repo, data, cases = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
script = os.path.join(repo, "scripts", "orch_status.py")
env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", data),
       "SWARM_DIR": os.path.join(data, ".swarm"), "SWARM_PURE_PYTHON": "1"}
results = []
for name, argv in cases:
    proc = subprocess.run([sys.executable, script, *argv, "--root", data], cwd=data, env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    error = json.loads(proc.stdout)["error"] if proc.returncode == 2 else None
    results.append({"name": name, "exit": proc.returncode,
                    "error_code": error["code"] if error else None,
                    "message": error["message"] if error else None,
                    "stdout": None if error else proc.stdout})
print(json.dumps({"results": results}))
"""

_NATIVE_WIRE_CHILD = r"""
import json, os, sys
repo, data, cases = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.path.insert(0, repo)
from swarm._native_loader import load_native
native = load_native()
swarm_dir = os.path.join(data, ".swarm")
snapshot = {"agent": "A01", "script": "orch_status", "correlation": None,
            "db_path": os.path.join(swarm_dir, "tasks.db"), "swarm_dir": swarm_dir,
            "events": 0, "tasks": [], "verdicts": []}
requests = {
    "list": {**snapshot, "mode": "list"},
    "history_unknown_task": {**snapshot, "mode": "history",
                             "history": {"task_id": "T-9999", "task": None, "transitions": []}},
    "history_malformed_id": {**snapshot, "mode": "history",
                             "history": {"task_id": "../escape", "task": None,
                                         "transitions": []}},
}
results = []
for name, _ in cases:
    request = json.dumps(requests[name]).encode("utf-8")
    try:
        reply = native.decode_reply_wire(native.orch_status_invoke(request))
    except ValueError as exc:
        results.append({"name": name, "exit": 2, "error_code": str(exc).split(":", 1)[0],
                        "message": str(exc), "stdout": None})
        continue
    results.append({"name": name, "exit": reply["exit_code"], "error_code": None,
                    "message": None, "stdout": reply["stdout"]})
print(json.dumps({"native_loaded": "swarm._native" in sys.modules, "results": results}))
"""


def _leg(code: str, *args: str, pure: bool) -> dict[str, Any]:
    """Runs one child process and returns its JSON. A child failure is a test failure."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", str(REPO))}
    if pure:
        env["SWARM_PURE_PYTHON"] = "1"
    proc = subprocess.run([sys.executable, "-c", code, *args], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _retry_views() -> tuple[dict[str, Any], dict[str, Any]]:
    pure = _leg(_RETRY_CHILD, str(REPO), "pure", pure=True)
    native = _leg(_RETRY_CHILD, str(REPO), "native", pure=False)
    assert pure["native_loaded"] is False
    assert native["native_loaded"] is True
    return pure["view"], native["view"]


def test_error_codes_match_the_pure_python_oracle() -> None:
    pure = _leg(_ERROR_CHILD, str(REPO), "pure", json.dumps(ERROR_INPUTS), pure=True)
    native = _leg(_ERROR_CHILD, str(REPO), "native", json.dumps(ERROR_INPUTS), pure=False)

    assert pure["native_loaded"] is False
    assert native["native_loaded"] is True
    assert native["wire_codes"] == pure["wire_codes"]
    assert native["code_from_wire"] == pure["code_from_wire"]


def test_retryable_set_matches_the_pure_python_oracle() -> None:
    pure_view, native_view = _retry_views()

    assert native_view["retryable"] == pure_view["retryable"]


def test_retry_delays_match_the_pure_python_oracle() -> None:
    pure_view, native_view = _retry_views()

    assert native_view["base_delay_ms"] == pure_view["base_delay_ms"]
    assert native_view["max_delay_ms"] == pure_view["max_delay_ms"]


def test_wire_responses_match_the_pure_python_oracle(tmp_path: Path) -> None:
    data = str(tmp_path.resolve())
    cases = json.dumps(WIRE_CASES)
    pure = _leg(_PURE_WIRE_CHILD, str(REPO), data, cases, pure=True)["results"]
    native_leg = _leg(_NATIVE_WIRE_CHILD, str(REPO), data, cases, pure=False)
    assert native_leg["native_loaded"] is True
    native = native_leg["results"]

    assert [answer["name"] for answer in native] == [name for name, _ in WIRE_CASES]
    by_name = {oracle["name"]: oracle for oracle in pure}
    for answer in native:
        oracle = by_name[answer["name"]]
        if answer["error_code"] is None:
            # The list reply is compared byte for byte.
            assert oracle["exit"] == 0 and answer["exit"] == 0, answer["name"]
            assert answer["stdout"] == oracle["stdout"], answer["name"]
            continue
        # A refusal must agree on the code, and neither side may echo the id it refused. The texts
        # are not identical: the wire wraps its message with the operation name and checks a
        # malformed id before it reads the store.
        assert oracle["exit"] == 2, answer["name"]
        assert oracle["error_code"] == answer["error_code"], answer["name"]
        refused = REFUSED_IDS[answer["name"]]
        assert refused not in oracle["message"], answer["name"]
        assert refused not in answer["message"], answer["name"]
