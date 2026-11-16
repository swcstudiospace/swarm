"""A11 DevOps / Platform Engineer lane tools.

Pure checks over payload data for the A11 decision logic: environment tier and
autonomy, production change gating, GitOps drift, cost overrun against the
120 percent rule, image reference immutability, and freeze windows. Nothing
here reads files, the network, the clock, or the environment.

Tools:
- devops_env_tier: classify an environment name as ephemeral, staging, or production.
- devops_change_gate: decide whether a change may proceed given its tier and evidence.
- devops_drift_count: count drifted keys and choose the reconcile action.
- devops_cost_overrun: compare actual cost with modeled cost under the 120 percent rule.
- devops_image_tag_check: flag image references not pinned by digest or version tag.
- devops_freeze_window: test whether a UTC instant falls inside a freeze window.
- devops_provenance_gate: accept or reject a build.artifact record by its provenance evidence.
- devops_deploy_gate: combine verdict, freeze, drift freeze, provenance and tier into one deploy decision.
- devops_rollback_target: choose retry, rollback or escalate after an IaC apply failure.
- devops_tf_blast_radius: summarise terraform show -json resource changes for a change ticket.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from swarm.tools.support import respond

_PRODUCTION_WORDS = frozenset({"prod", "production", "prd"})
_STAGING_WORDS = frozenset({"staging", "stg", "stage", "preprod"})
_EPHEMERAL_WORDS = frozenset({"ephemeral", "preview", "pr"})
_OVERRUN_PERCENT = 120
_FREEZE_RECONCILES = 3
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SEMVER = re.compile(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]+)?")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_MUTABLE_TAGS = frozenset(
    {
        "latest", "main", "master", "develop", "dev", "edge", "nightly", "stable",
        "head", "trunk", "current", "canary", "staging", "prod", "production",
    }
)


def env_tier(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        words = set(re.findall(r"[a-z]+", payload["env_name"].lower()))
        # Production wins over staging, and staging over ephemeral, so a mixed name fails closed.
        if words & _PRODUCTION_WORDS:
            tier = "production"
        elif words & _STAGING_WORDS:
            tier = "staging"
        elif words & _EPHEMERAL_WORDS:
            tier = "ephemeral"
        else:
            return {
                "state": "INVALID_INPUT",
                "field": "env_name",
                "message": "name has no production, staging, or ephemeral marker",
            }
        return {"state": "SUCCESS", "tier": tier, "autonomy": "L3" if tier == "production" else "L2"}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def change_gate(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        if payload["env_tier"] != "production":
            return {"state": "SUCCESS", "decision": "allowed", "missing": ""}
        evidence = {
            "change_ticket": payload.get("change_ticket", ""),
            "blast_radius": payload.get("blast_radius", ""),
            "rollback_plan": payload.get("rollback_plan", ""),
        }
        missing = [name for name, text in evidence.items() if not text.strip()]
        if missing:
            return {"state": "SUCCESS", "decision": "blocked", "missing": ", ".join(missing)}
        return {"state": "SUCCESS", "decision": "needs-human-approval", "missing": ""}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def _state_map(pairs: list[dict[str, Any]]) -> dict[str, str] | None:
    state: dict[str, str] = {}
    for pair in pairs:
        if pair["key"] in state:
            return None
        state[pair["key"]] = pair["value"]
    return state


def drift_count(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        want = _state_map(payload["desired"])
        if want is None:
            return {"state": "INVALID_INPUT", "field": "desired", "message": "keys must be unique"}
        have = _state_map(payload["actual"])
        if have is None:
            return {"state": "INVALID_INPUT", "field": "actual", "message": "keys must be unique"}
        missing = [key for key in want if key not in have]
        unexpected = [key for key in have if key not in want]
        changed = [key for key in want if key in have and want[key] != have[key]]
        drifted = sorted(set(missing) | set(unexpected) | set(changed))
        # drift_streak counts prior consecutive reconciles that still reported drift.
        if not drifted:
            action = "none"
        elif payload["drift_streak"] + 1 >= _FREEZE_RECONCILES:
            action = "freeze-and-escalate"
        else:
            action = "auto-reconcile"
        return {
            "state": "SUCCESS",
            "drift_count": len(drifted),
            "drifted_keys": ", ".join(drifted),
            "action": action,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def cost_overrun(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        modeled = payload["modeled_cost"]
        actual = payload["actual_cost"]
        if modeled <= 0:
            return {"state": "INVALID_INPUT", "field": "modeled_cost", "message": "must be above zero"}
        # Decimal(repr(...)) compares the decimal values the caller wrote, not binary float residue.
        overrun = Decimal(repr(actual)) * 100 > Decimal(repr(modeled)) * _OVERRUN_PERCENT
        try:
            ratio = round(actual / modeled, 4)
        except OverflowError:
            ratio = math.inf
        if not math.isfinite(ratio):
            return {"state": "INVALID_INPUT", "field": "modeled_cost", "message": "actual/modeled ratio is not finite"}
        return {
            "state": "SUCCESS",
            "ratio": ratio,
            "overrun": overrun,
            "action": "auto-downscale" if overrun else "none",
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def image_tag_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        ref = payload["image_ref"]
        if any(ch.isspace() for ch in ref):
            return {"state": "INVALID_INPUT", "field": "image_ref", "message": "must not contain whitespace"}
        if ref.startswith("oci://"):
            ref = ref[len("oci://"):]
        name, at, digest = ref.partition("@")
        if at and not _DIGEST.fullmatch(digest):
            return {
                "state": "INVALID_INPUT",
                "field": "image_ref",
                "message": "digest must be sha256 followed by 64 lowercase hex characters",
            }
        repo, _colon, tag = name.rsplit("/", 1)[-1].partition(":")
        if not repo:
            return {"state": "INVALID_INPUT", "field": "image_ref", "message": "repository name is empty"}
        # Version and commit tags pass on convention; the registry must enforce tag immutability for that to hold.
        if at:
            verdict, reason = "pass", "digest-pinned"
        elif not tag:
            verdict, reason = "fail", "no-tag"
        elif tag.lower() in _MUTABLE_TAGS:
            verdict, reason = "fail", "mutable-tag"
        elif _SEMVER.fullmatch(tag):
            verdict, reason = "pass", "version-tag"
        elif _COMMIT.fullmatch(tag):
            verdict, reason = "pass", "commit-sha-tag"
        else:
            verdict, reason = "fail", "unrecognized-tag"
        return {"state": "SUCCESS", "verdict": verdict, "reason": reason, "tag": tag}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def _instant(text: str) -> datetime | None:
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.utcoffset() is not None else None


def freeze_window(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        now = _instant(payload["now"])
        if now is None:
            return {
                "state": "INVALID_INPUT",
                "field": "now",
                "message": "needs an ISO 8601 timestamp with a UTC offset or Z",
            }
        index = -1
        remaining = 0
        for position, window in enumerate(payload["windows"]):
            start = _instant(window["start"])
            end = _instant(window["end"])
            if start is None or end is None or end <= start:
                return {
                    "state": "INVALID_INPUT",
                    "field": "windows",
                    "message": f"window {position} needs offset timestamps with end after start",
                }
            # Start is inclusive and end is exclusive; the first matching window wins.
            if index == -1 and start <= now < end:
                index = position
                remaining = int((end - now).total_seconds())
        return {"state": "SUCCESS", "frozen": index != -1, "window_index": index, "remaining_s": remaining}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _digest_pinned(image: str) -> bool:
    """True when the reference carries ``@sha256:<64 lowercase hex>`` (an ``oci://`` prefix is allowed)."""
    if any(ch.isspace() for ch in image):
        return False
    ref = image[len("oci://"):] if image.startswith("oci://") else image
    name, at, digest = ref.partition("@")
    return bool(name) and bool(at) and _DIGEST.fullmatch(digest) is not None


def provenance_gate(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        record = payload["record"]
        provenance = record.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        missing: list[str] = []
        warnings: list[str] = []
        attestation = provenance.get("attestation")
        if not isinstance(attestation, str) or not attestation.strip():
            missing.append("provenance.attestation")
        tree_digest = record.get("tree_digest")
        if not isinstance(tree_digest, str) or _DIGEST.fullmatch(tree_digest) is None:
            missing.append("tree_digest")
        # The schema leaves image and dirty untyped because devops_build_record.py writes null for both
        # (no --image, no git) and the registry validator has no nullable type: the types are checked here.
        image = record.get("image")
        if image is not None and not isinstance(image, str):
            return _invalid("record", "record.image must be a string or null")
        dirty = record.get("dirty")
        if dirty is not None and not isinstance(dirty, bool):
            return _invalid("record", "record.dirty must be true, false or null")
        if image is None or (isinstance(image, str) and not image.strip()):
            # devops_build_record.py makes --image optional: a build with no image is not an unpinned image.
            warnings.append("image-absent")
        elif not _digest_pinned(image):
            missing.append("image")
        if dirty is True:
            warnings.append("dirty-tree")
        elif dirty is None:
            warnings.append("dirty-unknown")
        builder = provenance.get("builder")
        if not isinstance(builder, str) or not builder.strip():
            warnings.append("builder-missing")
        if provenance.get("reproducible") is not True:
            warnings.append("not-reproducible")
        return {
            "state": "SUCCESS",
            "decision": "reject" if missing else "accept",
            "missing": missing,
            "warnings": warnings,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def deploy_gate(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        blockers: list[str] = []
        verdict = payload["security_verdict"]
        # Fail closed: only an explicit pass from A10 opens the gate; a missing verdict blocks like a fail.
        if verdict == "missing":
            blockers.append("security-verdict-missing")
        elif verdict != "pass":
            blockers.append("security-verdict-fail")
        if payload["frozen"]:
            blockers.append("freeze-window")
        if payload["drift_frozen"]:
            blockers.append("drift-freeze")
        if not payload["provenance_ok"]:
            blockers.append("provenance")
        if blockers:
            decision = "block"
        elif payload["env_tier"] == "production":
            decision = "needs-human-approval"
        else:
            decision = "allow"
        return {"state": "SUCCESS", "decision": decision, "blockers": blockers}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def rollback_target(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        seen: set[str] = set()
        best: tuple[datetime, str] | None = None
        for position, snapshot in enumerate(payload["snapshots"]):
            moment = _instant(snapshot["at"])
            if moment is None:
                return _invalid("snapshots", f"snapshot {position} needs an ISO 8601 timestamp with a UTC offset or Z")
            if snapshot["id"] in seen:
                return _invalid("snapshots", f"snapshot id {snapshot['id']!r} appears twice")
            seen.add(snapshot["id"])
            # Latest good snapshot wins; equal instants fall to the greater id so the answer ignores list order.
            if snapshot["applied_ok"] and (best is None or (moment, snapshot["id"]) > best):
                best = (moment, snapshot["id"])
        target = best[1] if best else ""
        if payload["attempts"] <= 1:
            action = "retry"
        elif target:
            action = "rollback"
        else:
            action = "escalate"
        return {"state": "SUCCESS", "action": action, "target_id": target}

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


_TF_SHAPES = {
    frozenset({"no-op"}): "noop",
    frozenset({"read"}): "noop",
    frozenset({"create"}): "create",
    frozenset({"update"}): "update",
    frozenset({"delete"}): "delete",
    frozenset({"create", "delete"}): "replace",
}
_NOTE_MAX = 2000
_NOTE_LISTED = 10


def _listed(items: list[str]) -> str:
    shown = ", ".join(items[:_NOTE_LISTED])
    return shown + (f", +{len(items) - _NOTE_LISTED} more" if len(items) > _NOTE_LISTED else "")


def tf_blast_radius(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        counts = {"create": 0, "update": 0, "delete": 0, "replace": 0}
        types: set[str] = set()
        destructive: list[str] = []
        for position, change in enumerate(payload["resource_changes"]):
            actions = change["actions"]
            kind = _TF_SHAPES.get(frozenset(actions)) if len(actions) == len(set(actions)) else None
            if kind is None:
                return _invalid(
                    "resource_changes",
                    f"change {position} ({change['address']}) has an unsupported action combination {actions!r}",
                )
            if kind == "noop":
                continue
            counts[kind] += 1
            types.add(change["type"])
            if kind in ("delete", "replace"):
                destructive.append(f"{change['address']} ({kind})")
        destructive.sort()
        total = sum(counts.values())
        if total == 0:
            note = "no resource changes"
        else:
            note = (
                f"{total} resource change(s): {counts['create']} create, {counts['update']} update, "
                f"{counts['delete']} delete, {counts['replace']} replace; types: {_listed(sorted(types))}"
            )
            if destructive:
                note += f"; destructive: {_listed(destructive)}"
        if len(note) > _NOTE_MAX:
            note = note[: _NOTE_MAX - 3] + "..."
        return {
            "state": "SUCCESS",
            "creates": counts["create"],
            "updates": counts["update"],
            "deletes": counts["delete"],
            "replaces": counts["replace"],
            "destructive": bool(destructive),
            "note": note,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict
