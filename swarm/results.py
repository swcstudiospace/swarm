"""The one task.result path shared by the headless runner and `orch_status.py --ingest`.

Both callers do: parse_result → validate_result → apply_result → reconcile.

The ONE intended mode difference:
  * mode="headless": the session is over, so a final state of IN_PROGRESS is a contract failure
    (FAILED "E-CONTRACT: session ended in IN_PROGRESS"); a missing/invalid result also becomes
    FAILED E-CONTRACT and counts as an attempt (retry ladder → ESCALATED at max_attempts).
  * mode="ingest": IN_PROGRESS is a claim/heartbeat; PLANNED/RETRY tasks whose dependencies are satisfied are
    auto-claimed (CLAIMED → IN_PROGRESS, event task.claimed) before the reported state is applied. A BLOCKED
    task (e.g. needs: human-approval) or one with unmet dependencies is rejected: A01 releases BLOCKED only with
    `orch_status.py --transition` after approval. Invalid input is rejected with E-CONTRACT and no transition
    (never consumes an attempt).
Both modes accept a gate task's IN_REVIEW only when its gate script recorded a verdict on every gate_for target
during the current lease (WR-08); otherwise "E-CONTRACT: gate script not run" (headless FAILED on that attempt, ingest exit 2).
A review gate task's IN_REVIEW is also refused when its verdicts{} fails a target (agent_failed) whose review row from
this lease is not `fail`: "E-CONTRACT: review verdict mismatch: ..." (headless FAILED on that attempt, ingest exit 2, no transition).
A target is approved only from rows whose gate task (the signed gate_task) has an accepted result (IN_REVIEW,
APPROVED or DONE), so a refused gate task's rows hold its targets even after it leaves the lease FAILED or BLOCKED.
reconcile holds such a target, and TaskStore.transition refuses its APPROVED on every other path too (E-POLICY, T-05-31).
A gate task that is still PLANNED or RETRY holds no lease, so an ingest cannot be satisfied by a row of its previous
lease (T-05-30): every gate_for target counts as missing.
A headless rejection fails the row only inside one write transaction, and only while that row is still IN_PROGRESS
on the attempt that produced the result (`task["attempt"]` on the snapshot passed to apply_result, not the attempt
re-read after a race). TaskStore.transition increments attempt when PLANNED, RETRY or BLOCKED moves to CLAIMED, so a
retry that lands between the script check and the locked recheck is a different attempt: the rejection event is
emitted and the new lease is left as it stands. Ingest still raises and does not claim or fail that lease.
Rejections on both paths emit task.result.rejected {task_id, mode, reason}.

Replay of an ingest (D-P24). An ingest that applied a result records `{task_id, attempt, digest, reply}` in
`notes.ingest_keys` (digest = `result_digest`, the sha256 of the canonical result; reply = the state apply_result
returned), newest INGEST_KEYS_BOUND (32) kept. The key is the LAST write of the transaction that holds the transition,
the notes, the artifact rows and the gate feedback, so there are exactly two crash outcomes: before the commit nothing
is applied and no key exists (the retry applies); after it everything and the key exist (the retry is skipped).
A retry whose (task, attempt, digest) is already recorded applies nothing: no transition, no artifact row, no feedback,
no note write. It emits one `task.result.deduplicated` record and returns the stored reply, so the caller sees the
first answer; there is no extra field (the return value is the state string both callers share, and the event is the
only trace). Only a move the state machine accepts is skipped: a replay `transition` would refuse (IN_REVIEW again,
DONE, ...) still raises exactly as before. Refused or invalid results write no key, and a new attempt is a new key.
Headless is never keyed: its result comes from a live session, not a delivery that can be repeated. The same bytes
resubmitted after a rework (same attempt) are indistinguishable from a replay and are skipped while the key is kept.
"""
from __future__ import annotations
import hashlib
import json
import re
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, Callable

from .errors import SwarmError, ErrorCode
from .gates import BLOCKING_SEVERITY, SEVERITIES
from .schema import load_schema, validate
from .taskstore import LEGAL_TRANSITIONS, TaskStore, TaskState as S

if TYPE_CHECKING:
    from .substrate_handoff import HandoffBridge
    from .substrate_lease import LeaseBridge

JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.S)
SCHEMA_NAME = "task.result.v1"
# Emitter callers differ: Ctx.emit takes `task_id=`; the batch reconcile emitter does not (reachable only with approve=False).
Emit = Callable[..., object]


def parse_result(text: str) -> dict[str, Any] | None:
    """Last parseable fenced ```json block, else the whole text as a bare JSON object."""
    for raw in reversed(JSON_BLOCK.findall(text or "")):
        try:
            parsed: dict[str, Any] = json.loads(raw)  # JSON_BLOCK captures one `{...}` object, so the parse is an object
            return parsed
        except json.JSONDecodeError:
            continue
    try:
        obj = json.loads(text or "")
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def validate_result(result: dict[str, Any] | None, *, task_id: str) -> dict[str, Any]:
    """Schema-check `result` and bind it to the assigned task; raise E-CONTRACT otherwise."""
    if result is None:
        raise SwarmError(ErrorCode.E_CONTRACT, "no JSON result block from agent", task_id=task_id)
    errors = validate(result, load_schema(SCHEMA_NAME))
    if errors:
        raise SwarmError(ErrorCode.E_CONTRACT, f"task.result invalid: {'; '.join(errors)}"[:500], task_id=task_id)
    if result["task_id"] != task_id:
        raise SwarmError(ErrorCode.E_CONTRACT, f"task_id mismatch: result names {result['task_id']!r}, assigned {task_id!r}",
                         task_id=task_id)
    return result


def reject(store: TaskStore, task_id: str, *, attempt: int, reason: str, mode: str, emit: Emit) -> str:
    """Record a rejected result.

    Headless moves the row to FAILED (counts as an attempt) only inside one write transaction, and only
    when it is still IN_PROGRESS on `attempt` — the attempt that produced the result. A different attempt,
    or a row that has left IN_PROGRESS, is left unchanged; the rejection event is emitted either way.
    Ingest emits the event and does not transition.
    """
    emit("task.result.rejected", {"task_id": task_id, "mode": mode, "reason": reason})
    if mode == "headless":
        with store.transaction():
            current = store.get(task_id)
            if current["state"] == S.IN_PROGRESS.value and current["attempt"] == attempt:
                store.transition(task_id, S.FAILED, reason=reason[:500])
                return S.FAILED.value
            state: str = current["state"]
            return state
    new_state: str = store.get(task_id)["state"]
    return new_state


def check_manual_transition(store: TaskStore, task_id: str, to_state: str) -> None:
    """Refuse a manual (`orch_status --transition`, the omp swarm_transition tool) move that skips a check only the
    ingest path makes (T-05-31). A gate task reaches IN_REVIEW only through apply_result, which checks that its gate
    script ran during this lease and that the agent's verdicts agree with the rows; moving it by hand would bypass both.
    A manual APPROVED needs no check here: TaskStore.transition applies the missing and unaccepted gate rules to it.
    Raises E-INPUT for an unknown task, as the transition itself would."""
    task = store.get(task_id)
    if to_state == S.IN_REVIEW.value and task["notes_json"].get("gate"):
        raise SwarmError(ErrorCode.E_POLICY,
                         f"{task_id} is a gate task: only a task.result ingest may move it to IN_REVIEW (it checks that the "
                         "gate script ran during this lease and that the agent's verdicts match the recorded rows)",
                         task_id=task_id)


def _lease_start(store: TaskStore, task_id: str) -> float | None:
    """ts of the task's latest CLAIMED transition (its current lease start), else None."""
    claims = [h["ts"] for h in store.history(task_id) if h["to_state"] == S.CLAIMED.value]
    return claims[-1] if claims else None


def _gate_script_missing(store: TaskStore, task: dict[str, Any]) -> list[str]:
    """gate_for targets of a gate task lacking a verifying row that its gate script issued during the current lease
    (since the latest CLAIMED). With no CLAIMED row the lease start is unknown, so every target counts as missing.
    A PLANNED or RETRY gate task holds no lease at all (T-05-30): "the latest CLAIMED" would be its PREVIOUS lease, and an
    ingest that claims it would be satisfied by a row that lease's script issued. Every target counts as missing then."""
    notes = task["notes_json"]
    targets = list(notes.get("gate_for") or [])
    if task["state"] in (S.PLANNED.value, S.RETRY.value):
        return targets
    since = _lease_start(store, task["task_id"])
    if since is None:
        return targets
    return [t for t in targets
            if not store.gate_verdict_since(t, gate=notes["gate"], gate_task_id=task["task_id"], since=since)]


# The only agent verdicts that pass a target (after strip/lower-case). `approve` is A09's legacy alias of `pass`;
# request_changes, block, fail, the human-only waive, unknown and missing verdicts all fail (WR-17/WR-18).
PASS_VERDICTS = frozenset({"pass", "approve"})


def agent_verdict(entry: dict[str, Any]) -> str:
    """A gate agent's verdicts{} entry's verdict string, normalized (stripped, lower-case; "" when missing).
    validate_result guarantees the entry is an object and its verdict a string."""
    return str(entry.get("verdict") or "").strip().lower()


def agent_failed(entry: dict[str, Any]) -> bool:
    """Fail closed: an entry fails its target unless its verdict is in PASS_VERDICTS. Only for entries present in
    verdicts{}."""
    return agent_verdict(entry) not in PASS_VERDICTS


def _review_verdict_mismatch(store: TaskStore, task: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Sorted gate_for targets of a review gate task whose agent verdicts{} entry fails (agent_failed) while the review
    row its script issued during the current lease is not `fail`. A failing entry under a key that is not a gate_for
    id applies to every target. Targets without such a row are _gate_script_missing's concern."""
    notes = task["notes_json"]
    targets = list(notes.get("gate_for") or [])
    verdicts = result.get("verdicts")
    failing = {k for k, v in (verdicts if isinstance(verdicts, dict) else {}).items() if agent_failed(v)}
    since = _lease_start(store, task["task_id"])
    if not failing or since is None:
        return []
    accused = targets if failing - set(targets) else sorted(failing)
    out = []
    for t in accused:
        recorded = store.gate_verdict_value_since(t, gate=notes["gate"], gate_task_id=task["task_id"], since=since)
        if recorded is not None and recorded != "fail":
            out.append(t)
    return sorted(out)


# Severities that fail a review target (rev_gate's blocking set).
BLOCKING_SEVERITIES = frozenset(SEVERITIES[SEVERITIES.index(BLOCKING_SEVERITY):])


def _counts_major(finding: object) -> bool:
    """A finding under a failing entry counts as major or worse unless its severity is a known one below major
    (case-insensitive); non-object findings and missing/unknown severities count as major, as swarm_run normalizes."""
    if not isinstance(finding, dict):
        return True
    sev = finding.get("severity")
    if isinstance(sev, str) and sev.lower() in SEVERITIES:
        return sev.lower() in BLOCKING_SEVERITIES
    return True


def agent_findings(entry: dict[str, Any]) -> tuple[list[object], bool]:
    """(findings, synthesized) of a verdicts{} entry. A failing entry without a finding of major or worse gets one
    synthesized major `agent-verdict` finding appended (IN-15), so an agent's failure always fails its target instead of
    reducing to an empty list or to minor findings that rev_gate passes. Passing entries are returned unchanged."""
    items = list(entry.get("findings") or [])
    if not agent_failed(entry) or any(_counts_major(f) for f in items):
        return items, False
    verdict = agent_verdict(entry) or "no verdict"
    why = "without a major finding" if items else "without findings"
    return items + [{"severity": "major", "kind": "agent-verdict", "summary": f"gate agent reported {verdict!r} {why}"}], True


def _agent_verdict_feedback(store: TaskStore, task: dict[str, Any], result: dict[str, Any]) -> None:
    """A gate agent's verdicts{} is advisory text only (D-12): failing entries (agent_failed) become feedback on the
    gate task's own gate_for targets. Verdict rows are written solely by the gate scripts (swarm.verdicts)."""
    notes = task["notes_json"]
    gate = notes.get("gate")
    if not gate:
        return
    verdicts = result.get("verdicts")
    for target, v in (verdicts if isinstance(verdicts, dict) else {}).items():
        if target not in notes.get("gate_for", []) or not agent_failed(v):
            continue
        store.append_feedback(target, {"gate": gate, "source": "agent", "findings": agent_findings(v)[0]})


class _GateRefused(SwarmError):
    """A gate result that must be refused. A different SwarmError from the same write, such as an illegal
    transition, is not one of these and must keep propagating."""


def _gate_result_error(store: TaskStore, task: dict[str, Any], result: dict[str, Any]) -> _GateRefused | None:
    """Why this gate task's IN_REVIEW result must be refused, or None when the script's rows from this lease agree.

    Measured on `task` as it stands now, including lease history read from the store. A caller holding the write lock
    on a row it has just re-read therefore sees a re-lease that landed after an earlier check (T-05-30): the latest
    CLAIMED is the new lease, and a row the previous lease's script issued no longer counts."""
    notes = task["notes_json"]
    if not notes.get("gate"):
        return None
    tid = task["task_id"]
    missing = _gate_script_missing(store, task)
    if missing:
        return _GateRefused(ErrorCode.E_CONTRACT, f"gate script not run for {missing}", task_id=tid)
    if notes["gate"] != "review":
        return None
    mismatch = _review_verdict_mismatch(store, task, result)
    if not mismatch:
        return None
    return _GateRefused(
        ErrorCode.E_CONTRACT,
        f"review verdict mismatch: agent verdicts fail {mismatch} but the review rows recorded this "
        f"lease do not; re-run swarm_gate with a major finding for each of {mismatch}",
        task_id=tid, targets=mismatch)


def _refuse_gate(store: TaskStore, tid: str, err: SwarmError, mode: str, emit: Emit, *, attempt: int) -> str:
    """Refuse a gate result. Headless rejects `attempt`; ingest raises and does not claim or fail a new lease."""
    if mode == "headless":
        return reject(store, tid, attempt=attempt, reason=str(err), mode=mode, emit=emit)
    raise err


# Replay keys: the newest INGEST_KEYS_BOUND entries of `notes.ingest_keys` are kept. A replay is a retry of one delivery
# (a lost reply, a re-run of the same command), which comes within a few ingests of the original; 32 distinct results
# for one task is far beyond a task's heartbeats plus its rework loops (MAX_REWORK_LOOPS = 2). A replay older than the
# bound is applied again, which is today's behaviour: the bound only keeps `notes` from growing without limit.
INGEST_KEYS_BOUND = 32


def result_digest(result: dict[str, Any]) -> str:
    """sha256 hex of the result serialised with sorted keys, compact separators and ensure_ascii=False, UTF-8: the
    parameters of `envelope._canonical`. That function is not reused because it drops a top-level `sig` member,
    so two results that differ only in `sig` would share a key; a result is hashed whole."""
    body = json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(body).hexdigest()


def _stored_keys(task: dict[str, Any]) -> list[dict[str, Any]]:
    raw = task["notes_json"].get("ingest_keys")
    return [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []


def _replay_reply(task: dict[str, Any], *, attempt: int, digest: str) -> str | None:
    """The reply the first application of this (task, attempt, digest) returned, or None when it was not applied."""
    for e in _stored_keys(task):
        if e.get("task_id") == task["task_id"] and e.get("attempt") == attempt and e.get("digest") == digest:
            reply = e.get("reply")
            return reply if isinstance(reply, str) else None
    return None


def _record_key(store: TaskStore, tid: str, *, attempt: int, digest: str, reply: str) -> None:
    """Append the replay key, keeping the newest INGEST_KEYS_BOUND. The caller's write transaction is still open."""
    task = store.get(tid)
    kept = [e for e in _stored_keys(task) if not (e.get("attempt") == attempt and e.get("digest") == digest)]
    kept.append({"task_id": tid, "attempt": attempt, "digest": digest, "reply": reply})
    store.set_notes(tid, ingest_keys=kept[-INGEST_KEYS_BOUND:])


def apply_result(store: TaskStore, task: dict[str, Any], *, agent_id: str, result: dict[str, Any], meta: dict[str, Any],
                 emit: Emit, mode: str) -> str:
    """Apply a validated result to the assigned task only; returns the task's new state."""
    tid = task["task_id"]
    state = result["state"]
    if mode == "headless" and state == S.IN_PROGRESS.value:
        return reject(store, tid, attempt=task["attempt"], reason="E-CONTRACT: session ended in IN_PROGRESS",
                      mode=mode, emit=emit)
    if mode == "ingest" and task["state"] == S.BLOCKED.value:
        raise SwarmError(ErrorCode.E_CONTRACT, f"{tid} is BLOCKED; A01 releases it with orch_status --transition after approval",
                         task_id=tid)
    # Before a PLANNED/RETRY ingest claims a new lease: a refusal must leave that task unclaimed. The same check runs
    # again under the write lock at the transition, which is what closes the race below.
    if state == S.IN_REVIEW.value and task["notes_json"].get("gate"):
        err = _gate_result_error(store, task, result)  # D-12: only script rows satisfy a gate
        if err is not None:
            return _refuse_gate(store, tid, err, mode, emit, attempt=task["attempt"])
    if mode == "ingest" and task["state"] in (S.PLANNED.value, S.RETRY.value):
        with store.transaction():  # the dependency check and the claim see one snapshot
            if not store.deps_satisfied(store.get(tid)):
                raise SwarmError(ErrorCode.E_CONTRACT, f"{tid} has unmet dependencies", task_id=tid)
            store.transition(tid, S.CLAIMED, actor=agent_id, reason="claimed via task.result ingest")
            store.transition(tid, S.IN_PROGRESS, actor=agent_id, reason="lease started")
        emit("task.claimed", {"task_id": tid, "agent": agent_id, "mode": mode})
    current = store.get(tid)["state"]
    if state == S.BLOCKED.value:
        target, reason = S.BLOCKED, str(result.get("needs", "blocked"))[:500]
    elif state == S.FAILED.value:
        target, reason = S.FAILED, json.dumps(result.get("error", {}))[:500]
    elif state == S.IN_REVIEW.value:
        target, reason = S.IN_REVIEW, "agent reported IN_REVIEW"
    else:  # ingest heartbeat: only legal while (now) IN_PROGRESS
        target, reason = None, ""
        if current != S.IN_PROGRESS.value:
            raise SwarmError(ErrorCode.E_CONTRACT, f"illegal transition {current} → IN_PROGRESS for {tid}", task_id=tid)
    # The transition and its provenance (notes, artifact rows, gate-target feedback) commit in one
    # transaction: transition/set_notes/append_feedback join the outer block (TaskStore.transaction
    # nests) and add_artifact's INSERT + outputs UPDATE ride along, so a crash at any point leaves a
    # fully-applied or fully-rolled-back result — never a new state with stale notes or missing
    # artifacts. Lock order matches TaskStore.transition: state row + audit row first, provenance after.
    # An ingest also records its replay key (`notes.ingest_keys`) as the LAST write of that same transaction and
    # checks for it first, under the same write lock (see the module docstring, "Replay of an ingest").
    replayed: tuple[str, int, str] | None = None
    new_state = ""
    if target is not None:
        # Re-read under the same write lock as the transition. Between the check above and here another caller can
        # move a gate task through RETRY → CLAIMED → IN_PROGRESS; the state is IN_PROGRESS again, so the move to
        # IN_REVIEW would be legal, and the previous lease's row would approve the target (T-05-30).
        try:
            with store.transaction():
                fresh = store.get(tid)
                digest = result_digest(result)
                # A replay is skipped only where the state machine would accept the move today; an illegal one
                # (IN_REVIEW → IN_REVIEW, anything from DONE) falls through and is refused by `transition` as before.
                hit = _replay_reply(fresh, attempt=fresh["attempt"], digest=digest) if mode == "ingest" else None
                if hit is not None and target in LEGAL_TRANSITIONS[S(fresh["state"])]:
                    replayed = (hit, fresh["attempt"], digest)
                else:
                    if target is S.IN_REVIEW:
                        err = _gate_result_error(store, fresh, result)
                        if err is not None:
                            raise err
                    store.transition(tid, target, actor=agent_id, reason=reason)
                    store.set_notes(tid, result=result, meta=meta)
                    for o in result.get("outputs", []) or []:
                        store.add_artifact(tid, kind=o.get("kind", "artifact"), uri=o.get("uri", ""),
                                           version=str(o.get("version", "1")),
                                           digest=o.get("digest", ""), producer=agent_id)
                    if target is S.IN_REVIEW:
                        _agent_verdict_feedback(store, fresh, result)
                    new_state = store.get(tid)["state"]
                    if mode == "ingest":
                        _record_key(store, tid, attempt=fresh["attempt"], digest=digest, reply=new_state)
        except _GateRefused as err:
            # `task["attempt"]` is the attempt that produced this result. A retry increments attempt on the
            # way back to CLAIMED; failing the re-read row would kill that replacement lease.
            return _refuse_gate(store, tid, err, mode, emit, attempt=task["attempt"])
    else:
        # Heartbeat: no transition, but notes + artifacts still commit together.
        with store.transaction():
            fresh = store.get(tid)
            digest = result_digest(result)
            hit = _replay_reply(fresh, attempt=fresh["attempt"], digest=digest) if mode == "ingest" else None
            if hit is not None:
                replayed = (hit, fresh["attempt"], digest)
            else:
                store.set_notes(tid, result=result, meta=meta)
                for o in result.get("outputs", []) or []:
                    store.add_artifact(tid, kind=o.get("kind", "artifact"), uri=o.get("uri", ""),
                                       version=str(o.get("version", "1")),
                                       digest=o.get("digest", ""), producer=agent_id)
                new_state = store.get(tid)["state"]
                if mode == "ingest":
                    _record_key(store, tid, attempt=fresh["attempt"], digest=digest, reply=new_state)
    if replayed is not None:
        reply, attempt, digest = replayed
        emit("task.result.deduplicated", {"task_id": tid, "mode": mode, "attempt": attempt, "digest": digest, "reply": reply})
        return reply
    return new_state


def _stalled_gates(store: TaskStore, task: dict[str, Any], reasons: dict[str, str], corr: str) -> list[str]:
    """Sorted "gate:reason" for required gates missing for a reason other than fail that no live gate task (state
    outside DONE/CANCELLED/ESCALATED) of that gate covers: nothing is left to issue them, so the task would stall."""
    tid = task["task_id"]
    live = {g["notes_json"].get("gate") for g in store.list(correlation_id=corr)
            if tid in (g["notes_json"].get("gate_for") or [])
            and g["state"] not in (S.DONE.value, S.CANCELLED.value, S.ESCALATED.value)}
    return sorted(f"{g}:{r}" for g, r in reasons.items() if r != "fail" and g not in live)


RERUN_SUFFIX = re.compile(r"(\.r\d+)+$")


def _rerun_index(task: dict[str, Any]) -> int:
    """Rerun sequence of a gate task within its lineage: N for "<base>.rN", 0 for the original gate task."""
    m = re.search(r"\.r(\d+)$", task["task_id"])
    return int(m.group(1)) if m else 0


def _ancestors(task: dict[str, Any], rows: list[dict[str, Any]]) -> set[str]:
    """Every task id that `task` depends on, directly or through other tasks of `rows`."""
    by_id = {row["task_id"]: row for row in rows}
    seen: set[str] = set()
    frontier = list(task["depends_on"])
    while frontier:
        dep = frontier.pop()
        if dep in seen or dep not in by_id:
            continue
        seen.add(dep)
        frontier.extend(by_id[dep]["depends_on"])
    return seen


def reconcile(store: TaskStore, corr: str, emit: Emit, leases: LeaseBridge | None = None,
              handoffs: HandoffBridge | None = None, *, approve: bool = True,
              stop_requested: Callable[[], bool] | None = None) -> list[str]:
    """Apply A01 gate/rework rules to IN_REVIEW tasks; reopen gate tasks after rework; escalate stalled gates.

    `leases` is the runner's substrate_lease.LeaseBridge, or None (ingest, dry runs, substrate off). It is told about
    each transition here that changes who may hold the node: DONE completes it, CHANGES_REQUESTED releases and re-claims
    it for the rework, ESCALATED releases it (LEASE-07). `handoffs` is the runner's substrate_handoff.HandoffBridge, or
    None likewise. It writes a packet at each transition here that changes the accountable agent: a failing gate hands
    the rework to the producer, and ESCALATED hands the task on (HAND-01). The transition, packet and settlement share
    the keeper's per-task lock, so neither thread releases the lease before the handoff finishes.

    `stop_requested` (keyword-only, optional) is asked before each next IN_REVIEW target, never inside one: a target
    already admitted completes its transaction, packet, settlement and gate/rerun lineage first. Omitted, it is never
    called."""
    notes_log = []
    for t in store.list(correlation_id=corr, state=S.IN_REVIEW.value):
        if stop_requested is not None and stop_requested():
            break  # before the next target: every target already acted on is complete, lineage included
        tid = t["task_id"]
        latest = store.latest_verdicts(tid, verified_only=True)
        failing = [g for g in store.required_gates(tid) if latest.get(g, {}).get("verdict") == "fail"]
        if failing:
            before = t["rework_loops"]
            with leases.transition(tid) if leases is not None else nullcontext():
                with store.transaction():  # CHANGES_REQUESTED then IN_PROGRESS commit together: a crash between them strands the task
                    nt = store.transition(tid, S.CHANGES_REQUESTED, reason=f"gates failed: {failing}")
                    if nt["state"] != S.ESCALATED.value:
                        store.transition(tid, S.IN_PROGRESS, reason="rework loop")
                if nt["state"] == S.ESCALATED.value:
                    emit("escalation.request", {"task_id": tid, "reason_code": "E-CONTRACT", "evidence": failing,
                                                "options": ["human review", "cancel", "waive gate (L3)"]})
                    notes_log.append(f"{tid}: ESCALATED after {before} rework loops")
                    if handoffs is not None:
                        handoffs.escalated(tid, reason=f"the rework cap was reached with gates {failing} failing",
                                           failing=failing, verdicts=latest)
                    if leases is not None:
                        leases.settle(tid)
                else:
                    notes_log.append(f"{tid}: CHANGES_REQUESTED → rework #{nt['rework_loops']} ({failing})")
                    if handoffs is not None:  # the verdict rows read above: the transition made them stale
                        handoffs.gate_failed(tid, failing, latest, rework=nt["rework_loops"])
                    if leases is not None:  # the rework is a new hold, visible in the trail, not a silent re-run
                        leases.rework(tid)
            if nt["state"] != S.ESCALATED.value:
                # reopen gate tasks that target this task so they re-run after rework: one rerun per gate lineage
                # (<base>, <base>.r1, <base>.r2, ...), cloned from its latest member, never from a superseded one, and
                # numbered by the lineage (a multi-target gate's members are triggered by different targets). A leased
                # latest member runs against the pre-rework artifact, so it gets a rerun too (WR-04); a member that has
                # not started (PLANNED/RETRY) or is left to A01 (FAILED/BLOCKED/ESCALATED/CANCELLED) gets none
                # a completed scan (a non-gate task that depends on the target: netchk, polchk, res) holds a result from the
                # pre-rework artifact too, so it joins its lineage; the consumers that have not finished wait for its rerun
                lineages: dict[str, list[dict[str, Any]]] = {}
                for g in store.list(correlation_id=corr):
                    if tid in g["notes_json"].get("gate_for", []) or (not g["notes_json"].get("gate") and tid in g["depends_on"]
                                                                      and not store.required_gates(g["task_id"])):
                        lineages.setdefault(RERUN_SUFFIX.sub("", g["task_id"]), []).append(g)
                created: dict[str, tuple[str, set[str]]] = {}  # new rerun id → (its gate, its lineage's member ids)
                for base, members in lineages.items():
                    g = max(members, key=_rerun_index)
                    if g["state"] not in (S.DONE.value, S.IN_REVIEW.value, S.APPROVED.value,
                                          S.CLAIMED.value, S.IN_PROGRESS.value):
                        continue
                    new_id = f"{base}.r{_rerun_index(g) + 1}"
                    store.create(task_id=new_id, correlation_id=corr, capability=g["capability"],
                                 title=g["title"].removesuffix(" (rerun)") + " (rerun)",
                                 agent_id=g["agent_id"], dag_depth=g["dag_depth"], depends_on=g["depends_on"],
                                 acceptance=g["acceptance"], budget=g["budget"], risk_class=g["risk_class"],
                                 priority=g["priority"],
                                 notes={k: v for k, v in g["notes_json"].items() if k not in ("result", "meta", "ingest_keys")})
                    store.transition(new_id, S.VALIDATED)
                    store.transition(new_id, S.PLANNED, reason="gate rerun")
                    created[new_id] = (g["notes_json"].get("gate"), {m["task_id"] for m in members})
                # once every rerun of this rework exists: downstream of any lineage member waits for that lineage's
                # rerun. A release-gate rerun (it reads the other gate reruns' verdicts and the rollout plan) waits for the
                # other gate reruns and for the scan reruns upstream of it; a scan downstream of the release gate does not hold it
                rows = store.list(correlation_id=corr)
                for d in rows:
                    if d["state"] == S.DONE.value:
                        continue
                    release_rerun = created.get(d["task_id"], ("", set()))[0] == "release"
                    upstream = _ancestors(d, rows) if release_rerun else set()
                    extra = [nid for nid, (gate, mids) in created.items()
                             if nid != d["task_id"] and nid not in d["depends_on"]
                             and (mids.intersection(d["depends_on"])
                                  or (release_rerun and ((gate and gate != "release") or RERUN_SUFFIX.sub("", nid) in upstream)))]
                    if extra:
                        store.update(d["task_id"], depends_on=d["depends_on"] + extra)
            continue
        # CR-01: a leased (CLAIMED/IN_PROGRESS) gate task of a required gate for this target has no accepted result
        # yet, or its result was refused (review verdict mismatch): a row it recorded may be contradicted, so hold
        # the target until that gate task's result is accepted
        leased = {g["notes_json"].get("gate") for g in store.list(correlation_id=corr)
                  if tid in (g["notes_json"].get("gate_for") or [])
                  and g["state"] in (S.CLAIMED.value, S.IN_PROGRESS.value)}
        if leased.intersection(store.required_gates(tid)):
            continue
        # T-05-11: the hold outlives the lease. A passing row counts only once the gate task that recorded it has an
        # accepted result, so a refused gate task ingested FAILED/BLOCKED, or moved FAILED → RETRY, never approves
        # its targets; its live lineage waits (no stall), an ESCALATED/CANCELLED issuer escalates below
        reasons = store.missing_gate_reasons(tid)
        reasons.update(store.unaccepted_gates(tid, reasons))
        if not reasons:
            if not approve:
                emit("task.approval.skipped", {"task_id": tid, "reason": "advisory ingest: approval not attempted"},
                     task_id=tid)
                notes_log.append(f"{tid}: advisory, approval not attempted")
                continue
            with store.transaction():  # APPROVED without DONE would strand the task in APPROVED: both commit or neither
                store.transition(tid, S.APPROVED, reason="all required gates pass")
                store.transition(tid, S.DONE, reason="approved")
            notes_log.append(f"{tid}: DONE")
            if leases is not None:
                leases.settle(tid)
            continue
        # WR-08: a gate absent/expired/... with no gate task left to issue it would stall silently. Escalate once per
        # distinct stall (notes.gate_stall); no transition — A01 or a human decides.
        stall = _stalled_gates(store, t, reasons, corr)
        if stall != (t["notes_json"].get("gate_stall") or []):
            store.set_notes(tid, gate_stall=stall)
            if stall:
                emit("escalation.request", {"task_id": tid, "reason_code": "E-CONTRACT", "evidence": stall,
                                            "options": ["re-run gate", "human review", "cancel"]})
                notes_log.append(f"{tid}: gate stall {stall} → escalation.request")
    return notes_log
