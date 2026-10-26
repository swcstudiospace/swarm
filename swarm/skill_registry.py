"""Skill sources under prompts/skills/<owner>/<short>.md and their validation (v2.2 design 4.8).

`<owner>` is an agent slug or `shared`. `load_skills` parses every source file tolerantly (a malformed file
becomes a `Skill` carrying `parse_errors`), and `validate_skills` returns one actionable message per problem,
each naming the file. `python3 -m swarm.skill_registry check` exits non-zero on any message.
`docs` writes docs/skills/catalog.md and docs/skills/agents/<owner>.md; `docs --check` fails naming each stale file.

Count policy comes from `agents.json` top-level `capability_policy` (working default 10-15 skills per agent).
A policy entry may carry `"enforce": false`: its minimum is then not checked (its maximum still is). An agent
with no skill yet is not reported as below the minimum unless the registry states a skills minimum above zero
explicitly, so a registry without a `skills` entry stays valid while the sources are still being authored.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_SUBDIR = Path("prompts") / "skills"
SHARED_OWNER = "shared"
MAX_BYTES = 6 * 1024
MAX_DESCRIPTION = 160
FRONTMATTER_KEYS = ("name", "description", "owner", "tools")
SECTIONS = ("When to Use", "Procedure", "Outputs", "Failure Handling", "Handover")
DEFAULT_POLICY: dict[str, dict[str, int]] = {"tools": {"min": 10, "max": 15}, "skills": {"min": 10, "max": 15}}
SIMILARITY_LIMIT = 0.85
# omp skills the generator itself writes (build_agents: A01's driver skill); a source skill may never take the name.
# Agent index skills are named by agent slug, which no source name can equal (`<slug>--<short>` or `swarm-<short>`).
RESERVED_NAMES = frozenset({"swarm-orchestrate"})
# Tool names a procedure step may cite besides the skill's own registry tools.
_HOST_TOOLS = frozenset({"read", "grep", "glob", "bash", "write", "edit", "task", "yield", "eval"})
_NAME_PART = r"[a-z0-9]+(?:-[a-z0-9]+)*"
_NAME_RE = re.compile(rf"^{_NAME_PART}$")
_STEP_RE = re.compile(r"^(\d+)\.\s+\S")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_WORD_RE = re.compile(r"[a-z0-9_]+")
_CALL_RE = re.compile(r"\bcall(?:s|ing)?\s+`?([a-z0-9_]+)")


@dataclass(frozen=True)
class Skill:
    """One parsed source file. `path` is repo-relative; `body` excludes the frontmatter."""

    path: str
    owner_dir: str
    stem: str
    name: str = ""
    description: str = ""
    owner: str = ""
    tools: tuple[str, ...] = ()
    body: str = ""
    size: int = 0
    parse_errors: tuple[str, ...] = field(default=())

    @property
    def short(self) -> str:
        """The part of `name` after the owner prefix (`<slug>--` or `swarm-`)."""
        if self.owner_dir == SHARED_OWNER:
            return self.name[len("swarm-"):] if self.name.startswith("swarm-") else self.name
        prefix = f"{self.owner_dir}--"
        return self.name[len(prefix):] if self.name.startswith(prefix) else self.name


def _split_frontmatter(text: str) -> tuple[list[str] | None, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:]).strip("\n")
    return None, text


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_tools(raw: str) -> tuple[tuple[str, ...], str | None]:
    raw = raw.strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return (), "tools must be written as a list like [a, b]"
    inner = raw[1:-1].strip()
    if not inner:
        return (), None
    return tuple(_unquote(part) for part in inner.split(",")), None


def parse_skill(text: str, rel_path: str, owner_dir: str, stem: str) -> Skill:
    """Parse one source file. Problems are recorded on the result, never raised."""
    errors: list[str] = []
    fm_lines, body = _split_frontmatter(text)
    values: dict[str, str] = {}
    tools: tuple[str, ...] = ()
    if fm_lines is None:
        errors.append("missing frontmatter: the file must open with a `---` block")
    else:
        for line in fm_lines:
            if not line.strip():
                continue
            key, sep, value = line.partition(":")
            key = key.strip()
            if not sep or not key:
                errors.append(f"frontmatter line {line!r} is not `key: value`")
            elif key in values:
                errors.append(f"frontmatter key {key!r} appears twice")
            elif key not in FRONTMATTER_KEYS:
                errors.append(f"frontmatter key {key!r} is not allowed (allowed: {', '.join(FRONTMATTER_KEYS)})")
            else:
                values[key] = value.strip()
        for key in FRONTMATTER_KEYS:
            if key not in values:
                errors.append(f"frontmatter is missing {key!r}")
        if "tools" in values:
            tools, problem = _parse_tools(values["tools"])
            if problem:
                errors.append(problem)
    return Skill(
        path=rel_path,
        owner_dir=owner_dir,
        stem=stem,
        name=_unquote(values.get("name", "")),
        description=_unquote(values.get("description", "")),
        owner=_unquote(values.get("owner", "")),
        tools=tools,
        body=body,
        size=len(text.encode("utf-8")),
        parse_errors=tuple(errors),
    )


def load_skills(root: Path | str | None = None) -> list[Skill]:
    """Every prompts/skills/<owner>/*.md under `root`, ordered by owner then file name."""
    base = Path(root) if root is not None else ROOT
    skills_dir = base / SKILLS_SUBDIR
    out: list[Skill] = []
    if not skills_dir.is_dir():
        return out
    for owner_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
        for path in sorted(owner_dir.glob("*.md")):
            rel = path.relative_to(base).as_posix()
            out.append(parse_skill(path.read_text(encoding="utf-8"), rel, owner_dir.name, path.stem))
    return out


def skills_for_owner(skills: list[Skill], owner: str) -> list[Skill]:
    return [s for s in skills if s.owner_dir == owner]


def skills_for(owner: str, *, root: Path | str | None = None) -> list[dict[str, Any]]:
    """Catalog view of one owner's skills (an agent slug or `shared`): dicts with name, description, owner, tools."""
    return [
        {"name": s.name, "description": s.description, "owner": s.owner_dir, "tools": list(s.tools), "path": s.path}
        for s in skills_for_owner(load_skills(root), owner)
    ]


def _h2_headings(body: str) -> list[str]:
    headings: list[str] = []
    fenced = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            headings.append(line[3:].strip())
    return headings


def _procedure_steps(body: str) -> list[tuple[int, str]]:
    """(number, text) of each numbered step in the Procedure section; continuation lines join their step."""
    steps: list[tuple[int, str]] = []
    in_section = False
    fenced = False
    for line in body.splitlines():
        if not fenced and line.startswith("## "):
            in_section = line[3:].strip() == "Procedure"
            continue
        if _FENCE_RE.match(line):
            fenced = not fenced
        if not in_section:
            continue
        match = _STEP_RE.match(line)
        if match and not fenced:
            steps.append((int(match.group(1)), line))
        elif steps and line.strip():
            steps[-1] = (steps[-1][0], steps[-1][1] + " " + line.strip())
    return steps


def _names_tool_or_script(step: str, tools: tuple[str, ...]) -> bool:
    if "scripts/" in step:
        return True
    words = set(_WORD_RE.findall(step.lower()))
    return bool(words & (set(tools) | _HOST_TOOLS))


def _word_set(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD_RE.findall(text.lower()) if len(w) > 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _policy(agents_doc: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """(effective policy, whether the registry states a skills minimum explicitly)."""
    given = agents_doc.get("capability_policy")
    given = given if isinstance(given, dict) else {}
    policy = {**DEFAULT_POLICY, **{k: v for k, v in given.items() if isinstance(v, dict)}}
    skills_given = given.get("skills")
    explicit_min = isinstance(skills_given, dict) and isinstance(skills_given.get("min"), int)
    return policy, explicit_min


def _min_enforced(entry: object) -> bool:
    return not (isinstance(entry, dict) and entry.get("enforce") is False)


def _tool_callers(agents_doc: dict[str, Any]) -> dict[str, set[str]]:
    """Registry tool name -> permitted caller agent ids, over agent registered_tools and shared_tools."""
    callers: dict[str, set[str]] = {}
    containers: list[dict[str, Any]] = []
    for agent in agents_doc.get("agents") or []:
        if isinstance(agent, dict):
            containers.extend(t for t in agent.get("registered_tools") or [] if isinstance(t, dict))
    containers.extend(t for t in agents_doc.get("shared_tools") or [] if isinstance(t, dict))
    for tool in containers:
        name = tool.get("name")
        if isinstance(name, str):
            permitted = tool.get("permitted_callers")
            callers[name] = {c for c in permitted if isinstance(c, str)} if isinstance(permitted, list) else set()
    return callers


def validate_skills(skills: list[Skill], agents_doc: dict[str, Any], *, counts: bool = True) -> list[str]:
    """One message per problem, each starting with the offending file (or `agents.json`). Empty means valid.

    `counts=False` skips the per-owner count policy (the generator validates content only; counts are a registry gate).
    """
    errors: list[str] = []
    agents = [a for a in agents_doc.get("agents") or [] if isinstance(a, dict)]
    slug_to_id = {a["slug"]: a["id"] for a in agents if "slug" in a and "id" in a}
    agent_ids = set(slug_to_id.values())
    callers = _tool_callers(agents_doc)
    policy, explicit_min = _policy(agents_doc)
    names: dict[str, str] = {}
    reserved = RESERVED_NAMES | frozenset(slug_to_id)

    for s in skills:
        err = errors.append
        for problem in s.parse_errors:
            err(f"{s.path}: {problem}")
        if s.owner_dir != SHARED_OWNER and s.owner_dir not in slug_to_id:
            err(f"{s.path}: owner directory {s.owner_dir!r} is neither an agent slug nor 'shared'")
        if s.owner and s.owner != s.owner_dir:
            err(f"{s.path}: frontmatter owner {s.owner!r} does not match its directory {s.owner_dir!r}")
        if s.name:
            _check_name(s, err, reserved)
            if s.name in names:
                err(f"{s.path}: name {s.name!r} is already used by {names[s.name]}")
            else:
                names[s.name] = s.path
        if s.description:
            if "\n" in s.description or len(s.description) > MAX_DESCRIPTION:
                err(f"{s.path}: description must be one line of at most {MAX_DESCRIPTION} characters ({len(s.description)})")
            if not s.description.startswith("Use when"):
                err(f"{s.path}: description must start with 'Use when'")
        elif "description" not in " ".join(s.parse_errors):
            err(f"{s.path}: description is empty")
        if s.size > MAX_BYTES:
            err(f"{s.path}: {s.size} bytes exceeds the {MAX_BYTES}-byte limit")
        _check_tools(s, callers, agent_ids, slug_to_id, err)
        _check_cited_tools(s, callers, err)
        _check_sections(s, err)

    if counts:
        errors.extend(_check_counts(skills, agents_doc, slug_to_id, policy, explicit_min))
    errors.extend(_check_duplicates(skills))
    errors.extend(_check_baseline(agents_doc, names))
    return errors


def _check_name(s: Skill, err: Callable[[str], None], reserved: frozenset[str] = frozenset()) -> None:
    if s.name in reserved:
        err(f"{s.path}: skill name {s.name!r} is reserved for a generated skill (driver or agent index); pick another name")
    if s.owner_dir == SHARED_OWNER:
        ok = s.name.startswith("swarm-") and bool(_NAME_RE.match(s.name)) and len(s.name) > len("swarm-")
        if not ok:
            err(f"{s.path}: shared skill name {s.name!r} must be 'swarm-<short>' using [a-z0-9] and single hyphens")
    else:
        prefix = f"{s.owner_dir}--"
        if not s.name.startswith(prefix) or not _NAME_RE.match(s.name[len(prefix):]):
            err(f"{s.path}: agent skill name {s.name!r} must be '{prefix}<short>' with <short> matching [a-z0-9]+(-[a-z0-9]+)*")
    if s.short != s.stem:
        err(f"{s.path}: file name {s.stem + '.md'!r} must equal the <short> part of the name ({s.short + '.md'!r})")


def _check_tools(s: Skill, callers: dict[str, set[str]], agent_ids: set[str], slug_to_id: dict[str, str],
                 err: Callable[[str], None]) -> None:
    seen: set[str] = set()
    for tool in s.tools:
        if tool in seen:
            err(f"{s.path}: tool {tool!r} is listed twice")
            continue
        seen.add(tool)
        if tool not in callers:
            err(f"{s.path}: tool {tool!r} is not in the registry")
        elif s.owner_dir == SHARED_OWNER:
            missing = sorted(agent_ids - callers[tool])
            if missing:
                err(f"{s.path}: shared skill uses {tool!r}, which {', '.join(missing)} may not call")
        elif s.owner_dir in slug_to_id and slug_to_id[s.owner_dir] not in callers[tool]:
            err(f"{s.path}: owner {s.owner_dir} ({slug_to_id[s.owner_dir]}) is not a permitted caller of {tool!r}")


def _check_cited_tools(s: Skill, callers: dict[str, set[str]], err: Callable[[str], None]) -> None:
    """Every registry tool a Procedure step calls (`call <tool>`, `Call `<tool>``) must be in the skill's own `tools`
    list: the list is what the agent page and the generated skill advertise. A tool only mentioned (`never call the
    <tool>`, `<tool> gives only names`) is prose, not a call."""
    listed = set(s.tools)
    for number, text in _procedure_steps(s.body):
        called = {m.group(1) for m in _CALL_RE.finditer(text.lower())}
        for tool in sorted(called & callers.keys() - listed):
            err(f"{s.path}: procedure step {number} calls tool {tool!r}, which is not in the skill's tools list")


def _check_sections(s: Skill, err: Callable[[str], None]) -> None:
    found = _h2_headings(s.body)
    for section in SECTIONS:
        if section not in found:
            err(f"{s.path}: missing section '## {section}'")
    extra = [h for h in found if h not in SECTIONS]
    if extra:
        err(f"{s.path}: unexpected section(s) {', '.join(repr('## ' + h) for h in extra)}")
    ordered = [h for h in found if h in SECTIONS]
    if len(ordered) != len(set(ordered)):
        err(f"{s.path}: a section appears more than once")
    elif ordered != [x for x in SECTIONS if x in ordered]:
        err(f"{s.path}: sections out of order; expected {' > '.join(SECTIONS)}")
    if "Procedure" in found:
        steps = _procedure_steps(s.body)
        if not steps:
            err(f"{s.path}: '## Procedure' needs numbered steps")
        for index, (number, text) in enumerate(steps, 1):
            if number != index:
                err(f"{s.path}: procedure step {number} is out of sequence (expected {index})")
            if not _names_tool_or_script(text, s.tools):
                err(f"{s.path}: procedure step {number} names no tool or script it uses")


def _check_counts(skills: list[Skill], agents_doc: dict[str, Any], slug_to_id: dict[str, str], policy: dict[str, Any],
                  explicit_min: bool) -> list[str]:
    errors: list[str] = []
    lo, hi = _bounds(policy.get("skills"))
    if not _min_enforced(policy.get("skills")):
        lo = None
    combined = agents_doc.get("capability_policy", {}).get("combined") if isinstance(agents_doc.get("capability_policy"), dict) else None
    clo, chi = _bounds(combined) if isinstance(combined, dict) else (None, None)
    if not _min_enforced(combined):
        clo = None
    tool_counts = {
        a["slug"]: len([t for t in a.get("registered_tools") or [] if isinstance(t, dict)])
        for a in agents_doc.get("agents") or []
        if isinstance(a, dict) and "slug" in a
    }
    for slug in sorted(slug_to_id):
        count = len(skills_for_owner(skills, slug))
        where = f"prompts/skills/{slug}"
        if count == 0 and not (explicit_min and lo):
            continue  # not authored yet: not an error until the registry demands a minimum
        if lo is not None and count < lo:
            errors.append(f"{where}: {count} skills is below the policy minimum of {lo}")
        if hi is not None and count > hi:
            errors.append(f"{where}: {count} skills exceeds the policy maximum of {hi}")
        if clo is not None or chi is not None:
            total = count + tool_counts.get(slug, 0)
            if clo is not None and total < clo:
                errors.append(f"{where}: {total} tools+skills is below the combined minimum of {clo}")
            if chi is not None and total > chi:
                errors.append(f"{where}: {total} tools+skills exceeds the combined maximum of {chi}")
    return errors


def _bounds(entry: object) -> tuple[int | None, int | None]:
    if not isinstance(entry, dict):
        return None, None
    lo, hi = entry.get("min"), entry.get("max")
    return (lo if isinstance(lo, int) else None), (hi if isinstance(hi, int) else None)


def _check_duplicates(skills: list[Skill]) -> list[str]:
    errors: list[str] = []
    by_owner: dict[str, list[Skill]] = {}
    for s in skills:
        by_owner.setdefault(s.owner_dir, []).append(s)
    for items in by_owner.values():
        words = {s.path: (_word_set(s.description), _word_set(s.body)) for s in items}
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                desc_sim = _jaccard(words[a.path][0], words[b.path][0])
                body_sim = _jaccard(words[a.path][1], words[b.path][1])
                if body_sim >= SIMILARITY_LIMIT or desc_sim >= SIMILARITY_LIMIT:
                    errors.append(
                        f"{b.path}: near-duplicate of {a.path} (description {desc_sim:.2f}, body {body_sim:.2f}); "
                        "merge them or make the procedures distinct"
                    )
    return errors


def _check_baseline(agents_doc: dict[str, Any], names: dict[str, str]) -> list[str]:
    """When `shared_baseline.skills` is a list of names, each must exist as a skill source."""
    baseline = agents_doc.get("shared_baseline")
    wanted = baseline.get("skills") if isinstance(baseline, dict) else None
    if not isinstance(wanted, list):
        return []
    return [f"agents.json: shared_baseline skill {n!r} has no source under prompts/skills/shared" for n in wanted if isinstance(n, str) and n not in names]


DOCS_HEADER = "<!-- generated by python3 -m swarm.skill_registry docs; do not edit -->"
DOCS_DIR = "docs/skills"
CATALOG_PATH = f"{DOCS_DIR}/catalog.md"
AGENT_PAGES_DIR = f"{DOCS_DIR}/agents"


def _section(body: str, heading: str) -> str:
    """Text of one `## heading` section (fence-aware), stripped; empty when absent."""
    lines: list[str] = []
    inside = False
    fenced = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            inside = line[3:].strip() == heading
            continue
        if inside:
            lines.append(line)
    return " ".join(part.strip() for part in lines if part.strip())


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _tools_cell(tools: tuple[str, ...]) -> str:
    return ", ".join(f"`{t}`" for t in tools) if tools else "none"


def _owners(skills: list[Skill], agents_doc: dict[str, Any]) -> list[tuple[str, str]]:
    """(owner dir, display title) in page order: shared first, then agents in registry order."""
    owners = [(SHARED_OWNER, "Shared baseline")]
    for agent in agents_doc.get("agents") or []:
        if isinstance(agent, dict) and agent.get("slug"):
            owners.append((agent["slug"], f"{agent.get('id', '')} {agent.get('code', '')} — {agent.get('name', agent['slug'])}".strip()))
    known = {slug for slug, _ in owners}
    for s in skills:
        if s.owner_dir not in known:
            known.add(s.owner_dir)
            owners.append((s.owner_dir, s.owner_dir))
    return owners


def render_docs(skills: list[Skill], agents_doc: dict[str, Any]) -> dict[str, str]:
    """Repo-relative path -> generated text: the catalog plus one page per owner (shared included)."""
    owners = _owners(skills, agents_doc)
    catalog = [
        DOCS_HEADER,
        "",
        "# Skill catalog",
        "",
        f"{len(skills)} skills. Source: `prompts/skills/<owner>/<short>.md`. Per-owner pages: `docs/skills/agents/<slug>.md`.",
        "",
        "| Name | Owner | Description | Tools called | Size (bytes) |",
        "| --- | --- | --- | --- | --- |",
    ]
    pages: dict[str, str] = {}
    for owner, heading in owners:
        mine = sorted(skills_for_owner(skills, owner), key=lambda s: s.name)
        for s in mine:
            catalog.append(f"| `{s.name}` | {owner} | {_cell(s.description)} | {_tools_cell(s.tools)} | {s.size} |")
        page = [
            DOCS_HEADER,
            "",
            f"# {heading} — skills",
            "",
            f"Owner: `{owner}`. {len(mine)} skill(s), {sum(s.size for s in mine)} bytes of source. Overview: [catalog](../catalog.md).",
            "",
            "## Index",
            "",
            "| Skill | Trigger (description) | Tools used |",
            "| --- | --- | --- |",
        ]
        page += [f"| `{s.name}` | {_cell(s.description)} | {_tools_cell(s.tools)} |" for s in mine]
        page += ["", "## Handover notes", ""]
        page += [f"- `{s.name}`: {_section(s.body, 'Handover') or 'none'}" for s in mine] or ["- none"]
        pages[f"{AGENT_PAGES_DIR}/{owner}.md"] = "\n".join(page) + "\n"
    catalog.append("")
    return {CATALOG_PATH: "\n".join(catalog), **pages}


def write_docs(skills: list[Skill], agents_doc: dict[str, Any], root: Path, *, check: bool) -> int:
    """Write the generated docs under `root`, or with `check` fail naming each stale, missing or orphan file."""
    rendered = render_docs(skills, agents_doc)
    drifted: list[str] = []
    for rel, text in rendered.items():
        dest = root / rel
        if check:
            if not dest.is_file() or dest.read_text(encoding="utf-8") != text:
                drifted.append(rel)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
    pages_dir = root / AGENT_PAGES_DIR
    for orphan in sorted(pages_dir.glob("*.md")) if pages_dir.is_dir() else []:
        rel = orphan.relative_to(root).as_posix()
        if rel not in rendered:
            if check:
                drifted.append(rel)
            else:
                orphan.unlink()
    if drifted:
        print("skill docs drift:\n" + "\n".join(drifted), file=sys.stderr)
        return 1
    if check:
        print("skill docs ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m swarm.skill_registry", description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("check", "docs"))
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    ap.add_argument("--agents", help="agents.json path (default: <root>/agents.json)")
    ap.add_argument("--check", action="store_true", help="with `docs`: fail instead of writing when committed docs differ")
    args = ap.parse_args(argv)
    root = Path(args.root)
    agents_path = Path(args.agents) if args.agents else root / "agents.json"
    try:
        doc = json.loads(agents_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"{agents_path}: cannot read registry ({exc})", file=sys.stderr)
        return 2
    skills = load_skills(root)
    errors = validate_skills(skills, doc)
    for message in errors:
        print(message, file=sys.stderr)
    if errors:
        print(f"skill registry: {len(errors)} error(s) in {len(skills)} skill(s)", file=sys.stderr)
        return 1
    if args.command == "docs":
        return write_docs(skills, doc, root, check=args.check)
    print(f"skill registry: {len(skills)} skill(s) ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
