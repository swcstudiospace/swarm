"""A15 documentation tools: deterministic checks over markdown text and supplied registries.

Mission: verify internal links and heading anchors, Keep-a-Changelog section order,
conventional-commit grouping, API and ADR render coverage, prose readability,
document staleness and runbook verification dates. Pure functions over the payload.

Tools: docs_link_targets, docs_anchor_check, docs_changelog_section_order,
docs_conventional_groups, docs_api_endpoint_coverage, docs_flesch_grade,
docs_stale_doc_age, docs_runbook_verified_ratio, docs_adr_render_coverage.
"""

from __future__ import annotations

import posixpath
import re
from collections import Counter
from collections.abc import Hashable, Iterable
from datetime import date
from urllib.parse import unquote
from typing import Any

from swarm.tools.support import respond

CHANGELOG_ORDER = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
CONVENTIONAL_SECTION = {"feat": "added", "fix": "fixed", "perf": "changed", "refactor": "changed"}
HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
ADR_RENDER_SCOPE = ("accepted", "superseded", "deprecated")

_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_PATH_PARAM = re.compile(r"\{[^{}/]+\}|:[A-Za-z_]\w*")
_CONVENTIONAL = re.compile(r"^([A-Za-z]+)(?:\([^()\s]+\))?(!)?: \S")
_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)*")
_SENTENCE_END = re.compile(r"[.!?]+")
_VOWEL_RUN = re.compile(r"[aeiouy]+")


def _unique[T: Hashable](items: Iterable[T]) -> list[T]:
    return list(dict.fromkeys(items))


def _duplicates[T: Hashable](items: Iterable[T]) -> list[T]:
    return [item for item, count in Counter(items).items() if count > 1]


def _invalid(field: str, message: str) -> dict[str, str]:
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_UNRESOLVABLE = "\0"  # never a listed file: a target whose encoded form hides a path separator


def _link_path(source: str, target: str) -> str | None:
    """Repo-relative path a link targets, None when the link leaves the repo, _UNRESOLVABLE when it cannot name a file.

    The query and fragment are dropped first, then every segment is percent-decoded (`my%20doc.md` is `my doc.md`).
    A segment that decodes to something containing `/`, `\\` or NUL (`..%2F..%2Fetc`) is data, not a separator, and
    never resolves, so encoding cannot be used to climb out of the tree. A literal or `%2e%2e` `..` is normalized as
    before and stays broken when it climbs above the root.
    """
    target = target.strip()
    if target.startswith("//") or _SCHEME.match(target):
        return None
    path = target.split("#", 1)[0].split("?", 1)[0]
    if not path:
        return source
    segments = []
    for segment in path.split("/"):
        decoded = unquote(segment)
        if "/" in decoded or "\\" in decoded or "\0" in decoded:
            return _UNRESOLVABLE
        segments.append(decoded)
    path = "/".join(segments)
    if path.startswith("/"):
        return posixpath.normpath(path.lstrip("/"))
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), path))


def _with_parents(files: set[str]) -> set[str]:
    dirs: set[str] = set()
    for path in files:
        parent = posixpath.dirname(path)
        while parent and parent not in dirs:
            dirs.add(parent)
            parent = posixpath.dirname(parent)
    return dirs


def docs_link_targets(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        files = {posixpath.normpath(path) for path in payload["files"]}
        known = files | _with_parents(files) | {"."}
        checked = external = 0
        broken: list[str] = []
        for link in payload["links"]:
            path = _link_path(posixpath.normpath(link["source"]), link["target"])
            if path is None:
                external += 1
                continue
            checked += 1
            if path not in known:
                broken.append(f"{link['source']} -> {link['target']}")
        return {
            "state": "SUCCESS",
            "checked": checked,
            "external": external,
            "broken_count": len(broken),
            "broken": broken,
        }

    return respond(payload, work)


def _slug(text: str) -> str:
    """GitHub-style heading slug: lowercase, spaces to hyphens, keep letters, digits, '-' and '_'."""
    return "".join(
        ("-" if ch == " " else ch) for ch in text.strip().lower() if ch == " " or ch.isalnum() or ch in "-_"
    )


def _heading_anchors(headings: list[str]) -> set[str]:
    """Anchors for ordered headings; repeated slugs get -1, -2 suffixes as GitHub does."""
    occurrences: dict[str, int] = {}
    anchors: set[str] = set()
    for text in headings:
        base = _slug(text)
        seen = occurrences.get(base, 0)
        anchors.add(base if seen == 0 else f"{base}-{seen}")
        occurrences[base] = seen + 1
    return anchors


def docs_anchor_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        anchors = _heading_anchors(payload["headings"])
        broken = [ref for ref in payload["references"] if ref.lstrip("#") and ref.lstrip("#") not in anchors]
        return {
            "state": "SUCCESS",
            "headings": len(payload["headings"]),
            "anchors": len(anchors),
            "checked": len(payload["references"]),
            "broken_count": len(broken),
            "broken": broken,
        }

    return respond(payload, work)


def docs_changelog_section_order(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        sections = payload["sections"]
        rank = {name: index for index, name in enumerate(CHANGELOG_ORDER)}
        known = [name for name in sections if name in rank]
        unknown = _unique(name for name in sections if name not in rank)
        duplicates = _unique(_duplicates(sections))
        out_of_order: list[str] = []
        highest = -1
        for name in known:
            if rank[name] < highest:
                out_of_order.append(name)
            highest = max(highest, rank[name])
        out_of_order = _unique(out_of_order)
        return {
            "state": "SUCCESS",
            "valid": not (unknown or duplicates or out_of_order),
            "known": len(known),
            "unknown": unknown,
            "duplicates": duplicates,
            "out_of_order": out_of_order,
        }

    return respond(payload, work)


def docs_conventional_groups(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        counts = {"added": 0, "changed": 0, "fixed": 0, "other": 0}
        breaking = 0
        unparsed: list[str] = []
        for subject in payload["subjects"]:
            match = _CONVENTIONAL.match(subject)
            if match is None:
                unparsed.append(subject)
                continue
            counts[CONVENTIONAL_SECTION.get(match.group(1).lower(), "other")] += 1
            if match.group(2):
                breaking += 1
        return {
            "state": "SUCCESS",
            "total": len(payload["subjects"]),
            "added": counts["added"],
            "changed": counts["changed"],
            "fixed": counts["fixed"],
            "other": counts["other"],
            "breaking": breaking,
            "unparsed": unparsed,
        }

    return respond(payload, work)


def _endpoint_key(text: str) -> tuple[str, str] | None:
    """(normalized key, original text) for 'METHOD /path'; path parameters collapse to {}."""
    parts = text.split(None, 1)
    if len(parts) != 2 or parts[0].upper() not in HTTP_METHODS or not parts[1].startswith("/"):
        return None
    path = _PATH_PARAM.sub("{}", parts[1].strip()).rstrip("/") or "/"
    return f"{parts[0].upper()} {path}", text.strip()


def _endpoint_map(entries: list[str]) -> dict[str, str] | None:
    """Normalized key mapped to first original text; None when an entry is malformed."""
    mapping: dict[str, str] = {}
    for text in entries:
        parsed = _endpoint_key(text)
        if parsed is None:
            return None
        mapping.setdefault(parsed[0], parsed[1])
    return mapping


def docs_api_endpoint_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        total = _endpoint_map(payload["total_endpoints"])
        if total is None:
            return _invalid("total_endpoints", "each entry must be 'METHOD /path' with a known HTTP method")
        documented = _endpoint_map(payload.get("documented_endpoints", []))
        if documented is None:
            return _invalid("documented_endpoints", "each entry must be 'METHOD /path' with a known HTTP method")
        covered = sum(1 for key in total if key in documented)
        covered_pct = round(covered * 100 / len(total), 2)
        return {
            "state": "SUCCESS",
            "total": len(total),
            "covered": covered,
            "covered_pct": covered_pct,
            "meets_target": covered_pct >= payload.get("target_pct", 100),
            "undocumented": [text for key, text in total.items() if key not in documented],
            "unmatched_documented": [text for key, text in documented.items() if key not in total],
        }

    return respond(payload, work)


def _syllables(word: str) -> int:
    lowered = word.lower()
    count = len(_VOWEL_RUN.findall(lowered))
    if lowered.endswith("e") and not lowered.endswith("le") and count > 1:
        count -= 1
    return max(1, count)


def docs_flesch_grade(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        text = payload["text"]
        words = _WORD.findall(text)
        if not words:
            return _invalid("text", "no words to score")
        sentences = max(1, sum(1 for segment in _SENTENCE_END.split(text) if _WORD.search(segment)))
        syllables = sum(_syllables(word) for word in words)
        words_per_sentence = len(words) / sentences
        syllables_per_word = syllables / len(words)
        grade = round(0.39 * words_per_sentence + 11.8 * syllables_per_word - 15.59, 1)
        reading_ease = round(206.835 - 1.015 * words_per_sentence - 84.6 * syllables_per_word, 1)
        return {
            "state": "SUCCESS",
            "words": len(words),
            "sentences": sentences,
            "syllables": syllables,
            "grade": grade,
            "reading_ease": reading_ease,
            "within_target": grade <= payload.get("max_grade", 9),
        }

    return respond(payload, work)


def docs_stale_doc_age(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        as_of = _parse_date(payload["as_of"])
        if as_of is None:
            return _invalid("as_of", "as_of must be a YYYY-MM-DD date")
        limit = payload["max_age_days"]
        stale: list[str] = []
        invalid: list[str] = []
        ages: list[int] = []
        for doc in payload["docs"]:
            verified = _parse_date(doc["last_verified"])
            if verified is None or verified > as_of:
                invalid.append(doc["path"])
                continue
            age = (as_of - verified).days
            ages.append(age)
            if age > limit:
                stale.append(doc["path"])
        total = len(payload["docs"])
        return {
            "state": "SUCCESS",
            "total": total,
            "checked": len(ages),
            "stale": stale,
            "stale_count": len(stale),
            "stale_pct": round(len(stale) * 100 / total, 2),
            "invalid": invalid,
            "oldest_days": max(ages, default=0),
        }

    return respond(payload, work)


def docs_runbook_verified_ratio(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        as_of = _parse_date(payload["as_of"])
        if as_of is None:
            return _invalid("as_of", "as_of must be a YYYY-MM-DD date")
        duplicates = _duplicates(rb["runbook_id"] for rb in payload["runbooks"])
        if duplicates:
            return _invalid("runbooks", f"duplicate runbook_id {duplicates[0]}")
        tier1 = [rb for rb in payload["runbooks"] if rb["tier"] == 1]
        if not tier1:
            return _invalid("runbooks", "no tier-1 runbook supplied")
        limit = payload["max_age_days"]
        unverified: list[str] = []
        verified_count = 0
        for rb in tier1:
            verified = _parse_date(rb["last_verified"])
            if verified is None or verified > as_of or (as_of - verified).days > limit:
                unverified.append(rb["runbook_id"])
            else:
                verified_count += 1
        verified_pct = round(verified_count * 100 / len(tier1), 2)
        return {
            "state": "SUCCESS",
            "tier1_total": len(tier1),
            "tier1_verified": verified_count,
            "tier1_verified_pct": verified_pct,
            "meets_target": verified_pct >= payload.get("target_pct", 95),
            "unverified": unverified,
        }

    return respond(payload, work)


def docs_adr_render_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        ids = [adr["id"] for adr in payload["adrs"]]
        duplicates = _duplicates(ids)
        if duplicates:
            return _invalid("adrs", f"duplicate ADR id {duplicates[0]}")
        in_scope = [adr["id"] for adr in payload["adrs"] if adr["status"] in ADR_RENDER_SCOPE]
        if not in_scope:
            return _invalid("adrs", "no accepted, superseded or deprecated ADR to render")
        rendered = _unique(payload["rendered"])
        rendered_set = set(rendered)
        known = set(ids)
        rendered_in_scope = sum(1 for adr_id in in_scope if adr_id in rendered_set)
        adr_rendered_pct = round(rendered_in_scope * 100 / len(in_scope), 2)
        return {
            "state": "SUCCESS",
            "in_scope": len(in_scope),
            "rendered_in_scope": rendered_in_scope,
            "adr_rendered_pct": adr_rendered_pct,
            "meets_target": adr_rendered_pct >= payload.get("target_pct", 100),
            "missing": [adr_id for adr_id in in_scope if adr_id not in rendered_set],
            "orphan_rendered": [adr_id for adr_id in rendered if adr_id not in known],
            "excluded": len(ids) - len(in_scope),
        }

    return respond(payload, work)


# --- v2.2 additions: runbook schema, digest staleness, terminology drift -----------------------------

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_QUOTE_PREFIX = re.compile(r"(?: {0,3}>[ \t]?)+")  # blockquote markers: `>`, `> `, `> >`, ...


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def docs_runbook_schema_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate runbook fragments against the executable schema A13 consumes (agents/A15-docs.md section 3)."""

    def work() -> dict[str, Any]:
        known = payload.get("known_actions")
        known_set = set(known) if known is not None else None
        seen_ids: set[str] = set()
        invalid: list[dict[str, str]] = []
        flagged: list[dict[str, str]] = []
        bad_ids: set[str] = set()

        def problem(runbook_id: str, field: str, reason: str) -> None:
            invalid.append({"runbook_id": runbook_id, "field": field, "reason": reason})
            bad_ids.add(runbook_id)

        for index, rb in enumerate(payload["runbooks"]):
            rid = rb["runbook_id"]
            if not rid.strip():
                return _invalid(f"runbooks[{index}].runbook_id", "runbook_id must not be blank")
            if rid in seen_ids:
                problem(rid, "runbook_id", f"duplicate runbook_id {rid}")
                continue
            seen_ids.add(rid)
            if _blank(rb.get("title")):
                problem(rid, "title", "title is missing")
            if _blank(rb.get("trigger")):
                problem(rid, "trigger", "trigger is missing")
            steps = rb.get("steps") or []
            if not steps:
                problem(rid, "steps", "runbook has no step")
            actions: dict[str, bool] = {}
            for number, step in enumerate(steps):
                action = step.get("action")
                if _blank(action):
                    problem(rid, f"steps[{number}].action", "step action is missing")
                    continue
                if known_set is not None and action not in known_set:
                    problem(rid, f"steps[{number}].action", f"unknown action {action}")
                actions[action] = actions.get(action, True) and step.get("reversible") is True
            for action in _unique(rb.get("whitelisted_for_automation") or []):
                if action not in actions:
                    problem(rid, "whitelisted_for_automation", f"action {action} is not a step of this runbook")
                elif not actions[action]:
                    flagged.append({"runbook_id": rid, "action": action})
            verified = rb.get("last_verified")
            if _blank(verified):
                problem(rid, "last_verified", "last_verified is missing")
            elif _parse_date(verified) is None:
                problem(rid, "last_verified", "last_verified must be a YYYY-MM-DD date")

        bad_ids.update(item["runbook_id"] for item in flagged)
        valid_count = len(seen_ids) - len(bad_ids)
        return {
            "state": "SUCCESS",
            "checked": len(seen_ids),
            "valid_count": valid_count,
            "valid": not invalid and not flagged,
            "invalid": invalid,
            "non_reversible_whitelisted": flagged,
        }

    return respond(payload, work)


def docs_digest_staleness(payload: dict[str, Any]) -> dict[str, Any]:
    """Staleness law (agents/A15-docs.md section 4.2): a doc is stale when its bound artifact digest differs from the current one."""

    def work() -> dict[str, Any]:
        current: dict[str, str] = {}
        for index, entry in enumerate(payload["current"]):
            artifact, digest = entry["artifact"], entry["digest"]
            if not artifact.strip():
                return _invalid(f"current[{index}].artifact", "artifact must not be blank")
            if not _DIGEST.fullmatch(digest):
                return _invalid(f"current[{index}].digest", "digest must be sha256: followed by 64 lowercase hex characters")
            if current.setdefault(artifact, digest) != digest:
                return _invalid("current", f"artifact {artifact} is listed with two different digests")
        paths = [doc["path"] for doc in payload["docs"]]
        duplicate = _duplicates(paths)
        if duplicate:
            return _invalid("docs", f"duplicate path {duplicate[0]}")
        stale: list[dict[str, str]] = []
        unbound: list[str] = []
        scopes: set[str] = set()
        for doc in payload["docs"]:
            artifact, bound = doc.get("bound_artifact"), doc.get("bound_digest")
            if _blank(artifact):
                unbound.append(doc["path"])
                continue
            if artifact not in current:
                reason = f"bound artifact {artifact} is not in the current set"
            elif _blank(bound):
                reason = "no bound_digest recorded"
            elif not _DIGEST.fullmatch(bound):
                reason = "bound_digest is malformed"
            elif bound != current[artifact]:
                reason = f"digest of {artifact} changed"
            else:
                continue
            stale.append({"path": doc["path"], "reason": reason})
            scopes.add(artifact)
        return {
            "state": "SUCCESS",
            "total": len(paths),
            "fresh_count": len(paths) - len(stale) - len(unbound),
            "stale": stale,
            "unbound": unbound,
            "stale_scopes": sorted(scopes),
        }

    return respond(payload, work)


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) outside fenced code blocks, with inline code spans blanked to keep columns.

    A fence closes only on a line of the same character and at least the opening length with no info string
    (CommonMark), so a four-backtick block may contain a three-backtick example. A fence inside a blockquote
    (`> ```md` ... `> ```) is a fence: the `>` markers are removed before matching, the closing fence must sit at
    the same quote depth, and the block ends where the quote ends. Limit: blockquotes are the only container
    tracked; a fence inside a list item is matched by its indentation alone.
    """
    lines: list[tuple[int, str]] = []
    marker = ""
    marker_depth = 0
    for number, line in enumerate(text.splitlines(), start=1):
        quote = _QUOTE_PREFIX.match(line)
        depth, inner = (quote.group(0).count(">"), line[quote.end():]) if quote else (0, line)
        if marker and depth < marker_depth:
            marker = ""  # the blockquote holding the fence ended, and the fence with it
        if marker:
            closing = _FENCE_CLOSE.match(inner)
            if depth == marker_depth and closing and closing.group(1)[0] == marker[0] and len(closing.group(1)) >= len(marker):
                marker = ""
            continue
        opening = _FENCE_OPEN.match(inner)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            marker, marker_depth = opening.group(1), depth
            continue
        lines.append((number, _INLINE_CODE.sub(lambda m: " " * len(m.group(0)), line)))
    return lines


MAX_REPORTED_VIOLATIONS = 500  # the list is a sample; violation_count and by_term stay exact (the registry also bounds output bytes)


def docs_terminology_drift(payload: dict[str, Any]) -> dict[str, Any]:
    """Glossary lint (agents/A15-docs.md sections 5 and 6): deprecated or variant terms per file and line."""

    def work() -> dict[str, Any]:
        owner: dict[str, str] = {}
        preferred_seen: set[str] = set()
        for index, entry in enumerate(payload["glossary"]):
            preferred = entry["preferred"].strip()
            if not preferred:
                return _invalid(f"glossary[{index}].preferred", "preferred must not be blank")
            if preferred.lower() in preferred_seen:
                return _invalid(f"glossary[{index}].preferred", f"duplicate preferred term {preferred}")
            preferred_seen.add(preferred.lower())
            for term in entry["deprecated"]:
                key = " ".join(term.lower().split())
                if not key:
                    return _invalid(f"glossary[{index}].deprecated", "deprecated term must not be blank")
                if key == preferred.lower():
                    return _invalid(f"glossary[{index}].deprecated", f"{term} equals its preferred term")
                if owner.setdefault(key, preferred) != preferred:
                    return _invalid(f"glossary[{index}].deprecated", f"{term} is deprecated for two preferred terms")
        paths = [item["path"] for item in payload["files"]]
        if _duplicates(paths):
            return _invalid("files", f"duplicate path {_duplicates(paths)[0]}")
        # One alternation, longest key first, so each line is scanned once whatever the glossary size. Capturing groups
        # per key would make the engine ~60x slower here (it loses the literal-prefix table), so the matched text is
        # resolved afterwards. Unicode case folding makes `ſ` match `s` and `İ` match `I`, which `.lower()` of the
        # match does not reproduce: that text is resolved by the first key (alternation order) that fully matches it.
        keys = sorted(owner, key=len, reverse=True)

        def key_regex(key: str) -> str:
            return r"\s+".join(map(re.escape, key.split()))

        pattern = (
            re.compile(r"(?<![A-Za-z0-9_])(?:" + "|".join(key_regex(key) for key in keys) + r")(?![A-Za-z0-9_])", re.IGNORECASE)
            if keys
            else None
        )
        # ASCII keys cannot fold onto each other, so a match whose lowered text is a key belongs to that key.
        ascii_keys = all(key.isascii() for key in keys)
        resolved: dict[str, str] = {}

        def preferred_for(found: str) -> str:
            cached = resolved.get(found)
            if cached is None:
                key = " ".join(found.lower().split())
                if ascii_keys and key in owner:
                    cached = owner[key]
                else:
                    cached = owner[next(k for k in keys if re.fullmatch(key_regex(k), found, re.IGNORECASE))]
                resolved[found] = cached
            return cached

        violations: list[dict[str, Any]] = []
        count = 0
        by_term: dict[str, int] = {}
        clean = 0
        for item in payload["files"]:
            before = count
            for number, line in _prose_lines(item["text"]):
                for m in pattern.finditer(line) if pattern is not None else ():
                    pref = preferred_for(m.group(0))
                    count += 1
                    if len(violations) < MAX_REPORTED_VIOLATIONS:
                        violations.append({"path": item["path"], "line": number, "found": m.group(0), "preferred": pref})
                    by_term[pref] = by_term.get(pref, 0) + 1
            clean += count == before
        return {
            "state": "SUCCESS",
            "files_checked": len(paths),
            "violation_count": count,
            "violations": violations,
            "violations_truncated": count > len(violations),
            "by_term": by_term,
            "pass_pct": round(clean * 100 / len(paths), 2),
        }

    return respond(payload, work)
