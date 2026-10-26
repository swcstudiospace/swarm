"""A02 (REQ) requirements tools: story INVEST and ambiguity checks, duplicate-story detection,
WSJF ranking, acceptance-criteria testability, traceability and NFR coverage, change impact.

Tools: req_invest_score, req_ambiguity_scan, req_story_duplicates, req_wsjf_rank,
req_criteria_testability, req_traceability_coverage, req_change_impact, req_nfr_coverage,
req_pii_scan, req_clarification_window, req_conflict_matrix.
Pure functions over the payload; standard library only.
"""
from __future__ import annotations

import math
import re
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from typing import Any

from swarm.tools.support import respond

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")
_SENTENCE_END = re.compile(r"[.!?]+")
_MEASURABLE = re.compile(r"\d|\bthen\b")
_MODALS = frozenset({"could", "likely", "may", "might", "perhaps", "possibly", "should"})
_QUANTIFIERS = frozenset({"few", "many", "most", "numerous", "several", "some", "various"})
_VAGUE = frozenset({
    "adequate", "appropriate", "approximately", "easily", "easy", "efficient", "etc", "fast",
    "flexible", "good", "intuitive", "minimal", "quick", "quickly", "reasonable", "robust",
    "seamless", "simple", "soon", "sufficient", "timely", "typically", "usually",
})
_AMBIGUOUS = _MODALS | _QUANTIFIERS | _VAGUE
# Solution-prescribing markers: a story that names them is not negotiable (INVEST N).
_HOW_WORDS = frozenset({
    "api", "cache", "column", "endpoint", "graphql", "json", "kafka", "microservice", "mysql",
    "postgres", "queue", "redis", "sql", "table", "using", "via", "websocket",
})
_STOP = frozenset({
    "a", "an", "and", "as", "be", "can", "for", "from", "i", "in", "is", "it", "its", "must",
    "my", "of", "on", "or", "our", "should", "so", "that", "the", "to", "want", "will", "with",
})
_NFR_KINDS = ("perf", "security", "a11y", "i18n", "compliance")
_CLARIFY_RATIO = 0.4  # ambiguity score above this requires a clarification.request
_MANUAL_LIMIT = 0.3  # manual-only criteria share above this requires clarification
_SCOPE_LIMIT = 0.2  # committed scope change above this requires human approval
_SMALL_POINTS = 8  # INVEST Small: estimate at or below this
_DUP_THRESHOLD = 0.6  # default Jaccard similarity for duplicate stories
_WSJF_MIN_STORIES = 20  # WSJF policy applies above this many stories; MoSCoW otherwise


def _tokens(text: str) -> frozenset[str]:
    kept = set()
    for word in _WORD.findall(text.lower()):
        if word in _STOP:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        kept.add(word)
    return frozenset(kept)


def invest_score(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        words = _WORD.findall(payload["story"].lower())
        if not words:
            return {"state": "INVALID_INPUT", "field": "story", "message": "story must contain words"}
        flat = f" {' '.join(words)} "
        estimate = payload.get("estimate_points")
        checks = (
            ("I", not payload.get("dependencies")),
            ("N", _HOW_WORDS.isdisjoint(words)),
            ("V", " so that " in flat or " in order to " in flat),
            ("E", estimate is not None),
            ("S", estimate is not None and estimate <= _SMALL_POINTS),
            ("T", bool(payload.get("criteria"))),
        )
        failed = [letter for letter, ok in checks if not ok]
        return {"state": "SUCCESS", "score": len(checks) - len(failed), "failed": failed}

    return respond(payload, work)


def ambiguity_scan(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        lowered = payload["text"].lower()
        if not _WORD.search(lowered):
            return {"state": "INVALID_INPUT", "field": "text", "message": "text must contain words"}
        sentences = [part for part in _SENTENCE_END.split(lowered) if _WORD.search(part)]
        terms: set[str] = set()
        flagged = 0
        for sentence in sentences:
            hits = [word for word in _WORD.findall(sentence) if word in _AMBIGUOUS]
            if hits:
                flagged += 1
                terms.update(hits)
        ratio = flagged / len(sentences)
        return {
            "state": "SUCCESS",
            "score": round(ratio, 4),
            "sentence_count": len(sentences),
            "flagged_sentences": flagged,
            "terms": sorted(terms),
            "needs_clarification": ratio > _CLARIFY_RATIO,
        }

    return respond(payload, work)


def story_duplicates(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        stories = payload["stories"]
        ids = [story["id"] for story in stories]
        if len(set(ids)) != len(ids):
            return {"state": "INVALID_INPUT", "field": "stories", "message": "story ids must be unique"}
        threshold = payload.get("threshold", _DUP_THRESHOLD)
        # The normalised word sequence (stop words kept) makes identical stories duplicates whatever their vocabulary.
        sets = [
            (story["id"], _tokens(story["text"]), tuple(_WORD.findall(story["text"].lower())))
            for story in stories
        ]
        pairs = []
        for index, (left_id, left, left_words) in enumerate(sets):
            for right_id, right, right_words in sets[index + 1 :]:
                if left_words and left_words == right_words:
                    similarity = 1.0
                else:
                    union = left | right
                    if not union:
                        continue
                    similarity = len(left & right) / len(union)
                if similarity >= threshold:
                    pairs.append({"left": left_id, "right": right_id, "similarity": round(similarity, 4)})
        pairs.sort(key=lambda pair: (-pair["similarity"], pair["left"], pair["right"]))
        return {"state": "SUCCESS", "pair_count": len(pairs), "pairs": pairs}

    return respond(payload, work)


def wsjf_rank(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        items = payload["items"]
        ids = [item["id"] for item in items]
        if len(set(ids)) != len(ids):
            return {"state": "INVALID_INPUT", "field": "items", "message": "item ids must be unique"}
        scored = [
            (
                item["id"],
                (item["business_value"] + item["time_criticality"] + item["risk_reduction"]) / item["job_size"],
            )
            for item in items
        ]
        scored.sort(key=lambda row: (-row[1], row[0]))
        ranked = [
            {"id": item_id, "wsjf": round(value, 4), "rank": rank}
            for rank, (item_id, value) in enumerate(scored, start=1)
        ]
        return {
            "state": "SUCCESS",
            "method": "WSJF" if len(items) > _WSJF_MIN_STORIES else "MOSCOW",
            "top_id": ranked[0]["id"],
            "ranked": ranked,
        }

    return respond(payload, work)


def criteria_testability(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        criteria = payload["criteria"]
        ids = [criterion["id"] for criterion in criteria]
        if len(set(ids)) != len(ids):
            return {"state": "INVALID_INPUT", "field": "criteria", "message": "criterion ids must be unique"}
        total = len(criteria)
        automated = sum(1 for criterion in criteria if criterion["automated"])
        manual = total - automated
        unmeasurable = [
            criterion["id"]
            for criterion in criteria
            if not criterion["automated"] and not _MEASURABLE.search(criterion["text"].lower())
        ]
        ratio = manual / total
        return {
            "state": "SUCCESS",
            "total": total,
            "automated": automated,
            "manual_only": manual,
            "manual_ratio": round(ratio, 4),
            "unmeasurable_ids": unmeasurable,
            "needs_clarification": ratio > _MANUAL_LIMIT,
        }

    return respond(payload, work)


def traceability_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        requirements = payload["requirement_ids"]
        if len(set(requirements)) != len(requirements):
            return {"state": "INVALID_INPUT", "field": "requirement_ids", "message": "requirement ids must be unique"}
        known = set(requirements)
        links = payload["links"]
        covered = {link["requirement_id"] for link in links if link["requirement_id"] in known}
        artifacts = {link["artifact_id"] for link in links if link["requirement_id"] in known}
        unknown = sorted({link["requirement_id"] for link in links} - known)
        ratio = len(covered) / len(requirements)
        return {
            "state": "SUCCESS",
            "total": len(requirements),
            "covered": len(covered),
            "coverage": round(ratio, 4),
            "uncovered": [req for req in requirements if req not in covered],
            "unknown_requirement_ids": unknown,
            "linked_artifacts": len(artifacts),
            "meets_target": ratio >= payload.get("target", 1.0),
        }

    return respond(payload, work)


def change_impact(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        affected = set(payload["affected_ids"])
        committed = set(payload["committed_ids"])
        tasks = sorted({link["task_id"] for link in payload["task_links"] if link["requirement_id"] in affected})
        affected_committed = len(affected & committed)
        ratio = affected_committed / len(committed)
        return {
            "state": "SUCCESS",
            "tasks_at_risk": len(tasks),
            "affected_tasks": tasks,
            "affected_committed": affected_committed,
            "scope_change_ratio": round(ratio, 4),
            "needs_human_approval": ratio > _SCOPE_LIMIT,
        }

    return respond(payload, work)


def nfr_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        nfrs = payload["nfrs"]
        present = {nfr["kind"] for nfr in nfrs}
        required = list(dict.fromkeys(payload.get("required_kinds", _NFR_KINDS)))
        missing = [kind for kind in required if kind not in present]
        return {
            "state": "SUCCESS",
            "nfr_count": len(nfrs),
            "present_kinds": sorted(present),
            "missing_kinds": missing,
            "coverage": round((len(required) - len(missing)) / len(required), 4),
            "complete": not missing,
        }

    return respond(payload, work)


# --- req_pii_scan -------------------------------------------------------------------------------
# Detection order is also overlap priority: a span claimed by an earlier kind is never re-claimed.
_PII_KINDS = ("ssn", "card", "email", "ipv4", "phone")
_PII_PATTERNS = {
    "ssn": re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"),
    # A maximal run of digits joined by single spaces or hyphens; the card itself is a window inside it (_card_spans).
    "card": re.compile(r"(?<!\d)\d(?:[ -]?\d)*(?!\d)"),
    "email": re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"),
    "ipv4": re.compile(
        r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?!\.?\d)"
    ),
    "phone": re.compile(
        r"(?<![\w.+-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}(?![\w-])"
        r"|(?<![\w+])\+\d{10,14}(?!\d)"
        r"|(?<![\w+])\+\d{1,3}(?:[ .-]\d{2,4}){2,4}(?![\w-])"
    ),
}
_HEALTH_TERMS = re.compile(r"\b(?:patient|diagnos\w*|medical records?|prescriptions?|protected health|health conditions?)\b", re.I)
_REGIMES = ("GDPR", "CCPA", "HIPAA", "PCI-DSS")


_CARD_MIN_DIGITS, _CARD_MAX_DIGITS = 13, 19


def _card_spans(text: str, run_start: int, run_end: int) -> list[tuple[int, int]]:
    """Luhn-valid card windows inside one digit run, longest first, then leftmost.

    A window starts and ends on a digit-group boundary and holds 13 to 19 digits, so a card that follows or precedes
    other digit groups ("qty 2 4111 1111 1111 1111") is still found. Luhn sums come from prefix sums, O(1) per window.
    """
    groups = [(run_start + m.start(), run_start + m.end()) for m in re.finditer(r"\d+", text[run_start:run_end])]
    digits = [int(ch) for ch in text[run_start:run_end] if ch.isdigit()]
    doubled = [d * 2 - 9 if d > 4 else d * 2 for d in digits]
    # plain[p][k] / dbl[p][k]: sum over the first k digits whose index has parity p.
    plain = ([0], [0])
    dbl = ([0], [0])
    for index, (digit, twice) in enumerate(zip(digits, doubled)):
        for parity in (0, 1):
            hit = index % 2 == parity
            plain[parity].append(plain[parity][-1] + (digit if hit else 0))
            dbl[parity].append(dbl[parity][-1] + (twice if hit else 0))
    offsets = [0]
    for start, end in groups:
        offsets.append(offsets[-1] + (end - start))
    found: list[tuple[int, int, int]] = []
    for first in range(len(groups)):
        for last in range(first, len(groups)):
            count = offsets[last + 1] - offsets[first]
            if count > _CARD_MAX_DIGITS:
                break
            if count < _CARD_MIN_DIGITS:
                continue
            lo, hi = offsets[first], offsets[last + 1]
            parity = (hi - 1) % 2  # digits at this parity sit at even distance from the check digit
            total = (plain[parity][hi] - plain[parity][lo]) + (dbl[1 - parity][hi] - dbl[1 - parity][lo])
            if total % 10 == 0:
                found.append((count, groups[first][0], groups[last][1]))
    found.sort(key=lambda row: (-row[0], row[1]))
    return [(start, end) for _count, start, end in found]


class _Claimed:
    """Non-overlapping text spans, kept sorted so an overlap test is O(log n)."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []
        self.kinds: list[str] = []

    def overlaps(self, start: int, end: int) -> bool:
        index = bisect_right(self.starts, start)
        if index and self.ends[index - 1] > start:
            return True
        return index < len(self.starts) and self.starts[index] < end

    def add(self, start: int, end: int, kind: str) -> None:
        index = bisect_right(self.starts, start)
        self.starts.insert(index, start)
        self.ends.insert(index, end)
        self.kinds.insert(index, kind)


def pii_scan(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        text = payload["text"]
        if not text.strip():
            return {"state": "INVALID_INPUT", "field": "text", "message": "text must contain non-whitespace characters"}
        kinds = set(payload.get("kinds", _PII_KINDS))
        claimed = _Claimed()
        for kind in _PII_KINDS:
            if kind not in kinds:
                continue
            for match in _PII_PATTERNS[kind].finditer(text):
                if kind == "card":
                    for start, end in _card_spans(text, match.start(), match.end()):
                        if not claimed.overlaps(start, end):
                            claimed.add(start, end, kind)
                elif not claimed.overlaps(match.start(), match.end()):
                    claimed.add(match.start(), match.end(), kind)
        spans = list(zip(claimed.starts, claimed.ends, claimed.kinds))
        pieces: list[str] = []
        cursor = 0
        counts = dict.fromkeys(_PII_KINDS, 0)
        for start, end, kind in spans:
            pieces.append(text[cursor:start])
            pieces.append(f"[REDACTED:{kind.upper()}]")
            cursor = end
            counts[kind] += 1
        pieces.append(text[cursor:])
        found = [{"kind": kind, "count": counts[kind]} for kind in _PII_KINDS if counts[kind]]
        regimes = set()
        if counts["card"]:
            regimes.add("PCI-DSS")
        if counts["ssn"] or counts["email"] or counts["phone"] or counts["ipv4"]:
            regimes.update(("GDPR", "CCPA"))
        if _HEALTH_TERMS.search(text):
            regimes.add("HIPAA")
        return {
            "state": "SUCCESS",
            "found": found,
            "redacted_text": "".join(pieces),
            "has_pii": bool(found),
            "regimes_hint": [regime for regime in _REGIMES if regime in regimes],
        }

    return respond(payload, work)


# --- req_clarification_window -------------------------------------------------------------------
_DEFAULT_MAX_ROUNDS = 2  # clarification rounds before the spec must go PROVISIONAL
_DEFAULT_WINDOW_H = 48  # hours a stakeholder has to answer one round


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def clarification_window(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        now = _parse_timestamp(payload["now"])
        if now is None:
            return {"state": "INVALID_INPUT", "field": "now", "message": "now must be an ISO 8601 timestamp"}
        max_rounds = payload.get("max_rounds", _DEFAULT_MAX_ROUNDS)
        window_h = payload.get("window_h", _DEFAULT_WINDOW_H)
        if isinstance(window_h, bool) or not isinstance(window_h, (int, float)) or not math.isfinite(window_h) or window_h <= 0:
            return {"state": "INVALID_INPUT", "field": "window_h", "message": "window_h must be a finite number of hours above 0"}
        window = timedelta(hours=window_h)
        rounds = payload["rounds"]
        ids = [entry["id"] for entry in rounds]
        if len(set(ids)) != len(ids):
            return {"state": "INVALID_INPUT", "field": "rounds", "message": "round ids must be unique"}
        parsed = []
        for index, entry in enumerate(rounds):
            sent = _parse_timestamp(entry["sent_at"])
            if sent is None:
                return {"state": "INVALID_INPUT", "field": "rounds", "message": f"rounds[{index}].sent_at is not an ISO 8601 timestamp"}
            if sent > now:
                return {"state": "INVALID_INPUT", "field": "rounds", "message": f"rounds[{index}].sent_at is after now"}
            answered = None
            if "answered_at" in entry:
                answered = _parse_timestamp(entry["answered_at"])
                if answered is None:
                    return {"state": "INVALID_INPUT", "field": "rounds", "message": f"rounds[{index}].answered_at is not an ISO 8601 timestamp"}
                if answered < sent or answered > now:
                    return {"state": "INVALID_INPUT", "field": "rounds", "message": f"rounds[{index}].answered_at must lie between sent_at and now"}
            parsed.append((sent, entry["id"], answered))
        parsed.sort(key=lambda row: (row[0], row[1]))
        overdue = [rid for sent, rid, answered in parsed if answered is None and now - sent >= window]
        open_count = sum(1 for _, _, answered in parsed if answered is None)
        latencies = sorted((answered - sent).total_seconds() / 3600 for sent, _, answered in parsed if answered is not None)
        used = len(parsed)
        exhausted = used >= max_rounds
        result = {
            "state": "SUCCESS",
            "rounds_used": used,
            "answered": len(latencies),
            "open_within_window": open_count - len(overdue),
            "open_overdue": overdue,
            "exhausted": exhausted,
            "over_limit": used > max_rounds,
            "can_send_round": not exhausted,
            "must_go_provisional": exhausted and bool(overdue),
        }
        if latencies:
            middle = len(latencies) // 2
            median = latencies[middle] if len(latencies) % 2 else (latencies[middle - 1] + latencies[middle]) / 2
            result["p50_latency_h"] = round(median, 4)
        return result

    return respond(payload, work)


# --- req_conflict_matrix ------------------------------------------------------------------------
_BOUND_UPPER = frozenset({"lt", "le"})
_BOUND_LOWER = frozenset({"gt", "ge"})


def _finite_number(text: str) -> float | None:
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _same_value(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left["num"] is not None and right["num"] is not None:
        return bool(left["num"] == right["num"])
    return bool(left["value"].strip().casefold() == right["value"].strip().casefold())


def _violates(eq: dict[str, Any], bound: dict[str, Any]) -> bool:
    if eq["num"] is None:
        return False  # a non-numeric value cannot be compared with a numeric bound
    op, limit, value = bound["op"], bound["num"], eq["num"]
    return not {"lt": value < limit, "le": value <= limit, "gt": value > limit, "ge": value >= limit}[op]


def _clash(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    pair = {left["op"], right["op"]}
    if pair == {"eq"}:
        return None if _same_value(left, right) else "value"
    if pair == {"eq", "ne"}:
        return "polarity" if _same_value(left, right) else None
    if "eq" in pair and pair & (_BOUND_UPPER | _BOUND_LOWER):
        eq, bound = (left, right) if left["op"] == "eq" else (right, left)
        return "bounds" if _violates(eq, bound) else None
    if left["op"] in _BOUND_UPPER and right["op"] in _BOUND_LOWER:
        upper, lower = left, right
    elif left["op"] in _BOUND_LOWER and right["op"] in _BOUND_UPPER:
        upper, lower = right, left
    else:
        return None
    strict = upper["op"] == "lt" or lower["op"] == "gt"
    empty = lower["num"] > upper["num"] or (lower["num"] == upper["num"] and strict)
    return "bounds" if empty else None


def conflict_matrix(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        entries = payload["requirements"]
        ids = [entry["id"] for entry in entries]
        if len(set(ids)) != len(ids):
            return {"state": "INVALID_INPUT", "field": "requirements", "message": "requirement ids must be unique"}
        rows = []
        for index, entry in enumerate(entries):
            number = _finite_number(entry["value"])
            if entry["op"] in _BOUND_UPPER | _BOUND_LOWER and number is None:
                return {
                    "state": "INVALID_INPUT",
                    "field": "requirements",
                    "message": f"requirements[{index}].value must be a finite number for op {entry['op']}",
                }
            rows.append({**entry, "num": number, "index": index})
        conflicts: list[dict[str, Any]] = []
        blocked: set[str] = set()
        equal_rank: list[dict[str, Any]] = []
        for position, left in enumerate(rows):
            for right in rows[position + 1 :]:
                if (left["subject"].strip().casefold(), left["attribute"].strip().casefold()) != (
                    right["subject"].strip().casefold(),
                    right["attribute"].strip().casefold(),
                ):
                    continue
                kind = _clash(left, right)
                if kind is None:
                    continue
                conflicts.append(
                    {"left": left["id"], "right": right["id"], "subject": left["subject"], "attribute": left["attribute"], "kind": kind}
                )
                lp, rp = left.get("priority"), right.get("priority")
                if lp is not None and rp is not None and lp != rp:
                    blocked.add(left["id"] if lp > rp else right["id"])  # lower stakeholder rank (higher number) yields
                else:
                    blocked.update((left["id"], right["id"]))
                    if lp is not None and lp == rp:
                        equal_rank.append({"left": left["id"], "right": right["id"]})
        return {
            "state": "SUCCESS",
            "conflict_count": len(conflicts),
            "conflicts": conflicts,
            "blocked_ids": [rid for rid in ids if rid in blocked],
            "equal_rank": equal_rank,
            "needs_escalation": bool(equal_rank),
        }

    return respond(payload, work)
