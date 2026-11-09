"""G-1: the generated omp swarm-orchestrate skill is the renderer's output and binds the runtime constants A01 acts
on (strict-dispatch gate agents, rework cap, A01's swarm tool grants). Every expected value is derived from code."""
import json
import re

from swarm.manifest import load_manifest
from test_omp_agents import OMP_AGENTS, OMP_SKILLS, ROOT, _fm, builder, skills_mod

SKILL = OMP_SKILLS / "swarm-orchestrate" / "SKILL.md"


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Body of the `## <heading>` section, up to the next `## ` heading."""
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    assert m, heading
    return m.group(1)


def test_advisor_review_is_not_the_depth_cap():
    """The advisor model stays. Its review surface and notes do not replace the workpool-child depth cap."""
    text = _skill()
    depth = _section(text, "Depth cap & plan mode")
    assert "advisor review surface" in depth
    assert "<advisory>" in depth
    assert "not a swarm BLOCKED" in depth
    assert "If A01 cannot dispatch" in depth
    assert "Read-only discovery is not blocked" in depth
    assert "workpool child" in _section(text, "Negatives you must honour")


def test_skill_frontmatter_loadable():
    text = _skill()
    assert text == skills_mod.swarm_orchestrate_skill()
    fm = _fm(text)
    assert fm["name"] == "swarm-orchestrate"
    assert fm["description"].strip()




def test_final_yield_is_task_result():
    """F-1: A01's output schema is task.result v1, so step 8's final yield must be one (a bare swarm.status object
    is rejected), and A01's omp preamble must override its body's swarm.status <output_format> with that step.
    Required keys, field names and state values come from the schema file A01 is rendered with."""
    step = 8
    schema = json.loads((ROOT / "swarm" / "schemas" / "task.result.v1.json").read_text(encoding="utf-8"))
    a01 = next(a["slug"] for a in load_manifest() if a["id"] == "A01")
    a01_text = (OMP_AGENTS / f"{a01}.md").read_text(encoding="utf-8")
    assert json.loads(_fm(a01_text)["output"]) == schema
    required, props, states = set(schema["required"]), set(schema["properties"]), set(schema["properties"]["state"]["enum"])

    step8 = re.search(rf"^{step}\. (.*?)(?=^\d+\. |\Z)", _section(_skill(), "Procedure"), re.S | re.M)
    assert step8
    step8 = step8.group(1)
    assert re.search(r"\byield\b[^.\n]*\btask\.result\b", step8)
    assert not re.search(r"\byield\b\W+(?:(?:the|a|an|one)\s+)?`?swarm\.status\b", step8)
    assert not re.search(r"""["']?type["']?\s*:\s*["']?swarm\.status""", step8)

    fields = dict(re.findall(r"^\s+-\s+`(\w+)`([^\n]*)", step8, re.M))
    assert required <= set(fields) <= props
    assert "`correlation_id`" in fields["task_id"]
    # every task done → IN_REVIEW; otherwise BLOCKED carries `needs`, FAILED carries `error`
    named = set(re.findall(r"\b(IN_REVIEW|IN_PROGRESS|BLOCKED|FAILED)\b", fields["state"]))
    assert named == {"IN_REVIEW", "BLOCKED", "FAILED"} and named <= states
    assert re.search(r"IN_REVIEW when every task is DONE", fields["state"])
    assert {"needs", "error"} <= set(fields) and "BLOCKED" in fields["needs"] and "FAILED" in fields["error"]

    # the omp preamble (not the shared body, which keeps swarm.status for claude/grok) points the final yield at step 8
    assert re.search(r"<output_format>.*?\bswarm\.status\b.*?</output_format>", a01_text, re.S)
    runtime = re.search(r"<swarm_runtime>\n(.*?)</swarm_runtime>", a01_text, re.S)
    assert runtime and runtime.group(1) in builder.OMP_ORCH_PREAMBLE
    final = [ln for ln in runtime.group(1).splitlines() if re.search(r"\bfinal `yield`", ln)]
    assert len(final) == 1
    final = final[0]
    assert re.search(rf"\btask\.result\b.*\b{re.escape(_fm(_skill())['name'])} step {step}\b", final)
    assert required <= set(re.findall(r"`(\w+)`", final)) and "`summary_md`" in final
    assert re.search(r"swarm\.status block from <output_format> goes into `summary_md`", final)
    assert re.search(r"never yielded as-is", final)
