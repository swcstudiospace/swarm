"""Gate-verdict recording (D-12/D-13): gate scripts are the only writers of verdict rows.

A gate script invoked with a leased (IN_PROGRESS) gate task's id (notes.gate) records one signed verdict per
target in that task's Task Store notes.gate_for, signed with the target's correlation — agents cannot choose
or spoof targets. Any other --task-id writes only the envelope file and emits gate.verdict.unrecorded.
--dry-run verdicts are signed dry_run: true and recorded only for gate tasks A01 flagged as a runner dry-run.
Inside a headless agent session (SWARM_AGENT_SESSION=1, no signing keys) nothing is recorded: the runner re-runs
the gate script with its keys after the session (WR-12).
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .errors import SwarmError, ErrorCode
from .envelope import insecure_dev_key, real_key_configured, signing_config_error
from .gates import SEVERITIES, derive_verdict, make_finding, make_verdict

if TYPE_CHECKING:
    from .script_base import Ctx
    from .taskstore import TaskStore

# Names the missing key. Advisory envelopes carry this; it is not a verifying signature.
MISSING_KEY_REASON = (
    "fail-closed: no signing key configured "
    "(SWARM_ED25519_KEY and SWARM_SIGNING_KEY are unset; "
    "SWARM_ALLOW_INSECURE_DEV_KEY=1 opts into the dev key)"
)
SESSION_UNRECORDED = "agent session: the runner records this gate after the session"


def keyless_advisory() -> bool:
    """True when a verdict signed now would need the implicit dev key, which is refused.

    SWARM_REQUIRE_KEY=1 and an unloadable SWARM_ED25519_KEY stay on signing_config_error
    (the gate script exits 2). Those are not this advisory path.
    """
    if signing_config_error():
        return False
    return not real_key_configured() and insecure_dev_key() is None


def advisory_envelope(*, gate: str, task_id: str, agent_id: str, findings: list[dict[str, Any]], runs: dict[str, Any],
                      expires_s: int, correlation_id: str | None, extra: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    """Unsigned gate envelope marked advisory. It does not verify and is not a verdict row."""
    import time
    from .envelope import build_envelope
    payload: dict[str, Any] = {"gate": gate, "task_id": task_id, "verdict": derive_verdict(findings or []),
               "findings": findings or [], "runs": runs or {}, "expires_s": expires_s,
               "issued_at": time.time(), "advisory": True, "advisory_reason": reason, **(extra or {})}
    return build_envelope(source=agent_id, target="A01", msg_type="gate.verdict", payload=payload,
                           correlation_id=correlation_id, priority="P1")

# Parity mirror: omp/src/tools.ts GATE_SCRIPTS; omp/test/gate.test.ts pins the two maps equal.
GATE_SCRIPTS = {"quality": "qa_gate", "review": "rev_gate", "security": "sec_gate", "release": "rel_plan"}
SIM_FINDING = {"id": "SIM-1", "severity": "major", "kind": "functional", "summary": "simulated gate failure",
               "evidence": "", "ac_ref": None, "owner_suggestion": None, "location": None}


def agent_finding(f: object, i: int, path: str, *, prefix: str = "RF", owner: str = "A05") -> dict[str, Any]:
    """One agent-reported finding from a findings file, validated: an object with a severity in SEVERITIES
    (default minor); a missing id becomes <prefix>-<i>."""
    if not isinstance(f, dict):
        raise SwarmError(ErrorCode.E_INPUT, f"finding {i} in {path} is not an object")
    sev = f.get("severity", "minor")
    if sev not in SEVERITIES:
        raise SwarmError(ErrorCode.E_INPUT, f"bad severity {sev!r} in {path}")
    return make_finding(f.get("id") or f"{prefix}-{i:03d}", sev, f.get("kind", "semantic"), f.get("summary", ""),
                        evidence=f.get("evidence", ""), ac_ref=f.get("ac_ref"),
                        owner_suggestion=f.get("owner_suggestion", owner), location=f.get("location"))


def load_per_target(path: str | None, offset: int, *, prefix: str = "RF", owner: str = "A05") -> dict[str, list[dict[str, Any]]]:
    """--per-target-findings {target task id: [findings]} (D-13): the agent's findings for each gate target; a gate
    gives each listed target a verdict derived from the script's own findings plus only that target's list."""
    import json
    from pathlib import Path
    if not path:
        return {}
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or not all(isinstance(v, list) for v in raw.values()):
        raise SwarmError(ErrorCode.E_INPUT, f"{path}: expected an object {{target task id: [findings]}}")
    out: dict[str, list[dict[str, Any]]] = {}
    for target, items in raw.items():
        out[target] = [agent_finding(f, i, path, prefix=prefix, owner=owner) for i, f in enumerate(items, offset + 1)]
        offset += len(items)
    return out


def check_per_target_keys(own: dict[str, list[dict[str, Any]]], task_id: str | None, root: str | Path | None = None) -> None:
    """E-INPUT when --per-target-findings names a target that is not in the gate task's gate_for: a finding under a
    mistyped or stale id could otherwise never reach a verdict. Nothing to check without a gate task."""
    from .taskstore import TaskStore
    if not own or not task_id:
        return
    try:
        task = TaskStore(root=root).get(task_id)
    except SwarmError:
        return
    gate_for = (task.get("notes_json") or {}).get("gate_for")
    if not gate_for:
        return
    unknown = sorted(set(own) - set(gate_for))
    if unknown:
        raise SwarmError(ErrorCode.E_INPUT, f"per-target findings name ids that are not targets of {task_id}: "
                         f"{', '.join(unknown)} (targets: {', '.join(gate_for)})", task_id=task_id)


RISK_ORDER = ("low", "medium", "high")


def gate_risk_class(task_id: str | None, root: str | Path | None = None) -> str | None:
    """The highest risk class of a gate task and its gate_for targets (the quality gate's tier selection);
    None when task_id is not a known gate task. A target missing from the Task Store is skipped."""
    from .taskstore import TaskStore
    if not task_id:
        return None
    store = TaskStore(root=root)
    try:
        task = store.get(task_id)
    except SwarmError:
        return None
    notes = task.get("notes_json") or {}
    if not notes.get("gate"):
        return None
    risks = [task.get("risk_class")]
    for target in notes.get("gate_for") or []:
        try:
            risks.append(store.get(target).get("risk_class"))
        except SwarmError:
            continue
    known = [r for r in risks if r in RISK_ORDER]
    return max(known, key=RISK_ORDER.index) if known else None


def _per_target_blocks(per_target: dict[str, list[dict[str, Any]]] | None) -> bool:
    """True when any target's own findings would fail its verdict."""
    return any(derive_verdict(items) == "fail" for items in (per_target or {}).values())


def _advisory_per_target(per_target: dict[str, list[dict[str, Any]]] | None, *, gate: str, agent_id: str, runs: dict[str, Any],
                         expires_s: int, correlation_id: str | None, extra: dict[str, Any] | None, reason: str,
                         store: TaskStore | None) -> dict[str, dict[str, Any]]:
    """Unsigned per-target envelopes. Nothing here is written to the Task Store."""
    out: dict[str, dict[str, Any]] = {}
    for target, items in (per_target or {}).items():
        corr = correlation_id
        if store is not None:
            try:
                corr = store.get(target)["correlation_id"]
            except SwarmError:
                pass
        out[target] = advisory_envelope(gate=gate, task_id=target, agent_id=agent_id, findings=items, runs=runs,
                                        expires_s=expires_s, correlation_id=corr, extra=extra, reason=reason)
    return out


def gate_verdict(env: dict[str, Any], recorded: dict[str, dict[str, Any]]) -> str:
    """A gate script's overall verdict: fail when its envelope or any recorded per-target verdict fails."""
    verdicts = [env["payload"]["verdict"], *(e["payload"]["verdict"] for e in recorded.values())]
    return "fail" if "fail" in verdicts else "pass"


def simulated_failures() -> set[str]:
    """SWARM_DRYRUN_FAIL='T7f3a-be:quality,T7f3a-fe:review' makes those dry-run gate verdicts fail every time."""
    return {x.strip() for x in os.environ.get("SWARM_DRYRUN_FAIL", "").split(",") if x.strip()}


def simulated_per_target(targets: list[str] | None, gate: str) -> dict[str, list[dict[str, Any]]]:
    """Dry-run findings per target: SIM-1 (major) for targets named in SWARM_DRYRUN_FAIL, else none."""
    fails = simulated_failures()
    return {t: ([dict(SIM_FINDING)] if f"{t}:{gate}" in fails else []) for t in targets or []}


def resolve_targets(store: TaskStore, task_id: str | None, *, gate: str, correlation_id: str | None = None) -> list[str] | None:
    """gate_for of a gate task; None when task_id is not a gate task. E-POLICY when its gate differs, when
    correlation_id is given and is not the gate task's own correlation, or when a gate task with targets is
    not leased (IN_PROGRESS) — only a running gate task records verdicts."""
    if not task_id:
        return None
    try:
        task = store.get(task_id)
    except SwarmError as e:
        if e.code is not ErrorCode.E_INPUT:
            raise
        return None
    notes = task.get("notes_json") or {}
    if not notes.get("gate"):
        return None
    if notes["gate"] != gate:
        raise SwarmError(ErrorCode.E_POLICY, f"{task_id} is a {notes['gate']} gate task; this script issues {gate}",
                         task_id=task_id)
    own = task["correlation_id"]
    if correlation_id and correlation_id != own:
        raise SwarmError(ErrorCode.E_POLICY,
                         f"gate task {task_id} belongs to correlation {own!r}, not {correlation_id!r}; --correlation-id "
                         "defaults to the SWARM_CORRELATION_ID env var: unset a stale SWARM_CORRELATION_ID export or "
                         f"pass --correlation-id {own}", task_id=task_id)
    targets = list(notes.get("gate_for") or [])
    if targets and task["state"] != "IN_PROGRESS":
        raise SwarmError(ErrorCode.E_POLICY, f"{task_id} is not a running gate task (state {task['state']}); "
                         "lease it first", task_id=task_id)
    return targets


def record_gate_verdicts(store: TaskStore, *, gate_task_id: str | None, gate: str, agent_id: str, findings: list[dict[str, Any]],
                         runs: dict[str, Any], correlation_id: str | None, expires_s: int, extra: dict[str, Any] | None = None,
                         verdict: str | None = None, per_target: dict[str, list[dict[str, Any]]] | None = None,
                         emit: Callable[..., object], root: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Record one signed verdict row per gate_for target of `gate_task_id`; return {target: envelope}.
    Each target verdict is signed with the target's own correlation; a given `correlation_id` must be the
    gate task's (E-POLICY otherwise). All rows (and fail feedback) of one run commit together or not at all (D-13).
    `root` is the caller's --root, so dev-key signing events land in the same state dir."""
    targets = resolve_targets(store, gate_task_id, gate=gate, correlation_id=correlation_id)
    if not targets:
        emit("gate.verdict.unrecorded", {"task_id": gate_task_id, "gate": gate,
                                         "reason": "not a gate task" if targets is None else "empty gate_for"})
        return {}
    if keyless_advisory():
        emit("gate.verdict.unrecorded", {"task_id": gate_task_id, "gate": gate, "reason": MISSING_KEY_REASON})
        return {}
    out: dict[str, dict[str, Any]] = {}
    with store.transaction():
        for target in targets:
            tf = (per_target or {}).get(target, findings)
            env = make_verdict(gate=gate, task_id=target, agent_id=agent_id, findings=tf, runs=runs,
                               verdict=None if per_target and target in per_target else verdict,
                               expires_s=expires_s, correlation_id=store.get(target)["correlation_id"],
                               extra={**(extra or {}), "gate_task": gate_task_id}, root=root)
            store.record_verdict(target, env)
            if env["payload"]["verdict"] == "fail":
                store.append_feedback(target, {"gate": gate, "source": "script", "findings": tf})
            out[target] = env
    return out



def issue_gate(ctx: Ctx, *, gate: str, agent_id: str, findings: list[dict[str, Any]], runs: dict[str, Any], expires_s: int = 86400,
               extra: dict[str, Any] | None = None, simulate: bool = False,
               per_target: dict[str, list[dict[str, Any]]] | None = None) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Gate-script tail: sign the verdict for ctx.task_id, write verdicts/<id>.<gate>.json, and record rows on
    the gate task's targets. per_target ({target: findings}) gives each target a verdict derived from its own
    findings (D-13); targets it omits get `findings`. The file envelope always carries `findings`.
    simulate (--dry-run): canned pass, SIM-1 fail for SWARM_DRYRUN_FAIL targets, plus
    the caller's findings; every envelope carries a signed dry_run: true, and rows are recorded only when A01
    flagged the gate task as a runner dry-run (notes.dry_run), where they also count only on flagged targets."""
    import json
    from .paths import swarm_dir
    from .taskstore import TaskStore
    store = TaskStore(root=ctx.root) if ctx.task_id else None
    targets = (resolve_targets(store, ctx.task_id, gate=gate, correlation_id=ctx.correlation_id)
               if store else None)  # E-POLICY before any write
    gate_task = store.get(ctx.task_id) if store and ctx.task_id and targets is not None else None
    # file envelope: the caller's correlation, else the gate task's; a non-gate/unknown id keeps a minted one
    corr = ctx.correlation_id or (gate_task["correlation_id"] if gate_task else None)
    if simulate:
        extra = {**(extra or {}), "dry_run": True}
    from .script_base import check_task_id
    task = check_task_id(ctx.task_id or ("T-dry" if simulate else "T-unassigned"))
    session = os.environ.get("SWARM_AGENT_SESSION") == "1"
    out_dir = swarm_dir(ctx.root, create=True) / "verdicts"
    out_dir.mkdir(parents=True, exist_ok=True)
    if signing_config_error():
        # REQUIRE_KEY or an unloadable Ed25519 seed: same E-POLICY as before, no file, no row.
        make_verdict(gate=gate, task_id=task, agent_id=agent_id, findings=findings, runs=runs, expires_s=expires_s,
                     correlation_id=corr, extra=extra, root=ctx.root, audit=not session)
    if keyless_advisory():
        # No implicit dev key. The file is advisory and does not verify; nothing is recorded.
        # An agent session keeps its existing unrecorded reason (WR-15) and still emits no security.dev_key.
        # Per-target failures stay in the returned map (unsigned) and on the file verdict, so a clean
        # script check plus a blocking per-target finding still fails the gate.
        reason = SESSION_UNRECORDED if session else MISSING_KEY_REASON
        env = advisory_envelope(gate=gate, task_id=task, agent_id=agent_id, findings=findings, runs=runs,
                                expires_s=expires_s, correlation_id=corr, extra=extra, reason=reason)
        if _per_target_blocks(per_target):
            env["payload"]["verdict"] = "fail"
        recorded = _advisory_per_target(per_target, gate=gate, agent_id=agent_id, runs=runs, expires_s=expires_s,
                                        correlation_id=corr, extra=extra, reason=reason, store=store)
        (out_dir / f"{task}.{gate}.json").write_text(json.dumps(env, indent=2))
        ctx.emit("gate.verdict.unrecorded", {"task_id": ctx.task_id, "gate": gate, "reason": reason})
        return env, recorded
    # WR-15: a key-less agent-session preview never records, so it must not raise the security.dev_key
    # misconfiguration signal; gate.verdict.unrecorded below marks it instead.
    # A session that does hold a key still must not report pass when a per-target finding blocks:
    # the file verdict is set before signing, and no row is recorded.
    file_verdict = "fail" if session and _per_target_blocks(per_target) else None
    env = make_verdict(gate=gate, task_id=task, agent_id=agent_id, findings=findings, runs=runs, expires_s=expires_s,
                       correlation_id=corr, extra=extra, root=ctx.root, audit=not session, verdict=file_verdict)
    (out_dir / f"{task}.{gate}.json").write_text(json.dumps(env, indent=2))
    if session:
        ctx.emit("gate.verdict.unrecorded", {"task_id": ctx.task_id, "gate": gate,
                                             "reason": "agent session: the runner records this gate after the session"})
        return env, {}
    if store is None:
        ctx.emit("gate.verdict.unrecorded", {"task_id": None, "gate": gate, "reason": "no --task-id"})
        return env, {}
    if simulate:
        if targets and not (gate_task and gate_task["notes_json"].get("dry_run")):
            ctx.emit("gate.verdict.unrecorded", {"task_id": ctx.task_id, "gate": gate,
                                                 "reason": "dry-run outside a runner dry-run"})
            return env, {}
        per_target = {t: sim + list(findings) for t, sim in simulated_per_target(targets, gate).items()}
    recorded = record_gate_verdicts(store, gate_task_id=ctx.task_id, gate=gate, agent_id=agent_id, findings=findings,
                                    runs=runs, correlation_id=ctx.correlation_id, expires_s=expires_s, extra=extra,
                                    per_target=per_target, emit=ctx.emit, root=ctx.root)
    return env, recorded
