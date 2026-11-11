"""Blast-radius lanes: disjoint slices of one plan that run at the same time.

A parallel plan is one correlation with N short lanes, not N control planes. Each lane is one
implementer task on its own branch and worktree, so the same agent can hold several lanes at once.
No lane reviews itself. A join merges the branches, and the review gate then runs once, on the
merged tree, with Greptile as the review source.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn

from .errors import ErrorCode, SwarmError
from .manifest import by_capability, get_agent
from .taskstore import GATES_BY_RISK

_FENCE = re.compile(r"```blast-radii[^\n]*\n(.*?)```", re.DOTALL)
_SLICE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
_RESERVED = frozenset({"join", "rev", "qa", "sec", "rel"})
_LANE_AGENTS = frozenset({"A02", "A03", "A04", "A05", "A06", "A07", "A11", "A13", "A14", "A15",
                          "A16", "A17", "A18", "A19", "A20", "A21", "A22", "A23", "A24", "A25",
                          "A26", "A27", "A28", "A29", "A30"})
# One default capability per lane agent, kept in step with _LANE_AGENTS; each is in that agent's capabilities.
_DEFAULT_CAPABILITY = {
    "A02": "req.spec",
    "A03": "design.blueprint",
    "A04": "ux.spec",
    "A05": "code.backend",
    "A06": "code.frontend",
    "A07": "data.migration",
    "A11": "ci.pipeline",
    "A13": "obs.slo",
    "A14": "maint.patch",
    "A15": "docs.bundle",
    "A16": "cloud.architecture",
    "A17": "platform.golden_path",
    "A18": "supply.image",
    "A19": "event.topology",
    "A20": "finops.model",
    "A21": "cluster.capacity",
    "A22": "gitops.manifest",
    "A23": "mesh.traffic",
    "A24": "kpol.admission",
    "A25": "resilience.backup",
    "A26": "web3.contract",
    "A27": "audit.static",
    "A28": "chain.node",
    "A29": "dapp.wallet",
    "A30": "custody.policy",
}
_GATE_ROWS = {
    "review": ("rev", "gate.review", "A09", "Greptile review of the merged branch",
               {"role": "review", "review_source": "greptile", "review_scope": "merged-branch"}),
    "quality": ("qa", "gate.quality", "A08", "Quality gate on the merged branch", {"role": "gate"}),
    "security": ("sec", "gate.security", "A10", "Security gate on the merged branch", {"role": "gate"}),
    "release": ("rel", "release.plan", "A12", "Release gate for the merged branch", {"role": "gate"}),
}
_MAX_SLICES = 32


def _fail(message: str) -> NoReturn:
    raise SwarmError(ErrorCode.E_INPUT, message)


def _path(value: object, slice_id: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(f"slice {slice_id}: each blast-radius path must be a non-empty string")
    raw = value.strip().replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~"):
        _fail(f"slice {slice_id}: path must be repo-relative, got {value!r}")
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        _fail(f"slice {slice_id}: path must stay inside the repo, got {value!r}")
    return "/".join(parts)


def _overlaps(left: str, right: str) -> bool:
    if left == right:
        return True
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    return long.startswith(short + "/")


def normalize_slices(raw: object) -> list[dict[str, Any]]:
    """Validate a blast-radius array. Paths are disjoint, ids are task-id safe, agents are implementers."""
    if not isinstance(raw, list):
        _fail("blast radii must be a JSON array")
    if len(raw) < 2:
        _fail("a parallel plan needs at least two disjoint blast radii")
    if len(raw) > _MAX_SLICES:
        _fail(f"a parallel plan accepts at most {_MAX_SLICES} blast radii")
    slices: list[dict[str, Any]] = []
    seen: list[tuple[str, str]] = []
    ids: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            _fail("each blast radius must be an object")
        slice_id = item.get("id")
        if not isinstance(slice_id, str) or not _SLICE_ID.fullmatch(slice_id):
            _fail(f"blast-radius id must match {_SLICE_ID.pattern}, got {slice_id!r}")
        if slice_id in _RESERVED or slice_id in ids:
            _fail(f"blast-radius id {slice_id!r} is reserved or repeated")
        ids.add(slice_id)
        paths = item.get("paths")
        if not isinstance(paths, list) or not paths:
            _fail(f"slice {slice_id}: paths must be a non-empty array")
        cleaned, local = [], set()
        for path in paths:
            norm = _path(path, slice_id)
            if norm in local:
                _fail(f"slice {slice_id}: repeated path {norm}")
            local.add(norm)
            for owner, other in seen:
                if _overlaps(norm, other):
                    _fail(f"blast radii overlap: {slice_id}:{norm} and {owner}:{other}")
            seen.append((slice_id, norm))
            cleaned.append(norm)
        agent_ref = item.get("agent")
        capability = item.get("capability")
        if agent_ref:
            try:
                agent = get_agent(str(agent_ref))
            except KeyError as exc:
                _fail(f"slice {slice_id}: {exc}")
        elif isinstance(capability, str) and capability:
            owners = by_capability(capability)
            if not owners:
                _fail(f"slice {slice_id}: no agent offers capability {capability}")
            agent = owners[0]
        else:
            _fail(f"slice {slice_id}: set agent or capability")
        if agent["id"] not in _LANE_AGENTS:
            _fail(f"slice {slice_id}: {agent['id']} is a control or gate agent; a lane needs an implementer")
        if not isinstance(capability, str) or not capability:
            capability = _DEFAULT_CAPABILITY[agent["id"]]
        if capability not in agent["capabilities"]:
            _fail(f"slice {slice_id}: {agent['id']} does not offer {capability}")
        title = item.get("title") or f"Blast radius {slice_id}: {', '.join(cleaned)}"
        if not isinstance(title, str) or not title.strip():
            _fail(f"slice {slice_id}: title must be a non-empty string")
        acceptance = item.get("acceptance") or [f"edits stay inside {', '.join(cleaned)}"]
        if not isinstance(acceptance, list) or not all(isinstance(line, str) and line for line in acceptance):
            _fail(f"slice {slice_id}: acceptance must be an array of strings")
        slices.append({"id": slice_id, "paths": cleaned, "agent": agent["id"], "capability": capability,
                       "title": title.strip(), "acceptance": acceptance})
    return slices


def slices_from_brief(brief: str) -> object | None:
    """The single ```blast-radii``` JSON array in a brief, or None when the brief has no block."""
    found = _FENCE.findall(brief or "")
    if not found:
        return None
    if len(found) > 1:
        _fail("brief has more than one ```blast-radii``` block")
    try:
        parsed: object = json.loads(found[0])
        return parsed
    except json.JSONDecodeError as exc:
        _fail(f"blast-radii block is not JSON: {exc.msg}")
    return None


def load_slices(*, brief: str, slices_json: str | None, slices_path: str | None) -> list[dict[str, Any]] | None:
    """The one slice source. A brief block, --slices, and --slices-json together are an input error."""
    found: list[tuple[str, object]] = []
    fenced = slices_from_brief(brief)
    if fenced is not None:
        found.append(("the brief", fenced))
    if slices_json:
        try:
            found.append(("--slices-json", json.loads(slices_json)))
        except json.JSONDecodeError as exc:
            _fail(f"--slices-json is not JSON: {exc.msg}")
    if slices_path:
        try:
            found.append(("--slices", json.loads(Path(slices_path).read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError) as exc:
            _fail(f"--slices {slices_path}: {exc}")
    if not found:
        return None
    if len(found) > 1:
        names = " and ".join(name for name, _ in found)
        _fail(f"pass blast radii only once; got {names}")
    return normalize_slices(found[0][1])


def parallel_rows(slices: list[dict[str, Any]], risk: str) -> list[tuple[Any, ...]]:
    """Lanes with no per-lane review, a join, then the risk class's gates once on the merged branch.

    Slice rows leave `gates` as None so the slice keeps the risk-class gate set. The review that
    satisfies it is the single post-merge gate, not a review inside the lane. The join opts out
    (`[]`) because nothing records a verdict on the join itself.
    """
    if risk not in GATES_BY_RISK:
        _fail(f"unknown risk class {risk!r}")
    ids = [item["id"] for item in slices]
    rows: list[tuple[Any, ...]] = []
    for item in slices:
        rows.append((item["id"], item["capability"], item["agent"], item["title"], [], None, None,
                     list(item["acceptance"]), {"role": "slice", "blast_radius": list(item["paths"])}))
    rows.append(("join", "ci.pipeline", "A11", "Merge blast-radius branches onto the integration branch",
                 list(ids), [], None, ["every slice branch is merged into the integration branch"],
                 {"role": "join", "slices": list(ids)}))
    needed = GATES_BY_RISK[risk]
    predecessors: list[str] = []
    for name in needed:
        if name == "release":
            continue
        suffix, capability, agent, title, lane = _GATE_ROWS[name]
        rows.append((suffix, capability, agent, title, ["join"], {"gate": name, "for": list(ids)}, None, [], lane))
        predecessors.append(suffix)
    if "release" in needed:
        suffix, capability, agent, title, lane = _GATE_ROWS["release"]
        rows.append((suffix, capability, agent, title, predecessors or ["join"],
                     {"gate": "release", "for": list(ids)}, None, [], lane))
    return rows


def lane_notes(lane: dict[str, Any], prefix: str, suffix: str) -> dict[str, Any]:
    """Task-store notes for one parallel-plan row. Branch names are fixed at plan time."""
    role = lane.get("role")
    if role == "slice":
        return {"role": "slice", "blast_radius": list(lane["blast_radius"]),
                "branch": f"swarm/{prefix}/{suffix}", "isolate": "worktree"}
    if role == "join":
        return {"role": "join", "merge_branches": [f"swarm/{prefix}/{slice_id}" for slice_id in lane["slices"]]}
    return {key: value for key, value in lane.items() if key != "slices"}


def lane_section(notes: dict[str, Any]) -> str:
    """Assignment text for a lane, the join, or the single Greptile review. Empty for every other task."""
    role = notes.get("role")
    if role == "slice":
        paths = ", ".join(notes.get("blast_radius") or [])
        return (
            "\n## Blast radius\n"
            f"Edit only these paths: {paths}\n"
            f"Branch: {notes.get('branch')}\n"
            "You are one replica of your agent. Other replicas are running other blast radii at the same time, "
            "each in its own worktree. Stay inside this radius and this branch. Do not review this branch and "
            "do not merge it. The join merges every lane, then one Greptile review covers the merged branch.\n"
        )
    if role == "join":
        branches = "\n".join(f"- {branch}" for branch in notes.get("merge_branches") or [])
        return (
            "\n## Join\n"
            "The lane branches below merge into the integration branch. Do not review them.\n"
            f"{branches}\n"
        )
    if notes.get("review_source") == "greptile":
        return (
            "\n## Greptile review\n"
            "This is the only code review for the plan. The slice branches are already merged. Review that "
            "merged branch once with scripts/rev_greptile_ingest.py and your review gate. Do not send the "
            "lanes back for separate reviews. If the merged branch has no open pull request to ingest, "
            "finish BLOCKED and name the missing pull request; do not invent findings.\n"
        )
    return ""


def take_agent_slot(counts: dict[str, int], agent_id: str, ceiling: int) -> bool:
    """True when this round can start another replica of `agent_id`. Does not consume a slot on False.

    The ceiling is the agent's own max_parallel. Several replicas are the normal case; only the
    ceiling, and the round's global cap, hold a further replica back.
    """
    if counts.get(agent_id, 0) >= ceiling:
        return False
    counts[agent_id] = counts.get(agent_id, 0) + 1
    return True


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def prepare_slice_worktree(repo: Path, dest: Path, branch: str, *,
                           sleep: Callable[[float], None] = time.sleep) -> Path:
    """A worktree at `dest` on `branch`, created from HEAD when the branch is new.

    Parallel lanes call this together. Git locks the main checkout, so a lock failure is retried; `sleep` is injectable (as `acp.Bus`).
    """
    if not branch or branch.startswith("-") or ".." in branch.split("/"):
        raise SwarmError(ErrorCode.E_CONTRACT, f"refusing blast-radius branch {branch!r}")
    git_dir = repo / ".git"
    if not git_dir.exists():
        raise SwarmError(ErrorCode.E_DEP, f"{repo} is not a git checkout; blast-radius lanes need worktrees")
    dest = dest.resolve()
    if (dest / ".git").exists():
        head = _git(dest, "symbolic-ref", "--quiet", "HEAD")
        current = head.stdout.strip().removeprefix("refs/heads/") if head.returncode == 0 else ""
        if current != branch:
            raise SwarmError(ErrorCode.E_CONTRACT,
                             f"worktree {dest} is on {current or 'no branch'}, not {branch}; refusing to reuse it")
        return dest
    if dest.exists():
        raise SwarmError(ErrorCode.E_CONTRACT, f"worktree path {dest} exists and is not a worktree")
    dest.parent.mkdir(parents=True, exist_ok=True)
    exists = _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    cmd = ["worktree", "add", str(dest), branch] if exists.returncode == 0 else [
        "worktree", "add", "-b", branch, str(dest), "HEAD"]
    last = ""
    for attempt in range(5):
        proc = _git(repo, *cmd)
        if proc.returncode == 0:
            return dest
        last = (proc.stderr or proc.stdout or "worktree add failed").strip()
        if "lock" not in last.lower() or attempt == 4:
            break
        sleep(0.05 * (attempt + 1))
    raise SwarmError(ErrorCode.E_DEP, f"worktree for {branch} failed: {last[-400:]}")


def merge_branches(repo: Path, branches: list[str]) -> None:
    """Merge each slice branch into `repo`. A conflict aborts the merge and leaves the checkout clean."""
    if not branches:
        raise SwarmError(ErrorCode.E_CONTRACT, "join has no slice branches")
    for branch in branches:
        if not branch or branch.startswith("-") or ".." in branch.split("/"):
            raise SwarmError(ErrorCode.E_CONTRACT, f"refusing to merge {branch!r}")
        proc = _git(repo, "-c", "user.name=agent-swarm", "-c", "user.email=swarm@localhost",
                    "merge", "--no-edit", branch)
        if proc.returncode != 0:
            _git(repo, "merge", "--abort")
            detail = (proc.stderr or proc.stdout or "merge failed").strip()
            raise SwarmError(ErrorCode.E_CONTRACT, f"cannot merge {branch}: {detail[-400:]}")


def join_result_text(repo: Path, task: dict[str, Any]) -> str:
    """Merge the lane branches, then the same IN_REVIEW payload a finished implementer returns."""
    branches = list(task["notes_json"].get("merge_branches") or [])
    merge_branches(repo, branches)
    payload = {"task_id": task["task_id"], "state": "IN_REVIEW",
               "outputs": [{"kind": task["capability"], "uri": f"git://{task['task_id']}", "version": "1", "digest": ""}],
               "metrics": {}, "summary_md": "merged " + ", ".join(branches)}
    return f"join\n```json\n{json.dumps(payload)}\n```"
