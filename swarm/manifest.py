"""Agent manifest registry — loads agents.json (07-scalability.md §1 schema).

`validate_agents` checks the whole roster (uniqueness, required keys, field shapes) and
returns every problem at once; `load_manifest` raises on any, so a broken roster fails fast.
"""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
AGENTS_FILE = Path(os.environ.get("SWARM_AGENTS_FILE", ROOT / "agents.json"))
REQUIRED = ("id", "code", "name", "slug", "class", "lane", "capabilities", "consumes",
            "produces", "max_parallel", "autonomy_ceiling", "scripts", "description")

_ID = re.compile(r"A(\d{2})")
_SLUG = re.compile(r"a(\d{2})-[a-z0-9]+(?:-[a-z0-9]+)*")
_CODE = re.compile(r"[A-Z0-9]+")
_SCRIPT = re.compile(r"scripts/[A-Za-z0-9_][A-Za-z0-9_.-]*\.py")
_AUTONOMY_LEVELS = frozenset({"L1", "L2", "L3", "L4"})
_TEXT_FIELDS = ("name", "class", "lane", "description")
_LIST_FIELDS = ("capabilities", "consumes", "produces", "scripts")
_NON_EMPTY_LISTS = ("capabilities", "produces")


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _check_list(label: str, field: str, value: object, problems: list[str]) -> None:
    if not isinstance(value, list):
        problems.append(f"{label}: {field} must be a list, got {type(value).__name__}")
        return
    if field in _NON_EMPTY_LISTS and not value:
        problems.append(f"{label}: {field} must not be empty")
    bad = [v for v in value if not _nonempty_str(v)]
    if bad:
        problems.append(f"{label}: {field} must contain only non-empty strings, got {bad!r}")
    seen: set[str] = set()
    dupes: list[str] = []
    for v in value:
        if _nonempty_str(v):
            if v in seen and v not in dupes:
                dupes.append(v)
            seen.add(v)
    if dupes:
        problems.append(f"{label}: {field} has duplicate entries {dupes!r}")
    if field == "scripts":
        for v in value:
            if _nonempty_str(v) and (not _SCRIPT.fullmatch(v) or ".." in v.split("/")):
                problems.append(f"{label}: scripts entry {v!r} must be a repo-relative scripts/<name>.py path")


def _check_agent(index: int, agent: object, problems: list[str]) -> str:
    """Validate one agent entry in isolation; returns the label used in messages."""
    if not isinstance(agent, dict):
        problems.append(f"agents[{index}]: entry must be an object, got {type(agent).__name__}")
        return f"agents[{index}]"
    ident = agent.get("id")
    label = f"agent {ident}" if _nonempty_str(ident) else f"agents[{index}]"
    missing = [k for k in REQUIRED if k not in agent]
    if missing:
        problems.append(f"{label} manifest missing {missing}")

    id_digits = None
    if "id" in agent:
        m = _ID.fullmatch(ident) if isinstance(ident, str) else None
        if m:
            id_digits = m.group(1)
        else:
            problems.append(f"{label}: id {ident!r} must match ^A\\d{{2}}$ (e.g. 'A07')")
    if "slug" in agent:
        slug = agent["slug"]
        m = _SLUG.fullmatch(slug) if isinstance(slug, str) else None
        if not m:
            problems.append(f"{label}: slug {slug!r} must match a<NN>-<lowercase-words> (e.g. 'a07-data')")
        elif id_digits is not None and m.group(1) != id_digits:
            problems.append(f"{label}: slug {slug!r} number {m.group(1)} does not match id {ident!r}")
    if "code" in agent:
        code = agent["code"]
        if not (isinstance(code, str) and _CODE.fullmatch(code)):
            problems.append(f"{label}: code {code!r} must be a non-empty string of [A-Z0-9]")
    for field in _TEXT_FIELDS:
        if field in agent and not _nonempty_str(agent[field]):
            problems.append(f"{label}: {field} must be a non-empty string, got {agent[field]!r}")
    if "max_parallel" in agent:
        mp = agent["max_parallel"]
        if not isinstance(mp, int) or isinstance(mp, bool) or mp < 1:
            problems.append(f"{label}: max_parallel must be an integer >= 1, got {mp!r}")
    for field in _LIST_FIELDS:
        if field in agent:
            _check_list(label, field, agent[field], problems)
    if "autonomy_ceiling" in agent:
        ceiling = agent["autonomy_ceiling"]
        if not isinstance(ceiling, dict):
            problems.append(f"{label}: autonomy_ceiling must be an object, got {type(ceiling).__name__}")
        else:
            for action, level in ceiling.items():
                if not isinstance(level, str) or level not in _AUTONOMY_LEVELS:
                    problems.append(
                        f"{label}: autonomy_ceiling[{action!r}] must be one of {sorted(_AUTONOMY_LEVELS)}, got {level!r}"
                    )
    return label


def validate_agents(agents: object) -> list[str]:
    """Return every problem found in the roster document; an empty list means it is valid."""
    if not isinstance(agents, list) or not agents:
        return [f"agents must be a non-empty list of objects, got {_describe(agents)}"]
    problems: list[str] = []
    seen: dict[str, dict[str, int]] = {"id": {}, "slug": {}, "code": {}}
    for index, agent in enumerate(agents):
        label = _check_agent(index, agent, problems)
        if not isinstance(agent, dict):
            continue
        for field in seen:
            value = agent.get(field)
            if not isinstance(value, str) or not value:
                continue
            key = value if field == "id" else value.lower()
            if key in seen[field]:
                problems.append(f"{label}: duplicate {field} {value!r} (already used by agents[{seen[field][key]}])")
            else:
                seen[field][key] = index
    return problems


def _describe(value: object) -> str:
    return "an empty list" if value == [] else type(value).__name__


def load_manifest(path: Path | None = None) -> list[dict[str, Any]]:
    source = Path(path or AGENTS_FILE)
    try:
        data: object = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source.name}: invalid JSON: {exc}") from exc
    if isinstance(data, dict):
        if "agents" not in data:
            raise ValueError(f"{source.name}: document has no 'agents' key")
        agents: object = data["agents"]
    else:
        agents = data
    problems = validate_agents(agents)
    if problems:
        raise ValueError("\n".join(f"{source.name}: {p}" for p in problems))
    # validate_agents() accepted this value, so it is a non-empty list of agent objects.
    return cast(list[dict[str, Any]], agents)


def get_agent(ident: str, path: Path | None = None) -> dict[str, Any]:
    ident_l = ident.lower()
    for a in load_manifest(path):
        if ident_l in (a["id"].lower(), a["code"].lower(), a["slug"].lower()):
            return a
    raise KeyError(f"no agent matches {ident!r}")


def by_capability(capability: str, path: Path | None = None) -> list[dict[str, Any]]:
    return [a for a in load_manifest(path) if capability in a["capabilities"]]
