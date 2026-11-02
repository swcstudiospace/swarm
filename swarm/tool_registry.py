"""Tool registry over agents.json.

Registration lives on each agent as `registered_tools`, plus a top-level `shared_tools`
list. `call_tool` is the only call path: an unregistered name never resolves a handler.
"""
from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeGuard

from swarm.tools.support import Busy, StillRunning, Waiting, caller_scope, invoke_bounded

ROOT = Path(__file__).resolve().parent.parent
AGENTS_FILE = Path(os.environ.get("SWARM_AGENTS_FILE", ROOT / "agents.json"))
CONTRACT = "swarm.tool.v1"
SCHEMA_FILE = ROOT / "swarm" / "schemas" / "tool.contract.v1.json"

PREFIX = {
    "A01": "orch_",
    "A02": "req_",
    "A03": "arch_",
    "A04": "ux_",
    "A05": "be_",
    "A06": "fe_",
    "A07": "data_",
    "A08": "qa_",
    "A09": "rev_",
    "A10": "sec_",
    "A11": "devops_",
    "A12": "rel_",
    "A13": "obs_",
    "A14": "maint_",
    "A15": "docs_",
    "A16": "cloud_",
    "A17": "plat_",
    "A18": "supply_",
    "A19": "event_",
    "A20": "finops_",
    "A21": "k8s_",
    "A22": "gitops_",
    "A23": "mesh_",
    "A24": "kpol_",
    "A25": "resil_",
    "A26": "sol_",
    "A27": "audit_",
    "A28": "chain_",
    "A29": "dapp_",
    "A30": "custody_",
    "shared": "shared_",
}
ERROR_STATES = (
    "INVALID_INPUT",
    "UNAUTHORIZED",
    "DEPENDENCY_UNAVAILABLE",
    "TIMEOUT",
    "PARTIAL_SUCCESS",
)
TERMINAL_STATES = ("SUCCESS", *ERROR_STATES)
IDEMPOTENCY = {"idempotent", "at-least-once", "unsafe"}
EXAMPLE_KEYS = ("success", "invalid_input", "unauthorized_caller", "dependency", "timeout", "partial")
TIERS = ("READ", "WRITE", "DESTRUCTIVE", "EXTERNAL")
# v2.2 (D2): no tool may be DESTRUCTIVE or EXTERNAL. WRITE tools write only inside swarm_dir().
ALLOWED_TIERS = ("READ", "WRITE")
DEFAULT_TIER = "READ"
REQUIRED_FIELDS = (
    "name",
    "owner",
    "purpose",
    "input_schema",
    "output_schema",
    "errors",
    "timeout_s",
    "idempotency",
    "permitted_callers",
    "entrypoint",
    "handler",
    "examples",
)


def load_document(path: Path | None = None) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(Path(path or AGENTS_FILE).read_text(encoding="utf-8"))
    return document


def iter_entries(data: dict[str, Any]) -> Iterator[tuple[str, object]]:
    """Yield (container, tool) for lane tools then shared tools. container is an agent id or 'shared'."""
    for agent in data.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        tools = agent.get("registered_tools") or []
        if isinstance(tools, list):
            for tool in tools:
                yield str(agent.get("id")), tool
    shared = data.get("shared_tools") or []
    if isinstance(shared, list):
        for tool in shared:
            yield "shared", tool


def validate_registry(path: Path | None = None, *, root: Path | None = None) -> list[str]:
    """Actionable registration errors. Empty means every contract may be called."""
    root = root or ROOT
    try:
        data = load_document(path)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"agents.json: cannot read registry ({exc})"]
    errors: list[str] = []
    if data.get("tool_contract") != CONTRACT:
        errors.append(f"agents.json: tool_contract must be {CONTRACT!r}")
    errors.extend(_schema_file_errors())
    if "shared_tools" not in data or not isinstance(data.get("shared_tools"), list):
        errors.append("agents.json: shared_tools must be a list (empty until the shared pool exists)")
    agents = data.get("agents")
    if not isinstance(agents, list):
        return errors + ["agents.json: agents must be a list"]
    agent_ids = {a.get("id") for a in agents if isinstance(a, dict)}
    for agent in agents:
        if isinstance(agent, dict) and "registered_tools" in agent and not isinstance(agent["registered_tools"], list):
            errors.append(f"agent {agent.get('id')}: registered_tools must be a list")

    seen: dict[str, str] = {}
    ready: list[tuple[str, dict[str, Any]]] = []
    for container, tool in iter_entries(data):
        if not isinstance(tool, dict):
            errors.append(f"{container}: tool entry must be an object")
            continue
        problems, ok = _check_tool(tool, container, agent_ids, root)
        errors.extend(problems)
        if not ok:
            continue
        name = tool["name"]
        if name in seen:
            errors.append(f"tool {name}: name collides with the entry owned by {seen[name]}")
            continue
        seen[name] = tool["owner"]
        ready.append((container, tool))

    for _container, tool in ready:
        errors.extend(_check_handler(tool, root))
    errors.extend(_policy_errors(data, agents))
    errors.extend(_baseline_errors(data, agents))
    return errors


_AUDIT_ID = re.compile(r"[A-Za-z0-9._:@-]{1,80}")


def _audit_id(value: object) -> str | None:
    """The value when it has the shape of an identifier and is not secret-shaped (never free text), else None."""
    from swarm.redact import redact_text

    if not isinstance(value, str) or not _AUDIT_ID.fullmatch(value) or redact_text(value) != value:
        return None
    return value


def _plan_of(task_id: str) -> str | None:
    """The correlation id of the task's plan from the Task Store (read-only; absent store or task is None)."""
    import sqlite3

    from swarm.paths import swarm_dir

    db = swarm_dir() / "tasks.db"
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
        try:
            row = conn.execute("SELECT correlation_id FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):  # a locked or unreadable store only costs the correlation id, never the audit row
        return None
    return _audit_id(row[0]) if row else None


def _audit_call(name: object, caller: object, payload: object, result: object, started: float) -> None:
    """One local-only `swarm.tool.call` run-log row per registry call (SEC-03): names, state and duration, never a payload
    value. The ids come from the run's env (SWARM_CORRELATION_ID, SWARM_TASK_ID), which an agent session does not inherit
    (the runner strips them), so the fallbacks are the call's own `task_id` / `correlation_id` identifiers, the plan of that
    task in the Task Store, and the latest plan. A failure to log never changes the call result and never raises."""
    try:
        from swarm import runlog
        from swarm.paths import latest_correlation

        state = result.get("state") if isinstance(result, dict) else None
        state = state if isinstance(state, str) else "DEPENDENCY_UNAVAILABLE"
        body: dict[str, object] = {
            "tool": _audit_id(name) or "",
            "caller": _audit_id(caller) or "",
            "state": state,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }
        if state not in ("SUCCESS", "PARTIAL_SUCCESS"):
            body["error_state"] = state
        named = payload if isinstance(payload, dict) else {}
        task = _audit_id(os.environ.get("SWARM_TASK_ID")) or _audit_id(named.get("task_id"))
        corr = _audit_id(os.environ.get("SWARM_CORRELATION_ID")) or _audit_id(named.get("correlation_id"))
        if corr is None and task is not None:
            corr = _plan_of(task)
        if corr is None:
            corr = _audit_id(latest_correlation())
        runlog.emit("swarm.tool.call", body, source="registry", correlation_id=corr, task_id=task, tee=False)
    except Exception:  # noqa: BLE001 - the audit is best effort: the call result is the contract
        return


def call_tool(
    name: str,
    caller: str,
    payload: object,
    *,
    timeout_s: float | None = None,
    path: Path | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call one registered tool and record it in the run log (`swarm.tool.call`, local only). Unregistered names do not
    import a handler.

    ``data`` is the registry to use. When it is omitted, the on-disk document is loaded. The audit row is written when
    the swarm state dir already exists or the tool is WRITE tier; a READ call in a workspace without `.swarm` writes nothing.
    """
    started = time.monotonic()
    existed = _state_dir_exists()
    seen: dict[str, object] = {}
    result = _call_tool(name, caller, payload, timeout_s=timeout_s, path=path, data=data, seen=seen)
    if existed or seen.get("tier") == "WRITE":  # a READ call never creates the state dir, not even for its own audit row
        _audit_call(name, caller, payload, result, started)
    return result


def _state_dir_exists() -> bool:
    try:
        from swarm.paths import swarm_dir

        return swarm_dir(create=False).exists()
    except Exception:  # noqa: BLE001 - unknown means "do not create it": the call itself is unaffected
        return False


def _call_tool(
    name: str,
    caller: str,
    payload: object,
    *,
    timeout_s: float | None = None,
    path: Path | None = None,
    data: dict[str, Any] | None = None,
    seen: dict[str, object] | None = None,
) -> dict[str, Any]:
    if not isinstance(name, str) or not name:
        return {"state": "INVALID_INPUT", "field": "name", "message": "tool name is required"}
    if data is None:
        try:
            data = load_document(path)
        except (OSError, json.JSONDecodeError) as exc:
            return {"state": "DEPENDENCY_UNAVAILABLE", "message": f"registry unreadable: {exc}"}
    tool = _find(data, name)
    if tool is None:
        return {"state": "INVALID_INPUT", "field": "name", "message": f"tool {name!r} is not registered"}
    if seen is not None:
        seen["tier"] = tool_tier(tool)
    if caller not in (tool.get("permitted_callers") or []):
        return {
            "state": "UNAUTHORIZED",
            "caller": caller,
            "target": name,
            "reason": "caller is not permitted",
        }
    if not isinstance(payload, dict):
        return {"state": "INVALID_INPUT", "field": "payload", "message": "payload must be an object"}
    field_errors = _payload_errors(tool.get("input_schema") or {}, payload)
    if field_errors:
        field, message = field_errors[0]
        return {"state": "INVALID_INPUT", "field": field, "message": message}
    limit = tool["timeout_s"] if timeout_s is None else timeout_s
    try:
        fn = _import_handler(tool["handler"])
        with caller_scope(caller):
            raw = invoke_bounded(fn, payload, float(limit))
    except Waiting:
        return {"state": "TIMEOUT", "message": "previous call is still running"}
    except Busy:
        return {"state": "DEPENDENCY_UNAVAILABLE", "message": "previous call is still running"}
    except (TimeoutError, StillRunning):
        return {"state": "TIMEOUT", "message": f"exceeded {limit}s"}
    except OSError as exc:
        return {"state": "DEPENDENCY_UNAVAILABLE", "message": str(exc)}
    except Exception as exc:
        return {"state": "DEPENDENCY_UNAVAILABLE", "message": f"handler failed: {exc}"}
    return _finish(tool, raw)


def tool_tier(tool: dict[str, Any]) -> str:
    """The declared side-effect tier; an absent field means READ."""
    tier = tool.get("side_effect_tier")
    return tier if isinstance(tier, str) else DEFAULT_TIER


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate, call, and generate the agents.json tool registry")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="exit 1 when any contract is incomplete, colliding, or stubbed")
    call = sub.add_parser("call", help="call one registered tool")
    call.add_argument("--name", required=True)
    call.add_argument("--caller", required=True)
    call.add_argument("--input", default="{}", dest="payload")
    docs = sub.add_parser("docs", help="write docs/tools from the registry")
    docs.add_argument("--check", action="store_true", help="fail when committed docs disagree")
    gen = sub.add_parser("gen-tests", help="write tests/generated/test_tool_contracts.py")
    gen.add_argument("--check", action="store_true", help="fail when the generated test disagrees")
    args = parser.parse_args(argv)

    if args.cmd == "check":
        errors = validate_registry()
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("tool registry ok")
        return 0
    if args.cmd == "call":
        try:
            payload = json.loads(args.payload)
        except json.JSONDecodeError as exc:
            print(f"invalid --input JSON: {exc}", file=sys.stderr)
            return 2
        result = call_tool(args.name, args.caller, payload)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("state") in ("SUCCESS", "PARTIAL_SUCCESS") else 1
    if args.cmd == "docs":
        from swarm.tool_generate import write_docs

        return write_docs(check=args.check)
    from swarm.tool_generate import write_contract_tests

    return write_contract_tests(check=args.check)


def _schema_file_errors() -> list[str]:
    try:
        required = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))["required"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        return [f"tool.contract.v1.json: unreadable ({exc})"]
    if list(required) != list(REQUIRED_FIELDS):
        return ["tool.contract.v1.json: required fields drifted from the checker"]
    try:
        tiers = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))["properties"]["side_effect_tier"]["enum"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return [f"tool.contract.v1.json: side_effect_tier is not declared ({exc})"]
    if list(tiers) != list(TIERS):
        return ["tool.contract.v1.json: side_effect_tier values drifted from the checker"]
    return []


def find_tool(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    """The registry entry for a tool name, else None."""
    return _find(data, name)


def _find(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for _container, tool in iter_entries(data):
        if isinstance(tool, dict) and tool.get("name") == name:
            return tool
    return None


def _check_tool(tool: dict[str, Any], container: str, agent_ids: set[object], root: Path) -> tuple[list[str], bool]:
    name = tool.get("name") if isinstance(tool.get("name"), str) else "?"
    owner = tool.get("owner") if isinstance(tool.get("owner"), str) else "?"
    where = f"tool {name} (owner {owner})"
    errors: list[str] = []
    missing = [key for key in REQUIRED_FIELDS if key not in tool]
    if missing:
        errors.append(f"{where}: missing {', '.join(missing)}")
    if tool.get("stub") is True:
        errors.append(f"{where}: stub contracts cannot be registered")
    if owner != container:
        errors.append(f"{where}: owner must be {container!r}, the agent (or shared list) that declares it")
    prefix = PREFIX.get(owner if isinstance(owner, str) else "")
    if prefix is None:
        errors.append(f"{where}: owner must be one of {', '.join(PREFIX)}")
    elif not isinstance(name, str) or not _conventional_name(name) or not name.startswith(prefix):
        errors.append(f"{where}: name must match {prefix}[a-z0-9_]+")
    purpose = tool.get("purpose")
    if "purpose" in tool and (not isinstance(purpose, str) or not purpose.strip() or "|" in purpose or "\n" in purpose):
        errors.append(f"{where}: purpose must be one non-empty line without '|'")
    errors.extend(_check_schemas(where, tool))
    if "errors" in tool:
        errors.extend(_check_errors(where, tool.get("errors")))
    timeout = tool.get("timeout_s")
    if "timeout_s" in tool and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600):
        errors.append(f"{where}: timeout_s must be a number in (0, 3600]")
    if "idempotency" in tool and tool.get("idempotency") not in IDEMPOTENCY:
        errors.append(f"{where}: idempotency must be one of {', '.join(sorted(IDEMPOTENCY))}")
    errors.extend(_check_tier(where, tool))
    callers = tool.get("permitted_callers")
    if "permitted_callers" in tool:
        if not isinstance(callers, list) or not callers or not all(isinstance(c, str) and c in agent_ids for c in callers):
            errors.append(f"{where}: permitted_callers must be a non-empty list of agent ids")
        elif len(callers) != len(set(callers)):
            errors.append(f"{where}: permitted_callers contains a duplicate")
    errors.extend(_check_entrypoint(where, tool, root))
    if "examples" in tool and not errors:
        errors.extend(_check_examples(where, tool, agent_ids))
    structural = bool(errors) or bool(missing) or tool.get("stub") is True
    return errors, not structural


def _check_tier(where: str, tool: dict[str, Any]) -> list[str]:
    if "side_effect_tier" not in tool:
        return []
    tier = tool["side_effect_tier"]
    if tier not in TIERS:
        return [f"{where}: side_effect_tier must be one of {', '.join(TIERS)} (absent means READ)"]
    if tier not in ALLOWED_TIERS:
        return [
            f"{where}: side_effect_tier {tier} is not allowed in v2.2: register only READ or WRITE tools; "
            "WRITE tools may write only inside the swarm state dir, so split the destructive or external step "
            "out of the tool and leave it to a human-approved path"
        ]
    return []


def _range_error(where: str, spec: object) -> str | None:
    if not isinstance(spec, dict):
        return f"agents.json: {where} must be an object with integer min and max"
    low, high = spec.get("min"), spec.get("max")
    if not _is_count(low) or not _is_count(high) or low > high:
        return f"agents.json: {where} needs integer min and max with 0 <= min <= max"
    return None


def _policy_errors(data: dict[str, Any], agents: list[object]) -> list[str]:
    """capability_policy shape, then per-agent tool counts against it (D3)."""
    policy = data.get("capability_policy")
    if not isinstance(policy, dict):
        return ["agents.json: capability_policy must be an object with tools and skills min/max"]
    errors: list[str] = []
    for kind in ("tools", "skills"):
        problem = _range_error(f"capability_policy.{kind}", policy.get(kind))
        if problem:
            errors.append(problem)
    if "combined" in policy:
        problem = _range_error("capability_policy.combined", policy["combined"])
        if problem:
            errors.append(problem)
    tools_policy = policy.get("tools")
    if isinstance(tools_policy, dict) and "enforce" in tools_policy and not isinstance(tools_policy["enforce"], bool):
        errors.append("agents.json: capability_policy.tools.enforce must be true or false")
    if errors:
        return errors
    if not isinstance(tools_policy, dict):  # unreachable: _range_error accepted no other value for capability_policy.tools
        return errors
    high, low = tools_policy["max"], tools_policy["min"]
    enforce = tools_policy.get("enforce", True)
    for agent in agents:
        if not isinstance(agent, dict) or not isinstance(agent.get("registered_tools"), list):
            continue
        count = len(agent["registered_tools"])
        if count > high:
            errors.append(f"agent {agent.get('id')}: {count} registered tools exceed capability_policy.tools.max {high}")
        elif enforce and count < low:
            errors.append(f"agent {agent.get('id')}: {count} registered tools are below capability_policy.tools.min {low}")
    return errors


def _baseline_errors(data: dict[str, Any], agents: list[object]) -> list[str]:
    """shared_baseline: every listed tool is a shared tool that all agents may call (section 8)."""
    baseline = data.get("shared_baseline")
    if not isinstance(baseline, dict):
        return ["agents.json: shared_baseline must be an object with tools and skills lists"]
    errors: list[str] = []
    for kind in ("tools", "skills"):
        names = baseline.get(kind)
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            errors.append(f"agents.json: shared_baseline.{kind} must be a list of names")
        elif len(names) != len(set(names)):
            errors.append(f"agents.json: shared_baseline.{kind} contains a duplicate")
    if errors:
        return errors
    shared = {t.get("name"): t for t in data.get("shared_tools") or [] if isinstance(t, dict)}
    agent_ids = sorted(str(a.get("id")) for a in agents if isinstance(a, dict))
    for name in baseline["tools"]:
        tool = shared.get(name)
        if tool is None:
            errors.append(f"agents.json: shared_baseline tool {name!r} is not a shared tool")
            continue
        callers = tool.get("permitted_callers")
        callers = set(callers) if isinstance(callers, list) else set()
        missing = [a for a in agent_ids if a not in callers]
        if missing:
            errors.append(f"agents.json: shared_baseline tool {name!r} must permit every agent; missing {', '.join(missing)}")
    return errors


def _conventional_name(name: str) -> bool:
    return bool(name) and name[0].islower() and all(ch.islower() or ch.isdigit() or ch == "_" for ch in name)


_SCHEMA_TYPES = ("string", "integer", "number", "boolean", "array", "object")
_SCHEMA_COMMON = {"const", "description", "enum", "type"}
_SCHEMA_BY_TYPE = {
    "string": {"maxLength", "minLength"},
    "integer": {"exclusiveMaximum", "exclusiveMinimum", "maximum", "minimum"},
    "number": {"exclusiveMaximum", "exclusiveMinimum", "maximum", "minimum"},
    "boolean": set(),
    "array": {"items", "maxItems", "minItems"},
    "object": {"additionalProperties", "properties", "required"},
}


def _check_schemas(where: str, tool: dict[str, Any]) -> list[str]:
    errors = []
    for key in ("input_schema", "output_schema"):
        schema = tool.get(key)
        if key not in tool:
            continue
        if not isinstance(schema, dict) or schema.get("type") != "object":
            errors.append(f"{where}: {key} must be a JSON schema object with type 'object'")
            continue
        errors.extend(_schema_keyword_errors(f"{where}: {key}", schema))
    return errors


def _schema_keyword_errors(where: str, schema: dict[str, Any]) -> list[str]:
    expected = schema.get("type")
    # A list or other non-string is unhashable; never use it as a dict key.
    supported = isinstance(expected, str) and expected in _SCHEMA_BY_TYPE
    allowed = (_SCHEMA_COMMON | _SCHEMA_BY_TYPE[expected]) if supported and isinstance(expected, str) else _SCHEMA_COMMON
    unknown = sorted(key for key in schema if key not in allowed)
    errors = []
    if not supported:
        errors.append(f"{where}: type must be one of {', '.join(_SCHEMA_TYPES)}")
    if unknown:
        if supported:
            errors.append(f"{where}: keywords {', '.join(unknown)} are not valid for {expected}")
        else:
            errors.append(f"{where}: unsupported schema keywords {', '.join(unknown)}")
    errors.extend(_keyword_value_errors(where, schema))
    props = schema.get("properties")
    if isinstance(props, dict):
        for name, child in props.items():
            if isinstance(child, dict):
                errors.extend(_schema_keyword_errors(f"{where}.{name}", child))
            else:
                errors.append(f"{where}.{name}: schema must be an object")
    elif "properties" in schema:
        errors.append(f"{where}: properties must be an object")
    items = schema.get("items")
    if isinstance(items, dict):
        errors.extend(_schema_keyword_errors(f"{where}.items", items))
    elif "items" in schema:
        errors.append(f"{where}.items: schema must be an object")
    return errors


def _keyword_value_errors(where: str, schema: dict[str, Any]) -> list[str]:
    errors = []
    for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        if key in schema and not _is_number(schema[key]):
            errors.append(f"{where}: {key} must be a number")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and not _is_count(schema[key]):
            errors.append(f"{where}: {key} must be a non-negative integer")
    required = schema.get("required")
    if "required" in schema and (not isinstance(required, list) or not all(isinstance(item, str) for item in required)):
        errors.append(f"{where}: required must be a list of strings")
    elif isinstance(required, list) and isinstance(schema.get("properties"), dict):
        missing = [item for item in required if item not in schema["properties"]]
        if missing:
            errors.append(f"{where}: required fields {', '.join(missing)} are not in properties")
    enum = schema.get("enum")
    if "enum" in schema and (not isinstance(enum, list) or not enum):
        errors.append(f"{where}: enum must be a non-empty list")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        errors.append(f"{where}: additionalProperties must be a boolean")
    return errors


def _is_number(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_errors(where: str, errors_field: object) -> list[str]:
    if not isinstance(errors_field, list):
        return [f"{where}: errors must list {', '.join(ERROR_STATES)}"]
    if set(errors_field) != set(ERROR_STATES) or len(errors_field) != len(ERROR_STATES):
        return [f"{where}: errors must be exactly {', '.join(ERROR_STATES)}"]
    return []


def _check_entrypoint(where: str, tool: dict[str, Any], root: Path) -> list[str]:
    errors = []
    entrypoint = tool.get("entrypoint")
    handler = tool.get("handler")
    if "entrypoint" in tool and not isinstance(entrypoint, str):
        errors.append(f"{where}: entrypoint must be a repo-relative file path")
    elif isinstance(entrypoint, str):
        path = Path(entrypoint)
        if path.is_absolute() or ".." in path.parts or not (root / path).is_file():
            errors.append(f"{where}: entrypoint {entrypoint!r} must be a file inside the repo")
        elif isinstance(handler, str) and _handler_file(handler) != path.as_posix():
            errors.append(f"{where}: handler {handler!r} must point at entrypoint {entrypoint!r}")
    if "handler" in tool and not isinstance(handler, str):
        errors.append(f"{where}: handler must look like swarm.tools.<module>:<function>")
    elif isinstance(handler, str) and _handler_file(handler) is None:
        errors.append(f"{where}: handler must look like swarm.tools.<module>:<function>")
    return errors


def _handler_file(handler: str) -> str | None:
    module, sep, func = handler.partition(":")
    if not sep or not module.startswith("swarm.tools.") or module.count(".") != 2:
        return None
    leaf = module.rsplit(".", 1)[1]
    if not _conventional_name(leaf) or not func or not _conventional_name(func):
        return None
    return f"swarm/tools/{leaf}.py"


def _check_handler(tool: dict[str, Any], root: Path) -> list[str]:
    where = f"tool {tool.get('name')} (owner {tool.get('owner')})"
    entrypoint = tool.get("entrypoint")
    handler = tool.get("handler")
    if not isinstance(entrypoint, str) or not isinstance(handler, str):
        return []
    try:
        _import_handler(handler)
    except Exception as exc:
        return [f"{where}: handler {handler!r} cannot be imported ({exc})"]
    expected = root / entrypoint
    module = tool["handler"].split(":", 1)[0]
    found = Path(importlib.import_module(module).__file__ or "")
    if found.resolve() != expected.resolve():
        return [f"{where}: handler file {found} is not entrypoint {expected}"]
    return []


def _check_examples(where: str, tool: dict[str, Any], agent_ids: set[object]) -> list[str]:
    examples = tool.get("examples")
    if not isinstance(examples, dict):
        return [f"{where}: examples must be an object with {', '.join(EXAMPLE_KEYS)}"]
    errors = []
    extra = set(examples) - set(EXAMPLE_KEYS)
    missing = [key for key in EXAMPLE_KEYS if key not in examples]
    if extra or missing:
        errors.append(f"{where}: examples must contain exactly {', '.join(EXAMPLE_KEYS)}")
        return errors
    schema = tool["input_schema"]
    for key in ("success", "dependency", "timeout", "partial"):
        payload = examples[key]
        if not isinstance(payload, dict) or _payload_errors(schema, payload):
            errors.append(f"{where}: examples.{key} must satisfy input_schema")
    invalid = examples["invalid_input"]
    if not isinstance(invalid, dict) or not _payload_errors(schema, invalid):
        errors.append(f"{where}: examples.invalid_input must be an object that fails input_schema")
    caller = examples["unauthorized_caller"]
    permitted = tool["permitted_callers"]
    if not isinstance(caller, str) or not caller or caller in permitted:
        errors.append(f"{where}: examples.unauthorized_caller must be an agent id that is not permitted")
    elif caller not in agent_ids and agent_ids - set(permitted):
        errors.append(f"{where}: examples.unauthorized_caller must be an agent id that is not permitted")
    # when every agent is permitted (the shared baseline), no agent id can be refused: any other name is the probe
    return errors


def _payload_errors(schema: dict[str, Any], payload: dict[str, Any]) -> list[tuple[str, str]]:
    if schema.get("type") not in (None, "object"):
        return [("payload", "input_schema type must be object")]
    props = schema.get("properties") or {}
    if not isinstance(props, dict):
        return [("payload", "input_schema properties must be an object")]
    errors: list[tuple[str, str]] = []
    required = schema.get("required") or []
    for key in required:
        if key not in payload:
            errors.append((str(key), f"missing required field {key}"))
    if schema.get("additionalProperties") is False:
        for key in payload:
            if key not in props:
                errors.append((str(key), f"unexpected field {key}"))
    for key, value in payload.items():
        if key not in props or not isinstance(props[key], dict):
            continue
        message = _value_error(props[key], value)
        if message:
            errors.append((str(key), message))
    return errors


def _value_error(spec: dict[str, Any], value: object) -> str | None:
    """Enforce declared type, numeric bounds, string length, array items, and nested objects.

    ``items``, ``minItems``, ``minimum``, and nested ``required`` are applied here.
    Registration rejects keywords this function does not implement.
    """
    checkers = {
        "string": _string_error,
        "integer": _integer_error,
        "number": _number_error,
        "boolean": _boolean_error,
        "object": _object_error,
        "array": _array_error,
    }
    kind = spec.get("type")
    checker = checkers.get(kind) if isinstance(kind, str) else None
    if checker is not None:
        message = checker(spec, value)
        if message:
            return message
    if "const" in spec and value != spec["const"]:
        return f"expected {spec['const']!r}"
    if "enum" in spec and value not in spec["enum"]:
        return f"expected one of {spec['enum']!r}"
    return None


def _string_error(spec: dict[str, Any], value: object) -> str | None:
    if not isinstance(value, str):
        return "expected string"
    if _is_count(spec.get("minLength")) and len(value) < spec["minLength"]:
        return f"shorter than {spec['minLength']}"
    if _is_count(spec.get("maxLength")) and len(value) > spec["maxLength"]:
        return f"longer than {spec['maxLength']}"
    return None


def _integer_error(spec: dict[str, Any], value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return "expected integer"
    return _bound_error(spec, value)


def _number_error(spec: dict[str, Any], value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "expected number"
    if isinstance(value, float) and not math.isfinite(value):
        # NaN and Infinity compare false against every bound, so a handler written for finite input fails OPEN on them
        # (a NaN error rate passes `<= limit`). JSON has no such numbers: refuse them once, here, for every tool.
        return "expected a finite number"
    return _bound_error(spec, value)


def _boolean_error(_spec: dict[str, Any], value: object) -> str | None:
    if not isinstance(value, bool):
        return "expected boolean"
    return None


def _object_error(spec: dict[str, Any], value: object) -> str | None:
    if not isinstance(value, dict):
        return "expected object"
    nested = _payload_errors(spec, value)
    if nested:
        field, message = nested[0]
        return f"{field}: {message}"
    return None


def _array_error(spec: dict[str, Any], value: object) -> str | None:
    if not isinstance(value, list):
        return "expected array"
    min_items = spec.get("minItems")
    max_items = spec.get("maxItems")
    if _is_count(min_items) and len(value) < min_items:
        return f"fewer than {min_items} items"
    if _is_count(max_items) and len(value) > max_items:
        return f"more than {max_items} items"
    item_spec = spec.get("items")
    if isinstance(item_spec, dict):
        for index, item in enumerate(value):
            message = _value_error(item_spec, item)
            if message:
                return f"[{index}] {message}"
    return None


def _bound_error(spec: dict[str, Any], value: int | float) -> str | None:
    minimum = spec.get("minimum")
    exclusive_minimum = spec.get("exclusiveMinimum")
    maximum = spec.get("maximum")
    exclusive_maximum = spec.get("exclusiveMaximum")
    if _is_number(minimum) and value < minimum:
        return f"below minimum {minimum}"
    if _is_number(exclusive_minimum) and value <= exclusive_minimum:
        return f"not above {exclusive_minimum}"
    if _is_number(maximum) and value > maximum:
        return f"above maximum {maximum}"
    if _is_number(exclusive_maximum) and value >= exclusive_maximum:
        return f"not below {exclusive_maximum}"
    return None


def _import_handler(handler: str) -> Callable[[dict[str, Any]], object]:
    module_name, func_name = handler.split(":", 1)
    module = importlib.import_module(module_name)
    fn: Callable[[dict[str, Any]], object] = getattr(module, func_name)
    if not callable(fn):
        raise TypeError(f"{handler} is not callable")
    return fn


OUTPUT_LIMIT_DEFAULT = 256 * 1024
OUTPUT_LIMIT_FLOOR, OUTPUT_LIMIT_CEILING = 4096, 16 * 1024 * 1024


def output_limit() -> int:
    """Bytes a tool result may serialise to: env SWARM_TOOL_OUTPUT_MAX_BYTES, clamped to [4 KiB, 16 MiB], default 256 KiB."""
    try:
        value = int(os.environ.get("SWARM_TOOL_OUTPUT_MAX_BYTES", ""))
    except ValueError:
        return OUTPUT_LIMIT_DEFAULT
    return min(max(value, OUTPUT_LIMIT_FLOOR), OUTPUT_LIMIT_CEILING)


def _within_limits(result: dict[str, Any]) -> dict[str, Any]:
    """The last gate on a result (SEC-02): it must be strict JSON (no NaN or Infinity, which most parsers refuse) and no
    larger than `output_limit()`. An oversized result is WITHHELD, not truncated: a half result is worse than none for a
    gate or a handover. A SUCCESS or PARTIAL_SUCCESS becomes a PARTIAL_SUCCESS with completed_fraction 0 so the caller
    narrows its input; any other state keeps its state (a failed call stays a failure) and its short routing fields."""
    try:
        size = len(json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        return {"state": "DEPENDENCY_UNAVAILABLE", "message": f"handler result is not strict JSON ({str(exc)[:120]})"}
    limit = output_limit()
    if size > limit and result.get("state") not in ("SUCCESS", "PARTIAL_SUCCESS"):
        kept = {k: result[k] for k in ("field", "caller", "target")
                if isinstance(result.get(k), str) and len(result[k]) <= 200}
        return {"state": result["state"], **kept, "output_withheld": True, "output_bytes": size, "limit_bytes": limit,
                "message": f"{result['state']} result of {size} bytes exceeds the {limit}-byte output limit"}
    if size > limit:
        return {"state": "PARTIAL_SUCCESS", "completed_fraction": 0.0, "output_withheld": True, "output_bytes": size,
                "limit_bytes": limit, "message": f"result of {size} bytes exceeds the {limit}-byte output limit; narrow the input"}
    return result


def _finish(tool: dict[str, Any], raw: object) -> dict[str, Any]:
    return _within_limits(_finish_state(tool, raw))


def _finish_state(tool: dict[str, Any], raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("state") not in TERMINAL_STATES:
        return {"state": "DEPENDENCY_UNAVAILABLE", "message": "handler did not return a terminal state"}
    state = raw["state"]
    if state == "PARTIAL_SUCCESS":
        fraction = raw.get("completed_fraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 <= fraction <= 1:
            return {"state": "DEPENDENCY_UNAVAILABLE", "message": "PARTIAL_SUCCESS requires completed_fraction in [0, 1]"}
        return raw
    if state != "SUCCESS":
        return raw
    output_errors = _payload_errors(tool.get("output_schema") or {}, raw)
    if output_errors:
        field, message = output_errors[0]
        return {"state": "DEPENDENCY_UNAVAILABLE", "message": f"SUCCESS payload does not match output_schema ({field}: {message})"}
    return raw


if __name__ == "__main__":
    sys.exit(main())
