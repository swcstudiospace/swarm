"""omp agent + skill generation contract (AGENT-01..07, ORCH-01)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from swarm.manifest import load_manifest
from swarm.skill_registry import load_skills

ROOT = Path(__file__).resolve().parent.parent
OMP_AGENTS = ROOT / "omp" / "agents"
OMP_SKILLS = ROOT / "omp" / "skills"
# Generated omp agent files are intentionally long XML prompts (the user explicitly accepted long
# prompts). The largest, a01-orchestrator.md (its spawns list names the 29 other agents), stays under 64 KiB with headroom for growth.
OMP_AGENT_MAX_BYTES = 64 * 1024


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


skills_mod = _load("_write_skills")


def _fm_lines(text):
    """Frontmatter lines: between the opening `---` line and the next line that is exactly `---`."""
    lines = text.split("\n")
    assert lines[0] == "---"
    return lines[1:lines.index("---", 1)]


def _fm(text):
    """Decode frontmatter; double-quoted values are JSON string literals (valid YAML double-quoted scalars)."""
    fm = {}
    for line in _fm_lines(text):
        k, v = line.split(": ", 1)
        fm[k] = json.loads(v) if v.startswith('"') else v
    return fm


def test_omp_agent_files_match_manifest():
    slugs = {a["slug"] for a in load_manifest()}
    assert {p.stem for p in OMP_AGENTS.glob("*.md")} == slugs
    for slug in slugs:
        assert _fm((OMP_AGENTS / f"{slug}.md").read_text(encoding="utf-8"))["name"] == slug


def test_omp_skill_files_exist():
    for a in load_manifest():
        p = OMP_SKILLS / a["slug"] / "SKILL.md"
        fm = _fm(p.read_text(encoding="utf-8"))
        assert fm["name"] == a["slug"]
        assert fm["description"].strip('"').strip()


def test_omp_skill_matches_renderer():
    for a in load_manifest():
        p = OMP_SKILLS / a["slug"] / "SKILL.md"
        assert skills_mod.omp_skill(a) == p.read_text(encoding="utf-8"), a["slug"]


def test_omp_source_skills_match_renderer():
    for s in load_skills(ROOT):
        p = OMP_SKILLS / s.name / "SKILL.md"
        assert skills_mod.source_skill(s) == p.read_text(encoding="utf-8"), s.name


builder = _load("build_agents")
SCHEMA = ROOT / "swarm" / "schemas" / "task.result.v1.json"
BRIDGE = ["swarm_catalog", "swarm_tool_call"]
SWARM_NAMES = {"swarm_plan", "swarm_status", "swarm_ingest", "swarm_transition", "swarm_assignment", "swarm_gate", *BRIDGE}
OMP_TOOLS = {"read", "grep", "glob", "bash", "write", "edit", "task"} | SWARM_NAMES
GATE_AGENTS = {"A08", "A09", "A10", "A12"}
KEY_ORDER = ["name", "description", "tools", "spawns", "blocking", "autoloadSkills", "output"]


def _agents():
    return list(load_manifest())


def _omp(a):
    return (OMP_AGENTS / f"{a['slug']}.md").read_text(encoding="utf-8")


def _keys(text):
    return [line.split(": ", 1)[0] for line in _fm_lines(text)]


# Contract written from the spec, not from build_agents: manifest tool names map to the fixed omp
# tool names, and only A01 (orchestrator) and the gate agents receive hidden swarm_* grants.
OMP_TOOL_SPEC = {"Read": "read", "Grep": "grep", "Glob": "glob", "Bash": "bash", "Write": "write", "Edit": "edit", "Agent": "task"}
ROLE_GRANT_SPEC = {
    "A01": ["swarm_plan", "swarm_status", "swarm_ingest", "swarm_transition", "swarm_assignment"],
    "A08": ["swarm_gate"],
    "A09": ["swarm_gate"],
    "A10": ["swarm_gate"],
    "A12": ["swarm_gate"],
}
# Every agent also holds the registry bridge, after its role grants (DESIGN 5.3).
SWARM_GRANT_SPEC = {f"A{n:02d}": ROLE_GRANT_SPEC.get(f"A{n:02d}", []) + BRIDGE for n in range(1, 31)}


def test_omp_tools_exact():
    for a in _agents():
        tools = _fm(_omp(a))["tools"].split(", ")
        assert tools == [OMP_TOOL_SPEC[t] for t in a["tools"]] + SWARM_GRANT_SPEC.get(a["id"], []), a["id"]
    assert _fm(_omp(_a("A01")))["tools"] == (
        "read, grep, glob, bash, task, write, swarm_plan, swarm_status, swarm_ingest, swarm_transition, swarm_assignment, "
        "swarm_catalog, swarm_tool_call"
    )


def test_omp_tools_follow_manifest_and_authority_ceiling():
    for a in _agents():
        tools = _fm(_omp(a))["tools"].split(", ")
        assert tools == [OMP_TOOL_SPEC[t] for t in a["tools"]] + SWARM_GRANT_SPEC.get(a["id"], []), a["id"]
        assert not {"eval", "run_code"} & set(tools), a["id"]


def test_swarm_tool_grants():
    """Only A01 leases and requests assignments; gate agents retain only their verdict authority; all hold the bridge."""
    for a in _agents():
        swarm = [t for t in _fm(_omp(a))["tools"].split(", ") if t.startswith("swarm_")]
        if a["id"] == "A01":
            assert swarm == ["swarm_plan", "swarm_status", "swarm_ingest", "swarm_transition", "swarm_assignment", *BRIDGE]
        elif a["id"] in GATE_AGENTS:
            assert swarm == ["swarm_gate", *BRIDGE], a["id"]
        else:
            assert swarm == BRIDGE, a["id"]


@pytest.mark.parametrize(
    ("aid", "grant"),
    [
        ("A01", ["swarm_plan", "swarm_status", "swarm_ingest", "swarm_transition", "swarm_gate"]),
        ("A01", ["swarm_gate"]),
        ("A05", ["swarm_status"]),
        ("A09", ["swarm_gate", "swarm_transition"]),
        ("A08", ["swarm_nope"]),
    ],
)
def test_swarm_tools_guard(monkeypatch, aid, grant):
    monkeypatch.setitem(builder.SWARM_TOOLS, aid, grant)
    with pytest.raises(ValueError, match=aid):
        builder.render_omp(_a(aid), _agents())


@pytest.mark.parametrize("aid", [a["id"] for a in _agents() if a["id"] != "A01"])
def test_specialists_cannot_receive_assignment_transport(monkeypatch, aid):
    monkeypatch.setitem(builder.SWARM_TOOLS, aid, ["swarm_assignment"])
    with pytest.raises(ValueError, match=aid):
        builder.render_omp(_a(aid), _agents())


def test_omp_no_model_key():
    for a in _agents():
        text = _omp(a)
        assert "model" not in _keys(text)
        head = text.split("---", 2)[1]
        for bad in ("opus", "sonnet", "haiku", "inherit"):
            assert bad not in head.split("output:")[0].lower(), (a["id"], bad)


def _a(aid):
    return next(a for a in _agents() if a["id"] == aid)


def test_render_omp_unmapped_tool_raises():
    with pytest.raises(ValueError, match="A05"):
        builder.render_omp({**_a("A05"), "tools": ["Read", "WebFetch"]}, _agents())


def test_render_omp_empty_tools_raises():
    with pytest.raises(ValueError, match="A05"):
        builder.render_omp({**_a("A05"), "tools": []}, _agents())


def test_render_omp_specialist_task_raises():
    with pytest.raises(ValueError, match="A09"):
        builder.render_omp({**_a("A09"), "tools": ["Read", "Agent"]}, _agents())


def test_omp_spawn_topology():
    agents = _agents()
    for a in agents:
        text = _omp(a)
        fm = _fm(text)
        if a["id"] == "A01":
            assert fm["spawns"].split(", ") == [x["slug"] for x in agents if x["id"] != "A01"]
            assert a["slug"] not in fm["spawns"]
            assert "task" in fm["tools"].split(", ")
        else:
            assert "\nspawns: \"\"\n" in text, a["id"]
            assert "task" not in fm["tools"].split(", ")


def test_omp_spawns_full_under_only():
    from conftest import run_script
    r = run_script("build_agents.py", "--check", "--only", "a01-orchestrator")
    assert r.returncode == 0, r.stdout + r.stderr


def test_omp_output_equals_schema():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for a in _agents():
        text = _omp(a)
        fm = _fm(text)
        assert json.loads(fm["output"]) == schema
        assert "schemaMode" not in _keys(text)


def test_omp_autoload_own_slug():
    """The list starts with the agent's own slug and every entry exists as a generated skill (omp drops unknown names silently)."""
    for a in _agents():
        parts = [x.strip() for x in _fm(_omp(a))["autoloadSkills"].split(",")]
        assert parts[0] == a["slug"], a["id"]
        if a["id"] == "A01":
            assert parts[:2] == ["a01-orchestrator", "swarm-orchestrate"]
        assert len(parts) == len(set(parts)), a["id"]
        for name in parts:
            assert (OMP_SKILLS / name / "SKILL.md").is_file(), (a["id"], name)


def test_omp_autoload_shared_handover_skills_follow_their_sources():
    """The two handover skills are autoloaded exactly when their sources exist (D6)."""
    shared = {s.name for s in load_skills(ROOT) if s.owner_dir == "shared"}
    expected = [n for n in ("swarm-handover-intake", "swarm-handover-emit") if n in shared]
    for a in _agents():
        parts = _fm(_omp(a))["autoloadSkills"].split(",")
        assert [n for n in parts if n in ("swarm-handover-intake", "swarm-handover-emit")] == expected, a["id"]
        assert parts[len(parts) - len(expected):] == expected, a["id"]


def _skill_dirs():
    return sorted(p for p in OMP_SKILLS.iterdir() if (p / "SKILL.md").is_file())


def test_omp_skill_names_match_dirs_and_are_unique():
    names = [_fm((p / "SKILL.md").read_text(encoding="utf-8"))["name"] for p in _skill_dirs()]
    assert names == [p.name for p in _skill_dirs()]
    assert len(names) == len(set(names))


def test_omp_visible_skills_are_the_index_skills_only():
    """Every skill outside the index skills (one per agent) and the driver skill is hidden; the visible count stays <= agents + 1."""
    visible_names = {a["slug"] for a in _agents()} | {"swarm-orchestrate"}
    visible = set()
    for p in _skill_dirs():
        fm = _fm((p / "SKILL.md").read_text(encoding="utf-8"))
        hidden = fm.get("disable-model-invocation") == "true"
        assert hidden == (p.name not in visible_names), p.name
        if not hidden:
            visible.add(p.name)
    assert visible == visible_names and len(visible) <= len(_agents()) + 1


def test_omp_per_agent_skill_count_within_policy():
    doc = json.loads((ROOT / "agents.json").read_text(encoding="utf-8"))
    policy = doc.get("capability_policy", {}).get("skills", {"max": 15})
    by_owner = {}
    for s in load_skills(ROOT):
        by_owner.setdefault(s.owner_dir, []).append(s.name)
    for owner, names in by_owner.items():
        if owner != "shared":
            assert len(names) <= policy["max"], owner
        assert len(names) == len(set(names)), owner


def test_omp_index_skill_lists_registered_tools_and_skills():
    sources = load_skills(ROOT)
    for a in _agents():
        text = (OMP_SKILLS / a["slug"] / "SKILL.md").read_text(encoding="utf-8")
        tools = a.get("registered_tools") or []
        assert ("## Registered tools" in text) == bool(tools), a["id"]
        for t in tools:
            assert f"`{t['name']}` ({t.get('side_effect_tier', 'READ')})" in text, (a["id"], t["name"])
        own = [s for s in sources if s.owner_dir == a["slug"]]
        assert ("## Skills" in text) == bool(own), a["id"]
        for s in own:
            assert f"`skill://{s.name}`" in text, (a["id"], s.name)


def test_omp_blocking_set():
    blocking = set()
    for a in _agents():
        fm = _fm(_omp(a))
        if "blocking" in fm:
            assert fm["blocking"] == "true", a["id"]
            blocking.add(a["id"])
    assert blocking == {"A01", "A08", "A09", "A10", "A12"}


def test_omp_frontmatter_contract_fields():
    required = {"name", "description", "tools", "spawns", "autoloadSkills", "output"}
    for a in _agents():
        keys = _keys(_omp(a))
        assert len(keys) == len(set(keys)), a["id"]
        assert required <= set(keys) <= required | {"blocking"}, a["id"]


def test_omp_frontmatter_key_order():
    for a in _agents():
        keys = _keys(_omp(a))
        assert keys == [k for k in KEY_ORDER if k in keys] and len(keys) == len(set(keys))
        assert set(KEY_ORDER) - {"blocking"} <= set(keys)


def test_omp_no_bundled_name_collision():
    slugs = {a["slug"] for a in _agents()}
    assert not slugs & {"scout", "reviewer", "security-reviewer", "task", "sonic", "main", "sub"}


GATES = ("a08-qa", "a09-reviewer", "a10-security", "a12-release")


def test_a01_strict_dispatch():
    text = builder.OMP_ORCH_PREAMBLE
    assert 'schemaMode: "strict"' in text
    rule = next(line for line in text.splitlines() if 'schemaMode: "strict"' in line)
    for gate in GATES:
        assert gate in rule, gate
    assert _omp(_a("A01")).count(rule) == 1
    omp_rule = next(line for line in _omp(_a("A01")).splitlines() if 'schemaMode: "strict"' in line)
    for gate in GATES:
        assert gate in omp_rule, gate


def test_a01_lease_assignment_dispatch_protocol():
    for text in (builder.OMP_ORCH_PREAMBLE, _omp(_a("A01"))):
        block = text[text.index("<swarm_runtime>\n"):text.index("</swarm_runtime>")]
        lines = block.splitlines()
        lease = _rule(lines, "- Lease:")
        assignment = _rule(lines, "- Assignment:")
        dispatch = _rule(lines, "- Dispatch:")
        assert lease < assignment < dispatch
        assert {"CLAIMED", "IN_PROGRESS"} <= set(lines[lease].split())
        for token in ("swarm_assignment", "task_id", "correlation_id", "assignment_prompt",
                      'status: "ok"', "assignment_verified: true"):
            assert token in lines[assignment], token
        for token in ("unsigned", "advisory", "dry-run", "no dispatch authority",
                      "Never construct or sign", "signing material", "eval", "xd://run_code", "raw swarm state"):
            assert token in lines[assignment], token
        for token in ("verified assignment", "`agent_slug`", "`prompt` unchanged"):
            assert token in lines[dispatch], token


def _a01_runtime_lines():
    text = _omp(_a("A01"))
    block = text[text.index("<swarm_runtime>\n") : text.index("</swarm_runtime>")]
    return [line for line in block.splitlines()[1:] if line.strip()]


def _rule(lines, needle):
    hits = [i for i, line in enumerate(lines) if needle in line]
    assert len(hits) == 1, (needle, hits)
    return hits[0]


def test_a01_gate_is_first():
    lines = _a01_runtime_lines()
    assert "Step 0" in lines[0]
    assert _omp(_a("A01")).count("Step 0") == 1
    plan = _rule(lines, "plan mode")
    depth = _rule(lines, '"depth"')
    common = lines.index(builder._OMP_COMMON.splitlines()[0])
    strict = _rule(lines, 'schemaMode: "strict"')
    assert 0 < plan < depth < common < strict


def test_a01_plan_mode_discriminator():
    lines = _a01_runtime_lines()
    plan = lines[_rule(lines, "plan mode")]
    assert "`bash`" in plan and "`_bash`" in plan
    assert "`write`" not in plan and "`_write`" not in plan
    assert "IN_REVIEW" in plan and "summary_md" in plan and "spawn nothing" in plan


def test_a01_depth_rule_blocked_not_in_review():
    lines = _a01_runtime_lines()
    depth = lines[_rule(lines, '"depth"')]
    for token in ("`task`", "`_task`", '"BLOCKED"', "never IN_REVIEW"):
        assert token in depth, token
    assert "without reading, running or writing anything first" in depth
    assert "advisor review surface" in depth
    assert "work pool" in depth
    assert "Read-only discovery is not blocked" in depth


def test_a01_prose_brief_is_not_a_signing_stop():
    """A pasted signature is not a prerequisite for planning. The workpool-child yield stays needs depth."""
    lines = _a01_runtime_lines()
    brief = lines[_rule(lines, "prose brief")]
    assert "swarm_plan" in brief and "swarm_status" in brief
    assert "do not ask for credentials" in brief
    assert "separate release hold" in brief
    assert '"depth"' not in brief
    depth = lines[_rule(lines, '"depth"')]
    assert "signed task.assign requirement" in depth


def test_a01_advisor_note_is_not_a_depth_block():
    """The Omp advisor model stays. An <advisory> note is balanced-budget review, not the depth cap."""
    lines = _a01_runtime_lines()
    advisor = lines[_rule(lines, "<advisory>")]
    assert "not a swarm BLOCKED" in advisor
    assert "work pool" in advisor
    assert "Keep the advisor model" in advisor
    assert "read-only nodes" in advisor
    assert "A01 cannot dispatch" in advisor
    assert '"depth"' not in advisor


def test_specialist_preamble_unchanged():
    for a in _agents():
        if a["id"] != "A01":
            assert "Step 0" not in _omp(a), a["id"]


def test_omp_yield_delivery():
    for a in _agents():
        text = _omp(a)
        assert "`yield` tool with your `task.result`" in text, a["id"]
        assert "replaces any fenced-json finish instruction" in text, a["id"]


def test_specialist_preamble_no_spawn():
    assert "`task`" not in builder.OMP_PREAMBLE
    assert "spawn" not in builder.OMP_PREAMBLE.lower()


def test_omp_preamble_keeps_autonomy_ceiling():
    for a in _agents():
        assert 'L3/L4, stop and report `"state": "BLOCKED", "needs": "human-approval: …"`' in _omp(a), a["id"]


def test_omp_registry_bridge_paragraph_is_short_and_only_in_omp_preambles():
    line = next(ln for ln in builder._OMP_COMMON.splitlines() if ln.startswith("- Registry tools and skills:"))
    assert len(line.encode("utf-8")) < 700
    for token in ("swarm_catalog", "swarm_tool_call", "skill://<name>", "never a parameter"):
        assert token in line
    for a in _agents():
        assert _omp(a).count(line) == 1, a["id"]
        assert "swarm_tool_call" not in (ROOT / ".claude" / "agents" / f"{a['slug']}.md").read_text(encoding="utf-8"), a["id"]


def test_omp_size_bound():
    sizes = {p.stem: p.stat().st_size for p in OMP_AGENTS.glob("*.md")}
    assert max(sizes.values()) <= OMP_AGENT_MAX_BYTES
    assert max(sizes, key=sizes.get) == "a01-orchestrator"


def test_omp_no_host_paths():
    files = [p for p in (ROOT / "omp").rglob("*") if p.is_file()] + [ROOT / ".grok" / "hooks" / "agent-swarm.json"]
    for p in files:
        assert "/root/" not in p.read_text(encoding="utf-8"), p


def _yaml_lists(text):
    """Minimal reader for a YAML mapping of block lists (`key:` then `  - item` lines); stdlib only."""
    out, key = {}, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line[0].isspace():
            assert line.endswith(":"), line
            key = line[:-1]
            out[key] = []
        else:
            item = line.strip()
            assert key is not None and item.startswith("- "), line
            out[key].append(item[2:].strip())
    return out


def test_omp_wiring():
    """D-11 / PKG-04: the repo loads its own package through `.omp/config.yml` only, never a symlink or copies."""
    config = ROOT / ".omp" / "config.yml"
    assert _yaml_lists(config.read_text(encoding="utf-8")) == {"extensions": ["omp"]}
    for name in ("agents", "skills", "extensions"):
        assert not os.path.lexists(ROOT / ".omp" / name), name
    ignored = subprocess.run(["git", "check-ignore", "-q", ".omp/config.yml"], cwd=ROOT)
    assert ignored.returncode == 1  # 1 = not ignored (0 would mean .gitignore hides the wiring)
    pkg = json.loads((ROOT / "omp" / "package.json").read_text(encoding="utf-8"))
    assert pkg["omp"]["extensions"] == ["./src/index.ts"]


_TREE = ("scripts", "swarm", "prompts", "agents.json", ".claude", ".grok", ".cursor", "omp", "grokbot")


@pytest.fixture()
def omp_tree(tmp_path):
    """Copy of the generator inputs/outputs; build_agents.py resolves ROOT from its own path."""
    ignore = shutil.ignore_patterns("__pycache__")
    for name in _TREE:
        src = ROOT / name
        if src.is_dir():
            shutil.copytree(src, tmp_path / name, ignore=ignore)
        else:
            shutil.copy2(src, tmp_path / name)
    return tmp_path


def _check(tree):
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    cmd = [sys.executable, str(tree / "scripts" / "build_agents.py"), "--check"]
    return subprocess.run(cmd, cwd=tree, capture_output=True, text=True, env=env)


def test_omp_check_clean_on_copy(omp_tree):
    r = _check(omp_tree)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("up-to-date")


@pytest.mark.parametrize(
    "rel",
    ["omp/agents/a05-backend.md", "omp/skills/a05-backend/SKILL.md", "omp/skills/swarm-orchestrate/SKILL.md"],
    ids=["agent", "skill", "orchestrate-skill"],
)
def test_omp_check_detects_drift(omp_tree, rel):
    target = omp_tree / rel
    edited = target.read_text(encoding="utf-8") + "\nhand edit: tools: task\n"
    target.write_text(edited, encoding="utf-8")
    r = _check(omp_tree)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.startswith("stale:") and rel in r.stdout
    assert target.read_text(encoding="utf-8") == edited


@pytest.mark.parametrize("rel", ["omp/agents/zz-orphan.md", "omp/skills/zz-orphan/SKILL.md"], ids=["agent", "skill"])
def test_omp_check_detects_orphan(omp_tree, rel):
    orphan = omp_tree / rel
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("---\nname: zz-orphan\n---\n", encoding="utf-8")
    r = _check(omp_tree)
    assert r.returncode == 1, r.stdout + r.stderr
    assert r.stdout.startswith("stale:") and rel in r.stdout
    assert orphan.exists()
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    subprocess.run([sys.executable, str(omp_tree / "scripts" / "build_agents.py")], cwd=omp_tree, check=True, env=env, capture_output=True)
    assert not orphan.exists()
    assert _check(omp_tree).returncode == 0


FIXTURE_SKILLS = ROOT / "tests" / "fixtures" / "skills"


def _add_source_skill(tree, fixture="valid.md", short="route-review", owner="a05-backend"):
    dest = tree / "prompts" / "skills" / owner
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{short}.md").write_text((FIXTURE_SKILLS / fixture).read_text(encoding="utf-8"), encoding="utf-8")


def _empty_the_skill_corpus(tree):
    """Start a scenario from no skill sources: drop prompts/skills and the baseline skill names that need them.

    The real agents.json lists the shared baseline skills, and `_check_baseline` refuses a tree where those
    sources are missing, so a scenario that builds its own shared sources must not declare the real ones.
    """
    shutil.rmtree(tree / "prompts" / "skills", ignore_errors=True)
    path = tree / "agents.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["shared_baseline"]["skills"] = []
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _build(tree, *args):
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    return subprocess.run([sys.executable, str(tree / "scripts" / "build_agents.py"), *args], cwd=tree, capture_output=True, text=True, env=env)


def test_source_skill_is_generated_hidden_and_listed_in_its_index(omp_tree):
    _add_source_skill(omp_tree)
    assert _build(omp_tree).returncode == 0
    skill = (omp_tree / "omp" / "skills" / "a05-backend--route-review" / "SKILL.md").read_text(encoding="utf-8")
    fm = _fm(skill)
    assert fm["name"] == "a05-backend--route-review" and fm["disable-model-invocation"] == "true"
    assert fm["description"].startswith("Use when")
    assert "generated from prompts/skills/a05-backend/route-review.md" in skill
    assert "## Procedure" in skill and "name:" not in skill.split("---", 2)[2]
    index = (omp_tree / "omp" / "skills" / "a05-backend" / "SKILL.md").read_text(encoding="utf-8")
    assert "## Skills" in index and "`skill://a05-backend--route-review`" in index
    assert _check(omp_tree).returncode == 0


def test_source_skill_drift_is_detected(omp_tree):
    _add_source_skill(omp_tree)
    assert _build(omp_tree).returncode == 0
    rel = "omp/skills/a05-backend--route-review/SKILL.md"
    target = omp_tree / rel
    target.write_text(target.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
    r = _check(omp_tree)
    assert r.returncode == 1 and r.stdout.startswith("stale:") and rel in r.stdout


def test_stray_skill_dir_is_an_orphan_but_a_source_skill_dir_is_not(omp_tree):
    _add_source_skill(omp_tree)
    assert _build(omp_tree).returncode == 0
    stray = omp_tree / "omp" / "skills" / "a05-backend--stray" / "SKILL.md"
    stray.parent.mkdir()
    stray.write_text("---\nname: a05-backend--stray\n---\n", encoding="utf-8")
    r = _check(omp_tree)
    assert r.returncode == 1 and "omp/skills/a05-backend--stray/SKILL.md" in r.stdout
    assert "route-review" not in r.stdout
    assert _build(omp_tree).returncode == 0
    assert not stray.exists() and (omp_tree / "omp" / "skills" / "a05-backend--route-review" / "SKILL.md").is_file()


def test_removed_source_skill_leaves_an_orphan_that_the_build_prunes(omp_tree):
    _add_source_skill(omp_tree)
    assert _build(omp_tree).returncode == 0
    (omp_tree / "prompts" / "skills" / "a05-backend" / "route-review.md").unlink()
    r = _check(omp_tree)
    assert r.returncode == 1 and "omp/skills/a05-backend--route-review/SKILL.md" in r.stdout
    assert _build(omp_tree).returncode == 0 and _check(omp_tree).returncode == 0


def test_invalid_source_skill_fails_the_build_before_writing(omp_tree):
    _add_source_skill(omp_tree, "bad_name.md")
    before = (omp_tree / "omp" / "agents" / "a05-backend.md").read_text(encoding="utf-8")
    r = _build(omp_tree)
    assert r.returncode == 1 and "invalid skill sources" in r.stderr and "route-review.md" in r.stderr
    assert (omp_tree / "omp" / "agents" / "a05-backend.md").read_text(encoding="utf-8") == before


def test_shared_handover_skills_are_autoloaded_once_their_sources_exist(omp_tree):
    _empty_the_skill_corpus(omp_tree)
    shared = omp_tree / "prompts" / "skills" / "shared"
    shared.mkdir(parents=True)
    for short, what in (("handover-intake", "reads the incoming bundle and checks its digests"), ("handover-emit", "writes the outgoing bundle for the next agent")):
        (shared / f"{short}.md").write_text(f"""---
name: swarm-{short}
description: Use when an agent {what}.
owner: shared
tools: []
---

## When to Use

- At the {short.split("-")[1]} edge of a task: {what}.

## Procedure

1. Run `scripts/swarm_tool.py` for the {short} step.

## Outputs

- The {short} result.

## Failure Handling

- Report BLOCKED naming the {short} step.

## Handover

- Pass the {short} result along.
""", encoding="utf-8")
    assert _build(omp_tree).returncode == 0, _build(omp_tree).stderr
    for a in _agents():
        parts = _fm((omp_tree / "omp" / "agents" / f"{a['slug']}.md").read_text(encoding="utf-8"))["autoloadSkills"].split(",")
        assert parts[-2:] == ["swarm-handover-intake", "swarm-handover-emit"], a["id"]
        assert parts[0] == a["slug"]
        for name in parts:
            assert (omp_tree / "omp" / "skills" / name / "SKILL.md").is_file(), name
    assert _check(omp_tree).returncode == 0


def test_autoload_without_a_generated_skill_fails_the_build(monkeypatch):
    monkeypatch.setattr(builder, "omp_autoload", lambda agent: [agent["slug"], "swarm-does-not-exist"])
    with pytest.raises(ValueError, match="swarm-does-not-exist"):
        builder.render_omp(_a("A05"), _agents())


TRICKY = 'path C:\\new dir "quoted": a\n---\nb — end'


@pytest.mark.parametrize("render", ["agent", "skill"])
def test_omp_description_round_trips(render):
    agents = _agents()
    a = dict(agents[4], description=TRICKY)
    text = builder.render_omp(a, agents) if render == "agent" else skills_mod.omp_skill(a)
    desc = _fm(text)["description"]
    assert TRICKY in desc
    assert desc.startswith(f"{a['id']} {a['code']} ")
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load("\n".join(_fm_lines(text)))["description"] == desc


def test_omp_orphan_symlink_never_followed(omp_tree, tmp_path_factory):
    victim = tmp_path_factory.mktemp("victim")
    (victim / "SKILL.md").write_text("keep", encoding="utf-8")
    link = omp_tree / "omp" / "skills" / "zz-link"
    link.symlink_to(victim, target_is_directory=True)
    r = _check(omp_tree)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "omp/skills/zz-link" in r.stdout
    env = {k: v for k, v in os.environ.items() if k != "SWARM_AGENTS_FILE"}
    w = subprocess.run([sys.executable, str(omp_tree / "scripts" / "build_agents.py")], cwd=omp_tree, env=env, capture_output=True, text=True)
    assert w.returncode == 1, w.stdout + w.stderr
    assert "refused" in w.stderr and "Traceback" not in w.stderr
    assert (victim / "SKILL.md").read_text(encoding="utf-8") == "keep"
    assert link.is_symlink()



# Cursor contract: the spawn grant is written as "with the Task tool" in the A01 preamble
# (.cursor/agents/a01-orchestrator.md). Specialists only mention the Task tool as forbidden
# ("must not call the Task tool"), so the affirmative literal must appear in A01 alone.
CURSOR_SPAWN_LITERAL = "spawn specialists with the Task tool"


def test_cursor_task_tool_grant_only_on_orchestrator():
    cursor_dir = ROOT / ".cursor" / "agents"
    for a in _agents():
        path = cursor_dir / f"{a['slug']}.md"
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        has = CURSOR_SPAWN_LITERAL in text
        assert has == (a["slug"] == "a01-orchestrator"), (path.name, has)


_CLEAN_SHARED_SKILL = """---
name: {name}
description: Use when a fixture needs a shared skill that passes every source rule.
owner: shared
tools: []
---

# Fixture

## When to Use

- A test needs a valid shared skill.

## Procedure

1. Read the assignment with the `read` tool.

## Outputs

- Nothing.

## Failure Handling

- Report BLOCKED.

## Handover

- Nothing to hand over.
"""


def test_reserved_driver_name_is_refused_and_never_overwrites_the_driver_skill(omp_tree):
    """R3 P2: a shared source named `swarm-orchestrate` overwrote A01's generated driver skill and then reported stale forever."""
    _empty_the_skill_corpus(omp_tree)
    assert _build(omp_tree).returncode == 0
    driver = omp_tree / "omp" / "skills" / "swarm-orchestrate" / "SKILL.md"
    before = driver.read_text(encoding="utf-8")
    shared = omp_tree / "prompts" / "skills" / "shared"
    shared.mkdir(parents=True)
    (shared / "orchestrate.md").write_text(_CLEAN_SHARED_SKILL.format(name="swarm-orchestrate"), encoding="utf-8")
    built = _build(omp_tree)
    assert built.returncode == 1, built.stdout + built.stderr
    assert "reserved" in built.stderr and "prompts/skills/shared/orchestrate.md" in built.stderr
    assert driver.read_text(encoding="utf-8") == before


def _scratch_writer(tmp_path):
    """A fresh _write_skills module whose ROOT is a scratch dir holding only agents.json (no shared baseline skills)."""
    mod = _load("_write_skills")
    document = json.loads((ROOT / "agents.json").read_text(encoding="utf-8"))
    document["shared_baseline"]["skills"] = []
    (tmp_path / "agents.json").write_text(json.dumps(document), encoding="utf-8")
    mod.ROOT = tmp_path
    return mod


def _written(tmp_path):
    return sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file() and p.name != "agents.json" and "prompts" not in p.parts)


def test_write_skills_refuses_invalid_sources_before_writing_anything(tmp_path):
    """R3 P3: an unparseable source (empty name) made `_write_skills.py` write a stray omp/skills/SKILL.md."""
    mod = _scratch_writer(tmp_path)
    broken = tmp_path / "prompts" / "skills" / "a05-backend"
    broken.mkdir(parents=True)
    (broken / "nameless.md").write_text("---\nname:\ndescription: Use when a thing.\nowner: a05-backend\ntools: []\n---\n\n# x\n", encoding="utf-8")
    assert mod.main() == 1
    assert _written(tmp_path) == []


def test_write_skills_refuses_the_reserved_driver_name(tmp_path):
    mod = _scratch_writer(tmp_path)
    shared = tmp_path / "prompts" / "skills" / "shared"
    shared.mkdir(parents=True)
    (shared / "orchestrate.md").write_text(_CLEAN_SHARED_SKILL.format(name="swarm-orchestrate"), encoding="utf-8")
    assert mod.main() == 1
    assert _written(tmp_path) == []


def test_write_skills_writes_everything_when_sources_are_valid(tmp_path):
    mod = _scratch_writer(tmp_path)
    shared = tmp_path / "prompts" / "skills" / "shared"
    shared.mkdir(parents=True)
    (shared / "fixture.md").write_text(_CLEAN_SHARED_SKILL.format(name="swarm-fixture"), encoding="utf-8")
    assert mod.main() == 0
    written = _written(tmp_path)
    assert "omp/skills/swarm-fixture/SKILL.md" in written and "omp/skills/swarm-orchestrate/SKILL.md" in written
    assert (tmp_path / "omp" / "skills" / "swarm-orchestrate" / "SKILL.md").read_text(encoding="utf-8") == mod.swarm_orchestrate_skill()
