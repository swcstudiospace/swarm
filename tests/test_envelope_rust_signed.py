"""Phase 14.5 unit U3.4b: the Rust `gsd-envelope` crate against the Python envelope oracle.

Two kinds of test live here.

* `test_envelope_vectors_are_fresh` needs no binary. It regenerates the vectors the Rust tests read
  (`crates/gsd-envelope/tests/vectors/envelope.json`, written by `tests/parity/gen_envelope_vectors.py` from the real
  `swarm/envelope.py`) in memory and compares them with the committed file, so it fails when the Python oracle changes under the
  vectors or when someone edits the file by hand.
* The `rust_signed` tests pipe envelope bodies through the Rust example `sign_stdin` and verify the output with the Python
  `verify_envelope`; Rust and Python must also produce the identical `sig` (HMAC-SHA256 and Ed25519 are deterministic). They skip,
  with the reason, unless `GSD_ENVELOPE_SIGN_BIN` names an executable file. The orchestrator builds the example
  (`cargo build -p gsd-envelope --example sign_stdin`) and sets the variable; this module never runs cargo.

Test keys are derived at run time from the documented seed phrase of the generator; no key is written to any file.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest  # type: ignore[import-not-found, unused-ignore]  # absent from a mypy environment that is not this interpreter's

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "parity"))

import gen_envelope_vectors as gen  # noqa: E402
from swarm import envelope  # noqa: E402

BIN_ENV = "GSD_ENVELOPE_SIGN_BIN"
RUN_TIMEOUT_S = 20
SIGN_ENVS = {name: (env, declines) for name, env, declines in gen.SIGN_ENVS}
# Key environments the Rust binary signs under (each also names the oracle's answer through gen.oracle_sign).
SIGNING_MODES = (
    "hmac",
    "hmac_unicode_secret",
    "hmac_long_secret",
    "ed25519",
    "ed25519_upper_hex",
    "ed25519_spaced_hex",
    "ed25519_trailing_newline",
    "ed25519_wins_over_hmac",
    "ed25519_bad_seed_with_hmac_uses_hmac",
    "dev_opt_in_implicit",
    "dev_opt_in_explicit_dev_value",
    "key_file_hidden_by_hmac",
)
# Environments where the oracle refuses to sign, with the exit code Rust must use: 1 fail-closed (same text), 3 declined.
REFUSING_MODES = {
    "no_key": 1,
    "require_without_key": 1,
    "ed25519_not_hex": 1,
    "dev_opt_in_with_require": 1,
    "dev_value_without_opt_in": 1,
    "agent_session_key_file": 1,
    "key_file_alone": 3,
    "key_file_empty": 3,
    "key_file_with_dev_opt_in": 3,
}


@pytest.fixture(scope="module")
def sign_bin() -> str:
    path = os.environ.get(BIN_ENV)
    if not path:
        pytest.skip(f"{BIN_ENV} is not set: build `cargo build -p gsd-envelope --example sign_stdin` and point it at the binary")
    binary = Path(path)
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        pytest.skip(f"{BIN_ENV}={path} is not an executable file")
    return str(binary)


def run_sign(binary: str, spec: Mapping[str, str], text: str) -> subprocess.CompletedProcess[str]:
    """Runs the Rust signer with exactly the case's signing variables (HOME and XDG unset: no conventional key file)."""
    env = {"PATH": os.environ.get("PATH", "")}
    env.update(gen.resolve_env(spec, gen.public_dev_key()))
    return subprocess.run([binary], input=text, capture_output=True, text=True, env=env, timeout=RUN_TIMEOUT_S, check=False)


def test_envelope_vectors_are_fresh() -> None:
    pytest.importorskip("cryptography", reason="the vector generator derives Ed25519 public keys with the cryptography package")
    committed = gen.VECTOR_PATH.read_text(encoding="utf-8")
    assert gen.build_vectors() == committed, "run `python3 tests/parity/gen_envelope_vectors.py` and commit the regenerated vector file"


def test_vectors_hold_no_private_key_material() -> None:
    """The committed file carries the public phrase, public keys and signatures; no derived seed or HMAC key may appear in it."""
    text = gen.VECTOR_PATH.read_text(encoding="utf-8")
    for label in ("ed-a", "ed-b", "hmac-a", "hmac-b"):
        assert gen.key_value("@" + label, "") not in text, f"the derived key {label} is written into the vector file"


@pytest.mark.parametrize("body_name", sorted(gen.BODIES))
@pytest.mark.parametrize("mode", SIGNING_MODES)
def test_rust_signed_envelope_verifies_in_python_and_matches_its_signature(sign_bin: str, mode: str, body_name: str) -> None:
    spec, _declines = SIGN_ENVS[mode]
    text = gen.BODIES[body_name]
    done = run_sign(sign_bin, spec, text)
    assert done.returncode == 0, f"exit {done.returncode}: {done.stderr}"
    assert done.stdout.endswith("\n") and done.stdout.count("\n") == 1, "one line of JSON"
    rust_signed: dict[str, Any] = json.loads(done.stdout)

    original = json.loads(text)
    assert {k: v for k, v in rust_signed.items() if k != "sig"} == {k: v for k, v in original.items() if k != "sig"}

    with gen.environment(gen.resolve_env(spec, gen.public_dev_key())):
        assert envelope.verify_envelope(copy.deepcopy(rust_signed)) is True
        python_signed = json.loads(text)
        envelope.sign_envelope(python_signed, audit=False)
        tampered = copy.deepcopy(rust_signed)
        tampered["tampered"] = True
        assert envelope.verify_envelope(tampered) is False
    assert rust_signed["sig"] == python_signed["sig"], "Rust and Python must produce the identical signature"


@pytest.mark.parametrize("mode", sorted(REFUSING_MODES))
def test_rust_refuses_where_python_refuses(sign_bin: str, mode: str) -> None:
    spec, _declines = SIGN_ENVS[mode]
    done = run_sign(sign_bin, spec, gen.BODIES["plain"])
    assert done.returncode == REFUSING_MODES[mode], done.stderr
    assert done.stdout == "", "a refused signing prints no envelope"
    if REFUSING_MODES[mode] == 1:
        answer = gen.oracle_sign(spec, gen.BODIES["plain"], gen.public_dev_key())
        assert answer["ok"] is False
        assert done.stderr.strip() == answer["error"], "the fail-closed text must equal Python's"
    else:
        assert "declined" in done.stderr


@pytest.mark.parametrize("body_name", sorted(gen.RUST_DECLINES))
def test_rust_declines_bodies_it_cannot_represent_exactly(sign_bin: str, body_name: str) -> None:
    done = run_sign(sign_bin, gen.HMAC_A, gen.RUST_DECLINES[body_name])
    assert done.returncode == 3 and done.stdout == "", done.stderr


@pytest.mark.parametrize("mode", [name for name, _env in gen.LAYOUT_MODES])
@pytest.mark.parametrize("layout_name", [layout[0] for layout in gen.LAYOUTS])
def test_rust_signer_follows_python_on_conventional_key_paths(sign_bin: str, layout_name: str, mode: str) -> None:
    """F-1: where Python's O_NOFOLLOW walk refuses (even the dev key), the Rust signer must decline (exit 3, nothing printed) and
    never sign; where its `lstat` walk agrees with Python it must answer exactly as Python does."""
    _name, kind, operations, base_env = next(layout for layout in gen.LAYOUTS if layout[0] == layout_name)
    spec: gen.LayoutEnv = {**base_env, **dict(gen.LAYOUT_MODES)[mode]}
    rust_class, _declined_as = gen.layout_class(kind, mode)
    dev_key = gen.public_dev_key()
    with tempfile.TemporaryDirectory(prefix="gsd-envelope-layout-test-") as tmp:
        root = Path(tmp).resolve()
        gen.build_layout(root, operations)
        answer = gen.oracle_sign(spec, gen.BODIES["plain"], dev_key, str(root))
        env = {"PATH": os.environ.get("PATH", "")}
        env.update({name: value for name, value in gen.resolve_layout_env(spec, str(root), dev_key).items() if value is not None})
        done = subprocess.run([sign_bin], input=gen.BODIES["plain"], capture_output=True, text=True, env=env, timeout=RUN_TIMEOUT_S, check=False)
    if rust_class == "decline":
        assert answer["ok"] is False, "a declined layout is one where Python refuses"
        assert done.returncode == 3 and done.stdout == "" and "declined" in done.stderr, (done.returncode, done.stderr)
    elif answer["ok"]:
        assert done.returncode == 0, done.stderr
        assert json.loads(done.stdout)["sig"] == answer["sig"]
    else:
        assert done.returncode == 1 and done.stdout == "", (done.returncode, done.stderr)
        assert done.stderr.strip() == answer["error"]
