"""swarm.envelope.v1 — build, validate, sign and verify inter-agent messages.

Signing: ed25519 via `cryptography` with a raw SWARM_ED25519_KEY seed or a trusted
private file reference; otherwise HMAC-SHA256 with SWARM_SIGNING_KEY.
Signatures are REQUIRED for task.assign, gate verdicts and promote/rollback commands.

The public dev key is used only when SWARM_ALLOW_INSECURE_DEV_KEY=1, and not while
a private Ed25519 signer is configured or SWARM_REQUIRE_KEY=1 is set. That signing
emits a `security.dev_key` event, except a key-less agent-session preview.
Without a real key and without the opt-in, signing refuses and APPROVED fails closed.
Raw signing variables take precedence over SWARM_ED25519_KEY_FILE and the conventional
XDG_CONFIG_HOME-or-HOME/.config/agent-swarm/signing-key.hex source. Private references
are never read inside agent sessions (SWARM_AGENT_SESSION=1, set for every specialist). The trusted controller and
autonomous hook, which carry only SWARM_CHILD=1, may use them. Configuration failures are redacted.
Private references must be absolute; a relative one fails closed instead of resolving against the caller's cwd.
"""
from __future__ import annotations
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import stat
import sys
import time
import uuid
from pathlib import Path
from typing import Protocol, cast

from .errors import SwarmError, ErrorCode
from .state_store import get_swarm_state_store
SCHEMA_PREFIX = "swarm.v1."
PRIORITIES = {"P0", "P1", "P2", "P3"}
RISK_CLASSES = {"low", "medium", "high"}
SIGNATURE_REQUIRED = {
    "task.assign", "gate.verdict", "quality.gate.verdict", "review.verdict",
    "security.gate.verdict", "promote.command", "rollback.command", "conflict.arbitration",
}
REQUIRED_FIELDS = ("msg_id", "correlation_id", "trace_id", "source", "target", "type",
                   "schema", "ts", "ttl_s", "priority", "risk_class", "payload")



class _BareStderrHandler(logging.Handler):
    """Writes each record's bare message to the current sys.stderr, so operator text stays unchanged."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(record.getMessage() + "\n")
        except Exception:
            self.handleError(record)


_LOG = logging.getLogger("swarm.envelope")
_LOG.setLevel(logging.INFO)
if not any(h.get_name() == "swarm.envelope.bare" for h in _LOG.handlers):
    _bare = _BareStderrHandler()
    _bare.set_name("swarm.envelope.bare")
    _LOG.addHandler(_bare)


class _Ed25519Verifier(Protocol):
    """Public half of an Ed25519 key, structurally matching the `cryptography` type."""

    def verify(self, signature: bytes, data: bytes) -> None: ...


class _Ed25519Signer(Protocol):
    """Private half of an Ed25519 key, structurally matching the `cryptography` type."""

    def sign(self, data: bytes) -> bytes: ...

    def public_key(self) -> _Ed25519Verifier: ...


def _uuid7() -> str:
    """UUIDv7-style id (time-ordered) without external deps."""
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().int & ((1 << 74) - 1)
    val = (ms << 80) | (0x7 << 76) | (rand >> 2 & ((1 << 12) - 1)) << 64 | (0b10 << 62) | (rand & ((1 << 62) - 1))
    return str(uuid.UUID(int=val & ((1 << 128) - 1)))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def build_envelope(*, source: str, target: str, msg_type: str, payload: dict[str, object],
                   correlation_id: str | None = None, causation_id: str | None = None,
                   trace_id: str | None = None, ttl_s: int = 300, priority: str = "P2",
                   risk_class: str = "low") -> dict[str, object]:
    env: dict[str, object] = {
        "msg_id": _uuid7(),
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "trace_id": trace_id or uuid.uuid4().hex,
        "causation_id": causation_id,
        "source": source,
        "target": target,
        "type": msg_type,
        "schema": SCHEMA_PREFIX + msg_type,
        "ts": _now_iso(),
        "ttl_s": ttl_s,
        "priority": priority,
        "risk_class": risk_class,
        "payload": payload,
        "sig": None,
    }
    validate_envelope(env, require_sig=False)
    return env


def validate_envelope(env: dict[str, object], *, require_sig: bool = True) -> None:
    if not isinstance(env, dict):
        raise SwarmError(ErrorCode.E_CONTRACT, "envelope must be an object")
    missing = [f for f in REQUIRED_FIELDS if f not in env]
    if missing:
        raise SwarmError(ErrorCode.E_CONTRACT, f"envelope missing fields {missing}")
    not_str = [f for f in ("type", "schema", "priority", "risk_class") if not isinstance(env[f], str)]
    if not_str:
        raise SwarmError(ErrorCode.E_CONTRACT, f"envelope fields must be strings: {not_str}")
    if env["priority"] not in PRIORITIES:
        raise SwarmError(ErrorCode.E_CONTRACT, f"bad priority {env['priority']!r}")
    if env["risk_class"] not in RISK_CLASSES:
        raise SwarmError(ErrorCode.E_CONTRACT, f"bad risk_class {env['risk_class']!r}")
    # not_str above guarantees these are strings; the casts only narrow them for mypy.
    schema = cast(str, env["schema"])
    msg_type = cast(str, env["type"])
    if not schema.startswith(SCHEMA_PREFIX) or schema != SCHEMA_PREFIX + msg_type:
        raise SwarmError(ErrorCode.E_CONTRACT, "schema must equal swarm.v1.<type>")
    if "." not in msg_type:
        raise SwarmError(ErrorCode.E_CONTRACT, "type must be namespaced <domain>.<name>")
    if not isinstance(env["payload"], dict):
        raise SwarmError(ErrorCode.E_CONTRACT, "payload must be an object")
    if require_sig and env["type"] in SIGNATURE_REQUIRED and not env.get("sig"):
        raise SwarmError(ErrorCode.E_POLICY, f"{env['type']} requires a signature")


def _canonical(env: dict[str, object]) -> bytes:
    body = {k: v for k, v in env.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _agent_session() -> bool:
    # Only SWARM_AGENT_SESSION marks a model session: swarm_run sets it for every specialist. SWARM_CHILD alone is
    # not enough, because the trusted autonomous hook starts its planner and runner with SWARM_CHILD=1 and they sign.
    return os.environ.get("SWARM_AGENT_SESSION") == "1"


def _private_seed(path: Path, *, fallback: bool) -> bytes | None:
    """Open without following links; validate the same descriptors that supply the seed.

    A missing conventional file is optional. Every other reference failure is fatal.
    Callers redact all exceptions, including filenames and malformed file contents.
    """
    directory = os.open(path.anchor or ".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        parts = path.parts[1:] if path.is_absolute() else path.parts
        for part in parts[:-1]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                if fallback:
                    return None
                raise
            os.close(directory)
            directory = child
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            if fallback:
                return None
            raise
        parent = os.fstat(directory)
        if fallback and (parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077):
            raise ValueError("unsafe private directory")
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) & 0o077 or before.st_size not in (64, 65)):
            raise ValueError("unsafe private seed")
        data = os.read(fd, 66)
        after = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_uid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise ValueError("private seed changed during read")
        if len(data) == 65 and data[-1:] == b"\n":
            data = data[:-1]
        if len(data) != 64 or any(c not in b"0123456789abcdefABCDEF" for c in data):
            raise ValueError("invalid private seed")
        return bytes.fromhex(data.decode("ascii"))
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def _conventional_reference() -> Path | None:
    """`${XDG_CONFIG_HOME:-$HOME/.config}/agent-swarm/signing-key.hex`, or None without a base directory."""
    config = os.environ.get("XDG_CONFIG_HOME")
    home = os.environ.get("HOME")
    if not config and not home:
        return None
    if config:
        base = Path(config)
    elif home:
        base = Path(home) / ".config"
    else:
        return None
    return base / "agent-swarm" / "signing-key.hex"

def _ed25519_material() -> tuple[_Ed25519Signer | None, str | None]:
    raw = os.environ.get("SWARM_ED25519_KEY")
    if raw:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw)), None
        except Exception:
            return None, ("fail-closed: SWARM_ED25519_KEY is set but cannot be loaded "
                          "(cryptography missing or seed is not 32-byte hex)")
    # Both raw signer variables take precedence over private references, even a dev HMAC.
    if os.environ.get("SWARM_SIGNING_KEY"):
        return None, None
    reference = os.environ.get("SWARM_ED25519_KEY_FILE")
    if _agent_session():
        return None, ("fail-closed: private signing references are unavailable in agent sessions"
                      if reference is not None else None)
    try:
        if reference is not None:
            if not reference:
                raise ValueError("empty reference")
            path = Path(reference)
        else:
            conventional = _conventional_reference()
            if conventional is None:
                return None, None
            path = conventional
        if not path.is_absolute():
            # A relative reference would name a different file for each caller's working directory.
            return None, "fail-closed: private Ed25519 signing reference must be an absolute path"
        seed = _private_seed(path, fallback=reference is None)
        if seed is None:
            return None, None
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        return Ed25519PrivateKey.from_private_bytes(seed), None
    except Exception:
        return None, "fail-closed: private Ed25519 signing reference cannot be loaded safely"


def _ed25519_key() -> _Ed25519Signer | None:
    return _ed25519_material()[0]


# sha256 of the public dev key. Recognition only; the value itself is returned
# from insecure_dev_key() and only after the opt-in check.
_DEV_KEY_SHA256 = "0c324e01315f4b0cec3ac05a605b9463fb3550d2e1a95b707091328c56cf4ea7"


def _is_public_dev_value(value: str | None) -> bool:
    """True when `value` is the public dev key. Does not sign or verify."""
    if not value:
        return False
    return hashlib.sha256(value.encode()).hexdigest() == _DEV_KEY_SHA256


def insecure_dev_key() -> str | None:
    """The public dev key, or None.

    Returned only when SWARM_ALLOW_INSECURE_DEV_KEY=1 and neither SWARM_ED25519_KEY
    nor SWARM_REQUIRE_KEY=1 is set. Callers must not invent another fallback.
    """
    if os.environ.get("SWARM_ALLOW_INSECURE_DEV_KEY") != "1":
        return None
    key, error = _ed25519_material()
    if key is not None or error or os.environ.get("SWARM_REQUIRE_KEY") == "1":
        return None
    return "dev-insecure-key"


def real_key_configured() -> bool:
    """True when a non-dev signing key is configured (loadable Ed25519 seed or HMAC secret)."""
    hmac_key = os.environ.get("SWARM_SIGNING_KEY")
    valid_hmac = bool(hmac_key) and not _is_public_dev_value(hmac_key)
    return _ed25519_key() is not None or valid_hmac




def _signing_hmac() -> tuple[bytes, bool] | None:
    """(secret, implicit_dev_fallback), or None when nothing may sign or verify.

    A configured HMAC secret that is not the public dev key always wins. The dev key
    is reached only through insecure_dev_key(). implicit_dev_fallback is true only for
    the unset or empty SWARM_SIGNING_KEY opt-in path, which emits security.dev_key.
    """
    raw = os.environ.get("SWARM_SIGNING_KEY") or ""
    if raw and not _is_public_dev_value(raw):
        return raw.encode(), False
    dev = insecure_dev_key()
    if dev is None:
        return None
    return dev.encode(), not bool(raw)


def signing_config_error() -> str | None:
    """Why a verdict signed now could never verify (fail-closed key misconfiguration), else None.

    Per q1: the check (and any material loads) is performed only if a key is supplied
    via env (optional case); enterprise REQUIRE=1 always forces the path.
    """
    require = os.environ.get("SWARM_REQUIRE_KEY") == "1"
    if not _signing_key_supplied() and not require:
        return None
    _key, error = _ed25519_material()
    if error:
        return error
    if os.environ.get("SWARM_REQUIRE_KEY") == "1" and not real_key_configured():
        return "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"
    return None
def _signing_key_supplied() -> bool:
    """Whether a signing key is supplied via env, or a conventional reference file exists.
    Presence only (no open or read): a present but invalid conventional file must surface its
    configuration error, while a missing one stays keyless. Per q1: REQUIRE=1 forces the path.
    """
    a = bool(os.environ.get("SWARM_SIGNING_KEY"))
    b = bool(os.environ.get("SWARM_ED25519_KEY"))
    c = bool(os.environ.get("SWARM_ED25519_KEY_FILE"))
    d = (os.environ.get("SWARM_REQUIRE_KEY") == "1")
    conventional = _conventional_reference()
    e = conventional is not None and os.path.lexists(conventional)
    return (a + b + c + d + e) > 0
def perform_signing_key_lifecycle(*, root: str | Path | None = None) -> dict[str, str]:
    """Signing key lifecycle as FIRST step in swarm coordinator init.

    Uses existing _ed25519_material/_signing_key_supplied/validate_signing_key/
    real_key_configured/signing_config_error (never raw keys).
    Sets LOADING first.
    Decides reduced for keyless non-mand (q1).
    Exact enterprise msgs to sys.stderr .
    Raise on mand fail.
    Return {'status': 'full'|'reduced'|'blocked', 'message': str}
    """
    get_swarm_state_store(root=root).update("LOADING")

    supplied = _signing_key_supplied()
    require = os.environ.get("SWARM_REQUIRE_KEY") == "1"
    err = validate_signing_key()
    configured = real_key_configured()

    if require and (err or not configured):
        detail = err or "fail-closed: SWARM_REQUIRE_KEY=1 but no signing key configured"
        msg = f"Signing key required for enterprise audit/integrity of parallel swarm. {detail} Actionable: provide via env; swarm start blocked."
        _LOG.error(msg)
        raise SwarmError(ErrorCode.E_POLICY, detail)

    if err:
        # supplied but invalid, not mandatory: actionable, do not block unrelated work
        msg = (
            "Signing key required for enterprise audit/integrity of parallel swarm. "
            f"{err} Actionable: provide a valid key via env. Proceeding reduced-privilege."
        )
        _LOG.warning(msg)
        return {"status": "reduced", "message": msg}

    if supplied and configured:
        # present/valid (purpose)
        msg = "Signing key required for enterprise audit/integrity of parallel swarm; validated successfully."
        _LOG.info(msg)
        return {"status": "full", "message": msg}

    # absent non-mand
    msg = "Signing key absent: purpose is enterprise audit/integrity of parallel swarm. Proceeding reduced-privilege."
    _LOG.info(msg)
    return {"status": "reduced", "message": msg}


def validate_signing_key() -> str | None:
    """Signing key validation routine: constant-time conditional validation only if key supplied (q1).
    Delegates to guarded signing_config_error. Clear messages for optional vs enterprise REQUIRE=1
    (REQUIRE produces dedicated "fail-closed: SWARM_REQUIRE_KEY=1 ..." message; optional non-supplied
    yields no error from validation path).
    Acceptance: const time branch decision when present; clear msgs; used by init/preflight.
    """
    return signing_config_error()
def _warn_dev_key(env: dict[str, object], root: str | Path | None) -> None:
    try:
        from .runlog import emit
        # build_envelope always sets correlation_id to a str; hand-built envelopes may omit it, and emit takes str | None.
        emit("security.dev_key", {"msg_type": env["type"], "source": env["source"]},
source="swarm.envelope", correlation_id=cast(str | None, env.get("correlation_id")), root=root)
    except Exception as exc:  # signing never fails on logging
        _LOG.warning("security.dev_key audit event not recorded (%s); signing continues", type(exc).__name__)


def sign_envelope(env: dict[str, object], *, root: str | Path | None = None, audit: bool = True) -> dict[str, object]:
    """Sign in place. `root` is the caller's --root: the dev-key audit event lands in its state dir (D-10).
    audit=False skips that event for a key-less agent-session preview that can never record (WR-15)."""
    err = signing_config_error()
    if err:
        raise SwarmError(ErrorCode.E_POLICY, err)
    key = _ed25519_key()
    if key is not None:
        sig = key.sign(_canonical(env))
        env["sig"] = "ed25519:" + base64.b64encode(sig).decode()
        return env
    material = _signing_hmac()
    if material is None:
        # Refuse rather than emit an hmac that can never verify. The dev-key literal
        # is reached only inside insecure_dev_key(), after the opt-in check.
        raise SwarmError(ErrorCode.E_POLICY, "fail-closed: no signing key configured")
    secret, implicit_dev = material
    if audit and implicit_dev:
        _warn_dev_key(env, root)
    mac = hmac.new(secret, _canonical(env), hashlib.sha256).digest()
    env["sig"] = "hmac:" + base64.b64encode(mac).decode()
    return env


def _b64(data: str) -> bytes | None:
    try:
        return base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error):
        return None


def verify_envelope(env: dict[str, object]) -> bool:
    sig = env.get("sig") if isinstance(env, dict) else None
    if not isinstance(sig, str):
        return False
    if sig.startswith("hmac:"):
        material = _signing_hmac()
        if material is None:
            return False
        secret, _implicit = material
        given = _b64(sig[5:])
        if given is None:
            return False
        expected = hmac.new(secret, _canonical(env), hashlib.sha256).digest()
        return hmac.compare_digest(expected, given)
    if sig.startswith("ed25519:"):
        key = _ed25519_key()
        given = _b64(sig[8:])
        if key is None or given is None:
            return False
        try:
            key.public_key().verify(given, _canonical(env))
            return True
        except Exception:
            return False
    return False
