#!/usr/bin/env python3
"""A12 — release gate + progressive-delivery plan.

For --task-id / --task-ids / --correlation-id, verifies each task's required non-release gates with
TaskStore.missing_gate_reasons (only a verified, current, unexpired signed pass/waive counts; any other gate is
a '<gate>:<reason>' problem: absent, unsigned, bad-sig, mismatch, stale, dry-run, fail or expired), honours a
swarm-wide freeze (.swarm/release.freeze, set with --freeze "reason",
lifted with --unfreeze; --dry-run reads it but never writes it), and emits a signed `gate.verdict` with gate="release" that passes only when
every other required gate passes and no freeze is active. Also writes a canary release.plan
(5→25→50→100 %) with guardrails and rollback triggers to .swarm/releases/<release_id>.plan.json.
Verdict rows are recorded only when --task-id is a release gate task: it then evaluates, and records on,
exactly that task's notes.gate_for targets, each row from that target's own findings plus any freeze finding
(the overall verdict still fails when any target fails). --task-ids and correlation-wide runs are report-only.
"""
from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.script_base import AgentScript, check_task_id  # noqa: E402
from swarm.gates import make_finding  # noqa: E402
from swarm.verdicts import issue_gate, resolve_targets  # noqa: E402
from swarm.taskstore import TaskStore, GATES_BY_RISK  # noqa: E402
from swarm.paths import swarm_dir  # noqa: E402
from swarm.errors import SwarmError, ErrorCode  # noqa: E402

RISK_ORDER = ["low", "medium", "high"]
GUARDRAILS = {"low": {"error_rate_max": 0.01, "p95_ms_max": 500, "slo_burn_max": 6.0, "soak_min": 5},
              "medium": {"error_rate_max": 0.005, "p95_ms_max": 320, "slo_burn_max": 2.0, "soak_min": 15},
              "high": {"error_rate_max": 0.002, "p95_ms_max": 250, "slo_burn_max": 1.0, "soak_min": 30}}


def freeze_file(root: Path) -> Path:
    return swarm_dir(root) / "release.freeze"


def read_freeze(root: Path) -> dict | None:
    f = freeze_file(root)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (OSError, json.JSONDecodeError):
        return {"reason": "unreadable freeze file", "frozen_at": None, "by": "unknown"}


def freeze_finding(root: Path, frozen: dict, fid: str) -> dict:
    return make_finding(fid, "blocker", "freeze", f"deploy freeze active: {frozen.get('reason')} (since {frozen.get('frozen_at')})",
                        evidence=str(freeze_file(root)), owner_suggestion="A13")


def build_plan(release_id: str, risk: str, tasks: list[str], gates: list[str], artifacts: list[str]) -> dict:
    g = GUARDRAILS[risk]
    return {"kind": "release.plan", "release_id": release_id, "strategy": "canary", "steps_pct": [5, 25, 50, 100],
            "guardrails": g, "gates_required": gates, "risk_class": risk, "auto_rollback": True,
            "approval": "L4:human-four-eyes" if risk == "high" else "L2:auto",
            "rollback_triggers": [f"error_rate > {g['error_rate_max']} over 5m at any step",
                                  f"p95_latency_ms > {g['p95_ms_max']} over 5m at any step",
                                  f"slo_burn_rate > {g['slo_burn_max']}x (1h window)",
                                  "deploy.telemetry verdict == rollback-suggested", "incident.alert sev<=2 naming this release",
                                  "monitoring.degraded broadcast (fail-closed: halt at current step)"],
            "tasks": tasks, "artifacts": artifacts, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


def run(args, ctx) -> dict:
    swarm_dir(ctx.root, create=True)
    if args.freeze and not ctx.dry_run:
        rec = {"reason": args.freeze, "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "by": "A12@local", "correlation_id": ctx.correlation_id}
        freeze_file(ctx.root).write_text(json.dumps(rec, indent=2))
    if args.unfreeze and not ctx.dry_run and freeze_file(ctx.root).exists():
        freeze_file(ctx.root).unlink()
    frozen = read_freeze(ctx.root)  # read only; the dry-run honours an active freeze too (T-01-35)

    if ctx.dry_run:
        plan = build_plan("REL-dry", "medium", ["T-dry"], ["review", "quality"], [])
        findings = [freeze_finding(ctx.root, frozen, "RF-001")] if frozen else []
        env, recorded = issue_gate(ctx, gate="release", agent_id="A12@dry", findings=findings, simulate=True,
                                   runs={"review": "pass", "quality": "pass", "freeze": "active" if frozen else "none"})
        verdict = env["payload"]["verdict"]
        summary = (f"dry-run: release gate FAIL, deploy freeze active ({frozen.get('reason')})" if frozen
                   else "dry-run: canned release gate PASS with canary plan")
        return {"status": "ok" if verdict == "pass" else "fail", "verdict": verdict, "frozen": bool(frozen),
                "freeze": frozen, "plan": plan, "findings": findings, "envelope": env, "recorded": sorted(recorded),
                "dry_run": True, "summary": summary}

    store = TaskStore(root=ctx.root)
    # a release gate task evaluates exactly its Task Store gate_for targets (D-13); any other mode is report-only
    targets = resolve_targets(store, ctx.task_id, gate="release")
    if targets is not None:
        ids = targets
    else:
        ids = [t for t in (args.task_ids or "").split(",") if t]
        if ctx.task_id:
            ids.insert(0, ctx.task_id)
    if ctx.correlation_id and not ids:
        ids = [t["task_id"] for t in store.list(correlation_id=ctx.correlation_id)]
    findings, own, per_task, artifacts, risk = [], {}, {}, [], "low"  # own: target → its own findings (D-13)

    def add(tid: str, severity: str, kind: str, summary: str, owner: str) -> None:
        findings.append(make_finding(f"RF-{len(findings)+1:03d}", severity, kind, summary, owner_suggestion=owner))
        own[tid].append(findings[-1])

    if not ids:
        findings.append(make_finding("RF-000", "major", "input", "no task identified (pass --task-id/--task-ids/--correlation-id)",
                                     owner_suggestion="A01"))
    for tid in ids:
        own.setdefault(tid, [])
        try:
            task = store.get(tid)
        except SwarmError as e:
            if e.code is not ErrorCode.E_INPUT:
                raise
            per_task[tid] = {"gates": {}, "verdicts": {}, "required": [], "overall": "fail", "problems": ["task:unknown"]}
            add(tid, "major", "input", f"unknown task {tid} in Task Store", "A01")
            continue
        if RISK_ORDER.index(task["risk_class"]) > RISK_ORDER.index(risk):
            risk = task["risk_class"]
        required = [g for g in store.required_gates(tid) if g != "release"]
        # D-14: only verified signed envelopes count; any other row reads as its verification reason
        reasons = {g: r for g, r in store.missing_gate_reasons(tid).items() if g != "release"}
        problems = [f"{g}:{r}" for g, r in reasons.items()]
        status = {g: reasons.get(g, "ok") for g in required}
        per_task[tid] = {"state": task["state"], "risk_class": task["risk_class"], "required": required,
                         "gates": status, "verdicts": status, "overall": "fail" if problems else "pass",
                         "problems": problems, "expired": [g for g, r in reasons.items() if r == "expired"]}
        artifacts += [o.get("uri") for o in task["outputs"] if o.get("kind") == "build.artifact"]
        for gate, why in reasons.items():
            add(tid, "major", "gate", f"{tid}: {gate} gate {why}",
                {"review": "A09", "quality": "A08", "security": "A10"}.get(gate, "A01"))
        if not artifacts and task["risk_class"] != "low":
            add(tid, "minor", "provenance", f"{tid}: no build.artifact registered — A11 provenance required before promote",
                "A11")
    if frozen:  # a freeze fails every target
        findings.append(freeze_finding(ctx.root, frozen, f"RF-{len(findings)+1:03d}"))
        for mine in own.values():
            mine.append(findings[-1])

    primary = ids[0] if ids else "T-unassigned"
    release_id = check_task_id(args.release_id or f"REL-{primary}", "release id")  # a file name below (CR-01)
    gates = sorted({g for t in per_task.values() for g in t["required"]}) or [g for g in GATES_BY_RISK[risk] if g != "release"]
    plan = build_plan(release_id, risk, ids, gates, [a for a in artifacts if a])
    runs = {g: ("pass" if all(t["gates"].get(g, "ok") == "ok" for t in per_task.values()) else "fail") for g in gates}
    runs["freeze"] = "active" if frozen else "none"
    # a release gate task records each target's verdict from that target's own findings plus any freeze (D-13)
    env, recorded = issue_gate(ctx, gate="release", agent_id="A12@local", findings=findings, runs=runs,
                               extra={"release_id": release_id, "frozen": bool(frozen)},
                               per_target=own if targets is not None else None)
    verdict = env["payload"]["verdict"]
    rdir = swarm_dir(ctx.root) / "releases"
    rdir.mkdir(parents=True, exist_ok=True)
    plan["gate_verdict"] = verdict
    (rdir / f"{release_id}.plan.json").write_text(json.dumps(plan, indent=2))
    return {"recorded": sorted(recorded), "status": "ok" if verdict == "pass" else "fail", "verdict": verdict, "frozen": bool(frozen), "freeze": frozen,
            "release_id": release_id, "tasks": per_task, "plan": plan, "findings": findings, "envelope": env,
            "summary": f"release gate {verdict.upper()} for {release_id} ({len(ids)} task(s), risk={risk}"
                       f"{', FROZEN' if frozen else ''}) — canary {plan['steps_pct']}"}


def add_args(p):
    p.add_argument("--task-ids", help="comma-separated additional task ids to gate together")
    p.add_argument("--release-id", help="release identifier (default REL-<task_id>)")
    p.add_argument("--freeze", metavar="REASON", help="write .swarm/release.freeze with this reason before evaluating")
    p.add_argument("--unfreeze", action="store_true", help="remove an active freeze (declaring authority only)")


# Engine selector. The default row stays Python. `GSD_ENGINE=rust` serves `--json` except `--dry-run`, which stays
# this body so the inert-route seam in tests/test_engine_select.py still holds.
# The route writes (the binary records the release gate's verdicts in issue_gate's transaction), so it is a write route: Python
# answers only a binary reply that says nothing was written. A route that commits a verdict reports a committed outcome
# as final, so Python is not re-run.
def _binary_env(route: str) -> dict[str, str]:
    from swarm.engine_select import BINARY_ENV_NAMES
    if route != "rel-plan":
        raise KeyError(route)
    names = set(BINARY_ENV_NAMES) | {"SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE",
                                     "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY", "SWARM_AGENT_SESSION",
                                     "XDG_CONFIG_HOME", "SWARM_DRYRUN_FAIL"}
    return {key: value for key, value in os.environ.items() if key in names}


def _claims_rel(argv: list[str]) -> bool:
    return "--json" in argv and "--dry-run" not in argv and "--help" not in argv and "-h" not in argv


def _rel_commit(call, before, effects_path, reply, stdout):
    # The binary has committed the verdict and its reply is final; the gate routes write no effects manifest to replay.
    return reply, stdout


ENGINE_ROUTES: tuple = ()


if __name__ == "__main__":
    script = AgentScript("A12", "rel_plan", run, description=__doc__, add_args=add_args)
    answer = None
    if ENGINE_ROUTES or os.environ.get("GSD_ENGINE", "python") not in ("python", ""):
        from swarm import engine_select
        if ENGINE_ROUTES:
            answer = engine_select.select_engine(engine_select.Selector(script, ENGINE_ROUTES), sys.argv[1:])
        else:
            route = engine_select.Route(
                claims=_claims_rel, parse=lambda argv: argv if _claims_rel(argv) else None,
                default=lambda: "python", env_route=lambda argv, call: "rel-plan",
                write=engine_select.WriteFlow(
                    before=lambda call: None, commit=_rel_commit,
                    unknown=lambda call: ("the verdict may or may not have been recorded; check the gate's verdict rows and "
                                          "verdicts/ before running the gate again", {}),
                    preexec=lambda route: engine_select.no_core_dumps,
                ),
            )
            answer = engine_select.select_engine(
                engine_select.Selector(script, (route,), binary_env=_binary_env,
                                       env_keys=("SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT_SESSION")),
                sys.argv[1:],
            )
    sys.exit(answer if answer is not None else script.main())
