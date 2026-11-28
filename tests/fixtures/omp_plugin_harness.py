#!/usr/bin/env python3
"""Real OMP plugin-manager matrix harness for AC-14.7-01 (Phase 14.7, Wave 0).

Drives the actual `omp` binary only:
- plugin manager: `omp plugin install|enable|disable|uninstall <target>` and `omp plugin list [--json]`;
- fresh host start: `omp --mode rpc --no-session` (no `-p`, no prompt). The only RPC requests ever
  written are `get_state` and `get_available_commands`; any other RPC type is refused.

Every case runs in its own scratch directory with an isolated HOME, agent dir and XDG tree, so
installs and links never touch the operator's plugin tree. Host profile variables and credentials
(`SWARM_*`, `OMP_*`, `PI_*`, `*_API_KEY`, `*TOKEN*`, ...) are stripped from the child environment.
Transcripts are sanitized (scratch, HOME, workspace, repo and omp paths replaced by placeholders)
and written only under the transcript directory.

This module performs no work on import. Every host step requires SWARM_RUN_PLUGIN_MATRIX=1.
The harness records facts; it does not judge pass/fail. Tests assert on the returned structures.

Usage (explicit, operator-invoked only):
    SWARM_RUN_PLUGIN_MATRIX=1 python3 tests/fixtures/omp_plugin_harness.py --matrix [--out DIR] [--omp PATH] [--case NAME ...]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_DIR = REPO_ROOT / "omp"
PKG_ENTRY = PKG_DIR / "src" / "index.ts"
PACKAGE_NAME = "agent-swarm-omp"
MATRIX_ENV = "SWARM_RUN_PLUGIN_MATRIX"
TIMEOUT_ENV = "SWARM_MATRIX_TIMEOUT"
DEFAULT_TIMEOUT_S = 240.0
CASES = (
    "installed-only",
    "configured-only",
    "explicit-source",
    "duplicate-alias",
    "project-shadow",
    "relocation",
)

# Only these RPC requests are ever written. Both are read-only; neither starts a model turn.
RPC_REQUESTS = (
    {"id": "state", "type": "get_state"},
    {"id": "commands", "type": "get_available_commands"},
)
RPC_TYPES = frozenset(req["type"] for req in RPC_REQUESTS)

# Entry names the discovery summary counts (leading "/" stripped before comparison).
DISCOVERY_NAMES = (
    f"skill:{PACKAGE_NAME}/a01-orchestrator",
    f"skill:{PACKAGE_NAME}/swarm-orchestrate",
    "skill:a01-orchestrator",
    "skill:agent-swarm/a01-orchestrator",
)
_STRIP_PREFIXES = ("SWARM_", "OMP_", "PI_", "XDG_", "ANTHROPIC_", "OPENAI_", "GOOGLE_", "GEMINI_", "AWS_", "AZURE_")
_STRIP_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
_DIAG_MARKERS = ("warn", "error", "failed", "shadow", "exception")


class MatrixGateClosed(RuntimeError):
    """A host step was requested without SWARM_RUN_PLUGIN_MATRIX=1."""


class GuardViolation(RuntimeError):
    """An omp invocation fell outside the matrix allowlist (print mode, model turn, unknown route, foreign path)."""


@dataclass
class _Ctx:
    case: str
    out: Path
    omp: str
    scratch: Path
    home: Path
    workspace: Path
    env: dict[str, str]
    subs: dict[str, str]
    steps: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _RpcRun:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


def matrix_enabled() -> bool:
    """True only when the operator explicitly opted into real plugin-manager and host steps."""
    return os.environ.get(MATRIX_ENV) == "1"


def _require_gate() -> None:
    if not matrix_enabled():
        raise MatrixGateClosed(f"plugin matrix is gated; set {MATRIX_ENV}=1 to run real plugin-manager steps")


def _timeout_s() -> float:
    raw = os.environ.get(TIMEOUT_ENV)
    return float(raw) if raw else DEFAULT_TIMEOUT_S


def _guard_omp_argv(args: list[str], scratch: Path | None) -> None:
    """`args` excludes the binary. Allowlist: --version, plugin list/enable/disable/uninstall/install, RPC start."""
    if any(a in ("-p", "--print") for a in args):
        raise GuardViolation("print mode (-p/--print) starts a model session; refused")
    if args == ["--version"]:
        return
    if args in (["plugin", "list"], ["plugin", "list", "--json"]):
        return
    if len(args) == 3 and args[0] == "plugin" and args[1] in ("enable", "disable", "uninstall") and args[2] == PACKAGE_NAME:
        return
    if len(args) == 3 and args[0] == "plugin" and args[1] == "install":
        src = Path(args[2]).resolve()
        if src == PKG_DIR.resolve() or (scratch is not None and scratch.resolve() in src.parents):
            return
        raise GuardViolation(f"install source is neither the repo package nor matrix scratch: {args[2]}")
    if args[:3] == ["--mode", "rpc", "--no-session"]:
        rest = args[3:]
        if rest == [] or rest == ["-e", str(PKG_DIR)]:
            return
    raise GuardViolation(f"omp argv outside the matrix allowlist: {args!r}")


# Scratch-only model config for each case's isolated agent dir. It names the model id and holds no
# credential: `auth: none` reads no key, and the closed-port baseUrl means no request can succeed.
# Without it a fresh start has no default model and exits before answering RPC.
SCRATCH_MODELS_YML = (
    "providers:\n"
    "  scratch:\n"
    "    baseUrl: http://127.0.0.1:9/v1\n"
    "    api: anthropic-messages\n"
    "    auth: none\n"
    "    models:\n"
    "      - id: claude-haiku-5-5\n"
)


def _write_scratch_models(home: Path) -> None:
    agent_dir = home / ".omp" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "models.yml").write_text(SCRATCH_MODELS_YML, encoding="utf-8")


def _isolated_env(home: Path) -> dict[str, str]:
    if home.resolve() == Path.home().resolve():
        raise GuardViolation("isolated HOME resolves to the operator HOME; refusing")
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper.startswith(_STRIP_PREFIXES) or any(marker in upper for marker in _STRIP_MARKERS):
            continue
        env[key] = value
    agent_dir = home / ".omp" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "PI_CODING_AGENT_DIR": str(agent_dir),
        }
    )
    return env


def _resolve_omp(explicit: str | None) -> str:
    candidate = explicit or os.environ.get("OMP_BIN") or shutil.which("omp")
    if not candidate:
        raise FileNotFoundError("omp binary not found; pass --omp or set OMP_BIN")
    return str(Path(candidate).absolute())


def _subs(home: Path, workspace: Path, scratch: Path, omp: str) -> dict[str, str]:
    return {
        str(scratch): "<SCRATCH>",
        str(home): "<HOME>",
        str(workspace): "<WS>",
        str(REPO_ROOT): "<REPO>",
        omp: "<OMP>",
    }


def _sanitize(text: str, subs: dict[str, str]) -> str:
    for raw, token in sorted(subs.items(), key=lambda kv: len(kv[0]), reverse=True):
        if raw:
            text = text.replace(raw, token)
    return text


def _s(ctx: _Ctx, value: Any) -> str | None:
    return None if value is None else _sanitize(str(value), ctx.subs)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)


def _is_diagnostic(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in _DIAG_MARKERS)


def _record(
    ctx: _Ctx,
    label: str,
    args: list[str],
    exit_code: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool,
    *,
    kind: str,
    inline: bool,
) -> dict[str, Any]:
    index = len(ctx.steps) + 1
    rel = Path(ctx.case) / f"{index:02d}-{label}.txt"
    body = _sanitize(
        f"$ {ctx.omp} {' '.join(args)}\n# exit={exit_code} timed_out={str(timed_out).lower()}\n"
        f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n",
        ctx.subs,
    )
    dest = ctx.out / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")
    rec: dict[str, Any] = {
        "index": index,
        "kind": kind,
        "label": label,
        "argv": _sanitize(" ".join(args), ctx.subs),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "transcript": rel.as_posix(),
        "transcript_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "diagnostics": [_sanitize(line, ctx.subs) for line in stderr.splitlines() if _is_diagnostic(line)],
    }
    if inline:
        rec["stdout"] = _sanitize(stdout, ctx.subs)
        rec["stderr"] = _sanitize(stderr, ctx.subs)
    ctx.steps.append(rec)
    return rec


def _plugin(ctx: _Ctx, label: str, args: list[str]) -> dict[str, Any]:
    _guard_omp_argv(args, ctx.scratch)
    try:
        proc = subprocess.run(
            [ctx.omp, *args],
            cwd=ctx.workspace,
            env=ctx.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_timeout_s(),
            check=False,
        )
        code, out, err, timed = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        code, out, err, timed = None, _as_text(exc.stdout), _as_text(exc.stderr), True
    return _record(ctx, label, args, code, out, err, timed, kind="plugin", inline=True)


def _find_named(node: Any, name: str) -> dict[str, Any] | None:
    """First dict that names `name` either as its "name" value or as a key (manager JSON shape is not assumed)."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if cur.get("name") == name or name in cur:
                return cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return None


def _plugin_list(ctx: _Ctx, label: str) -> dict[str, Any]:
    """Records `plugin list` (text) and `plugin list --json`; returns the identity view of the package."""
    text_rec = _plugin(ctx, f"{label}-text", ["plugin", "list"])
    json_rec = _plugin(ctx, f"{label}-json", ["plugin", "list", "--json"])
    try:
        parsed: Any = json.loads(json_rec.get("stdout", ""))
        row = _find_named(parsed, PACKAGE_NAME)
        parsed_ok = True
    except json.JSONDecodeError:
        row, parsed_ok = None, False
    identity = {
        "text_rows": [line for line in text_rec.get("stdout", "").splitlines() if PACKAGE_NAME in line],
        "json_parsed": parsed_ok,
        "json_row": row,
    }
    json_rec["identity"] = identity
    return identity


def _frames(text: str) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            frames.append(value)
    return frames


def _response(frames: Iterable[dict[str, Any]], rtype: str) -> dict[str, Any] | None:
    rid = next(req["id"] for req in RPC_REQUESTS if req["type"] == rtype)
    for frame in frames:
        if frame.get("type") == "response" and (frame.get("id") == rid or frame.get("command") == rtype):
            return frame
    return None


def _responses_complete(text: str) -> bool:
    frames = _frames(text)
    return all(_response(frames, rtype) is not None for rtype in RPC_TYPES)


def _names(node: Any) -> list[str]:
    """Every string under a "name" key, at any depth (entries may be nested; duplicates are kept)."""
    found: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            value = cur.get("name")
            if isinstance(value, str):
                found.append(value)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _normalize(name: str) -> str:
    return name[1:] if name.startswith("/") else name


def _discover(frames: list[dict[str, Any]]) -> dict[str, Any]:
    state = _response(frames, "get_state")
    commands = _response(frames, "get_available_commands")
    command_names = _names(commands.get("data")) if commands else []
    counts = Counter(_normalize(name) for name in command_names)
    tool_counts = Counter(name for name in _names(state.get("data")) if name.startswith("swarm_")) if state else Counter()
    return {
        "get_state_success": bool(state and state.get("success") is True),
        "get_available_commands_success": bool(commands and commands.get("success") is True),
        "command_entries": len(command_names),
        "swarm_command_entries": counts["swarm"],
        "named_entries": {name: counts[name] for name in DISCOVERY_NAMES},
        "plugin_skill_entries": sum(n for name, n in counts.items() if name.startswith(f"skill:{PACKAGE_NAME}/")),
        "agent_skill_entries": sum(n for name, n in counts.items() if name.startswith("skill:a0")),
        "guarded_tool_entries": dict(sorted(tool_counts.items())),
    }


def _drain(stream: Any, sink: list[str]) -> None:
    for line in iter(stream.readline, ""):
        sink.append(line)


def _rpc_exchange(argv: list[str], cwd: Path, env: dict[str, str], timeout_s: float) -> _RpcRun:
    """Writes the allowlisted RPC requests, waits for their responses (or exit/timeout), then closes stdin."""
    payload = "".join(json.dumps(req) + "\n" for req in RPC_REQUESTS)
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out_lines: list[str] = []
    err_lines: list[str] = []
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_lines), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_lines), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        proc.stdin.write(payload)
        proc.stdin.flush()
    except BrokenPipeError:
        pass  # the host exited early; the transcript and exit code record it
    timed_out = False
    deadline = time.monotonic() + timeout_s
    while proc.poll() is None and not _responses_complete("".join(out_lines)):
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(0.1)
    try:
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out = True
    for reader in readers:
        reader.join(timeout=30)
    return _RpcRun("".join(out_lines), "".join(err_lines), proc.returncode, timed_out)


def _fresh_start(ctx: _Ctx, label: str, *, explicit_entry: bool = False) -> dict[str, Any]:
    """Fresh `omp --mode rpc --no-session` in ctx.workspace. No `-e` unless explicit_entry."""
    args = ["--mode", "rpc", "--no-session"] + (["-e", str(PKG_DIR)] if explicit_entry else [])
    _guard_omp_argv(args, ctx.scratch)
    run = _rpc_exchange([ctx.omp, *args], ctx.workspace, ctx.env, _timeout_s())
    rec = _record(ctx, label, args, run.exit_code, run.stdout, run.stderr, run.timed_out, kind="fresh_start", inline=False)
    rec["discovery"] = _discover(_frames(run.stdout))
    return rec


def _install_enable(ctx: _Ctx, source: Path, tag: str) -> None:
    _plugin(ctx, f"install-{tag}", ["plugin", "install", str(source)])
    _plugin(ctx, f"enable-{tag}", ["plugin", "enable", PACKAGE_NAME])


def _write_config(workspace: Path, entries: list[str]) -> Path:
    cfg = workspace / ".omp" / "config.yml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("extensions:\n" + "".join(f"  - {entry}\n" for entry in entries), encoding="utf-8")
    return cfg


def _link_target(path: Path) -> str | None:
    return os.readlink(path) if path.is_symlink() else None


# ---- matrix case bodies: each drives real steps and returns {"notes": {...}} of observed facts ----


def _case_installed_only(ctx: _Ctx) -> dict[str, Any]:
    _install_enable(ctx, PKG_DIR, "pkg")
    identity = _plugin_list(ctx, "list-after-enable")
    _fresh_start(ctx, "fresh-start-no-e")
    return {"notes": {"identity": identity}}


def _case_configured_only(ctx: _Ctx) -> dict[str, Any]:
    cfg = _write_config(ctx.workspace, [str(PKG_DIR)])
    identity = _plugin_list(ctx, "list-no-install")
    _fresh_start(ctx, "fresh-start-config-workspace")
    return {"notes": {"identity": identity, "config": _s(ctx, cfg.read_text(encoding="utf-8"))}}


def _case_explicit_source(ctx: _Ctx) -> dict[str, Any]:
    identity = _plugin_list(ctx, "list-no-install")
    _fresh_start(ctx, "fresh-start-explicit-e", explicit_entry=True)
    return {"notes": {"identity": identity, "explicit_entry": _s(ctx, PKG_DIR)}}


def _case_duplicate_alias(ctx: _Ctx) -> dict[str, Any]:
    _install_enable(ctx, PKG_DIR, "pkg")
    cfg = _write_config(ctx.workspace, [str(PKG_DIR)])
    identity = _plugin_list(ctx, "list-after-enable")
    _fresh_start(ctx, "fresh-start-installed-and-configured")
    return {"notes": {"identity": identity, "config": _s(ctx, cfg.read_text(encoding="utf-8"))}}


def _case_project_shadow(ctx: _Ctx) -> dict[str, Any]:
    _install_enable(ctx, PKG_DIR, "pkg")
    shadow_src = PKG_DIR / "skills" / "a01-orchestrator"
    shadow_dst = ctx.workspace / ".omp" / "skills" / "a01-orchestrator"
    shutil.copytree(shadow_src, shadow_dst)
    identity = _plugin_list(ctx, "list-after-enable")
    _fresh_start(ctx, "fresh-start-project-shadow")
    return {
        "notes": {
            "identity": identity,
            "shadow_dir": _s(ctx, shadow_dst),
            "shadow_files": sorted(_s(ctx, p) for p in shadow_dst.rglob("*") if p.is_file()),
        }
    }


def _case_relocation(ctx: _Ctx) -> dict[str, Any]:
    _install_enable(ctx, PKG_DIR, "repo")
    link = ctx.home / ".omp" / "plugins" / "node_modules" / PACKAGE_NAME
    before = _link_target(link)
    copy_root = ctx.scratch / "relocated" / "omp"
    shutil.copytree(PKG_DIR, copy_root, ignore=shutil.ignore_patterns("node_modules", ".git", "__pycache__"))
    _plugin(ctx, "install-relocated", ["plugin", "install", str(copy_root)])
    _plugin(ctx, "enable-relocated", ["plugin", "enable", PACKAGE_NAME])
    identity = _plugin_list(ctx, "list-after-relocation")
    after = _link_target(link)
    _fresh_start(ctx, "fresh-start-relocated")
    return {
        "notes": {
            "identity": identity,
            "link_path": _s(ctx, link),
            "link_before": _s(ctx, before),
            "link_after": _s(ctx, after),
            "link_replaced": before != after,
            "copy_root": _s(ctx, copy_root),
        }
    }


_CASE_BODIES: dict[str, Callable[[_Ctx], dict[str, Any]]] = {
    "installed-only": _case_installed_only,
    "configured-only": _case_configured_only,
    "explicit-source": _case_explicit_source,
    "duplicate-alias": _case_duplicate_alias,
    "project-shadow": _case_project_shadow,
    "relocation": _case_relocation,
}


def _case_result(ctx: _Ctx, body: dict[str, Any], error: str | None) -> dict[str, Any]:
    starts = [s for s in ctx.steps if s["kind"] == "fresh_start"]
    complete = (
        error is None
        and bool(starts)
        and all(s["exit_code"] == 0 and not s["timed_out"] for s in ctx.steps)
        and all(
            s["discovery"]["get_state_success"] and s["discovery"]["get_available_commands_success"] for s in starts
        )
    )
    return {
        "case": ctx.case,
        "capture_complete": complete,
        "error": None if error is None else _sanitize(error, ctx.subs),
        "notes": body.get("notes", {}),
        "steps": ctx.steps,
    }


def _omp_version(omp: str) -> str:
    _guard_omp_argv(["--version"], None)
    scratch = Path(tempfile.mkdtemp(prefix="swarm-omp-version-"))
    try:
        proc = subprocess.run(
            [omp, "--version"],
            cwd=scratch,
            env=_isolated_env(scratch),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_timeout_s(),
            check=False,
        )
        return (proc.stdout or proc.stderr).strip()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_case(name: str, out_dir: str | Path, omp_bin: str | None = None) -> dict[str, Any]:
    """Runs one matrix case in a fresh scratch dir and returns its structured result."""
    _require_gate()
    if name not in _CASE_BODIES:
        raise ValueError(f"unknown matrix case: {name!r}; expected one of {CASES}")
    omp = _resolve_omp(omp_bin)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="swarm-omp-matrix-"))
    try:
        home = scratch / "home"
        workspace = scratch / "ws"
        home.mkdir()
        workspace.mkdir()
        _write_scratch_models(home)
        ctx = _Ctx(
            case=name,
            out=out,
            omp=omp,
            scratch=scratch,
            home=home,
            workspace=workspace,
            env=_isolated_env(home),
            subs=_subs(home, workspace, scratch, omp),
        )
        error: str | None = None
        body: dict[str, Any] = {}
        try:
            body = _CASE_BODIES[name](ctx)
        except Exception as exc:  # recorded per case so the remaining cases still run
            error = f"{type(exc).__name__}: {exc}"
        return _case_result(ctx, body, error)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_matrix(
    out_dir: str | Path | None = None,
    omp_bin: str | None = None,
    cases: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Runs the selected cases (default: all six) against the real omp binary."""
    _require_gate()
    omp = _resolve_omp(omp_bin)
    out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="swarm-omp-transcripts-"))
    out.mkdir(parents=True, exist_ok=True)
    selected = tuple(cases) if cases else CASES
    version = _omp_version(omp)
    results = [run_case(name, out, omp) for name in selected]
    return {
        "omp_binary": Path(omp).name,
        "omp_version": version,
        "transcript_dir": str(out),
        "cases": results,
    }


def run_ac14701_matrix(
    transcript_dir: str | Path | None = None,
    omp_bin: str | None = None,
    cases: Iterable[str] | None = None,
) -> dict[str, Any]:
    """AC-14.7-01 matrix: real plugin-manager register/enable, fresh `omp` start without -e, and runtime discovery
    transcripts for each case, written under `transcript_dir` (a fresh temp directory when omitted)."""
    return run_matrix(transcript_dir, omp_bin, cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AC-14.7-01 real OMP plugin-manager matrix (operator-invoked).")
    parser.add_argument("--matrix", action="store_true", help=f"run the matrix; requires {MATRIX_ENV}=1")
    parser.add_argument("--out", help="transcript directory (default: fresh temp directory)")
    parser.add_argument("--omp", help="omp binary (default: $OMP_BIN or PATH)")
    parser.add_argument("--case", action="append", choices=CASES, help="run only this case (repeatable)")
    ns = parser.parse_args(argv)
    if not ns.matrix:
        parser.print_usage(sys.stderr)
        return 2
    if not matrix_enabled():
        print(f"refusing: set {MATRIX_ENV}=1 to run real plugin-manager steps", file=sys.stderr)
        return 2
    out = ns.out or tempfile.mkdtemp(prefix="swarm-omp-transcripts-")
    print(f"transcript directory: {out}", file=sys.stderr)
    result = run_ac14701_matrix(out, ns.omp, ns.case)
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if all(case["capture_complete"] for case in result["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
