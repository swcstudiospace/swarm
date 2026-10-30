"""GSD_ENGINE selector shared by the scripts that have, or will have, a route on the Rust engine (`gsd_engine`).

A script describes what it serves as DATA plus small hooks (`Route`, `WriteFlow`, `Selector`); `select_engine` runs the common flow:

1. `SWARM_PURE_PYTHON=1` and `GSD_ENGINE=python` are Python; unset or empty is the default row of the route that claims the argv;
   `rust` is explicit; any other value is `E-INPUT` (the value is not echoed). A default call reaches the binary only when a
   binary is found and the route's grammar serves the argv; an explicit call always goes to the binary, which is required.
2. The binary is `GSD_ENGINE_BIN` when set (it is then the only candidate), else `target/release/gsd_engine` when executable.
3. The binary is started with an allowlist of environment names, the route's grant included (the script's `binary_env` hook),
   and the whole call is bounded by `GSD_RUST_TIMEOUT_S` (default `RUST_CALL_TIMEOUT_S`; E-TIMEOUT never falls back).
4. A read answer is `run_read`: Python answers whenever the binary says it cannot (`RUST_FALLBACK_CODES`, an unusable reply, a
   binary that cannot start unless explicit). A write answer is `run_write`: a write is never run twice, so the outcome table
   (see its docstring) lets Python answer only when the binary says that nothing was written, and refuses every other doubtful
   outcome with a typed E-INTERNAL. After the binary's COMMIT the script's `commit` hook replays the effects it owns.

What is generic and what a script supplies (the inventory the code was cut along):

* generic, here: engine value parsing, binary resolution, the outer timeout, `BINARY_ENV_NAMES` (what any child needs), the core
  dump guard (`no_core_dumps`), reply parsing, the fallback policy, the write outcome table, the refusal and unknown-outcome
  replies, the unregistered-route policy below.
* per-route data (`Route`): which argv the route claims (`claims`), the grammar the binary serves (`parse`: the parsed call, or
  None), the default engine row (`default`, read at call time so a test or a flip changes it in one place), the name under which
  the environment policy is looked up (`env_route`).
* per-script code (`WriteFlow`, `Selector`): the state read before the binary starts, the effects replay and reply settlement
  after its COMMIT, the words of the unknown-outcome hint, the key grants (`binary_env`), the child guard (`preexec`), the
  variables forwarded in the request (`env_keys`).

An argv that no route claims is an unregistered route, which is what every script is until a unit gives it a `Route`. The binary
refuses such a request with `E-DEP` and `write_state = "not_started"`, and the selector hands that refusal to Python, so the
selector answers it without starting the binary: a default call is Python; an explicit one is Python once the binary is named
and startable (the same `E-INPUT` and `E-DEP` refusals as for a served argv come first).
"""
from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from swarm.errors import ErrorCode
from swarm.script_base import AgentScript

RUST_CALL_TIMEOUT_S = 660  # the binary's own deadline is 600 s and does not cover its start-up or its stdin read
RUST_FALLBACK_CODES = frozenset({"E-INTERNAL", "E-DEP", "E-CAPACITY", "E-CONTRACT", "E-INPUT"})
RELEASE_BINARY = Path(__file__).resolve().parent.parent / "target" / "release" / "gsd_engine"
# The environment the binary is started with (an explicit allowlist of names, never a prefix): what a process needs to run plus the
# one variable the binary reads (GSD_RUST_BACKEND; ids and the swarm directory travel in the request's `env`). Signing material is
# a per-route grant, which a script's `binary_env` hook adds.
BINARY_ENV_NAMES = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "GSD_RUST_BACKEND"})


def no_route_env(route: str) -> dict[str, str]:
    """The `binary_env` of a selector without routes: there is nothing to start, so a request for an environment is a bug."""
    raise KeyError(route)


@dataclass(frozen=True)
class WriteFlow:
    """What a script does around the one run of the binary for a route that writes. `call` is what `Route.parse` returned for the
    argv, or None when the argv is outside the grammar (reachable only under an explicit `rust`)."""
    before: Callable[[Any], Any]  # (call) -> what is read before the binary starts, handed to `commit`
    commit: Callable[[Any, Any, str, dict[str, Any], bytes], tuple[dict[str, Any], bytes]]  # (call, before, effects_path, reply, stdout)
    unknown: Callable[[Any], tuple[str, dict[str, Any]]]  # (call) -> (hint appended to the message, details added to write_state)
    preexec: Callable[[str], Callable[[], None] | None]  # (env route) -> the child's preexec_fn, None for none
    on_final: Callable[[Any, str], None] | None = None  # (call, effects_path) after a final refusal, before the reply is printed


@dataclass(frozen=True)
class Route:
    """One argv grammar of a script that the binary serves. `write` is set for a route that writes (`run_write`), else it reads."""
    claims: Callable[[list[str]], bool]  # the argv belongs to this route, served or not; the first route that claims it decides
    parse: Callable[[list[str]], Any]  # the call when the argv is inside the grammar the binary serves, else None
    default: Callable[[], str]  # the engine an unset or empty GSD_ENGINE resolves to: "python" or "rust"
    env_route: Callable[[list[str], Any], str]  # (argv, call) -> the key of the environment policy for this call
    write: WriteFlow | None = None


@dataclass(frozen=True)
class Selector:
    """A script's selector: the script that records external replies and answers refusals, and the routes it registered (none yet)."""
    script: AgentScript
    routes: tuple[Route, ...] = ()
    env_keys: tuple[str, ...] = ()  # variables of this process forwarded in the request's `env`
    binary_env: Callable[[str], dict[str, str]] = no_route_env


def no_core_dumps() -> None:
    """`preexec_fn` of a child that holds signing keys: `RLIMIT_CORE` 0 (soft and hard, so the child cannot raise it), so a crash of
    the process that holds keys in its environment and heap leaves no core file that carries them. It runs in the child between
    fork and exec and makes one system call (no import, no allocation worth the name: this process may have threads)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def refuse(selector: Selector, code: ErrorCode, message: str) -> int:
    """Prints an error reply shaped like the Rust engine's and returns exit 2."""
    body = {"agent": selector.script.agent_id, "script": selector.script.name, "status": "error",
            "error": {"code": code.value, "message": message, "details": {}, "handling": code.standard_handling}}
    print(json.dumps(body, indent=2))
    return AgentScript.EXIT_ERROR


def reply_object(stdout: bytes) -> dict[str, Any] | None:
    try:
        body = json.loads(stdout.decode("utf-8"))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def error_code(reply: dict[str, Any]) -> str | None:
    error = reply.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def error_details(reply: dict[str, Any]) -> dict[str, Any]:
    error = reply.get("error")
    details = error.get("details") if isinstance(error, dict) else None
    return details if isinstance(details, dict) else {}


def rust_timeout_s() -> int:
    """GSD_RUST_TIMEOUT_S when it is a positive integer, otherwise RUST_CALL_TIMEOUT_S."""
    raw = os.environ.get("GSD_RUST_TIMEOUT_S", "")
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return int(raw)
    return RUST_CALL_TIMEOUT_S


def run_read(selector: Selector, route: Route, argv: list[str], binary: str, *, explicit: bool) -> int | None:
    """Answers the call on the Rust engine at `binary`. None hands the call to the Python body.

    None (fallback) is returned exactly for these triggers: a reply whose error.code is in RUST_FALLBACK_CODES;
    an argument the binary cannot take (the request is not UTF-8 encodable); an exit-2 reply without a typed
    error.code; a reply that is not a JSON object; an exit code other than 0, 1 or 2; a binary that cannot be
    started when the call did not ask for `rust` explicitly (`explicit` False); and a run-log record that cannot be
    written (AgentScript.record_external_reply raised, so the Python body answers and applies its own failure rules).
    A Rust answer is recorded in the run log before it is printed, as the Python path's own `main` does (D1).
    Refused with exit 2 and never a fallback: a binary that cannot be started when `explicit`, and a binary that
    did not answer within the call deadline (E-TIMEOUT), explicit or not.
    """
    request = {"agent": selector.script.agent_id, "script": selector.script.name, "argv": argv, "root": os.getcwd(),
               "env": {key: os.environ[key] for key in selector.env_keys if os.environ.get(key)}}
    try:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return None  # fallback: an argument the binary cannot take
    try:
        done = subprocess.run([binary], input=payload, stdout=subprocess.PIPE, check=False, timeout=rust_timeout_s(),
                              env=selector.binary_env(route.env_route(argv, None)))
    except subprocess.TimeoutExpired:
        return refuse(selector, ErrorCode.E_TIMEOUT, "the gsd_engine binary did not answer within the call deadline")
    except OSError:
        if not explicit:
            return None  # fallback: the default engine never turns an unusable binary into an error
        return refuse(selector, ErrorCode.E_DEP, "the gsd_engine binary named by GSD_ENGINE_BIN could not be started")
    reply = reply_object(done.stdout)
    if reply is None:
        return None
    if done.returncode not in (AgentScript.EXIT_OK, AgentScript.EXIT_FAIL, AgentScript.EXIT_ERROR):
        return None
    if done.returncode == AgentScript.EXIT_ERROR:
        code = error_code(reply)
        # fallback: an exit-2 reply without a typed error.code, or one whose code the Python body serves
        if code is None or code in RUST_FALLBACK_CODES:
            return None
    try:
        selector.script.record_external_reply(argv, reply, done.returncode)
    except Exception:  # noqa: BLE001 - any failure to record hands the call to the Python body, which answers on its own rules
        return None
    sys.stdout.buffer.write(done.stdout)
    sys.stdout.flush()
    return done.returncode


def warn(selector: Selector, message: str) -> None:
    print(f"{selector.script.name}: {message}", file=sys.stderr)


def unknown_outcome(selector: Selector, route: Route, argv: list[str], call: Any, why: str) -> int:
    """Prints the typed E-INTERNAL for a write whose outcome is unknown, records it, and returns exit 2. The call is not repeated."""
    assert route.write is not None
    hint, extra = route.write.unknown(call)
    body = {"agent": selector.script.agent_id, "script": selector.script.name, "status": "error",
            "error": {"code": ErrorCode.E_INTERNAL.value, "message": f"outcome unknown: {why}; the call was NOT repeated. {hint}",
                      "details": {"write_state": "unknown", **extra}, "handling": ErrorCode.E_INTERNAL.standard_handling}}
    try:
        selector.script.record_external_reply(argv, body, AgentScript.EXIT_ERROR)
    except Exception as exc:  # noqa: BLE001 - the refusal is printed whether or not the run log takes it
        warn(selector, f"the unknown-outcome refusal could not be recorded ({type(exc).__name__})")
    print(json.dumps(body, indent=2))
    return AgentScript.EXIT_ERROR


def run_write(selector: Selector, route: Route, argv: list[str], binary: str, *, explicit: bool) -> int | None:
    """Answers a write call on the Rust engine at `binary`. None hands the call to the Python body, once.

    The request is the read request plus `effects_path`, a temp file this selector owns and removes; the binary fills it after
    its COMMIT. Outcome table (Design 7), by what the binary did:

    * exit 0 or 1 with a JSON object: committed (or a dry run). The route's `commit` hook runs the effects the Python path would
      have run, in its order, and returns the reply and the bytes to record and print (the row it settled may differ from the
      binary's own); a hook that finds the effects lost says so itself and nothing is run again. The answer is recorded (D1)
      and printed.
    * exit 2 with `error.details.write_state == "not_started"`: nothing was written; the Python body answers.
    * exit 2 with write_state `final`, or absent on any code but E-INTERNAL (E-CONTRACT, E-POLICY, E-INPUT of an unknown task or
      of an invalid id, Python's `max_attempts exceeded` E-TIMEOUT that names its task, ...): the binary's own refusal, with
      Python's text; recorded and printed as it is.
    * everything else is an unknown outcome, answered with a typed E-INTERNAL (`unknown_outcome`), no Python run, no fallback:
      the call deadline (the subprocess deadline, or an E-TIMEOUT that names no task), a signal or any exit code outside 0, 1, 2,
      an unparsable reply, E-INTERNAL without a write_state, an unrecognised write_state.
    * not started: a binary that cannot be started is Python's answer under the default engine and E-DEP under `explicit`,
      as for reads; so is an argument the binary cannot take (not UTF-8 encodable).
    """
    assert route.write is not None
    call = route.parse(argv)
    before = route.write.before(call)
    try:
        handle, effects_path = tempfile.mkstemp(prefix="gsd-effects-", suffix=".json")
    except OSError:
        return None if not explicit else refuse(selector, ErrorCode.E_DEP, "the effects file for the gsd_engine binary could not be created")
    os.close(handle)
    try:
        return _answer_write(selector, route, argv, binary, explicit, call, before, effects_path)
    finally:
        try:
            os.unlink(effects_path)
        except OSError:
            pass


def _answer_write(selector: Selector, route: Route, argv: list[str], binary: str, explicit: bool, call: Any, before: Any,
                  effects_path: str) -> int | None:
    assert route.write is not None
    request = {"agent": selector.script.agent_id, "script": selector.script.name, "argv": argv, "root": os.getcwd(),
               "env": {key: os.environ[key] for key in selector.env_keys if os.environ.get(key)}, "effects_path": effects_path}
    try:
        payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return None  # fallback: an argument the binary cannot take
    try:
        # A child that holds keys is started with core dumps off (a preexec_fn that fails is a start that did not happen).
        env_route = route.env_route(argv, call)
        done = subprocess.run([binary], input=payload, stdout=subprocess.PIPE, check=False, timeout=rust_timeout_s(),
                              env=selector.binary_env(env_route), preexec_fn=route.write.preexec(env_route))
    except subprocess.TimeoutExpired:
        return unknown_outcome(selector, route, argv, call, "the gsd_engine binary did not answer within the call deadline")
    except (OSError, subprocess.SubprocessError):
        if not explicit:
            return None  # fallback: nothing was started
        return refuse(selector, ErrorCode.E_DEP, "the gsd_engine binary named by GSD_ENGINE_BIN could not be started")
    code = done.returncode
    if code not in (AgentScript.EXIT_OK, AgentScript.EXIT_FAIL, AgentScript.EXIT_ERROR):
        how = f"killed by signal {-code}" if code < 0 else f"exit status {code}"
        return unknown_outcome(selector, route, argv, call, f"the gsd_engine binary ended with {how}")
    reply = reply_object(done.stdout)
    if reply is None:
        return unknown_outcome(selector, route, argv, call, "the gsd_engine binary printed no JSON object")
    stdout = done.stdout
    if code == AgentScript.EXIT_ERROR:
        failure, details = error_code(reply), error_details(reply)
        state = details.get("write_state")
        # Python's own `max_attempts exceeded` refusal is E-TIMEOUT too, and names its task in details; the call deadline's does not
        if failure == ErrorCode.E_TIMEOUT.value and not isinstance(details.get("task_id"), str):
            return unknown_outcome(selector, route, argv, call, "the gsd_engine binary reported E-TIMEOUT")
        if state == "not_started":
            return None  # Python answers: the binary wrote nothing
        if failure is None or state not in (None, "final") or (state is None and failure == ErrorCode.E_INTERNAL.value):
            return unknown_outcome(selector, route, argv, call, "the gsd_engine binary refused without saying that nothing was written")
        if route.write.on_final is not None:
            route.write.on_final(call, effects_path)
    else:
        reply, stdout = route.write.commit(call, before, effects_path, reply, stdout)
    try:
        selector.script.record_external_reply(argv, reply, code)
    except Exception as exc:  # noqa: BLE001 - a write is never handed to Python once the binary has answered
        warn(selector, f"the run-log record of the binary's answer failed ({type(exc).__name__}); the answer stands and was not repeated")
    sys.stdout.buffer.write(stdout)
    sys.stdout.flush()
    return code


def select_engine(selector: Selector, argv: list[str], *, release_binary: Path | None = None) -> int | None:
    """Resolves the engine for one call. An int is the final exit code (the selector already printed the answer); None runs the Python body.

    `release_binary` is the fallback binary when GSD_ENGINE_BIN is unset: `RELEASE_BINARY` (read now, not at import) unless the script
    passes its own, as orch_status passes the value of its module so that a patched name there is the one used."""
    if os.environ.get("SWARM_PURE_PYTHON") == "1":
        return None
    requested = os.environ.get("GSD_ENGINE", "")
    if requested == "python":
        return None
    if requested not in ("", "rust"):
        return refuse(selector, ErrorCode.E_INPUT, "GSD_ENGINE must be 'python' or 'rust'")
    explicit = requested == "rust"
    route = next((candidate for candidate in selector.routes if candidate.claims(argv)), None)
    if not explicit and (route is None or route.default() != "rust"):
        return None
    binary = os.environ.get("GSD_ENGINE_BIN", "")
    release = RELEASE_BINARY if release_binary is None else release_binary
    if not binary and release.is_file() and os.access(release, os.X_OK):
        binary = str(release)
    if not explicit:
        if route is None or route.parse(argv) is None or not binary or not Path(binary).is_file() or not os.access(binary, os.X_OK):
            return None
    if os.environ.get("GSD_RUST_BACKEND", "") not in ("", "host", "sandbox"):
        return refuse(selector, ErrorCode.E_INPUT, "GSD_RUST_BACKEND must be 'host' or 'sandbox' when set")
    if not binary:
        return refuse(selector, ErrorCode.E_DEP, "GSD_ENGINE=rust needs GSD_ENGINE_BIN set to the gsd_engine binary")
    if route is None:
        if not Path(binary).is_file() or not os.access(binary, os.X_OK):
            return refuse(selector, ErrorCode.E_DEP, "the gsd_engine binary named by GSD_ENGINE_BIN could not be started")
        return None  # unregistered route: the binary would answer E-DEP "not_started", which hands the call to Python
    return (run_write if route.write is not None else run_read)(selector, route, argv, binary, explicit=explicit)
