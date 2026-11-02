"""Generates `crates/gsd-envelope/tests/vectors/envelope.json`, the parity vectors of the Rust `gsd-envelope` crate.

Every expected value comes from the REAL oracle, `swarm/envelope.py`, evaluated in this process under a controlled environment:
the canonical form (`_canonical`), `verify_envelope`, `sign_envelope`, the signing-key precedence and fail-closed messages, and the
base64 acceptance rule (`_b64`). The file is deterministic (fixed ids, sorted keys), so a freshness test can regenerate it in memory
and compare it with the committed file: it then fails whenever the Python oracle changes under the vectors.

No secret is written anywhere. A test key is derived at run time from a documented seed phrase: the key named `@label` is
`sha256(KEY_PHRASE + label).hexdigest()` (64 hex characters, usable as an Ed25519 seed or as an HMAC secret string). The
committed file holds the phrase (it is public and documented), Ed25519 PUBLIC keys and signatures, never a seed or an HMAC key.
A value may carry transforms after `|`, applied in order: `upper`, `spaced` (a space between byte pairs), `nl` (a trailing newline),
`innerspace` (a space inside the first pair), `trunc` (drop the last byte), `long` (the value repeated 10 times), `short` (3
characters), `uni` (a non-ASCII suffix). `@dev` is the public development key that `insecure_dev_key()` returns.

Usage: `python3 tests/parity/gen_envelope_vectors.py` rewrites the file; `--check` exits 1 when the committed file is stale.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import itertools
import json
import os
import random
import struct
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast

REPO = Path(__file__).resolve().parents[2]
VECTOR_PATH = REPO / "crates" / "gsd-envelope" / "tests" / "vectors" / "envelope.json"
KEY_PHRASE = "agent-swarm gsd-envelope test vectors: derived key phrase, not a secret: "
ENV_NAMES = (
    "SWARM_ED25519_KEY",
    "SWARM_SIGNING_KEY",
    "SWARM_ED25519_KEY_FILE",
    "SWARM_AGENT_SESSION",
    "SWARM_ALLOW_INSECURE_DEV_KEY",
    "SWARM_REQUIRE_KEY",
    "XDG_CONFIG_HOME",
    "HOME",
)
# Ed25519 group order L; S + L is the malleated twin of a signature scalar S.
GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SWARM_PURE_PYTHON", "1")

from swarm import envelope as oracle  # noqa: E402  (after the repository root is on sys.path)
from swarm.errors import SwarmError  # noqa: E402

# ---------------------------------------------------------------------------------------------------------------------------------
# Envelope bodies, as JSON text. Rust parses the same text; the oracle parses it with json.loads.
# ---------------------------------------------------------------------------------------------------------------------------------


def _float_texts() -> list[str]:
    """Float spellings: curated edge cases, then 200 doubles from a fixed-seed generator in two spellings."""
    curated = [
        "0.1", "1.0", "-0.0", "0.0", "100.0", "1e16", "1e15", "9999999999999998.0", "1e-4", "0.0001", "0.00009999", "9.999e-05",
        "1.5e-7", "1e22", "1e23", "5e-324", "2.2250738585072014e-308", "1.7976931348623157e308", "123456789.123456789", "0.3",
        "1E5", "1e+5", "1.0e-10", "12345678901234567890.0", "0.1e1", "5e-5", "2.5e+300", "4.35", "-7.0",
        "3.141592653589793", "2.718281828459045e-100", "1e21", "123456789012345678.0", "0.000123", "1e-300", "-2.5e-8",
    ]
    rng = random.Random(1451)
    generated: list[str] = []
    for _ in range(200):
        value = struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0]
        if value != value or value in (float("inf"), float("-inf")):
            continue
        generated.append(repr(value))
        spelled = "%.17g" % value
        generated.append(spelled if any(c in spelled for c in ".eE") else spelled + ".0")
    return curated + generated


BODIES: dict[str, str] = {
    "plain": json.dumps(
        {
            "msg_id": "00000000-0000-7000-8000-000000000001",
            "correlation_id": "corr-1",
            "trace_id": "trace-1",
            "causation_id": None,
            "source": "a08-qa",
            "target": "a01-orchestrator",
            "type": "gate.verdict",
            "schema": "swarm.v1.gate.verdict",
            "ts": "2026-10-28T00:00:00.000Z",
            "ttl_s": 300,
            "priority": "P2",
            "risk_class": "low",
            "payload": {"gate": "qa", "task_id": "T-1", "verdict": "APPROVED", "issued_at": 1782604800.25, "dry_run": False},
        }
    ),
    "floats": '{"type":"x","payload":{"floats":[' + ",".join(_float_texts()) + "]}}",
    "unicode": json.dumps(
        {
            "type": "x",
            "payload": {"text": "h\u00e9llo \u65e5\u672c\u8a9e \U0001f600 \u2028\u2029 \u00a0 \u007f", "z": 1, "\u00e9": 2, "\U0001f600": 3, "\uffff": 4, "Z": 5},
        },
        ensure_ascii=True,
    ),
    "nested": '{"type":"x","payload":{"a":{"b":{"c":[[],{},[[]],{"d":null,"e":true,"f":false}]},"a":[1,[2,[3,[4]]]]},"empty_s":"","k":[{"y":1,"x":2}]}}',
    "ints": '{"type":"x","payload":{"max":9223372036854775807,"min":-9223372036854775808,"negzero":-0,"zero":0,"neg":-17,"one":1}}',
    "controls": json.dumps({"type": "x", "payload": {"s": "q\"b\\ / \n\r\t\b\f \u0000\u0001\u001f\u007f", "k\n\"": 1}}, ensure_ascii=True),
    "key_order": '{"type":"x","payload":{"b":1,"B":2,"a":3,"_":4,"~":5,"\\u00e9":6,"e":7,"\\uffff":8,"\\ud83d\\ude00":9,"\\ue000":10,"10":11,"9":12}}',
    "existing_sig": '{"b":1,"sig":"hmac:stale-signature-is-excluded","a":{"sig":"inner-sig-stays"},"type":"x"}',
    "duplicate_keys": '{"type":"x","a":1,"b":2,"a":3}',
    "whitespace": ' \n\t{ "type" : "x" ,\r\n "payload" : { "a" : [ 1 , 2 ] } } \n',
    "escaped_slash": '{"type":"x","payload":{"u":"a\\/b","e":"\\u0041\\u00e9\\ud83d\\ude00"}}',
    "empty": "{}",
    "float_overflow": '{"type":"x","payload":{"f":[1e400,-1e400,1e-400,-1e-400]}}',
}
# Bodies the Rust crate declines (it cannot represent them exactly) and why; the oracle still answers each of them.
RUST_DECLINES: dict[str, str] = {
    "bigint": '{"type":"x","payload":{"big":9223372036854775808,"huge":123456789012345678901234567890,"neg":-9223372036854775809}}',
    "nan_literal": '{"type":"x","payload":{"n":NaN,"i":Infinity,"m":-Infinity}}',
    "lone_surrogate": '{"type":"x","payload":{"s":"\\ud800"}}',
    "lone_low_surrogate": '{"type":"x","payload":{"s":"a\\udc00b"}}',
    "bad_pair": '{"type":"x","payload":{"s":"\\ud83d\\u0041"}}',
}
ALL_BODIES: dict[str, str] = {**BODIES, **RUST_DECLINES}


def _spell(base: str, transforms: list[str]) -> str:
    for transform in transforms:
        if transform == "upper":
            base = base.upper()
        elif transform == "spaced":
            base = " ".join(base[i : i + 2] for i in range(0, len(base), 2))
        elif transform == "nl":
            base += "\n"
        elif transform == "innerspace":
            base = base[:1] + " " + base[1:]
        elif transform == "trunc":
            base = base[:-2]
        elif transform == "long":
            base = base * 10
        elif transform == "short":
            base = base[:3]
        elif transform == "uni":
            base += "-\u00e9\u65e5"
        else:
            raise ValueError(f"unknown transform {transform}")
    return base


def key_value(spec: str, dev_key: str) -> str:
    """Resolves a vector value: `@label[|transform...]` is a derived key, `@dev` the public dev key, anything else is literal."""
    if not spec.startswith("@"):
        return spec
    label, *transforms = spec[1:].split("|")
    base = dev_key if label == "dev" else hashlib.sha256((KEY_PHRASE + label).encode("ascii")).hexdigest()
    return _spell(base, transforms)


@contextlib.contextmanager
def environment(values: Mapping[str, str | None]) -> Iterator[None]:
    """Runs with exactly `values` in the signing-related environment; HOME and XDG point at an empty directory unless given.

    A value of None removes the variable (after the defaults), so a layout vector can leave `HOME` or `XDG_CONFIG_HOME` unset.
    """
    saved = {name: os.environ.get(name) for name in ENV_NAMES}
    with tempfile.TemporaryDirectory(prefix="gsd-envelope-vectors-") as empty:
        try:
            for name in ENV_NAMES:
                os.environ.pop(name, None)
            os.environ["HOME"] = empty
            os.environ["XDG_CONFIG_HOME"] = empty
            for name, value in values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            yield
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def public_dev_key() -> str:
    with environment({"SWARM_ALLOW_INSECURE_DEV_KEY": "1"}):
        key = oracle.insecure_dev_key()
    if key is None:
        raise RuntimeError("the oracle returned no dev key under the opt-in")
    return key


def resolve_env(spec: Mapping[str, str], dev_key: str) -> dict[str, str]:
    return {name: key_value(value, dev_key) for name, value in spec.items()}


def resolve_layout_env(spec: Mapping[str, str | None], root: str, dev_key: str) -> dict[str, str | None]:
    """Like `resolve_env` for a layout vector: `{root}` becomes the layout's directory and None leaves the variable unset."""
    return {name: None if value is None else key_value(value.replace("{root}", root), dev_key) for name, value in spec.items()}


def canonical_of(text: str) -> str | None:
    """The oracle's canonical bytes as text, or None when the oracle raises (a lone surrogate cannot be encoded)."""
    try:
        return oracle._canonical(json.loads(text)).decode("utf-8")
    except UnicodeEncodeError:
        return None


def oracle_sign(spec: Mapping[str, str | None], text: str, dev_key: str, root: str = "") -> dict[str, Any]:
    """`sign_envelope` of the body under the environment `spec`: the signature and the dev-key audit flag, or the error text."""
    body = json.loads(text)
    with environment(resolve_layout_env(spec, root, dev_key)):
        try:
            ed_signer = oracle._ed25519_key()
            material = oracle._signing_hmac()
            oracle.sign_envelope(body, audit=False)
        except SwarmError as error:
            return {"ok": False, "error": str(error)}
        except UnicodeEncodeError:
            return {"ok": False, "error": "UnicodeEncodeError"}
    audit = ed_signer is None and material is not None and material[1]
    return {"ok": True, "sig": body["sig"], "dev_key_audit": bool(audit)}


def oracle_key_state(spec: Mapping[str, str | None], dev_key: str, root: str = "") -> dict[str, Any]:
    """The oracle's three key-state answers under `spec`: `real_key_configured()`, whether `insecure_dev_key()` returns the dev
    key, and `signing_config_error()`."""
    with environment(resolve_layout_env(spec, root, dev_key)):
        return {
            "real_key_configured": oracle.real_key_configured(),
            "dev_key_active": oracle.insecure_dev_key() is not None,
            "config_error": oracle.signing_config_error(),
        }


def oracle_verify(spec: Mapping[str, str | None], envelope: dict[str, object], dev_key: str) -> bool | str:
    with environment(resolve_layout_env(spec, "", dev_key)):
        try:
            return oracle.verify_envelope(envelope)
        except UnicodeEncodeError:
            return "UnicodeEncodeError"


# ---------------------------------------------------------------------------------------------------------------------------------
# Environments
# ---------------------------------------------------------------------------------------------------------------------------------

Env = dict[str, str]
HMAC_A: Env = {"SWARM_SIGNING_KEY": "@hmac-a"}
HMAC_B: Env = {"SWARM_SIGNING_KEY": "@hmac-b"}
ED_A: Env = {"SWARM_ED25519_KEY": "@ed-a"}
ED_B: Env = {"SWARM_ED25519_KEY": "@ed-b"}
DEV: Env = {"SWARM_ALLOW_INSECURE_DEV_KEY": "1"}
KEY_FILE = "/nonexistent/gsd-envelope-vector-key"

# (name, environment, Rust declines it). The oracle answers every one of them.
SIGN_ENVS: list[tuple[str, Env, bool]] = [
    ("hmac", HMAC_A, False),
    ("hmac_unicode_secret", {"SWARM_SIGNING_KEY": "@hmac-a|uni"}, False),
    ("hmac_long_secret", {"SWARM_SIGNING_KEY": "@hmac-a|long"}, False),
    ("hmac_short_secret", {"SWARM_SIGNING_KEY": "@hmac-a|short"}, False),
    ("hmac_require", {**HMAC_A, "SWARM_REQUIRE_KEY": "1"}, False),
    ("hmac_require_not_one", {**HMAC_A, "SWARM_REQUIRE_KEY": "yes"}, False),
    ("ed25519", ED_A, False),
    ("ed25519_upper_hex", {"SWARM_ED25519_KEY": "@ed-a|upper"}, False),
    ("ed25519_spaced_hex", {"SWARM_ED25519_KEY": "@ed-a|spaced"}, False),
    ("ed25519_trailing_newline", {"SWARM_ED25519_KEY": "@ed-a|nl"}, False),
    ("ed25519_inner_space", {"SWARM_ED25519_KEY": "@ed-a|innerspace"}, False),
    ("ed25519_31_bytes", {"SWARM_ED25519_KEY": "@ed-a|trunc"}, False),
    ("ed25519_320_chars", {"SWARM_ED25519_KEY": "@ed-a|long"}, False),
    ("ed25519_not_hex", {"SWARM_ED25519_KEY": "not-hex-at-all"}, False),
    ("ed25519_wins_over_hmac", {**ED_A, **HMAC_A}, False),
    ("ed25519_bad_seed_with_hmac_uses_hmac", {"SWARM_ED25519_KEY": "@ed-a|trunc", **HMAC_A}, False),
    ("ed25519_bad_seed_require_with_hmac", {"SWARM_ED25519_KEY": "@ed-a|trunc", **HMAC_A, "SWARM_REQUIRE_KEY": "1"}, False),
    ("no_key", {}, False),
    ("empty_hmac_key", {"SWARM_SIGNING_KEY": ""}, False),
    ("empty_ed25519_key", {"SWARM_ED25519_KEY": ""}, False),
    ("require_without_key", {"SWARM_REQUIRE_KEY": "1"}, False),
    ("require_with_dev_key_value", {"SWARM_REQUIRE_KEY": "1", "SWARM_SIGNING_KEY": "@dev", "SWARM_ALLOW_INSECURE_DEV_KEY": "1"}, False),
    ("require_with_ed25519", {**ED_A, "SWARM_REQUIRE_KEY": "1"}, False),
    ("dev_opt_in_implicit", DEV, False),
    ("dev_opt_in_empty_secret", {**DEV, "SWARM_SIGNING_KEY": ""}, False),
    ("dev_opt_in_explicit_dev_value", {**DEV, "SWARM_SIGNING_KEY": "@dev"}, False),
    ("dev_opt_in_not_one", {"SWARM_ALLOW_INSECURE_DEV_KEY": "true"}, False),
    ("dev_opt_in_zero", {"SWARM_ALLOW_INSECURE_DEV_KEY": "0"}, False),
    ("dev_value_without_opt_in", {"SWARM_SIGNING_KEY": "@dev"}, False),
    ("dev_opt_in_with_real_hmac", {**DEV, **HMAC_A}, False),
    ("dev_opt_in_with_ed25519", {**DEV, **ED_A}, False),
    ("dev_opt_in_with_bad_ed25519", {**DEV, "SWARM_ED25519_KEY": "@ed-a|trunc"}, False),
    ("dev_opt_in_with_require", {**DEV, "SWARM_REQUIRE_KEY": "1"}, False),
    ("agent_session_key_file", {"SWARM_AGENT_SESSION": "1", "SWARM_ED25519_KEY_FILE": KEY_FILE}, False),
    ("agent_session_empty_key_file", {"SWARM_AGENT_SESSION": "1", "SWARM_ED25519_KEY_FILE": ""}, False),
    ("agent_session_key_file_with_hmac", {"SWARM_AGENT_SESSION": "1", "SWARM_ED25519_KEY_FILE": KEY_FILE, **HMAC_A}, False),
    ("agent_session_empty_key_file_require", {"SWARM_AGENT_SESSION": "1", "SWARM_ED25519_KEY_FILE": "", "SWARM_REQUIRE_KEY": "1"}, False),
    ("agent_session_dev", {"SWARM_AGENT_SESSION": "1", **DEV}, False),
    ("agent_session_other_value", {"SWARM_AGENT_SESSION": "true", "SWARM_ED25519_KEY_FILE": KEY_FILE}, True),
    ("key_file_hidden_by_hmac", {"SWARM_ED25519_KEY_FILE": KEY_FILE, **HMAC_A}, False),
    ("key_file_hidden_by_ed25519", {"SWARM_ED25519_KEY_FILE": KEY_FILE, **ED_A}, False),
    ("key_file_hidden_by_bad_ed25519", {"SWARM_ED25519_KEY_FILE": KEY_FILE, "SWARM_ED25519_KEY": "@ed-a|trunc"}, False),
    ("key_file_alone", {"SWARM_ED25519_KEY_FILE": KEY_FILE}, True),
    ("key_file_empty", {"SWARM_ED25519_KEY_FILE": ""}, True),
    ("key_file_with_dev_opt_in", {"SWARM_ED25519_KEY_FILE": KEY_FILE, **DEV}, True),
    ("key_file_with_require", {"SWARM_ED25519_KEY_FILE": KEY_FILE, "SWARM_REQUIRE_KEY": "1"}, True),
]

# Body names signed under every environment above (the rest of the bodies use the first four environments).
SIGN_BODIES_ALL_ENVS = ("plain", "unicode")
CORE_ENVS = ("hmac", "ed25519")


# ---------------------------------------------------------------------------------------------------------------------------------
# F-1: the conventional key-file path. Python walks `${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex` with O_NOFOLLOW and
# treats every failure but "not found", a symlinked ancestor and a relative base as a configuration error, which refuses even the dev
# key; the Rust crate must answer exactly where its `lstat` walk agrees and decline everywhere else. Each layout is built in a fresh
# temporary directory (`{root}` in an environment value is that directory) by this generator and by the Rust test harness, from the
# operations recorded in the vector file:
#   ["dir", path]             make the directory and its parents
#   ["file", path]            write a regular file with placeholder text (never a key)
#   ["symlink", path, target] a symbolic link `path` -> `target` (`{root}` substituted in the target)
# As root a `chmod 000` ancestor does not block `lstat` or `open`, so EACCES cannot be produced here; the Rust unit tests inject it.
# ---------------------------------------------------------------------------------------------------------------------------------

Layout = list[list[str]]
LayoutEnv = dict[str, str | None]
KEY_FILE_PATH = "home/.config/agent-swarm/signing-key.hex"
REAL_TREE: Layout = [["dir", "home/.config/agent-swarm"]]
HOME_ONLY: LayoutEnv = {"HOME": "{root}/home", "XDG_CONFIG_HOME": None}
XDG_ONLY: LayoutEnv = {"HOME": None, "XDG_CONFIG_HOME": "{root}/xdg"}
LAYOUT_PLACEHOLDER = "placeholder text, not a key\n"

# (name, what Python's walk finds, operations, HOME/XDG environment). Kinds: absent (cleanly), present, unclear (a configuration error).
LAYOUTS: list[tuple[str, str, Layout, LayoutEnv]] = [
    ("clean_no_config_dir", "absent", [["dir", "home"]], HOME_ONLY),
    ("clean_empty_agent_swarm_dir", "absent", REAL_TREE, HOME_ONLY),
    ("clean_xdg_base", "absent", [["dir", "xdg/agent-swarm"]], XDG_ONLY),
    ("unset_home_and_xdg", "absent", [], {"HOME": None, "XDG_CONFIG_HOME": None}),
    ("empty_xdg_falls_back_to_home", "absent", REAL_TREE, {"HOME": "{root}/home", "XDG_CONFIG_HOME": ""}),
    ("relative_home_beside_absolute_xdg", "absent", [["dir", "xdg"]], {"HOME": "relative-home", "XDG_CONFIG_HOME": "{root}/xdg"}),
    ("present_regular_file", "present", [*REAL_TREE, ["file", KEY_FILE_PATH]], HOME_ONLY),
    ("present_directory", "present", [*REAL_TREE, ["dir", KEY_FILE_PATH]], HOME_ONLY),
    ("present_dangling_symlink", "present", [*REAL_TREE, ["symlink", KEY_FILE_PATH, "{root}/nowhere"]], HOME_ONLY),
    (
        "symlinked_config_dir",
        "unclear",
        [["dir", "home"], ["dir", "real-config"], ["symlink", "home/.config", "{root}/real-config"]],
        HOME_ONLY,
    ),
    ("symlinked_config_dir_dangling", "unclear", [["dir", "home"], ["symlink", "home/.config", "{root}/nowhere"]], HOME_ONLY),
    (
        "symlinked_config_dir_holding_a_file",
        "unclear",
        [
            ["dir", "home"],
            ["dir", "real-config/agent-swarm"],
            ["file", "real-config/agent-swarm/signing-key.hex"],
            ["symlink", "home/.config", "{root}/real-config"],
        ],
        HOME_ONLY,
    ),
    ("symlinked_home", "unclear", [["dir", "real-home/.config/agent-swarm"], ["symlink", "home", "{root}/real-home"]], HOME_ONLY),
    ("symlinked_xdg_base", "unclear", [["dir", "real-xdg/agent-swarm"], ["symlink", "xdg", "{root}/real-xdg"]], XDG_ONLY),
    (
        "symlinked_agent_swarm_dir",
        "unclear",
        [["dir", "home/.config"], ["dir", "real-agent"], ["symlink", "home/.config/agent-swarm", "{root}/real-agent"]],
        HOME_ONLY,
    ),
    ("regular_file_for_config_dir", "unclear", [["dir", "home"], ["file", "home/.config"]], HOME_ONLY),
    ("regular_file_for_agent_swarm_dir", "unclear", [["dir", "home/.config"], ["file", "home/.config/agent-swarm"]], HOME_ONLY),
    ("relative_xdg_config_home", "unclear", [["dir", "home"]], {"HOME": "{root}/home", "XDG_CONFIG_HOME": "relative-xdg"}),
    ("relative_home", "unclear", [], {"HOME": "relative-home", "XDG_CONFIG_HOME": None}),
    ("dot_home", "unclear", [], {"HOME": ".", "XDG_CONFIG_HOME": None}),
    ("relative_home_with_empty_xdg", "unclear", [], {"HOME": "relative-home", "XDG_CONFIG_HOME": ""}),
]
# (name, signing-related environment). The first three need the path to decide; the others show where Python never reaches it.
LAYOUT_MODES: list[tuple[str, Env]] = [
    ("dev_opt_in", DEV),
    ("require", {"SWARM_REQUIRE_KEY": "1"}),
    ("no_key", {}),
    ("agent_session_empty_reference_dev_opt_in", {"SWARM_AGENT_SESSION": "1", "SWARM_ED25519_KEY_FILE": "", **DEV}),
    ("hmac_hides_the_path", {**DEV, **HMAC_A}),
    ("agent_session_dev_opt_in", {"SWARM_AGENT_SESSION": "1", **DEV}),
]
PATH_DECIDES = ("dev_opt_in", "require", "no_key")


def layout_class(kind: str, mode: str) -> tuple[str, str | None]:
    """How Rust must answer a layout vector: ("exact", None), or ("decline", the `Declined` variant it must return).

    A present file is declined by every mode that reaches the path, and an agent session with an empty reference only asks whether
    the file is present (Rust answers a present one exactly and declines an unclear path). An unclear path is declined by every mode
    that reaches it. A clean absence never declines.
    """
    if kind == "absent":
        return "exact", None
    if kind == "present":
        return ("decline", "ConventionalKeyFile") if mode in PATH_DECIDES else ("exact", None)
    return ("decline", "ConventionalKeyPath") if mode in (*PATH_DECIDES, "agent_session_empty_reference_dev_opt_in") else ("exact", None)


def build_layout(root: Path, operations: Layout) -> None:
    for operation in operations:
        target = root / operation[1]
        if operation[0] == "dir":
            target.mkdir(parents=True, exist_ok=True)
        elif operation[0] == "file":
            target.write_text(LAYOUT_PLACEHOLDER, encoding="ascii")
        elif operation[0] == "symlink":
            target.symlink_to(operation[2].replace("{root}", str(root)))
        else:
            raise ValueError(f"unknown layout operation {operation[0]}")


def _check_layout_answer(name: str, kind: str, mode: str, answer: Mapping[str, Any]) -> None:
    """The facts the vector documents, checked against the oracle's real answer."""
    if kind == "absent" and mode == "dev_opt_in" and not (answer["ok"] and answer["dev_key_audit"]):
        raise RuntimeError(f"{name}: the oracle must sign with the dev key on a clean absence")
    if kind != "absent" and mode in PATH_DECIDES and answer["ok"]:
        raise RuntimeError(f"{name}: the oracle must refuse where the path is {kind}")
    if kind != "absent" and mode == "require" and not str(answer["error"]).startswith("E-POLICY: fail-closed: private Ed25519 signing reference"):
        raise RuntimeError(f"{name}: the oracle must report the reference error under REQUIRE_KEY")


def _layout_vectors(dev_key: str) -> list[dict[str, Any]]:
    body = ALL_BODIES["plain"]
    cases: list[dict[str, Any]] = []
    for layout_name, kind, operations, base_env in LAYOUTS:
        for mode, mode_env in LAYOUT_MODES:
            name = f"{layout_name}/{mode}"
            spec: LayoutEnv = {**base_env, **mode_env}
            with tempfile.TemporaryDirectory(prefix="gsd-envelope-layout-") as tmp:
                root = Path(tmp).resolve()
                build_layout(root, operations)
                answer = oracle_sign(spec, body, dev_key, str(root))
                state = oracle_key_state(spec, dev_key, str(root))
            _check_layout_answer(name, kind, mode, answer)
            rust_class, declined_as = layout_class(kind, mode)
            cases.append(
                {
                    "name": name,
                    "kind": kind,
                    "layout": operations,
                    "env": spec,
                    "body_text": body,
                    "python": answer,
                    "key_state": state,
                    "class": rust_class,
                    "declined_as": declined_as,
                }
            )
    return cases


def _key_state_vectors(dev_key: str) -> list[dict[str, Any]]:
    """`real_key_configured`, `insecure_dev_key() is not None` and `signing_config_error` for every combination of Ed25519 seed,
    HMAC secret, dev-key opt-in and `SWARM_REQUIRE_KEY` (no key file anywhere: HOME and XDG name an empty directory)."""
    seeds = [("no_seed", None), ("valid_seed", "@ed-a"), ("bad_seed", "@ed-a|trunc")]
    secrets = [("no_secret", None), ("real_secret", "@hmac-a"), ("dev_value_secret", "@dev"), ("empty_secret", "")]
    cases: list[dict[str, Any]] = []
    for (seed_name, seed), (secret_name, secret), opt_in, require in itertools.product(seeds, secrets, (False, True), (False, True)):
        spec: Env = {}
        if seed is not None:
            spec["SWARM_ED25519_KEY"] = seed
        if secret is not None:
            spec["SWARM_SIGNING_KEY"] = secret
        if opt_in:
            spec["SWARM_ALLOW_INSECURE_DEV_KEY"] = "1"
        if require:
            spec["SWARM_REQUIRE_KEY"] = "1"
        name = f"{seed_name}/{secret_name}/{'opt_in' if opt_in else 'no_opt_in'}/{'require' if require else 'no_require'}"
        cases.append({"name": name, "env": spec, "python": oracle_key_state(spec, dev_key)})
    return cases


# Compressed Edwards points a signature's R must not be: small order, and non-canonical spellings of the same points.
SMALL_ORDER_R = {
    "identity": bytes([1]) + bytes(31),
    "order_two": bytes.fromhex("ec" + "ff" * 30 + "7f"),
    "order_four": bytes(32),
    "order_eight": bytes.fromhex("c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a"),
}
NON_CANONICAL_R = {
    "identity_as_p_plus_one": bytes.fromhex("ee" + "ff" * 30 + "7f"),
    "order_four_as_p": bytes.fromhex("ed" + "ff" * 30 + "7f"),
    "identity_with_sign_bit": bytes([1]) + bytes(30) + bytes([0x80]),
}


def _ed25519_edge_vectors(dev_key: str) -> list[dict[str, Any]]:
    """Verify vectors for crafted Ed25519 signatures under `ED_A`: a small-order or non-canonical R, wrong lengths, and a last
    base64 character with different unused bits (which Python's decoder ignores)."""
    signed = _signed(ED_A, ALL_BODIES["plain"], dev_key)
    sig = str(signed["sig"])
    raw = base64.b64decode(sig.split(":", 1)[1])
    r, s = raw[:32], raw[32:]
    crafted: dict[str, str] = {}
    for label, point in {**{f"small_order_r_{k}": v for k, v in SMALL_ORDER_R.items()}, **{f"non_canonical_r_{k}": v for k, v in NON_CANONICAL_R.items()}}.items():
        crafted[label] = "ed25519:" + base64.b64encode(point + s).decode()
    crafted["small_order_r_identity_with_zero_s"] = "ed25519:" + base64.b64encode(SMALL_ORDER_R["identity"] + bytes(32)).decode()
    crafted["real_r_with_zero_s"] = "ed25519:" + base64.b64encode(r + bytes(32)).decode()
    crafted["trailing_byte"] = "ed25519:" + base64.b64encode(raw + b"\x00").decode()
    crafted["one_byte_short"] = "ed25519:" + base64.b64encode(raw[:-1]).decode()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    data = sig.split(":", 1)[1].rstrip("=")
    last = alphabet.index(data[-1])
    for bits in range(16):
        if bits != last & 15:
            crafted[f"extra_trailing_bits_{bits:02d}"] = "ed25519:" + data[:-1] + alphabet[(last & ~15) | bits] + "=="
    cases = []
    for label, forged in crafted.items():
        envelope = {**signed, "sig": forged}
        cases.append(
            {
                "name": f"ed25519_edge/{label}",
                "env": ED_A,
                "envelope_text": json.dumps(envelope, ensure_ascii=False),
                "python": oracle_verify(ED_A, envelope, dev_key),
                "rust": "match",
            }
        )
    return cases


def _f5_vectors(dev_key: str) -> list[dict[str, Any]]:
    """F-5 of the security review, pinned as Python's current behaviour kept for parity: an `hmac:` signature verifies while an
    Ed25519 signer is configured (also when its seed is malformed), and an `ed25519:` signature is never downgraded to HMAC."""
    hmac_signed = _signed(HMAC_A, ALL_BODIES["plain"], dev_key)
    ed_signed = _signed(ED_A, ALL_BODIES["plain"], dev_key)
    rows: list[tuple[str, Env, dict[str, Any]]] = [
        ("hmac_signature_beside_an_ed25519_signer", {**HMAC_A, **ED_A}, hmac_signed),
        ("hmac_signature_beside_a_malformed_ed25519_seed", {**HMAC_A, "SWARM_ED25519_KEY": "@ed-a|trunc"}, hmac_signed),
        ("ed25519_signature_is_not_downgraded_to_hmac", HMAC_A, ed_signed),
    ]
    return [
        {
            "name": f"f5_parity/{label}",
            "env": env,
            "envelope_text": json.dumps(envelope, ensure_ascii=False),
            "python": oracle_verify(env, envelope, dev_key),
            "rust": "match",
        }
        for label, env, envelope in rows
    ]


def _mutate(env: dict[str, Any], mutation: str) -> dict[str, Any]:
    """Applies a named change to a signed envelope (a deep copy); `sig` handling is by name."""
    out = copy.deepcopy(env)
    sig = out.get("sig")
    if mutation == "none":
        return out
    if mutation == "tamper_ttl":
        out["ttl_s"] = int(out.get("ttl_s", 0)) + 1
    elif mutation == "tamper_payload":
        out["payload"] = {"verdict": "REJECTED"}
    elif mutation == "added_member":
        out["extra"] = None
    elif mutation == "removed_member":
        out.pop("type", None)
    elif mutation == "renamed_key":
        out["tYpe"] = out.pop("type", "x")
    elif mutation == "drop_sig":
        out.pop("sig", None)
    elif mutation == "sig_null":
        out["sig"] = None
    elif mutation == "sig_number":
        out["sig"] = 7
    elif mutation == "sig_list":
        out["sig"] = [sig]
    elif mutation == "sig_empty":
        out["sig"] = ""
    elif mutation == "sig_prefix_only_hmac":
        out["sig"] = "hmac:"
    elif mutation == "sig_prefix_only_ed25519":
        out["sig"] = "ed25519:"
    elif mutation == "sig_unknown_prefix":
        out["sig"] = "rsa:" + str(sig).split(":", 1)[1]
    elif mutation == "sig_prefix_uppercase":
        out["sig"] = str(sig).replace("hmac:", "HMAC:").replace("ed25519:", "ED25519:")
    elif mutation == "sig_prefixes_swapped":
        out["sig"] = ("ed25519:" + str(sig)[5:]) if str(sig).startswith("hmac:") else ("hmac:" + str(sig)[8:])
    elif mutation == "sig_bad_alphabet":
        out["sig"] = str(sig)[:-6] + "@@@@@="
    elif mutation == "sig_truncated":
        out["sig"] = str(sig)[:-4]
    elif mutation == "sig_padding_stripped":
        out["sig"] = str(sig).rstrip("=")
    elif mutation == "sig_extra_padding":
        out["sig"] = str(sig) + "="
    elif mutation == "sig_trailing_newline":
        out["sig"] = str(sig) + "\n"
    elif mutation == "sig_leading_space":
        prefix, rest = str(sig).split(":", 1)
        out["sig"] = prefix + ": " + rest
    elif mutation == "sig_noncanonical_base64":
        out["sig"] = _noncanonical_spelling(str(sig))
    elif mutation == "sig_scalar_plus_group_order":
        out["sig"] = _malleated(str(sig))
    elif mutation == "sig_one_bit_flipped":
        prefix, rest = str(sig).split(":", 1)
        raw = bytearray(base64.b64decode(rest))
        raw[0] ^= 1
        out["sig"] = prefix + ":" + base64.b64encode(bytes(raw)).decode()
    else:
        raise ValueError(f"unknown mutation {mutation}")
    return out


def _noncanonical_spelling(sig: str) -> str:
    """Respells the last data character with different unused low bits: the same bytes, a different string."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    prefix, rest = sig.split(":", 1)
    data = rest.rstrip("=")
    pads = len(rest) - len(data)
    if pads == 0:
        return sig
    last = alphabet.index(data[-1])
    mask = 3 if pads == 1 else 15
    return prefix + ":" + data[:-1] + alphabet[(last & ~mask) | ((last + 1) & mask)] + "=" * pads


def _malleated(sig: str) -> str:
    prefix, rest = sig.split(":", 1)
    raw = base64.b64decode(rest)
    if prefix != "ed25519" or len(raw) != 64:
        return sig
    scalar = int.from_bytes(raw[32:], "little") + GROUP_ORDER
    return prefix + ":" + base64.b64encode(raw[:32] + scalar.to_bytes(32, "little")).decode()


# Verify matrix: (signing environment, verifying environments and whether Rust declines), mutations applied to a correct signature.
VERIFY_UNDER: dict[str, list[tuple[str, Env]]] = {
    "hmac_a": [
        ("hmac_a", HMAC_A),
        ("hmac_b", HMAC_B),
        ("no_key", {}),
        ("ed_a", ED_A),
        ("hmac_a_and_ed_a", {**HMAC_A, **ED_A}),
        ("hmac_a_and_bad_ed", {**HMAC_A, "SWARM_ED25519_KEY": "@ed-a|trunc"}),
        ("hmac_a_require", {**HMAC_A, "SWARM_REQUIRE_KEY": "1"}),
        ("hmac_a_unicode_variant", {"SWARM_SIGNING_KEY": "@hmac-a|uni"}),
    ],
    "ed_a": [
        ("ed_a", ED_A),
        ("ed_b", ED_B),
        ("no_key", {}),
        ("hmac_a", HMAC_A),
        ("ed_a_and_hmac_a", {**ED_A, **HMAC_A}),
        ("ed_a_upper", {"SWARM_ED25519_KEY": "@ed-a|upper"}),
        ("ed_a_spaced", {"SWARM_ED25519_KEY": "@ed-a|spaced"}),
        ("ed_a_newline", {"SWARM_ED25519_KEY": "@ed-a|nl"}),
        ("ed_a_inner_space", {"SWARM_ED25519_KEY": "@ed-a|innerspace"}),
        ("ed_a_31_bytes", {"SWARM_ED25519_KEY": "@ed-a|trunc"}),
    ],
    "dev": [
        ("dev_opt_in", DEV),
        ("no_key", {}),
        ("dev_opt_in_require", {**DEV, "SWARM_REQUIRE_KEY": "1"}),
        ("dev_opt_in_ed_a", {**DEV, **ED_A}),
        ("dev_opt_in_hmac_a", {**DEV, **HMAC_A}),
        ("dev_value_with_opt_in", {**DEV, "SWARM_SIGNING_KEY": "@dev"}),
        ("dev_value_without_opt_in", {"SWARM_SIGNING_KEY": "@dev"}),
        ("dev_opt_in_empty_secret", {**DEV, "SWARM_SIGNING_KEY": ""}),
        ("dev_opt_in_agent_session", {**DEV, "SWARM_AGENT_SESSION": "1"}),
    ],
}
SIGN_FOR_VERIFY: dict[str, Env] = {"hmac_a": HMAC_A, "ed_a": ED_A, "dev": DEV}
MUTATIONS_FOR_SIG_KINDS = [
    "none",
    "tamper_ttl",
    "tamper_payload",
    "added_member",
    "removed_member",
    "renamed_key",
    "drop_sig",
    "sig_null",
    "sig_number",
    "sig_list",
    "sig_empty",
    "sig_prefix_only_hmac",
    "sig_prefix_only_ed25519",
    "sig_unknown_prefix",
    "sig_prefix_uppercase",
    "sig_prefixes_swapped",
    "sig_bad_alphabet",
    "sig_truncated",
    "sig_padding_stripped",
    "sig_extra_padding",
    "sig_trailing_newline",
    "sig_leading_space",
    "sig_noncanonical_base64",
    "sig_scalar_plus_group_order",
    "sig_one_bit_flipped",
]


def _signed(spec: Mapping[str, str], text: str, dev_key: str) -> dict[str, Any]:
    body = json.loads(text)
    with environment(resolve_env(spec, dev_key)):
        oracle.sign_envelope(body, audit=False)
    return cast("dict[str, Any]", body)


def _public_keys() -> dict[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    keys: dict[str, str] = {}
    for label in ("ed-a", "ed-b"):
        seed = bytes.fromhex(key_value("@" + label, ""))
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key()
        keys[label] = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    return keys


def _b64_inputs() -> list[str]:
    inputs = ["".join(chars) for length in range(0, 6) for chars in itertools.product("ABQ=", repeat=length)]
    inputs += [
        "Zm9v", "Zm9vYg==", "Zm9vYmE=", "Zm9vYmFy", "Zm9v=", "Zm9v====", "AAAA=", "AAAAAAAA=", "AAAA===", " Zm9v", "Zm9v ", "Zm 9v", "Zm9v\n",
        "Zm9v\r\n", "Zm-v", "Zm_v", "Zm9\u00e9", "Zm9v\u0000", "A", "AA", "AAA", "====", "AA==AA", "AA=A", "Zm9vYg=", "Zm9vYg===", "Zm9vYg==Zg==",
        "/+/+", "////", "++++",
    ]
    return sorted(set(inputs))


def build_vectors() -> str:
    """The complete vector file, as the exact text to commit."""
    dev_key = public_dev_key()
    canonical = []
    for name, text in ALL_BODIES.items():
        canonical.append(
            {
                "name": name,
                "text": text,
                "python": canonical_of(text),
                "rust": "declined" if name in RUST_DECLINES else "match",
            }
        )

    sign = []
    for env_name, env, declines in SIGN_ENVS:
        body_names = list(ALL_BODIES) if env_name in CORE_ENVS else list(SIGN_BODIES_ALL_ENVS)
        for body_name in body_names:
            text = ALL_BODIES[body_name]
            sign.append(
                {
                    "name": f"{env_name}/{body_name}",
                    "env": env,
                    "body_text": text,
                    "python": oracle_sign(env, text, dev_key),
                    "rust": "declined" if declines or body_name in RUST_DECLINES else "match",
                }
            )

    verify = []
    for signer, verifiers in VERIFY_UNDER.items():
        matching = verifiers[0]
        keyless = next(item for item in verifiers if item[0] == "no_key")
        for body_name in ("plain", "unicode", "floats", "nested", "ints", "bigint"):
            signed = _signed(SIGN_FOR_VERIFY[signer], ALL_BODIES[body_name], dev_key)
            for mutation in MUTATIONS_FOR_SIG_KINDS:
                if mutation == "sig_scalar_plus_group_order" and signer != "ed_a":
                    continue
                if body_name != "plain" and (mutation not in ("none", "tamper_ttl") or (body_name == "floats" and mutation != "none")):
                    continue
                cases = verifiers if mutation == "none" and body_name == "plain" else [matching] if body_name != "plain" else [matching, keyless]
                mutated = _mutate(signed, mutation)
                text = json.dumps(mutated, ensure_ascii=False)
                for verifier_name, verifier_env in cases:
                    verify.append(
                        {
                            "name": f"{signer}/{body_name}/{mutation}/under_{verifier_name}",
                            "env": verifier_env,
                            "envelope_text": text,
                            "python": oracle_verify(verifier_env, mutated, dev_key),
                            "rust": "declined" if body_name in RUST_DECLINES else "match",
                        }
                    )

    non_objects = [("null", "null"), ("list", "[]"), ("string", '"hmac:abc"'), ("number", "1")]
    for label, text in non_objects:
        verify.append(
            {
                "name": f"non_object/{label}",
                "env": HMAC_A,
                "envelope_text": text,
                "python": oracle_verify(HMAC_A, json.loads(text), dev_key),
                "rust": "match",
            }
        )

    verify.extend(_ed25519_edge_vectors(dev_key))
    verify.extend(_f5_vectors(dev_key))

    base64_cases = []
    for text in _b64_inputs():
        decoded = oracle._b64(text)
        base64_cases.append({"input": text, "decoded_hex": None if decoded is None else decoded.hex()})

    vectors = {
        "about": "Generated by tests/parity/gen_envelope_vectors.py from the real swarm/envelope.py. Do not edit by hand.",
        "key_phrase": KEY_PHRASE,
        "key_notation": "@label[|transform...] is sha256(key_phrase + label) in hex; see the generator for the transforms; @dev is dev_key",
        "dev_key": dev_key,
        "dev_key_sha256": oracle._DEV_KEY_SHA256,
        "ed25519_public_keys": _public_keys(),
        "canonical": canonical,
        "sign": sign,
        "verify": verify,
        "base64": base64_cases,
        "key_state": _key_state_vectors(dev_key),
        "conventional": _layout_vectors(dev_key),
    }
    return json.dumps(vectors, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def main(argv: list[str]) -> int:
    target = Path(argv[argv.index("--out") + 1]) if "--out" in argv else VECTOR_PATH
    text = build_vectors()
    if "--check" in argv:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != text:
            print(f"{target} is stale: run python3 tests/parity/gen_envelope_vectors.py", file=sys.stderr)
            return 1
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
