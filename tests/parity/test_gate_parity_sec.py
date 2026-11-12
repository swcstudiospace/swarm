"""Parity of `sec_gate` on Python and on the host engine (phase 14.5, unit U3.6e).

Each case of `gen_sec_gate_vectors.py` runs three ways: the Python script; the engine binary probed directly with the
selector's request, which answers a served call (exit 0 or 1, a `sec_gate` body) or refuses a declined one (exit 2,
`E-DEP`, `write_state = not_started`); and the selector with `GSD_ENGINE=rust`, which hands a declined call to Python.
A served cell must be answered by the binary. A declined cell must be refused by the binary and then answered by Python.
Every cell's reply bytes equal Python's once the masked envelope fields (message ids, trace, timestamps, signature) are
replaced. Recovery cells cover failed audits with no counted findings, clean controls, and length-only secret evidence.
Keyed selectors pin the binary's digest at fixture time. The secret values are generated in the run; nothing secret-like
is committed. Unset `GSD_ENGINE_BIN` skips.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SCRIPT = ROOT / "scripts" / "sec_gate.py"

sys.path.insert(0, str(HERE))

import gen_sec_gate_vectors as gen  # noqa: E402

AUDIT_RECOVERY = [
    ("npm_error_json", "npm-audit", "package.json", "npm", '{"error": {"code": "EAUDITNOLOCK"}}', 1),
    ("npm_clean", "npm-audit", "package.json", "npm", '{"metadata": {"vulnerabilities": {}}}', 0),
    ("npm_zero_counts_failure", "npm-audit", "package.json", "npm", '{"metadata": {"vulnerabilities": {}}}', 1),
    ("go_load_failure", "govulncheck", "go.mod", "govulncheck", '{"error": "package load failed"}', 1),
    ("go_clean", "govulncheck", "go.mod", "govulncheck", '{"config": {}}', 0),
    ("pip_empty_failure", "pip-audit", "requirements.txt", "pip-audit", "[]", 1),
    ("pip_clean", "pip-audit", "requirements.txt", "pip-audit", "[]", 0),
    ("cargo_zero_counts_failure", "cargo-audit", "Cargo.lock", "cargo", '{"vulnerabilities": {"count": 0}}', 1),
    ("cargo_clean", "cargo-audit", "Cargo.lock", "cargo", '{"vulnerabilities": {"count": 0}}', 0),
]
AUDIT_EXPECTATIONS = {
    f"recovery_{name}": (audit, "fail" if status else "pass")
    for name, audit, _, _, _, status in AUDIT_RECOVERY
}
RECOVERY_SECRET = "ghp_" + "".join(random.Random(20261031).choices(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=36))
CASES = gen.build_cases(random.Random(20261030)) + [
    gen.Case(
        f"recovery_{name}", True, ["--json"],
        files={manifest: ""},
        shims={
            **({"cargo-audit": "exit 0"} if audit == "cargo-audit" else {}),
            binary: f"printf '%s\\n' '{output}'\nexit {status}",
        },
    )
    for name, audit, manifest, binary, output, status in AUDIT_RECOVERY
] + [
    gen.Case("recovery_secret_length_only", True, ["--json"],
             files={"src/config.py": f'token = "{RECOVERY_SECRET}"\n'}),
]

# Fields the signed envelope derives from the clock or from random ids: replaced before the byte comparison.
MASKS = [
    (re.compile(r'"(msg_id|correlation_id|trace_id|ts|sig)": "[^"]*"'), r'"\1": "<masked>"'),
    (re.compile(r'"issued_at": [-0-9.eE+]+'), '"issued_at": <masked>'),
]


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        pytest.skip("GSD_ENGINE_BIN is not set")
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is not executable: {value}")
    return value


@pytest.fixture
def engine_binary(monkeypatch: pytest.MonkeyPatch) -> str:
    binary = str(Path(_binary()).resolve())
    monkeypatch.setenv("GSD_ENGINE_BIN_SHA256", hashlib.sha256(Path(binary).read_bytes()).hexdigest())
    return binary


def _masked(raw: bytes, leg: Optional[Path] = None) -> str:
    """The reply as text: the leg's own base path named `<leg>` (each leg writes under its own base) and the envelope fields masked."""
    text = raw.decode("utf-8")
    if leg is not None:
        text = text.replace(str(leg), "<leg>")
    for pattern, replacement in MASKS:
        text = pattern.sub(replacement, text)
    return text


def _env(state: Path, path: str) -> dict[str, str]:
    env = os.environ.copy()
    env["SWARM_DIR"] = str(state / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(state / "claude")
    env["SWARM_SIGNING_KEY"] = "ab" * 32
    for key in ("SWARM_ED25519_KEY", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT_SESSION",
                "SWARM_DRYRUN_FAIL", "GSD_ENGINE", "GSD_ENGINE_BIN", "GSD_RUST_BACKEND"):
        env.pop(key, None)
    env["PATH"] = path
    return env


def _python(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, env=env, cwd=ROOT, timeout=120)


def _probe(binary: str, root: Path, argv: list[str], env: dict[str, str]) -> tuple[int, bytes, Optional[dict[str, Any]]]:
    """The selector's request, sent to the binary directly. The reply object is None when it is not a JSON object."""
    request = {
        "agent": "A10",
        "script": "sec_gate",
        "argv": argv,
        "root": str(root),
        "env": {key: value for key, value in env.items() if key.startswith(("SWARM_", "GSD_")) and value},
    }
    done = subprocess.run([binary], input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                          capture_output=True, env=env, cwd=ROOT, timeout=120)
    try:
        body = json.loads(done.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = None
    return done.returncode, done.stdout, body if isinstance(body, dict) else None


def _served(returncode: int, body: Optional[dict[str, Any]]) -> bool:
    return returncode in (0, 1) and body is not None and body.get("script") == "sec_gate"


def _declined(returncode: int, body: Optional[dict[str, Any]]) -> bool:
    if returncode != 2 or body is None:
        return False
    error = body.get("error")
    return (isinstance(error, dict) and error.get("code") == "E-DEP"
            and isinstance(error.get("details"), dict) and error["details"].get("write_state") == "not_started")


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_case_matches_python(case: gen.Case, tmp_path: Path, engine_binary: str) -> None:
    binary = engine_binary
    base = tmp_path / case.name
    base.mkdir()
    py_paths = gen.materialize(case, base / "py")
    rs_paths = gen.materialize(case, base / "rs")
    py_argv = gen.argv_for(case, py_paths)
    rs_argv = gen.argv_for(case, rs_paths)
    py_env = _env(base / "py-state", str(py_paths["shims"]))
    rs_env = _env(base / "rs-state", str(rs_paths["shims"]))

    python = _python(py_argv, py_env)
    returncode, raw, body = _probe(binary, rs_paths["root"], rs_argv, rs_env)

    if case.served:
        assert _served(returncode, body), f"{case.name}: the binary declined a served cell\n{raw!r}"
    else:
        assert _declined(returncode, body), f"{case.name}: the binary answered a declined cell\n{raw!r}"

    if case.served:
        assert returncode == python.returncode, f"{case.name}: exit {returncode} != {python.returncode}"
        assert _masked(raw, rs_paths["base"]) == _masked(python.stdout, py_paths["base"]), f"{case.name}: probe bytes differ from Python"

    selector_env = dict(rs_env)
    selector_env.update({"GSD_ENGINE": "rust", "GSD_ENGINE_BIN": binary, "GSD_RUST_BACKEND": "host"})
    selected = subprocess.run([sys.executable, str(SCRIPT), *rs_argv], capture_output=True, env=selector_env,
                              cwd=ROOT, timeout=120)
    assert selected.returncode == python.returncode, f"{case.name}: selector exit differs"
    assert _masked(selected.stdout, rs_paths["base"]) == _masked(python.stdout, py_paths["base"]), f"{case.name}: selector bytes differ from Python"

    if case.name in AUDIT_EXPECTATIONS:
        audit, expected = AUDIT_EXPECTATIONS[case.name]
        for answer in (json.loads(python.stdout), body, json.loads(selected.stdout)):
            assert answer["runs"][audit] == expected
            assert answer["verdict"] == expected
            if expected == "fail":
                assert any(finding["kind"] == "deps" and finding["severity"] == "major"
                           for finding in answer["findings"])
            else:
                assert answer["findings"] == []
    if case.name == "recovery_secret_length_only":
        for answer in (json.loads(python.stdout), body, json.loads(selected.stdout)):
            secrets = [finding for finding in answer["findings"] if finding["kind"] == "secrets"]
            assert len(secrets) == 1
        for output in (python.stdout, raw, selected.stdout):
            assert RECOVERY_SECRET.encode() not in output
            assert RECOVERY_SECRET[:4].encode() not in output
