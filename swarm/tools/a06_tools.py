"""A06 frontend checks: pure analysis of heading, image, control, tab-order, bundle, prop, and target records.

Every input comes from the payload. Nothing reads the DOM, the filesystem, the network, or the clock.
Tools:
  fe_heading_sequence_check  heading level order and skipped levels
  fe_alt_text_check          image alt presence, emptiness, generic values, decorative conflicts
  fe_form_label_check        visible form controls without an accessible name
  fe_tab_order_check         positive tabindex use and focus-order inversions
  fe_bundle_budget_check     bundle bytes against a budget and against a baseline regression
  fe_prop_type_coverage      share of component props with a concrete declared type
  fe_target_size_check       interactive targets below the minimum CSS-pixel size
  fe_token_literal_scan      raw colour and spacing literals that are not design-token references
  fe_state_parity_check      implemented states against ux.spec states, exact per screen
  fe_cwv_budget_check        Core Web Vitals and Lighthouse scores against thresholds
  fe_client_security_scan    XSS sinks, inline scripts, assets without SRI, PII keys in web storage
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from swarm.tools.support import respond

GENERIC_ALT = frozenset({"image", "img", "photo", "picture", "icon", "graphic", "untitled"})
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
UNTYPED = frozenset({"", "any", "unknown"})


def _invalid(field: str, message: str) -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _duplicate(values: Iterable[object]) -> object:
    """Return the first value that appears twice, or None."""
    seen: set[object] = set()
    for value in values:
        if value in seen:
            return value
        seen.add(value)
    return None


def fe_heading_sequence_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Heading levels must start at h1 and never descend more than one level at a time."""

    def work() -> dict[str, Any]:
        levels = payload["levels"]
        first_ok = levels[0] == 1
        skips = [i for i in range(1, len(levels)) if levels[i] - levels[i - 1] > 1]
        return {
            "state": "SUCCESS",
            "heading_count": len(levels),
            "first_level_ok": first_ok,
            "skip_count": len(skips),
            "skip_indexes": skips,
            "sequence_ok": first_ok and not skips,
        }

    return respond(payload, work)


def fe_alt_text_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Non-decorative images need meaningful alt text; decorative images must not carry any."""

    def work() -> dict[str, Any]:
        images = payload["images"]
        dup = _duplicate(image["id"] for image in images)
        if dup is not None:
            return _invalid("images", f"duplicate image id {dup!r}")
        checked = missing = empty = generic = conflicts = 0
        flagged: list[str] = []
        for image in images:
            alt = image["alt"].strip()
            if image["decorative"]:
                if image["has_alt"] and alt:
                    conflicts += 1
                    flagged.append(image["id"])
                continue
            checked += 1
            if not image["has_alt"]:
                missing += 1
            elif not alt:
                empty += 1
            elif alt.lower() in GENERIC_ALT or alt.lower().endswith(IMAGE_SUFFIXES):
                generic += 1
            else:
                continue
            flagged.append(image["id"])
        return {
            "state": "SUCCESS",
            "image_count": len(images),
            "checked_count": checked,
            "missing_count": missing,
            "empty_count": empty,
            "generic_count": generic,
            "decorative_conflict_count": conflicts,
            "flagged_ids": flagged,
            "alt_ok": not flagged,
        }

    return respond(payload, work)


def fe_form_label_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Every control except type hidden needs a label, a non-blank aria-label, or an aria-labelledby reference."""

    def work() -> dict[str, Any]:
        controls = payload["controls"]
        dup = _duplicate(control["id"] for control in controls)
        if dup is not None:
            return _invalid("controls", f"duplicate control id {dup!r}")
        visible = [control for control in controls if control["type"] != "hidden"]
        unlabeled = [
            control["id"]
            for control in visible
            if not (control["has_label"] or control["aria_label"].strip() or control["aria_labelledby"])
        ]
        return {
            "state": "SUCCESS",
            "control_count": len(controls),
            "checked_count": len(visible),
            "hidden_skipped_count": len(controls) - len(visible),
            "unlabeled_count": len(unlabeled),
            "unlabeled_ids": unlabeled,
            "labels_ok": not unlabeled,
        }

    return respond(payload, work)


def fe_tab_order_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Positive tabindex values override DOM order; count them and the resulting focus-order inversions."""

    def work() -> dict[str, Any]:
        elements = payload["elements"]
        dup = _duplicate(element["id"] for element in elements)
        if dup is not None:
            return _invalid("elements", f"duplicate element id {dup!r}")
        positive = [element for element in elements if element["tabindex"] > 0]
        values = [element["tabindex"] for element in positive]
        inversions = sum(
            1 for i in range(len(values)) for j in range(i + 1, len(values)) if values[i] > values[j]
        )
        return {
            "state": "SUCCESS",
            "element_count": len(elements),
            "focusable_count": sum(1 for element in elements if element["tabindex"] >= 0),
            "positive_count": len(positive),
            "positive_ids": [element["id"] for element in positive],
            "inversion_count": inversions,
            "order_ok": not positive,
        }

    return respond(payload, work)


def fe_bundle_budget_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Total bundle bytes must fit the budget and must not grow past the regression limit over a baseline."""

    def work() -> dict[str, Any]:
        chunks = payload["chunks"]
        dup = _duplicate(chunk["name"] for chunk in chunks)
        if dup is not None:
            return _invalid("chunks", f"duplicate chunk name {dup!r}")
        total = sum(chunk["bytes"] for chunk in chunks)
        budget = payload["budget_bytes"]
        baseline = payload.get("baseline_bytes")
        limit = payload.get("regression_limit_pct", 5)
        checked = baseline is not None
        regression = round((total - baseline) * 100 / baseline, 2) if checked else 0.0
        breach = checked and regression > limit
        return {
            "state": "SUCCESS",
            "total_bytes": total,
            "headroom_bytes": budget - total,
            "utilization_pct": round(total * 100 / budget, 2),
            "over_budget": total > budget,
            "largest_chunk": max(chunks, key=lambda chunk: chunk["bytes"])["name"],
            "regression_checked": checked,
            "regression_pct": regression,
            "regression_breach": breach,
            "budget_ok": total <= budget and not breach,
        }

    return respond(payload, work)


def fe_prop_type_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Each component's props need a concrete declared type; untyped means blank, any, or unknown."""

    def work() -> dict[str, Any]:
        components = payload["components"]
        dup = _duplicate(component["name"] for component in components)
        if dup is not None:
            return _invalid("components", f"duplicate component name {dup!r}")
        min_ratio = payload.get("min_ratio", 1.0)
        total = typed = 0
        failing: list[str] = []
        for component in components:
            props = component["props"]
            dup_prop = _duplicate(prop["name"] for prop in props)
            if dup_prop is not None:
                return _invalid("components", f"{component['name']} repeats prop {dup_prop!r}")
            good = sum(1 for prop in props if prop["declared_type"].strip() not in UNTYPED)
            total += len(props)
            typed += good
            ratio = good / len(props) if props else 1.0
            if ratio < min_ratio:
                failing.append(component["name"])
        return {
            "state": "SUCCESS",
            "component_count": len(components),
            "prop_count": total,
            "typed_prop_count": typed,
            "coverage_ratio": round(typed / total, 4) if total else 1.0,
            "failing_components": failing,
            "components_ok": not failing,
        }

    return respond(payload, work)


def fe_target_size_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Interactive targets must be at least min_px wide and tall (WCAG 2.2 SC 2.5.8 default 24 CSS px)."""

    def work() -> dict[str, Any]:
        targets = payload["targets"]
        dup = _duplicate(target["id"] for target in targets)
        if dup is not None:
            return _invalid("targets", f"duplicate target id {dup!r}")
        minimum = payload.get("min_px", 24)
        small = [
            target["id"]
            for target in targets
            if target["width_px"] < minimum or target["height_px"] < minimum
        ]
        return {
            "state": "SUCCESS",
            "target_count": len(targets),
            "undersized_count": len(small),
            "undersized_ids": small,
            "size_ok": not small,
        }

    return respond(payload, work)


# ---------------------------------------------------------------- fe_token_literal_scan

DEFAULT_TOKEN_REFS = ("var(", "theme(", "token(", "$")
COLOR_FUNCTIONS = ("rgb", "rgba", "hsl", "hsla", "hwb", "lab", "lch", "oklab", "oklch", "color")
NAMED_COLORS = frozenset(
    "black white red green blue yellow orange purple pink gray grey silver maroon navy teal olive lime "
    "aqua fuchsia brown cyan magenta".split()
)
COLOR_PROPERTY_HINTS = ("color", "background", "border", "outline", "fill", "stroke", "shadow", "caret", "accent")
_HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_-])")
_COLOR_FN_RE = re.compile(r"(?<![\w-])(?:" + "|".join(COLOR_FUNCTIONS) + r")\(", re.IGNORECASE)
_LENGTH_RE = re.compile(r"(?<![\w.#-])(-?\d*\.?\d+)(px|rem|em|pt|vw|vh|ch)(?![\w-])", re.IGNORECASE)
_WORD_RE = re.compile(r"(?<![\w$#-])([a-zA-Z]+)(?![\w(-])")
_QUOTED_RE = re.compile(r"\"[^\"]*\"|'[^']*'")


def _strip_function_calls(value: str, prefix: str) -> str:
    """Remove every balanced ``prefix ... )`` group (an unbalanced group runs to the end of the value)."""
    lowered = value.lower()
    needle = prefix.lower()
    out: list[str] = []
    i = 0
    while i < len(value):
        at = lowered.find(needle, i)
        if at == -1:
            out.append(value[i:])
            break
        if at > 0 and (value[at - 1].isalnum() or value[at - 1] in "_-"):
            out.append(value[i : at + len(needle)])
            i = at + len(needle)
            continue
        out.append(value[i:at])
        depth = 1
        j = at + len(needle)
        while j < len(value) and depth:
            depth += (value[j] == "(") - (value[j] == ")")
            j += 1
        out.append(" ")
        i = j
    return "".join(out)


def _strip_token_refs(value: str, patterns: Iterable[str]) -> str:
    value = _QUOTED_RE.sub(" ", _strip_function_calls(value, "url("))
    for pattern in patterns:
        if pattern.endswith("("):
            value = _strip_function_calls(value, pattern)
        else:
            value = re.sub(re.escape(pattern) + r"[A-Za-z_][\w.-]*", " ", value)
    return value


def _literals(prop: str, value: str) -> tuple[int, int]:
    """(colour literal count, spacing literal count) of a value that has had its token references removed."""
    colors = len(_HEX_RE.findall(value)) + len(_COLOR_FN_RE.findall(value))
    if any(hint in prop for hint in COLOR_PROPERTY_HINTS):
        colors += sum(1 for word in _WORD_RE.findall(value) if word.lower() in NAMED_COLORS)
    spacing = sum(1 for number, _unit in _LENGTH_RE.findall(value) if float(number) != 0.0)
    return colors, spacing


def fe_token_literal_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Colour and spacing values must be design-token references; hex, rgb(), hsl(), and non-zero px/rem/em are literals."""

    def work() -> dict[str, Any]:
        declarations = payload["declarations"]
        dup = _duplicate(item["id"] for item in declarations)
        if dup is not None:
            return _invalid("declarations", f"duplicate declaration id {dup!r}")
        patterns = payload.get("token_ref_patterns", DEFAULT_TOKEN_REFS)
        blank = next((p for p in patterns if not p.strip()), None)
        if blank is not None:
            return _invalid("token_ref_patterns", "a token reference pattern must not be blank")
        flagged: list[str] = []
        color_total = spacing_total = 0
        for item in declarations:
            if not item["property"].strip() or not item["value"].strip():
                return _invalid("declarations", f"declaration {item['id']!r} needs a property and a value")
            bare = _strip_token_refs(item["value"], patterns)
            colors, spacing = _literals(item["property"].strip().lower(), bare)
            if colors or spacing:
                flagged.append(item["id"])
            color_total += colors
            spacing_total += spacing
        return {
            "state": "SUCCESS",
            "declaration_count": len(declarations),
            "literal_count": color_total + spacing_total,
            "literal_ids": flagged,
            "by_kind": {"color": color_total, "spacing": spacing_total},
            "tokens_ok": not flagged,
        }

    return respond(payload, work)


# ---------------------------------------------------------------- fe_state_parity_check


def fe_state_parity_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Implemented states must equal ux.spec states exactly; dotted sub-states such as form.error are distinct states."""

    def work() -> dict[str, Any]:
        screens = payload["screens"]
        dup = _duplicate(screen["id"] for screen in screens)
        if dup is not None:
            return _invalid("screens", f"duplicate screen id {dup!r}")
        missing: dict[str, list[str]] = {}
        extra: dict[str, list[str]] = {}
        for screen in screens:
            for key in ("spec_states", "implemented_states"):
                if any(not state.strip() for state in screen[key]):
                    return _invalid("screens", f"{screen['id']} has a blank state in {key}")
                repeated = _duplicate(screen[key])
                if repeated is not None:
                    return _invalid("screens", f"{screen['id']} repeats state {repeated!r} in {key}")
            spec = set(screen["spec_states"])
            built = set(screen["implemented_states"])
            if spec - built:
                missing[screen["id"]] = sorted(spec - built)
            if built - spec:
                extra[screen["id"]] = sorted(built - spec)
        return {
            "state": "SUCCESS",
            "screen_count": len(screens),
            "missing": dict(sorted(missing.items())),
            "extra": dict(sorted(extra.items())),
            "parity_ok": not missing and not extra,
        }

    return respond(payload, work)


# ---------------------------------------------------------------- fe_cwv_budget_check

CWV_LIMITS = {"lcp_s": 2.5, "inp_ms": 200, "cls": 0.1}
LIGHTHOUSE_MINIMUMS = {"perf": 90, "a11y": 100}


def fe_cwv_budget_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Core Web Vitals must be at or under their limits and Lighthouse scores at or over their minimums (limit-equal passes)."""

    def work() -> dict[str, Any]:
        cwv = payload["cwv"]
        lighthouse = payload["lighthouse"]
        overrides = payload.get("thresholds", {})
        limits = {**CWV_LIMITS, **{k: overrides[k] for k in CWV_LIMITS if k in overrides}}
        minimums = dict(LIGHTHOUSE_MINIMUMS)
        for key in ("perf", "a11y", "bp", "seo"):
            if f"{key}_min" in overrides:
                minimums[key] = overrides[f"{key}_min"]
        for key in ("bp", "seo"):
            if key in minimums and key not in lighthouse:
                return _invalid("lighthouse", f"thresholds.{key}_min needs a lighthouse.{key} score")
        breaches: list[dict[str, Any]] = []
        cwv_breaches = 0
        for metric in ("lcp_s", "inp_ms", "cls"):
            if cwv[metric] > limits[metric]:
                breaches.append({"metric": metric, "value": cwv[metric], "limit": limits[metric]})
                cwv_breaches += 1
        for metric in ("perf", "a11y", "bp", "seo"):
            if metric in minimums and lighthouse[metric] < minimums[metric]:
                breaches.append({"metric": metric, "value": lighthouse[metric], "limit": minimums[metric]})
        return {
            "state": "SUCCESS",
            "breaches": breaches,
            "breach_count": len(breaches),
            "cwv_ok": cwv_breaches == 0,
            "lighthouse_ok": len(breaches) == cwv_breaches,
            "perf_ok": not breaches,
        }

    return respond(payload, work)


# ---------------------------------------------------------------- fe_client_security_scan

MAX_SECURITY_FINDINGS = 500
_COMMENT_STARTS = ("//", "/*", "*", "<!--")
_LINE_RULES = (
    ("dangerous_html", re.compile(r"dangerouslySetInnerHTML|(?<![\w-])v-html(?![\w-])"), True),
    ("inner_html", re.compile(r"\.(?:inner|outer)HTML\s*(?:\+)?=(?!=)|\.insertAdjacentHTML\s*\("), True),
    ("eval", re.compile(r"(?<![\w.$])eval\s*\(|(?<![\w$])new\s+Function\s*\("), False),
    ("document_write", re.compile(r"\bdocument\s*\.\s*write(?:ln)?\s*\("), False),
)
_INLINE_HANDLER_RE = re.compile(r"""\son[a-z]{3,}\s*=\s*["']""")
_OPEN_TAG_RE = re.compile(r"<[A-Za-z][^<>]*>")
_TAG_RE = re.compile(r"<(script|link)\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([a-zA-Z:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
_EXECUTABLE_TYPES = ("", "module", "text/javascript", "application/javascript")
_STORAGE_SETITEM_RE = re.compile(r"""\b(?:localStorage|sessionStorage)\s*\.\s*setItem\s*\(\s*(["'`])([^"'`]+)\1""")
_STORAGE_INDEX_RE = re.compile(r"""\b(?:localStorage|sessionStorage)\s*\[\s*(["'`])([^"'`]+)\1\s*\]\s*=(?!=)""")
_STORAGE_PROP_RE = re.compile(r"\b(?:localStorage|sessionStorage)\s*\.\s*([A-Za-z_$][\w$]*)\s*=(?!=)")
_KEY_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
PII_WORDS = frozenset("email phone ssn dob birth address passport password passwd jwt iban".split())
PII_PAIRS = frozenset("creditcard cardnumber accesstoken refreshtoken idtoken authtoken".split())


def _pii_key(key: str) -> bool:
    words = [w.lower() for w in _KEY_WORD_RE.findall(key)]
    pairs = {a + b for a, b in zip(words, words[1:])}
    return bool(PII_WORDS & set(words) or PII_PAIRS & pairs)


def _attributes(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ATTR_RE.finditer(tag)}


def _scan_text(text: str, sanitizers: tuple[str, ...]) -> set[tuple[int, str]]:
    lines = text.splitlines()
    hits: set[tuple[int, str]] = set()
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith(_COMMENT_STARTS):
            continue
        window = " ".join(lines[number - 1 : number + 2])
        for kind, rule, sanitizable in _LINE_RULES:
            if rule.search(line) and not (sanitizable and any(name in window for name in sanitizers)):
                hits.add((number, kind))
        keys = [m.group(2) for m in _STORAGE_SETITEM_RE.finditer(line)]
        keys += [m.group(2) for m in _STORAGE_INDEX_RE.finditer(line)]
        keys += [m.group(1) for m in _STORAGE_PROP_RE.finditer(line)]
        if any(_pii_key(key) for key in keys):
            hits.add((number, "pii_storage"))
    for tag in _OPEN_TAG_RE.finditer(text):
        handler = _INLINE_HANDLER_RE.search(tag.group(0))
        if handler:
            number = text.count("\n", 0, tag.start() + handler.start()) + 1
            if not lines[number - 1].lstrip().startswith(_COMMENT_STARTS):
                hits.add((number, "inline_handler"))
    for match in _TAG_RE.finditer(text):
        attrs = _attributes(match.group(0))
        number = text.count("\n", 0, match.start()) + 1
        if match.group(1).lower() == "script":
            source = attrs.get("src")
            if source is None:
                if attrs.get("type", "").strip().lower() in _EXECUTABLE_TYPES:
                    hits.add((number, "inline_script"))
                continue
        else:
            source = attrs.get("href")
            if "stylesheet" not in attrs.get("rel", "").lower().split():
                continue
        external = source is not None and source.strip().lower().startswith(("http://", "https://", "//"))
        if external and not attrs.get("integrity", "").strip():
            hits.add((number, "external_no_sri"))
    return hits


def fe_client_security_scan(payload: dict[str, Any]) -> dict[str, Any]:
    """Flag XSS sinks, inline scripts and handlers, external assets without integrity, and PII keys in web storage."""

    def work() -> dict[str, Any]:
        files = payload["files"]
        dup = _duplicate(item["id"] for item in files)
        if dup is not None:
            return _invalid("files", f"duplicate file id {dup!r}")
        names = tuple(payload.get("sanitizer_names", ()))
        if any(not name.strip() for name in names):
            return _invalid("sanitizer_names", "a sanitizer name must not be blank")
        findings: list[dict[str, Any]] = []
        by_kind: dict[str, int] = {}
        for item in files:
            for number, kind in sorted(_scan_text(item["text"], names)):
                by_kind[kind] = by_kind.get(kind, 0) + 1
                findings.append({"id": item["id"], "kind": kind, "line": number})
        return {
            "state": "SUCCESS",
            "file_count": len(files),
            "findings": findings[:MAX_SECURITY_FINDINGS],
            "findings_truncated": len(findings) > MAX_SECURITY_FINDINGS,
            "by_kind": dict(sorted(by_kind.items())),
            "secure_ok": not findings,
        }

    return respond(payload, work)
