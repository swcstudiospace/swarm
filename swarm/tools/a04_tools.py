"""A04 UX Designer tools. Pure checks over design tokens and UX specs supplied in the payload.

- ux_token_name_check: token path convention (kebab-case dot paths, depth, roots, duplicates).
- ux_target_size_check: WCAG 2.2 SC 2.5.8 minimum target size with the spacing exception.
  Clearances are measured by the caller; overlap between undersized targets' circles is not computed.
- ux_scale_monotonic_check: tone scale strictly monotonic in WCAG relative luminance.
- ux_spacing_scale_check: spacing values as integer multiples of a base unit.
- ux_state_coverage_check: required component states covered by declared states (dotted sub-states cover their family).
- ux_focus_contrast_check: WCAG 2.2 SC 1.4.11 focus indicator contrast against each adjacent surface.
- ux_motion_duration_check: animation duration bound and reduced-motion variant presence.
- ux_breakpoint_order_check: responsive breakpoint names unique and minimum widths strictly ascending.
- ux_token_semver_impact: diff two flat token maps; the semver bump, next version and deprecation aliases still owed.
- ux_spec_completeness_check: one ux.spec against the output_format shape, required states, focus order and contrast floor.
- ux_ac_trace_check: two-way trace of acceptance-criterion ids to per-screen ux.spec acceptance_refs.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, TypeGuard, cast

from swarm.tools.support import respond

_HEX = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_SEGMENT = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_TARGET_PX = 24
_CLEARANCE_PX = _TARGET_PX / 2
_SCALE_EPS = 1e-9


def _rgb(hex_text: str) -> tuple[int, int, int]:
    match = _HEX.match(hex_text.strip())
    if match is None:
        raise ValueError(f"invalid hex colour {hex_text!r}")
    digits = match.group(1)
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def _luminance(hex_text: str) -> float:
    def linear(channel: int) -> float:
        c = channel / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v) for v in _rgb(hex_text))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(lum_a: float, lum_b: float) -> float:
    hi, lo = max(lum_a, lum_b), min(lum_a, lum_b)
    return (hi + 0.05) / (lo + 0.05)


def _token_problem(name: str, max_depth: int, roots: list[str] | None) -> str | None:
    segments = name.split(".")
    if len(segments) > max_depth:
        return f"depth {len(segments)} exceeds max_depth {max_depth}"
    for segment in segments:
        if not _SEGMENT.match(segment):
            return f"segment {segment!r} is not kebab-case"
    if roots and segments[0] not in roots:
        return f"root {segments[0]!r} is not in roots"
    return None


def token_name_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        names = payload["names"]
        max_depth = payload["max_depth"]
        roots = payload.get("roots")
        invalid = []
        for name in names:
            problem = _token_problem(name, max_depth, roots)
            if problem:
                invalid.append(f"{name}: {problem}")
        duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
        return {
            "state": "SUCCESS",
            "checked": len(names),
            "invalid": invalid,
            "duplicates": duplicates,
            "conforming": not invalid and not duplicates,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def target_size_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        targets = payload["targets"]
        repeated = sorted(tid for tid, count in Counter(t["id"] for t in targets).items() if count > 1)
        if repeated:
            return {"state": "INVALID_INPUT", "field": "targets", "message": f"duplicate ids: {', '.join(repeated)}"}
        meets, exception, failing = 0, [], []
        for target in targets:
            if target["width_px"] >= _TARGET_PX and target["height_px"] >= _TARGET_PX:
                meets += 1
            elif target["center_to_nearest_edge_px"] >= _CLEARANCE_PX:
                exception.append(target["id"])
            else:
                failing.append(target["id"])
        return {
            "state": "SUCCESS",
            "total": len(targets),
            "meets_minimum": meets,
            "spacing_exception": exception,
            "failing": failing,
            "compliant": not failing,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def scale_monotonic_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        steps = payload["steps"]
        luminances = []
        for step in steps:
            try:
                luminances.append(_luminance(step["hex"]))
            except ValueError as exc:
                return {"state": "INVALID_INPUT", "field": "steps", "message": f"step {step['step']!r}: {exc}"}
        falling = payload["order"] == "light_to_dark"
        violations = []
        for i in range(1, len(steps)):
            before, after = luminances[i - 1], luminances[i]
            ok = before > after if falling else before < after
            if not ok:
                violations.append(f"{steps[i - 1]['step']}->{steps[i]['step']}")
        return {
            "state": "SUCCESS",
            "checked": len(steps),
            "luminance": [round(v, 4) for v in luminances],
            "monotonic": not violations,
            "violations": violations,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def spacing_scale_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        base = payload["base_px"]
        multiples, off_scale = [], []
        for value in payload["values_px"]:
            units = value / base
            nearest = round(units)
            if abs(units - nearest) <= _SCALE_EPS:
                multiples.append(nearest)
            else:
                off_scale.append(value)
        return {
            "state": "SUCCESS",
            "checked": len(payload["values_px"]),
            "multiples": multiples,
            "off_scale": off_scale,
            "on_scale": not off_scale,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def _norm_state(state: str) -> str:
    return state.strip().lower()


def _covers(state: str, family: str) -> bool:
    return state == family or state.startswith(family + ".")


def state_coverage_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        required = list(dict.fromkeys(_norm_state(s) for s in payload["required_states"]))
        declared = [_norm_state(s) for s in payload["declared_states"]]
        if "" in required:
            return {"state": "INVALID_INPUT", "field": "required_states", "message": "required states must not be blank"}
        if "" in declared:
            return {"state": "INVALID_INPUT", "field": "declared_states", "message": "declared states must not be blank"}
        missing = [family for family in required if not any(_covers(s, family) for s in declared)]
        unexpected = [s for s in declared if not any(_covers(s, family) for family in required)]
        return {
            "state": "SUCCESS",
            "required_count": len(required),
            "covered_count": len(required) - len(missing),
            "missing": missing,
            "unexpected": unexpected,
            "complete": not missing,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def focus_contrast_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        try:
            focus = _luminance(payload["focus_hex"])
        except ValueError as exc:
            return {"state": "INVALID_INPUT", "field": "focus_hex", "message": str(exc)}
        surfaces = payload["surface_hexes"]
        try:
            ratios = [_contrast(focus, _luminance(hex_text)) for hex_text in surfaces]
        except ValueError as exc:
            return {"state": "INVALID_INPUT", "field": "surface_hexes", "message": str(exc)}
        floor = payload["required_ratio"]
        return {
            "state": "SUCCESS",
            "checked": len(ratios),
            "ratios": [round(r, 2) for r in ratios],
            "lowest_ratio": round(min(ratios), 2),
            "failing_surfaces": [hex_text for hex_text, r in zip(surfaces, ratios) if r < floor],
            "meets": all(r >= floor for r in ratios),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def motion_duration_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        limit = payload["max_duration_ms"]
        animations = payload["animations"]
        return {
            "state": "SUCCESS",
            "checked": len(animations),
            "max_observed_ms": max(a["duration_ms"] for a in animations),
            "over_limit": [a["name"] for a in animations if a["duration_ms"] > limit],
            "without_reduced_motion": [a["name"] for a in animations if not a["has_reduced_motion_variant"]],
            "compliant": all(a["duration_ms"] <= limit and a["has_reduced_motion_variant"] for a in animations),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def breakpoint_order_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        breakpoints = payload["breakpoints"]
        duplicates = sorted(name for name, count in Counter(b["name"] for b in breakpoints).items() if count > 1)
        out_of_order = [
            cur["name"]
            for prev, cur in zip(breakpoints, breakpoints[1:])
            if cur["min_width_px"] <= prev["min_width_px"]
        ]
        ordered = sorted(breakpoints, key=lambda b: (b["min_width_px"], b["name"]))
        return {
            "state": "SUCCESS",
            "count": len(breakpoints),
            "valid": not duplicates and not out_of_order,
            "starts_at_zero": breakpoints[0]["min_width_px"] == 0,
            "out_of_order": out_of_order,
            "duplicate_names": duplicates,
            "sorted_names": [b["name"] for b in ordered],
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


_SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_MAX_TOKENS = 5000
_FIDELITY = ("hifi", "lofi")
_A11Y_TARGETS = ("WCAG-2.2-AA", "WCAG-2.2-AAA")


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _flat_map_problem(field: str, tokens: dict[str, Any]) -> dict[str, Any] | None:
    """INVALID_INPUT for a token map that is too large, has blank names or non-scalar values; else None."""
    if len(tokens) > _MAX_TOKENS:
        return _invalid(field, f"more than {_MAX_TOKENS} tokens")
    for name, value in tokens.items():
        if not name.strip():
            return _invalid(field, "token names must not be blank")
        if value is None or not isinstance(value, (str, int, float, bool)):
            return _invalid(field, f"{name}: value must be a string, number or boolean (flatten composite tokens to dotted paths)")
    return None


def _token_differs(before: object, after: object) -> bool:
    return before != after or isinstance(before, bool) != isinstance(after, bool)


def token_semver_impact(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        old, new, aliases = payload["old"], payload["new"], payload.get("aliases") or {}
        for field, tokens in (("old", old), ("new", new)):
            problem = _flat_map_problem(field, tokens)
            if problem:
                return problem
        if len(aliases) > _MAX_TOKENS:
            return _invalid("aliases", f"more than {_MAX_TOKENS} aliases")
        for old_name, target in aliases.items():
            if not isinstance(target, str) or not target.strip():
                return _invalid("aliases", f"{old_name}: alias target must be a non-blank token name")
        match = _SEMVER.match(payload["current_version"])
        if not match:
            return _invalid("current_version", "must be MAJOR.MINOR.PATCH with numeric parts and no leading zeros")
        major, minor, patch = (int(part) for part in match.groups())
        added = sorted(name for name in new if name not in old)
        removed = sorted(name for name in old if name not in new)
        changed = sorted(name for name in old if name in new and _token_differs(old[name], new[name]))
        breaking = sorted(set(removed) | set(changed))
        unaliased = [name for name in removed if aliases.get(name) not in new]
        if breaking:
            bump, next_version = "major", f"{major + 1}.0.0"
        elif added:
            bump, next_version = "minor", f"{major}.{minor + 1}.0"
        else:
            bump, next_version = "patch", f"{major}.{minor}.{patch + 1}"
        return {
            "state": "SUCCESS",
            "added": added,
            "removed": removed,
            "changed": changed,
            "breaking": breaking,
            "unaliased_removals": unaliased,
            "required_bump": bump,
            "next_version": next_version,
            "l3_required": bool(breaking),
            "diff_empty": not (added or removed or changed),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def _nonblank(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _spec_findings(spec: dict[str, Any]) -> tuple[list[str], list[str], bool]:
    """(missing_fields, bad_enums, empty_acceptance_refs) for the output_format shape of one ux.spec."""
    missing: list[str] = []
    bad: list[str] = []
    if not _nonblank(spec.get("screen_id")):
        missing.append("screen_id")
    for key in ("flows", "states"):
        value = spec.get(key)
        if not (isinstance(value, list) and value and all(_nonblank(v) for v in value)):
            missing.append(key)
    components = spec.get("components")
    if not (isinstance(components, list) and components):
        missing.append("components")
    else:
        for index, item in enumerate(components):
            if not (isinstance(item, dict) and _nonblank(item.get("ref"))):
                missing.append(f"components[{index}].ref")
            elif "props" in item and not isinstance(item["props"], dict):
                missing.append(f"components[{index}].props")
    a11y = spec.get("a11y")
    if not isinstance(a11y, dict):
        missing.append("a11y")
    else:
        target = a11y.get("target")
        if not _nonblank(target):
            missing.append("a11y.target")
        elif target not in _A11Y_TARGETS:
            bad.append(f"a11y.target: expected one of {', '.join(_A11Y_TARGETS)}, got {target!r}")
        order = a11y.get("focus_order")
        if not (isinstance(order, list) and all(_nonblank(v) for v in order)):
            missing.append("a11y.focus_order")
        if not _is_number(a11y.get("min_contrast")):
            missing.append("a11y.min_contrast")
    version = spec.get("tokens_version")
    if not _nonblank(version):
        missing.append("tokens_version")
    elif not _SEMVER.match(version):
        bad.append(f"tokens_version: expected MAJOR.MINOR.PATCH, got {version!r}")
    fidelity = spec.get("fidelity")
    if not _nonblank(fidelity):
        missing.append("fidelity")
    elif fidelity not in _FIDELITY:
        bad.append(f"fidelity: expected one of {', '.join(_FIDELITY)}, got {fidelity!r}")
    if not isinstance(spec.get("provisional"), bool):
        missing.append("provisional")
    refs = spec.get("acceptance_refs")
    empty_refs = False
    if not isinstance(refs, list) or not all(_nonblank(r) for r in refs):
        missing.append("acceptance_refs")
    else:
        empty_refs = not refs
    return missing, bad, empty_refs


def spec_completeness_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        spec = payload["spec"]
        required = list(dict.fromkeys(_norm_state(s) for s in payload["required_states"]))
        if "" in required:
            return _invalid("required_states", "required states must not be blank")
        interactive = payload.get("interactive_ids")
        if interactive is not None:
            if any(not i.strip() for i in interactive):
                return _invalid("interactive_ids", "interactive ids must not be blank")
            repeated = sorted(i for i, count in Counter(interactive).items() if count > 1)
            if repeated:
                return _invalid("interactive_ids", f"duplicate ids: {', '.join(repeated)}")
        floor = payload.get("min_contrast_floor", 4.5)
        missing, bad, empty_refs = _spec_findings(spec)
        states = spec.get("states")
        declared = [_norm_state(s) for s in states if isinstance(s, str)] if isinstance(states, list) else []
        missing_states = [family for family in required if not any(_covers(s, family) for s in declared)]
        a11y = spec.get("a11y") if isinstance(spec.get("a11y"), dict) else {}
        contrast = a11y.get("min_contrast")
        below_floor = _is_number(contrast) and contrast < floor
        order = a11y.get("focus_order")
        order = [i for i in order if isinstance(i, str)] if isinstance(order, list) else []
        unknown: list[str] = []
        absent: list[str] = []
        if interactive is not None:
            unknown = [i for i in dict.fromkeys(order) if i not in set(interactive)]
            absent = [i for i in interactive if i not in set(order)]
        return {
            "state": "SUCCESS",
            "missing_fields": missing,
            "bad_enums": bad,
            "missing_states": missing_states,
            "focus_order_unknown": unknown,
            "focus_order_missing": absent,
            "empty_acceptance_refs": empty_refs,
            "below_floor": below_floor,
            "complete": not (missing or bad or missing_states or unknown or absent or empty_refs or below_floor),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


def ac_trace_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        ids = [i.strip() for i in payload["acceptance_ids"]]
        if "" in ids:
            return _invalid("acceptance_ids", "acceptance ids must not be blank")
        repeated = sorted(i for i, count in Counter(ids).items() if count > 1)
        if repeated:
            return _invalid("acceptance_ids", f"duplicate ids: {', '.join(repeated)}")
        screens = payload["screens"]
        screen_ids = [s["screen_id"].strip() for s in screens]
        if "" in screen_ids:
            return _invalid("screens", "screen ids must not be blank")
        repeated = sorted(i for i, count in Counter(screen_ids).items() if count > 1)
        if repeated:
            return _invalid("screens", f"duplicate ids: {', '.join(repeated)}")
        known = set(ids)
        cited: set[str] = set()
        unknown: list[str] = []
        without: list[str] = []
        for screen_id, screen in zip(screen_ids, screens):
            refs = [r.strip() for r in screen["acceptance_refs"]]
            if "" in refs:
                return _invalid("screens", f"{screen_id}: acceptance refs must not be blank")
            if not refs:
                without.append(screen_id)
            for ref in dict.fromkeys(refs):
                if ref in known:
                    cited.add(ref)
                else:
                    unknown.append(f"{screen_id}: {ref}")
        uncovered = [i for i in ids if i not in cited]
        return {
            "state": "SUCCESS",
            "uncovered_acs": uncovered,
            "unknown_refs": unknown,
            "screens_without_refs": without,
            "coverage_pct": round(100 * (len(ids) - len(uncovered)) / len(ids), 2),
            "traced": not (uncovered or unknown or without),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict
