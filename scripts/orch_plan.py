#!/usr/bin/env python3
"""A01 — decompose a project brief into a typed task DAG in the Task Store.

Patterns encode the reference event flows of 04-integration-plan.md §3:
  feature     brief → REQ → (ARCH ∥ UXD) → DATA → (BE ∥ FE) → (QA ∥ REV ∥ SEC gates) → DEVOPS → REL → (OBS ∥ DOC)
  hotfix      incident → MAINT rca → BE patch → gates → REL → OBS
  dependency  SEC scan → MAINT patch.task → BE bumps → gates → REL
  cloud-native  brief → REQ → ARCH → cloud architecture → (event topology → BE ∥ golden path ∥ cost model) → image
              evidence → (QA ∥ REV ∥ SEC gates) → DEVOPS → REL → (OBS ∥ DOC); gates follow the plan risk class
  cluster     capacity review → (traffic ∥ admission policy) → GitOps manifests → (mesh and policy scans of the
              manifests ∥ resilience) → (QA ∥ REV ∥ SEC gates) → rollout plan → REL → (OBS ∥ runbook); the manifest
              row is risk high, so all four gates apply
  web3        REQ → ARCH → custody policy draft → smart contracts → (dapp ∥ audit ∥ chain infra) → custody compliance
              after the audit → (QA ∥ REV ∥ SEC gates) → DEVOPS → REL → (OBS ∥ DOC); contract and dapp rows are risk
              high, mainnet stays human-approved
  parallel    one short lane per disjoint blast radius (same agent allowed more than once, each on its own
              branch and worktree) → join merges the branches → one Greptile review of the merged branch,
              plus the rest of the risk-class gates once. Slices come from a ```blast-radii``` block, --slices,
              or --slices-json. A fence or either flag selects this pattern even when --pattern is feature.
  custom      --plan plan.json  { "tasks": [ {id, capability, agent, title, depends_on[], gates[], risk_class?} ] }

Writes a plan snapshot to .swarm/plans/<correlation_id>.json and emits plan.updated.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.script_base import AgentScript, Ctx, check_task_id  # noqa: E402
from swarm.taskstore import TaskStore, GATES_BY_RISK  # noqa: E402
from swarm.manifest import by_capability, get_agent  # noqa: E402
from swarm.paths import swarm_dir  # noqa: E402
from swarm.errors import SwarmError, ErrorCode  # noqa: E402
from swarm import substrate_client, substrate_tee  # noqa: E402
from swarm.blast_radius import lane_notes, load_slices, parallel_rows  # noqa: E402
from swarm.envelope import build_envelope, sign_envelope, verify_envelope, real_key_configured, signing_config_error  # noqa: E402

_LOG = logging.getLogger("swarm.orch_plan")

# (suffix, capability, agent, title, depends_on suffixes, gate spec), or the 8-field row
# (..., gate spec, risk_class, acceptance) when a row pins its own risk class.
# gate spec: None = derive from risk class; [] = no gates; {"gate": "quality", "for": [...]} = this IS a gate task
PATTERNS: dict[str, list[tuple]] = {
    "feature": [
        ("req", "req.spec", "A02", "Requirements spec + acceptance criteria", [], []),
        ("arch", "design.blueprint", "A03", "Architecture blueprint, API contract, ADRs", ["req"], []),
        ("ux", "ux.spec", "A04", "Design tokens + UX spec + a11y requirements", ["req"], []),
        ("data", "data.migration", "A07", "Data model, migrations, data contract", ["arch"], None),
        ("be", "code.backend", "A05", "Backend implementation against contracts", ["arch", "data"], None),
        ("fe", "code.frontend", "A06", "Frontend implementation against tokens/contracts", ["arch", "ux"], None),
        ("qa", "gate.quality", "A08", "Quality gate: tests + verdict", ["be", "fe", "data"], {"gate": "quality", "for": ["be", "fe", "data"]}),
        ("rev", "gate.review", "A09", "Review gate: code review verdict", ["be", "fe", "data"], {"gate": "review", "for": ["be", "fe", "data"]}),
        ("sec", "gate.security", "A10", "Security gate: SAST/secrets/deps verdict", ["be", "fe", "data"], {"gate": "security", "for": ["be", "fe", "data"]}),
        ("build", "build.artifact", "A11", "CI validation + build artifact record", ["qa", "rev", "sec"], []),
        ("rel", "release.plan", "A12", "Release gate + canary plan", ["build"], {"gate": "release", "for": ["be", "fe", "data"]}),
        ("obs", "obs.slo", "A13", "SLOs, alerts, deploy telemetry guardrails", ["rel"], []),
        ("docs", "docs.bundle", "A15", "Docs bundle, API reference, runbook, changelog", ["rel"], []),
    ],
    "hotfix": [
        ("rca", "maint.rca", "A14", "Root-cause analysis + minimal patch task", [], []),
        ("patch", "code.patch", "A05", "Minimal-diff hotfix", ["rca"], None),
        ("qa", "gate.quality", "A08", "Quality gate (priority)", ["patch"], {"gate": "quality", "for": ["patch"]}),
        ("rev", "gate.review", "A09", "Review gate (priority)", ["patch"], {"gate": "review", "for": ["patch"]}),
        ("sec", "gate.security", "A10", "Security gate (priority)", ["patch"], {"gate": "security", "for": ["patch"]}),
        ("rel", "release.promote", "A12", "Guarded promote + unfreeze", ["qa", "rev", "sec"], {"gate": "release", "for": ["patch"]}),
        ("obs", "obs.incident", "A13", "Confirm stabilization, close incident", ["rel"], []),
        ("retro", "maint.debt", "A14", "Retro → memory + debt register", ["obs"], []),
    ],
    "dependency": [
        ("scan", "sec.deps", "A10", "Dependency/CVE scan", [], []),
        ("patch", "maint.deps", "A14", "Patch tasks (24h deadline for KEV)", ["scan"], []),
        ("bump", "code.patch", "A05", "Dependency bumps", ["patch"], None),
        ("qa", "gate.quality", "A08", "Quality gate", ["bump"], {"gate": "quality", "for": ["bump"]}),
        ("rev", "gate.review", "A09", "Review gate", ["bump"], {"gate": "review", "for": ["bump"]}),
        ("sec", "gate.security", "A10", "Security gate", ["bump"], {"gate": "security", "for": ["bump"]}),
        ("rel", "release.plan", "A12", "Scheduled canary promotion", ["qa", "rev", "sec"], {"gate": "release", "for": ["bump"]}),
        ("obs", "obs.slo", "A13", "Regression watch", ["rel"], []),
    ],
    "cloud-native": [
        ("req", "req.spec", "A02", "Requirements spec + acceptance criteria", [], []),
        ("arch", "design.blueprint", "A03", "Architecture blueprint, API contract, ADRs", ["req"], []),
        ("cloud", "cloud.architecture", "A16", "Cloud-native architecture: landing zone, resilience, residency", ["arch"], []),
        ("plat", "platform.golden_path", "A17", "Golden path, catalog entry and platform claims", ["cloud"], []),
        ("events", "event.topology", "A19", "Event topology, schemas and serverless config", ["arch", "cloud", "plat"], None),
        ("be", "code.backend", "A05", "Backend implementation against contracts and the cloud architecture",
         ["arch", "cloud", "events", "plat"], None),
        ("cost", "finops.model", "A20", "Cost model, budget and carbon estimate", ["cloud"], []),
        ("img", "supply.image", "A18", "Pre-build image policy: Dockerfile lint and base-image pins", ["be"], []),
        ("qa", "gate.quality", "A08", "Quality gate: tests + verdict", ["be", "events"], {"gate": "quality", "for": ["be", "events"]}),
        ("rev", "gate.review", "A09", "Review gate: code review verdict", ["be", "events"], {"gate": "review", "for": ["be", "events"]}),
        ("sec", "gate.security", "A10", "Security gate: SAST/secrets/deps verdict", ["be", "events", "img"],
         {"gate": "security", "for": ["be", "events"]}),
        ("build", "build.artifact", "A11", "CI validation + build artifact record", ["qa", "rev", "sec"], []),
        ("prov", "supply.image", "A18", "Post-build evidence: SBOM, provenance and signature for the build artifact", ["build"], []),
        ("rel", "release.plan", "A12", "Release gate + canary plan", ["build", "prov", "plat", "cost"], {"gate": "release", "for": ["be", "events"]}),
        ("obs", "obs.slo", "A13", "SLOs, alerts, deploy telemetry guardrails", ["rel"], []),
        ("docs", "docs.bundle", "A15", "Docs bundle, API reference, runbook, changelog", ["rel"], []),
    ],
    "cluster": [
        ("cap", "cluster.capacity", "A21", "Cluster capacity, version-skew and deprecated-API review", [], []),
        ("net", "mesh.traffic", "A23", "Traffic, ingress and NetworkPolicy design", ["cap"], []),
        ("pol", "kpol.admission", "A24", "Pod Security, RBAC and admission policy set", ["cap"], []),
        ("gitops", "gitops.manifest", "A22", "Manifests, overlays and sync policy for the workload", ["cap", "net", "pol"], None, "high", []),
        ("netchk", "mesh.netpol", "A23", "NetworkPolicy and mesh coverage scan of the rendered manifests", ["gitops"], []),
        ("polchk", "kpol.admission", "A24", "Pod Security, RBAC and admission scan of the rendered manifests", ["gitops"], []),
        ("res", "resilience.backup", "A25", "PDB, spread, probes, backup and DR evidence", ["gitops"], []),
        ("qa", "gate.quality", "A08", "Quality gate: tests + verdict", ["gitops"], {"gate": "quality", "for": ["gitops"]}),
        ("rev", "gate.review", "A09", "Review gate: manifest and policy review", ["gitops"], {"gate": "review", "for": ["gitops"]}),
        ("sec", "gate.security", "A10", "Security gate: manifest, policy and supply-chain verdict", ["gitops", "polchk"],
         {"gate": "security", "for": ["gitops"]}),
        ("roll", "gitops.rollout", "A22", "Progressive-delivery and promotion plan", ["qa", "rev", "sec", "res", "netchk"], []),
        ("rel", "release.plan", "A12", "Release gate + canary plan", ["roll"], {"gate": "release", "for": ["gitops"]}),
        ("obs", "obs.slo", "A13", "SLOs, alerts, deploy telemetry guardrails", ["rel"], []),
        ("docs", "docs.runbook", "A15", "Runbook and changelog", ["rel"], []),
    ],
    "web3": [
        ("req", "req.spec", "A02", "Requirements spec + acceptance criteria", [], []),
        ("arch", "design.blueprint", "A03", "Protocol architecture, interfaces, ADRs", ["req"], []),
        ("cust", "custody.policy", "A30", "Signer, multisig, timelock and key-ceremony policy draft, before the audit", ["arch"], []),
        ("sol", "web3.contract", "A26", "Smart contracts, tests, gas report and deployment plan", ["arch", "cust"], None, "high", []),
        ("chain", "chain.node", "A28", "Network guard, RPC policy and indexer plan", ["sol", "cust"], []),
        ("dapp", "dapp.wallet", "A29", "Wallet and chain integration against the ABI", ["arch", "sol", "chain"], None, "high", []),
        ("aud", "audit.static", "A27", "Contract audit findings and invariant suite", ["sol"], []),
        ("comp", "custody.compliance", "A30", "Post-audit custody evidence: privileged roles, timelock band, compliance pack", ["cust", "aud"], []),
        ("qa", "gate.quality", "A08", "Quality gate: tests + verdict", ["sol", "dapp", "aud"], {"gate": "quality", "for": ["sol", "dapp"]}),
        ("rev", "gate.review", "A09", "Review gate: code review verdict", ["sol", "dapp"], {"gate": "review", "for": ["sol", "dapp"]}),
        ("sec", "gate.security", "A10", "Security gate: includes the audit findings", ["sol", "dapp", "aud"],
         {"gate": "security", "for": ["sol", "dapp"]}),
        ("build", "build.artifact", "A11", "CI validation + build artifact record", ["qa", "rev", "sec"], []),
        ("rel", "release.plan", "A12", "Release gate; mainnet deployment stays human-approved", ["build", "chain", "comp"],
         {"gate": "release", "for": ["sol", "dapp"]}),
        ("obs", "obs.slo", "A13", "SLOs, alerts, chain and indexer monitoring guardrails", ["rel"], []),
        ("docs", "docs.bundle", "A15", "Docs bundle, API reference, runbook, changelog", ["rel"], []),
    ],
}


def _budget(risk: str) -> dict:
    return {"low": {"max_tokens": 150_000, "max_wall_s": 900, "max_cost_usd": 3},
            "medium": {"max_tokens": 300_000, "max_wall_s": 1800, "max_cost_usd": 8},
            "high": {"max_tokens": 600_000, "max_wall_s": 3600, "max_cost_usd": 20}}[risk]


def load_custom(path: Path) -> list[tuple]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for t in data["tasks"]:
        if re.search(r"\.r\d+$", str(t["id"])):  # rework reruns take <task>.rN; a planned task must not claim that name
            raise SwarmError(ErrorCode.E_INPUT, f"task id {t['id']!r} ends in .rN, which rework reruns use; rename the task")
        agent = t.get("agent")
        if not agent:
            cands = by_capability(t["capability"])
            if not cands:
                raise SwarmError(ErrorCode.E_INPUT, f"no agent offers capability {t['capability']}")
            agent = cands[0]["id"]
        else:
            try:
                owner = get_agent(str(agent))
            except KeyError as exc:
                raise SwarmError(ErrorCode.E_INPUT, f"task {t['id']}: {exc.args[0]}") from exc
            if t["capability"] not in owner["capabilities"]:
                raise SwarmError(ErrorCode.E_INPUT, f"task {t['id']}: {owner['id']} does not offer {t['capability']}")
            agent = owner["id"]
        gates = t.get("gates")  # None → derived; [] → none; {"gate":..,"for":[..]} → gate task
        rows.append((t["id"], t["capability"], agent, t.get("title", t["id"]), t.get("depends_on", []), gates,
                     t.get("risk_class"), t.get("acceptance", [])))
    return rows


def _as_row(row: tuple) -> tuple:
    """Pad a pattern or custom row to 9 fields. The last field is the blast-radius lane, or None."""
    row = tuple(row)
    if len(row) == 6:
        return row + (None, [], None)
    if len(row) == 8:
        return row + (None,)
    if len(row) != 9:
        raise SwarmError(ErrorCode.E_INPUT, f"plan row has {len(row)} fields, expected 6, 8 or 9")
    return row


def _plan_sha_rows(rows: list[tuple]) -> list[list]:
    """Rows as hashed by older plans: eight fields, plus the lane only when this plan has one.

    A feature/hotfix/dependency/custom row therefore keeps the sha it had before lanes existed,
    so re-planning that same brief still reuses.
    """
    hashed = []
    for row in rows:
        base = list(row[:8])
        if row[8]:
            base.append(row[8])
        hashed.append(base)
    return hashed


def _derived_prefix(corr: str, n: int) -> str:
    return "T" + corr.replace("-", "")[:n]


def _existing(store: TaskStore, prefix: str, rows: list[tuple]) -> list[dict]:
    found = []
    for r in rows:
        try:
            found.append(store.get(f"{prefix}-{r[0]}"))
        except SwarmError:
            continue
    return found


def _reusable(existing, rows, pattern, brief, brief_sha, plan_sha, want_corr, args) -> str | None:
    """Correlation id to reuse when the prefix already holds this exact plan, else None."""
    owners = {t["correlation_id"] for t in existing}
    if len(existing) != len(rows) or len(owners) != 1:
        return None
    corr = owners.pop()
    if want_corr and want_corr != corr:
        return None
    for t, r in zip(existing, rows):  # _existing keeps row order, and every row is present here
        n = t["notes_json"]
        same_brief = n.get("brief_sha256") == brief_sha if "brief_sha256" in n else n.get("brief_excerpt") == brief[:2000]
        # Legacy rows without plan_sha256: pattern rows are fixed by the pattern; custom rows fail closed.
        same_plan = n["plan_sha256"] == plan_sha if "plan_sha256" in n else pattern != "custom"
        if n.get("pattern") != pattern or not same_brief or not same_plan:
            return None
        # WR-13: the per-task inputs this run would create (risk class decides the required gates)
        if (t["risk_class"], t["priority"], t["acceptance"]) != (r[6] or args.risk_class, args.priority, r[7] or args.acceptance):
            return None
    return corr


def _tasks_from_store(store: TaskStore, corr: str) -> list:
    return [{k: t[k] for k in ("task_id", "capability", "agent_id", "title", "depends_on", "risk_class", "state", "dag_depth",
                              "inputs", "acceptance", "budget", "priority")}
            | {"gates": store.required_gates(t["task_id"]), "notes": t["notes_json"]}
            for t in store.list(correlation_id=corr)]


def _read_plan_tasks(plan_file: Path) -> list | None:
    """Tasks from a finished snapshot. None when the file is missing or not valid JSON yet."""
    try:
        raw = plan_file.read_text(encoding="utf-8")
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        return None
    tasks = body.get("tasks") if isinstance(body, dict) else None
    return tasks if isinstance(tasks, list) else None


def _write_text_atomic(path: Path, text: str) -> None:
    """Publish text by rename so a concurrent reader never observes a truncated file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _reuse_result(store: TaskStore, ctx, corr: str, pattern: str) -> dict:
    # corr comes from the Task Store here, not argv: re-check it before it names a file (WR-05)
    plan_file = swarm_dir(ctx.root, create=True) / "plans" / f"{check_task_id(corr, 'correlation id')}.json"
    recorded = str(plan_file) if _read_plan_tasks(plan_file) is not None else None
    tasks = _tasks_from_store(store, corr)
    ctx.correlation_id = corr
    return _execution_result(store, ctx, {
        "status": "ok", "correlation_id": corr, "pattern": pattern, "reused": True, "tasks": tasks,
        "plan_file": recorded,
        "summary": f"reused existing plan for correlation {corr} ({len(tasks)} tasks, no changes)"})


_JS_SAFE_INT = 2 ** 53 - 1


def _json_safe(value):
    """The one number form signer and verifier share. JavaScript has a single number type, so the TS bridge turns
    100.0 into 100 on the way back; a signature over 100.0 would then cover different bytes. Whole floats therefore
    sign as ints. A number JSON cannot round-trip exactly fails closed rather than being signed."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or (value.is_integer() and abs(value) > _JS_SAFE_INT):
            raise SwarmError(ErrorCode.E_CONTRACT, "controller assignment carries a number JSON cannot round-trip")
        return int(value) if value.is_integer() else value
    if isinstance(value, int):
        if abs(value) > _JS_SAFE_INT:
            raise SwarmError(ErrorCode.E_CONTRACT, "controller assignment carries a number JSON cannot round-trip")
        return value
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    raise SwarmError(ErrorCode.E_CONTRACT, "controller assignment carries a value that is not JSON")


def _execution_result(store: TaskStore, ctx, result: dict) -> dict:
    """Authenticate a controller kickoff from persisted tasks, without claiming any work."""
    if any(os.environ.get(name) == "1" for name in ("SWARM_AGENT_SESSION", "SWARM_CHILD")):
        return result
    error = signing_config_error()
    if error:
        raise SwarmError(ErrorCode.E_POLICY, error)
    if not real_key_configured():
        return result
    corr = result["correlation_id"]
    tasks = _tasks_from_store(store, corr)
    if not tasks:
        raise SwarmError(ErrorCode.E_CONTRACT, "controller assignment requires a persisted plan")
    result["tasks"] = tasks
    # All authority-bearing fields come from the Task Store, never an offered envelope.
    pattern = tasks[0]["notes"]["pattern"]
    result["pattern"] = pattern
    plan = {}
    if result.get("plan_file"):
        try:
            plan = json.loads(Path(result["plan_file"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise SwarmError(ErrorCode.E_CONTRACT, "controller assignment requires a readable persisted plan") from None
        if not isinstance(plan, dict) or plan.get("correlation_id") != corr:
            raise SwarmError(ErrorCode.E_CONTRACT, "persisted controller plan correlation mismatch")
    plan = {**plan, "correlation_id": corr, "pattern": pattern, "tasks": tasks}
    risk = max((t["risk_class"] for t in tasks), key=("low", "medium", "high").index)
    acceptance = list(dict.fromkeys(criterion for t in tasks for criterion in t["acceptance"]))
    budget = {name: sum(t["budget"].get(name, 0) for t in tasks)
              for name in ("max_tokens", "max_wall_s", "max_cost_usd")}
    payload = {"task_id": corr, "correlation_id": corr, "agent_id": "A01", "capability": "plan.execute",
               "ready_tasks": [t["task_id"] for t in tasks
                               if t["state"] in ("CREATED", "VALIDATED", "PLANNED", "RETRY")
                               and store.deps_satisfied(t, tasks)], "inputs": [plan],
               "acceptance": acceptance, "budget": budget, "risk_class": risk}
    assignment = build_envelope(source="A01@controller", target="A01", msg_type="task.assign",
                                correlation_id=corr, payload=_json_safe(payload), risk_class=risk)
    sign_envelope(assignment, root=ctx.root)
    if not real_key_configured() or not verify_envelope(assignment):
        raise SwarmError(ErrorCode.E_POLICY, "fail-closed: controller assignment signature invalid")
    result["execution_assignment"] = assignment
    result["execution_assignment_verified"] = True
    return result


def _bind_graph(args, ctx, corr: str, brief: str, tasks: list[dict]) -> None:
    """ADR 0001 S1: bind this correlation to one Graph ID and register the node set. Fail-open, off without SUBSTRATE_URL.

    The Graph ID never enters the stored notes or the brief hash, so re-planning the same brief still reuses.
    """
    if not substrate_client.enabled():
        return
    try:
        offered = substrate_tee.resolve_graph_id(corr, explicit=args.graph_id, brief_text=brief)
        graph_id = substrate_tee.bind_graph(corr, offered, root=ctx.root)  # the server's winner, even on conflict
        if graph_id:
            substrate_client.mcp_call("graph_register", {
                "graph_id": graph_id, "repo": substrate_tee.repo_slug(ctx.root), "surface": substrate_tee.ORCH_SURFACE,
                "status": "planned",
                "nodes": [{"node_id": t["task_id"]} for t in tasks]}, surface=substrate_tee.ORCH_SURFACE)
    except Exception:  # noqa: BLE001 - substrate never changes the plan's outcome
        pass


def run(args, ctx) -> dict:
    if args.graph_id is not None and not substrate_tee.is_graph_id(args.graph_id):
        raise SwarmError(ErrorCode.E_INPUT, f"invalid --graph-id {args.graph_id!r}: expected ut-<base36>-<8 hex>")
    brief = ""
    if args.brief and Path(args.brief).exists():
        brief = Path(args.brief).read_text(encoding="utf-8")
    elif args.brief_text:
        brief = args.brief_text
    if not brief and not args.plan:
        if not ctx.dry_run:
            raise SwarmError(ErrorCode.E_INPUT, "provide --brief FILE, --brief-text, or --plan plan.json")
        brief = "(dry-run placeholder brief)"

    corr = ctx.correlation_id or str(uuid.uuid4())
    if args.plan and (args.slices or args.slices_json or "```blast-radii" in brief):
        raise SwarmError(ErrorCode.E_INPUT, "--plan is a custom DAG; it cannot be combined with blast-radius slices")
    if args.plan:
        args.pattern = "custom"
        rows = [_as_row(row) for row in load_custom(Path(args.plan))]
    else:
        slices = load_slices(brief=brief, slices_json=args.slices_json, slices_path=args.slices)
        if slices is not None:
            args.pattern = "parallel"
            rows = parallel_rows(slices, args.risk_class)
        elif args.pattern == "parallel":
            raise SwarmError(ErrorCode.E_INPUT, "pattern parallel needs a ```blast-radii``` block, --slices, or --slices-json")
        else:
            rows = [_as_row(row) for row in PATTERNS[args.pattern]]
    lanes = sum(1 for row in rows if row[8] and row[8].get("role") == "slice")
    brief_sha = hashlib.sha256(brief.encode("utf-8")).hexdigest()
    plan_sha = hashlib.sha256(json.dumps(_plan_sha_rows(rows), sort_keys=True).encode("utf-8")).hexdigest()
    if ctx.dry_run:
        prefix = args.prefix or _derived_prefix(corr, 4)
        lead = f"parallel blast radii: {lanes} lanes, Greptile review after merge; " if lanes else ""
        return {"status": "ok", "correlation_id": corr, "pattern": args.pattern, "dry_run": True,
                "tasks": [{"task_id": f"{prefix}-{r[0]}", "capability": r[1], "agent": r[2], "depends_on": r[4]} for r in rows],
                "summary": f"dry-run: {lead}would create {len(rows)} tasks"}

    if not any(os.environ.get(name) == "1" for name in ("SWARM_AGENT_SESSION", "SWARM_CHILD")):
        error = signing_config_error()
        if error:
            raise SwarmError(ErrorCode.E_POLICY, error)

    store = TaskStore(root=ctx.root)
    created = []
    depth = {}
    prefix = args.prefix
    try:
        with store.transaction():
            if prefix is None:
                prefix = _derived_prefix(corr, 4)
                # D-05/D-06: the 4-hex prefix is this correlation's own plan when it already holds those
                # tasks, so reuse or reject it below; fall back to 6 hex only for another correlation's tasks.
                if (ex4 := _existing(store, prefix, rows)) and corr not in {t["correlation_id"] for t in ex4}:
                    prefix = _derived_prefix(corr, 6)
            existing = _existing(store, prefix, rows)
            if existing:
                reused = _reusable(existing, rows, args.pattern, brief, brief_sha, plan_sha, ctx.correlation_id, args)
                if reused is None:
                    owners = sorted({t["correlation_id"] for t in existing})
                    hint = "pass a new --prefix" if args.prefix else "pass a new --correlation-id or --prefix for this brief"
                    raise SwarmError(ErrorCode.E_CONTRACT,
                                     f"prefix {prefix} already used by correlation {','.join(owners)} with a different "
                                     f"brief/pattern/plan/risk-class/priority/acceptance; {hint}")
            else:
                reused = None
                for suffix, cap, agent, title, deps, gates, risk_override, acceptance, lane in rows:
                    tid = f"{prefix}-{suffix}"
                    risk = risk_override or args.risk_class
                    depth[suffix] = 1 + max((depth[d] for d in deps), default=-1)
                    notes = {"pattern": args.pattern, "brief_excerpt": brief[:2000], "brief_sha256": brief_sha,
                             "plan_sha256": plan_sha}
                    if isinstance(gates, dict):
                        notes["gate"] = gates["gate"]
                        notes["gate_for"] = [f"{prefix}-{s}" for s in gates["for"]]
                        notes["gates"] = []
                    elif gates is not None:
                        notes["gates"] = gates
                    if lane:
                        notes.update(lane_notes(lane, prefix, suffix))
                    store.create(task_id=tid, correlation_id=corr, capability=cap, title=title, agent_id=agent,
                                 dag_depth=depth[suffix], depends_on=[f"{prefix}-{d}" for d in deps],
                                 acceptance=acceptance or args.acceptance, budget=_budget(risk), risk_class=risk,
                                 priority=args.priority, notes=notes)
                    store.transition(tid, "VALIDATED", reason="brief validated by A01")
                    store.transition(tid, "PLANNED", reason=f"pattern={args.pattern}")
                    created.append(store.get(tid))
    except sqlite3.IntegrityError as e:
        raise SwarmError(ErrorCode.E_CONTRACT, f"prefix {prefix} already in use; pass a new --prefix") from e

    if reused is not None:
        return _reuse_result(store, ctx, reused, args.pattern)

    plan = {"correlation_id": corr, "pattern": args.pattern, "risk_class": args.risk_class, "brief": brief,
            "tasks": _tasks_from_store(store, corr)}
    sdir = swarm_dir(ctx.root, create=True)
    plans = sdir / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(plans / f"{corr}.json", json.dumps(plan, indent=2))
    (sdir / "latest_correlation").write_text(corr)
    ctx.correlation_id = corr
    _bind_graph(args, ctx, corr, brief, plan["tasks"])  # before plan.updated, so that event and the exit record are tee'd
    ctx.emit("plan.updated", {"pattern": args.pattern, "task_count": len(created)})
    lines = [f"{t['task_id']:<14} {t['agent_id']:<4} d={t['dag_depth']} ← {','.join(t['depends_on']) or '-'}" for t in created]
    lead = f"parallel blast radii: {lanes} lanes, Greptile review after merge\n" if lanes else ""
    return _execution_result(store, ctx, {
        "status": "ok", "correlation_id": corr, "pattern": args.pattern, "tasks": plan["tasks"],
        "plan_file": str(plans / f"{corr}.json"),
        "summary": lead + f"created {len(created)} tasks for correlation {corr}\n" + "\n".join(lines)})


def add_args(p):
    p.add_argument("--repo", dest="root", default=argparse.SUPPRESS,
                   help="alias of --root (same flag as swarm_run.py --repo)")
    p.add_argument("--brief", help="path to brief markdown")
    p.add_argument("--brief-text", help="inline brief text")
    p.add_argument("--pattern", choices=list(PATTERNS) + ["custom", "parallel"], default="feature",
                   help="feature | hotfix | dependency | cloud-native (cloud landing zone, events, image, FinOps) | "
                        "cluster (Kubernetes capacity, policy, GitOps) | web3 (smart contracts, audit, custody); "
                        "parallel and custom are chosen by --slices/--plan")
    p.add_argument("--slices", help="JSON file of disjoint blast radii; selects the parallel plan")
    p.add_argument("--slices-json", help="JSON array of disjoint blast radii; selects the parallel plan")
    p.add_argument("--plan", help="custom DAG json (implies --pattern custom)")
    p.add_argument("--risk-class", choices=list(GATES_BY_RISK), default="medium")
    p.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], default="P2")
    p.add_argument("--prefix", default=None, help="task id prefix (default: T + first 4 hex of the correlation id)")
    p.add_argument("--acceptance", action="append", default=[], help="acceptance criterion (repeatable)")
    p.add_argument("--graph-id", default=None, help="substrate Graph ID to bind this correlation to (ut-<base36>-<8 hex>); "
                                                    "default: env SUBSTRATE_GRAPH_ID, a Graph ID in the brief, else minted")


# Engine selector. The default row stays Python. `GSD_ENGINE=rust` hands the call to the host plan route (`write_route::plan`),
# which decides the grammar it serves; an argv outside the flags below stays this body. `ENGINE_ROUTES` is the registry of routes
# this script registers and stays empty: the plan route is the one route, wired in this block.
_PLAN_NO_VALUE = frozenset({"--json", "--dry-run"})
_PLAN_VALUE = frozenset({"--root", "--repo", "--brief", "--brief-text", "--pattern", "--slices", "--slices-json", "--plan",
                         "--risk-class", "--priority", "--prefix", "--acceptance", "--graph-id", "--correlation-id", "--task-id"})
_PLAN_EVENTS = frozenset({"plan.updated"})


def _plan_options(argv: list[str]) -> list[tuple[str, str | None]] | None:
    """The (name, value) pairs of an argv made only of this script's long flags, or None when a token is anything else.

    A scalar option given twice is None too: argparse takes the last one, the host grammar serves each scalar once, so the
    binary is not started for it. `--repo` is `--root` (both are the same destination)."""
    options: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    index = 0
    while index < len(argv):
        name, equals, value = argv[index].partition("=")
        if name in _PLAN_NO_VALUE and not equals:
            options.append((name, None))
            index += 1
            continue
        if name not in _PLAN_VALUE:
            return None
        if equals:
            index += 1
        elif index + 1 < len(argv):
            value = argv[index + 1]
            index += 2
        else:
            return None
        key = "--root" if name == "--repo" else name
        if key != "--acceptance":
            if key in seen:
                return None
            seen.add(key)
        options.append((name, value))
    return options


def _claims_plan(argv: list[str]) -> bool:
    return _plan_options(argv) is not None


def _plan_root(argv: list[str]) -> Path:
    """The repository root the call names: the last `--root` or `--repo`, else the working directory (`Path(args.root).resolve()`)."""
    root = "."
    for name, value in _plan_options(argv) or []:
        if name in ("--root", "--repo") and value is not None:
            root = value
    return Path(root).resolve()


def _binary_env(route: str) -> dict[str, str]:
    from swarm.engine_select import BINARY_ENV_NAMES
    if route != "plan":
        raise KeyError(route)
    names = set(BINARY_ENV_NAMES) | {"SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY",
                                     "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION", "SWARM_CHILD", "SWARM_AGENT",
                                     "SUBSTRATE_URL", "SUBSTRATE_DISABLED", "XDG_CONFIG_HOME"}
    return {key: value for key, value in os.environ.items() if key in names}


def _plan_events(body: object) -> list[tuple[str, dict[str, Any]]] | None:
    """The events of a plan manifest, or None when the file is not the shape the host route writes."""
    if not isinstance(body, dict) or not isinstance(body.get("effects"), list) or not isinstance(body.get("live_refresh"), bool):
        return None
    correlation = body.get("correlation_id")
    events = []
    for item in body["effects"]:
        if not (isinstance(item, dict) and item.get("type") in _PLAN_EVENTS and isinstance(item.get("payload"), dict)
                and item.get("task_id") is None):
            return None
        events.append((item["type"], item["payload"]))
    if events and not isinstance(correlation, str):
        return None
    return events


def _plan_effects_lost(call: list[str], why: str) -> None:
    """A missing or unreadable manifest is a lost event: say so on the ledger and on stderr. The plan is committed; it is not run again."""
    _LOG.warning("orch_plan: effects lost: %s", why)
    try:
        Ctx("A01", "orch_plan", None, None, False, _plan_root(call)).emit("orch_plan.effects_lost", {"reason": why})
    except Exception:  # noqa: BLE001 - the answer stands; the stderr line above says what was lost
        pass


def _plan_commit(call: list[str], _before: None, effects_path: str, reply: dict[str, Any],
                 stdout: bytes) -> tuple[dict[str, Any], bytes]:
    """Replays a committed plan: its `plan.updated` events in order, then one live refresh when the manifest asks for it."""
    try:
        body: object = json.loads(Path(effects_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        body = None
    events = _plan_events(body)
    if events is None:
        _plan_effects_lost(call, "effects manifest missing or unparsable")
        return reply, stdout
    try:
        ctx = Ctx("A01", "orch_plan", None, body["correlation_id"], False, _plan_root(call))  # type: ignore[index]
        for event_type, payload in events:
            ctx.emit(event_type, payload)
        if body["live_refresh"]:  # type: ignore[index]
            from swarm import telemetry  # lazy, as TaskStore._notify_committed does
            store = TaskStore(root=ctx.root)
            telemetry.refresh_live(db_path=store.path, conn=store.conn)
    except Exception as exc:  # noqa: BLE001 - the plan is committed: say what was lost, never run it again
        _plan_effects_lost(call, f"replay failed: {type(exc).__name__}")
    return reply, stdout


ENGINE_ROUTES: tuple = ()


if __name__ == "__main__":
    script = AgentScript("A01", "orch_plan", run, description=__doc__, add_args=add_args)
    answer = None
    if ENGINE_ROUTES or os.environ.get("GSD_ENGINE", "python") not in ("python", ""):
        from swarm import engine_select
        if ENGINE_ROUTES:
            answer = engine_select.select_engine(engine_select.Selector(script, ENGINE_ROUTES), sys.argv[1:])
        else:
            route = engine_select.Route(
                claims=_claims_plan, parse=lambda argv: argv if _claims_plan(argv) else None,
                default=lambda: "python", env_route=lambda argv, call: "plan",
                write=engine_select.WriteFlow(
                    before=lambda call: None, commit=_plan_commit,
                    unknown=lambda call: ("the plan was not written", {}),
                    preexec=lambda route: engine_select.no_core_dumps,
                ),
            )
            answer = engine_select.select_engine(
                engine_select.Selector(script, (route,), binary_env=_binary_env,
                                       env_keys=("SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID",
                                                 "SWARM_AGENT_SESSION", "SWARM_CHILD", "SWARM_AGENT")),
                sys.argv[1:],
            )
    sys.exit(answer if answer is not None else script.main())
