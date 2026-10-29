"""Gate signing keys: what the Rust gate-issue tail signs verifies in Python, and no key reaches any output of it.

The Rust side is `crates/gsd-taskwriter/examples/gate_issue_apply.rs` (a thin driver of `TaskStoreWriter::issue_gate`, which reads the keys from
its process environment with `gsd_envelope::KeyMaterial::from_env`). Build it with
`cargo build -p gsd-taskwriter --example gate_issue_apply --offline`, or name a binary with `GATE_ISSUE_APPLY_BIN` (a named binary that
is missing fails the test; with none named and none built the tests skip).

- Cross-check: a store made by the real `TaskStore`, rows written by the Rust signer with a `secrets.token_hex(32)` key, then Python's own
  `swarm.envelope.verify_envelope`, `TaskStore.latest_verdicts` and `TaskStore.missing_gate_reasons` accept them (HMAC, Ed25519, both).
- Canaries: a `secrets.token_hex(32)` canary in `SWARM_SIGNING_KEY` and in `SWARM_ED25519_KEY` (and in malformed shapes of the seed) is in none
  of stdout, stderr, the verdict file or the store file, in any of the encodings of `_forms`, for every branch, refusals included. Keys are made
  at run time, passed in the child's environment only, and never printed or written.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from swarm.taskstore import TaskStore  # noqa: E402

SECRET_VARS = ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_REQUIRE_KEY",
               "SWARM_AGENT_SESSION", "SWARM_DRYRUN_FAIL")
MAJOR = {"id": "F-1", "severity": "major", "kind": "functional", "summary": "missing test", "evidence": "", "ac_ref": None,
         "owner_suggestion": None, "location": None}


def _binary() -> Path:
    named = os.environ.get("GATE_ISSUE_APPLY_BIN")
    if named:
        path = Path(named)
        assert path.is_file(), "GATE_ISSUE_APPLY_BIN names a binary that does not exist"
        return path
    built = REPO / "target" / "debug" / "examples" / "gate_issue_apply"
    if not built.is_file():
        pytest.skip("gate_issue_apply is not built: cargo build -p gsd-taskwriter --example gate_issue_apply --offline")
    return built


@pytest.fixture()
def apply_bin() -> Path:
    return _binary()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: Any) -> None:
    for name in SECRET_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SWARM_GSD_STATE_FILE", "")
    monkeypatch.delenv("SWARM_DIR", raising=False)


def _seed(db: Path) -> None:
    store = TaskStore(db)
    store.create(task_id="G1", correlation_id="corr-g1", capability="cap", notes={"gate": "quality", "gate_for": ["T-a", "T-b"]})
    for target in ("T-a", "T-b"):
        store.create(task_id=target, correlation_id=f"corr-{target}", capability="cap", notes={"gates": ["quality"]})
    store.conn.execute("UPDATE tasks SET state='IN_PROGRESS' WHERE task_id='G1'")
    store.conn.execute("UPDATE tasks SET state='IN_REVIEW' WHERE task_id IN ('T-a','T-b')")
    store.conn.commit()
    store.conn.close()


def _request(**more: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"gate": "quality", "agent_id": "A08@local", "findings": [], "runs": {"pytest": {"passed": 3}}, "task_id": "G1",
                            "per_target": {"T-a": [MAJOR], "T-b": []}, "now": time.time()}
    return {**base, **more}


def _run(binary: Path, db: Path, out_dir: Path, request: dict[str, Any], env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    child = {"PATH": os.environ.get("PATH", ""), "GATE_ISSUE_OUT_DIR": str(out_dir), **env}
    return subprocess.run([str(binary), str(db)], input=json.dumps(request).encode(), capture_output=True, env=child, timeout=50, check=False)


# ---- cross-check: Python accepts what Rust signed ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["hmac", "ed25519", "both"])
def test_rows_signed_by_rust_verify_in_python(apply_bin: Path, tmp_path: Path, monkeypatch: Any, kind: str) -> None:
    db, out = tmp_path / "tasks.db", tmp_path / "out"
    out.mkdir()
    _seed(db)
    env: dict[str, str] = {}
    if kind in ("hmac", "both"):
        env["SWARM_SIGNING_KEY"] = secrets.token_hex(32)
    if kind in ("ed25519", "both"):
        env["SWARM_ED25519_KEY"] = secrets.token_hex(32)
    done = _run(apply_bin, db, out, _request(), env)
    assert done.returncode == 0, done.stderr.decode()
    plan = json.loads(done.stdout)
    assert plan["error"] is None and plan["writes"] == 3 and [t for t, _ in plan["recorded"]] == ["T-a", "T-b"]
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    from swarm.envelope import verify_envelope

    store = TaskStore(db)
    prefix = "ed25519:" if kind in ("ed25519", "both") else "hmac:"
    for target in ("T-a", "T-b"):
        row = store.latest_verdicts(target)["quality"]
        envelope = json.loads(row["envelope_json"])
        assert verify_envelope(envelope) and row["sig"] == envelope["sig"] and envelope["sig"].startswith(prefix)
        assert envelope["payload"]["gate_task"] == "G1" and envelope["correlation_id"] == f"corr-{target}"
    assert store.missing_gate_reasons("T-a") == {"quality": "fail"}
    assert store.missing_gate_reasons("T-b") == {}
    assert store.missing_gates("T-b") == []
    assert len(store.get("T-a")["notes_json"]["feedback"]) == 1 and "feedback" not in store.get("T-b")["notes_json"]
    file_envelope = json.loads((out / "G1.quality.json").read_text())
    # The signed file verdict comes from the script's own findings (none here); per-target failures live in the rows (Python does the same).
    assert verify_envelope(file_envelope) and file_envelope["payload"]["verdict"] == "pass"


def test_a_wrong_key_does_not_verify_what_rust_signed(apply_bin: Path, tmp_path: Path, monkeypatch: Any) -> None:
    db, out = tmp_path / "tasks.db", tmp_path / "out"
    out.mkdir()
    _seed(db)
    done = _run(apply_bin, db, out, _request(), {"SWARM_SIGNING_KEY": secrets.token_hex(32)})
    assert done.returncode == 0, done.stderr.decode()
    monkeypatch.setenv("SWARM_SIGNING_KEY", secrets.token_hex(32))
    assert TaskStore(db).missing_gate_reasons("T-b") == {"quality": "bad-sig"}


# ---- canaries ---------------------------------------------------------------------------------------------------------------

def _b64_fragments(data: bytes) -> list[bytes]:
    found: list[bytes] = []
    for offset in range(3):
        text = base64.b64encode(b"\0" * offset + data).rstrip(b"=")
        head, tail = (0, 2, 3)[offset], (0, 1, 1)[(offset + len(data)) % 3]
        standard = text[head:len(text) - tail]
        found += [standard, standard.replace(b"+", b"-").replace(b"/", b"_")]
    return found


def _byte_forms(data: bytes) -> list[bytes]:
    def listed(separator: str, show: Callable[[int], str]) -> bytes:
        return separator.join(show(b) for b in data).encode()

    return [listed(", ", str), listed(",", str), listed("", lambda b: f"\\x{b:02x}"), b"0x" + data.hex().encode(),
            listed(", ", lambda b: f"0x{b:02x}"), listed(" ", lambda b: f"{b:02x}"), listed(" ", lambda b: f"{b:02X}"),
            repr(data)[2:-1].encode(), data]


def _forms(value: str) -> list[bytes]:
    raw = value.encode()
    found = [raw, value.upper().encode(), value.encode("utf-16-le"), value.encode("utf-16-be"), raw.hex().encode(), raw.hex().upper().encode(),
             *_b64_fragments(raw), *_byte_forms(raw)]
    try:
        key = bytes.fromhex(value)
    except ValueError:
        key = b""
    if key:
        found += [*_b64_fragments(key), *_byte_forms(key)]
    if len(value) >= 32:
        found += [value[:16].encode(), value[-16:].encode(), value[:16].upper().encode(), value[-16:].upper().encode()]
    return found


def _leaks(forms: list[bytes], blob: bytes) -> list[int]:
    return [i for i, needle in enumerate(forms) if needle and needle in blob]


def test_the_scanner_finds_every_form_of_a_canary() -> None:
    canary = secrets.token_hex(32)
    forms = _forms(canary)
    assert all(_leaks([form], b"x " + form + b" y") for form in forms)
    assert _leaks(forms, b"nothing here") == []


def test_no_canary_reaches_any_output_of_any_branch(apply_bin: Path, tmp_path: Path) -> None:
    signing, seed, other = secrets.token_hex(32), secrets.token_hex(32), secrets.token_hex(32)
    malformed = {"nonhex": seed + "zz", "odd": seed[:-1], "short": seed[:62], "long": seed + other, "0x": "0x" + seed, "quoted": f'"{seed}"'}
    secrets_in_play = [signing, seed, other, *malformed.values()]
    forms = [f for value in secrets_in_play for f in _forms(value)]
    cases: list[tuple[str, dict[str, str], dict[str, Any]]] = [
        ("hmac", {"SWARM_SIGNING_KEY": signing}, _request()),
        ("ed25519", {"SWARM_ED25519_KEY": seed}, _request()),
        ("both", {"SWARM_SIGNING_KEY": signing, "SWARM_ED25519_KEY": seed}, _request()),
        ("no-store", {"SWARM_SIGNING_KEY": signing}, _request(task_id=None, per_target={})),
        ("session", {"SWARM_SIGNING_KEY": signing, "SWARM_AGENT_SESSION": "1"}, _request()),
        ("dev-opt-in", {"SWARM_SIGNING_KEY": signing, "SWARM_ALLOW_INSECURE_DEV_KEY": "1"}, _request()),
        ("foreign-correlation", {"SWARM_SIGNING_KEY": signing, "SWARM_ED25519_KEY": seed}, _request(correlation_id="corr-other")),
        ("declined-request", {"SWARM_SIGNING_KEY": signing}, _request(extra={"verdict": "pass"})),
        ("seed-beside-hmac", {"SWARM_SIGNING_KEY": signing, "SWARM_ED25519_KEY": other + "zz"}, _request()),
        ("key-file-reference", {"SWARM_ED25519_KEY_FILE": f"/nonexistent/{other}/key.pem"}, _request()),
        ("advisory", {}, _request()),
        ("require-no-key", {"SWARM_REQUIRE_KEY": "1"}, _request()),
        *[(f"malformed-{name}", {"SWARM_ED25519_KEY": value}, _request()) for name, value in malformed.items()],
        *[(f"malformed-{name}-require", {"SWARM_ED25519_KEY": value, "SWARM_REQUIRE_KEY": "1"}, _request(task_id=None, per_target={}))
          for name, value in malformed.items()],
    ]
    signed = 0
    for name, env, request in cases:
        db, out = tmp_path / f"{name}.db", tmp_path / f"out-{name}"
        out.mkdir()
        _seed(db)
        done = _run(apply_bin, db, out, request, env)
        assert done.returncode in (0, 3), (name, done.returncode)
        blobs = {"stdout": done.stdout, "stderr": done.stderr, "store": db.read_bytes(),
                 **{f"file {f.name}": f.read_bytes() for f in out.iterdir()}}
        for channel, blob in blobs.items():
            assert _leaks(forms, blob) == [], f"a canary is in {channel} of {name}"
        signed += b'"sig": "hmac:' in done.stdout or b'"sig": "ed25519:' in done.stdout
    assert signed >= 4
