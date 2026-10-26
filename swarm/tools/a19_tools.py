"""A19 event-driven and serverless tools: pure analyses over supplied schemas, descriptors and capacity facts.

Tools:
- event_schema_compat: backward/forward/full compatibility of a JSON Schema or Avro event schema pair.
- event_cloudevents_check: CloudEvents 1.0 required/optional context attribute validation.
- event_asyncapi_lint: structural lint of an AsyncAPI 2.x/3.x document (JSON text).
- event_partition_plan: partition count from throughput, consumer capacity and key skew.
- event_lag_assessment: consumer lag drain time, trend and SLO breach.
- event_retry_schedule: exponential backoff with jitter fitted inside visibility timeout and retry budget.
- event_dlq_coverage: dead-letter queue, max-receive-count, redrive and retention coverage per consumer.
- event_idempotency_window: dedup-window and key strategy versus the redelivery window.
- event_ordering_check: ordering guarantee of a broker versus the partition key and consumer behaviour.
- event_function_sizing: serverless function memory, timeout, concurrency, cold start and downstream limits.
- event_scaler_sanity: KEDA/HPA-style event autoscaler bounds, cooldown and polling sanity.
- event_saga_coverage: saga step/compensation coverage, pivot placement and transactional outbox.
- event_retention_replay: retention versus replay window, catch-up time and approval gate.

Every result is computed from the payload (no clock, network or filesystem). Findings are {id, severity, summary} with
severity blocker|major|minor|info; `verdict` is fail when any blocker or major exists, warn on minor only, else pass.

Embedded reference facts and their public sources:
- CloudEvents 1.0.2 core specification (github.com/cloudevents/spec, spec.md): required id, source, specversion, type;
  optional datacontenttype, dataschema, subject, time; attribute names are lower-case a-z0-9 and SHOULD be <= 20 chars;
  `type` SHOULD be prefixed with a reverse-DNS name; `time` is RFC 3339; `dataschema` is an absolute URI.
- Avro 1.11 specification, "Schema Resolution": promotions int->long|float|double, long->float|double, float->double,
  string<->bytes; record/enum names compare unqualified; a reader field without a writer field needs a default; an enum
  symbol unknown to the reader needs a reader enum default.
- AWS SQS developer guide: the visibility timeout is at most 12 h (43200 s) from when the message is first received, and
  extending it does not reset that limit; a FIFO queue needs a FIFO dead-letter queue; DLQ retention is measured from the
  original enqueue time, so it must exceed the source retention; maxReceiveCount is 1..1000.
- AWS Lambda developer guide: memory 128..10240 MB, timeout <= 900 s, one vCPU equivalent at 1769 MB, and an SQS source
  queue visibility timeout of at least six times the function timeout.
- Ordering scopes: Kafka per partition, Kinesis per shard (after a reshard the parent shard must be read to its end before
  its children), SQS/SNS FIFO per message group id, Pub/Sub per ordering key (same region, ordering enabled), RabbitMQ per
  queue with a single consumer (a redelivery can reorder); SQS/SNS standard topics and the classic EventBridge event bus
  give no ordering guarantee (the newer EventBridge Custom Event Bus orders per event group for FIFO subscribers and is
  not modelled). Kafka keeps producer order under retries with `enable.idempotence=true` (up to 5 in-flight requests) and
  may reorder with more than one in-flight request without it.
- Retry jitter variants: "full" is uniform in [0, d], "equal" is uniform in [d/2, d] (AWS Architecture Blog, "Exponential
  Backoff And Jitter").
- Saga structure: compensable steps, one pivot step, retriable steps (Richardson, "Microservices Patterns").
- JSON Schema content models follow Confluent Schema Registry semantics: with `additionalProperties` open (the default) an
  added property definition breaks backward compatibility (old data may carry that property with another type) but keeps
  forward compatibility; with a closed model it is the reverse.
- AsyncAPI 2.6.0 / 3.0.0 JSON Schemas (github.com/asyncapi/spec-json-schemas): required fields are info.title/info.version,
  2.x `channels` and server url+protocol, 3.x operation action+channel and server host+protocol; a 2.x operation without a
  `message` is valid.
- KEDA ScaledObject spec: `cooldownPeriod` is the wait after the last active trigger before scaling to 0; the Kafka scaler
  never scales beyond the partition count unless `allowIdleConsumers` is set.

House policies, not platform requirements (the severity is this lane's judgment, not a rule of AWS, Kafka or KEDA): the p99 > 80%
of the timeout margin, more than 20 receive attempts, a hot partition above 2x the mean load, the DLQ alarm and redrive runbook
requirements, reaction time as polling interval + replica startup, Little's law on p95, and the saga idempotence rules.
Secret-shaped input (URL credentials, cloud and VCS tokens, long hex or random runs, key=value credentials, mnemonics) is
never echoed into a finding or output list; the detection is a heuristic on the shape of the text.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, TypeGuard

from swarm.tools.support import respond

_SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
_MAX_FINDINGS = 300
_SUMMARY_MAX = 300
_MIB = Decimal(1048576)

SQS_MAX_VISIBILITY_S = 43200
SQS_MAX_RECEIVE_COUNT = 1000
LAMBDA_MEMORY_MB = (128, 10240)
LAMBDA_MAX_TIMEOUT_S = 900
LAMBDA_MB_PER_VCPU = Decimal(1769)
LAMBDA_SQS_VISIBILITY_FACTOR = 6
TIMEOUT_MARGIN = Decimal("0.8")  # policy default: p99 above 80% of the timeout leaves no safety margin
HIGH_RECEIVE_COUNT = 20  # policy default: more attempts than this keeps a poison message in flight for long
_NODE_BUDGET = 20000
_MAX_DEPTH = 150
_MAX_JSON_DEPTH = 100
_MAX_REF_HOPS = 64  # a $ref chain is followed this many hops; a longer chain is too long or cyclic
SKEW_WARN = Decimal(2)  # policy default: the hottest partition carrying more than 2x the mean load is flagged
_MAX_FINITE_S = Decimal("1e300")  # a duration beyond this cannot be reported as a finite JSON number


# --- shared helpers -----------------------------------------------------------------------------------------------


def _invalid(field: str, message: str) -> dict[str, str]:
    """The one INVALID_INPUT shape: {state, field, message}. `field` is an input property name."""
    return {"state": "INVALID_INPUT", "field": field, "message": message}


def _respond(payload: dict[str, Any], work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """`support.respond` is untyped, so its result is Any; this restates it as the handler's result type."""
    result: dict[str, Any] = respond(payload, work)
    return result


def _dec(value: object) -> Decimal:
    """Exact decimal of the number as written (shortest repr): 0.1 is 0.1, not its binary neighbour."""
    return Decimal(repr(value))


def _r(value: Decimal | float | int, digits: int = 4) -> float:
    return round(float(value), digits)


def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def _floor(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_FLOOR))


def _n(value: object) -> str:
    """Compact human number for finding text."""
    d = value if isinstance(value, Decimal) else _dec(value)
    if d == d.to_integral_value():
        return str(int(d))
    return format(round(float(d), 4), "f").rstrip("0").rstrip(".")


_SECRET_SHAPES = (
    re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^/\s]*@"),  # credentials in a URL
    re.compile(r"[^\s/@:]+:[^\s/@:]+@[^\s/@]+"),  # user:password@host
    re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA)[A-Z0-9]{12,}"),  # AWS access key ids
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{16,}|(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),  # GitHub tokens
    re.compile(r"(?<![A-Za-z0-9])xox[abeprs]-[A-Za-z0-9-]{8,}"),  # Slack tokens
    re.compile(r"(?<![A-Za-z0-9])(?:sk|rk|pk)[-_][A-Za-z0-9_-]{16,}"),  # API secret keys (sk-..., sk_live_...)
    re.compile(r"eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),  # JWT
    re.compile(r"(?i)(?<![a-z0-9])(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),  # authorization header value
    re.compile(r"(?i)(?<![a-z0-9])(?:pass(?:word|wd)?|secret|token|api[_-]?key)s?\s*[=:]\s*\S+"),  # key=value credentials
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    re.compile(r"(?<![0-9A-Za-z])(?:0x)?[0-9a-fA-F]{32,}(?![0-9A-Za-z])"),  # long hex: private keys, digests
    re.compile(r"\b(?:[a-z]{3,10}\s+){11,}[a-z]{3,10}\b"),  # twelve or more lowercase words: a mnemonic phrase
)
_LONG_RUN = re.compile(r"[A-Za-z0-9+/_=]{32,}")
_SCAN_LIMIT = 512  # longer text is never an identifier worth echoing, and scanning it is quadratic in several shapes


def _safe(value: object, limit: int = 80) -> str:
    """`value` as text for a finding. Input echoed back into a finding or an output list can be a credential someone
    pasted into a name or value field: text with a secret shape (URL credentials, cloud/VCS tokens, long hex or random
    runs, key=value credentials, a mnemonic) or longer than 512 characters is replaced by a length-only marker, and text
    longer than `limit` is cut. This is a heuristic: a secret with no recognisable shape is not detected, so values are
    only echoed where the finding needs them."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > _SCAN_LIMIT or any(shape.search(text) for shape in _SECRET_SHAPES) or any(
            sum(ch.isdigit() for ch in run.group()) >= 4 for run in _LONG_RUN.finditer(text)):
        return f"<redacted: {len(text)} chars>"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _wrap(raw: list[tuple[str, str]], prefix: str, pinned: Sequence[tuple[str, str]] = ()) -> tuple[str, list[dict[str, str]]]:
    """Sort and number raw (severity, text) findings; the verdict is derived before any truncation. `pinned` findings are kept
    whatever the truncation: they take room from the findings it keeps, so the output never exceeds _MAX_FINDINGS (300)."""
    ordered = sorted(set(raw), key=lambda item: (_SEVERITY_RANK[item[0]], item[1]))
    severities = {item[0] for item in ordered} | {item[0] for item in pinned}
    verdict = "fail" if severities & {"blocker", "major"} else "warn" if "minor" in severities else "pass"
    room = _MAX_FINDINGS - len(pinned)
    kept = ordered if len(ordered) <= room else ordered[: room - 1]
    chosen = sorted([*kept, *pinned], key=lambda item: (_SEVERITY_RANK[item[0]], item[1]))
    items = [{"id": f"{prefix}-{i:03d}", "severity": sev, "summary": text[:_SUMMARY_MAX]} for i, (sev, text) in enumerate(chosen, 1)]
    if len(ordered) > len(kept):
        items.append({"id": f"{prefix}-TRUNC", "severity": "info",
                      "summary": f"{len(ordered) - len(kept)} further findings omitted"})
    return verdict, items


def _norm(value: object) -> object:
    """JSON value normalised for equality: 1 equals 1.0, booleans stay distinct from numbers."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in value.items()}
    return value


def _canon(value: object) -> str:
    return json.dumps(_norm(value), sort_keys=True, separators=(",", ":"))


def _is_num(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Bad(Exception):
    """A semantic input problem: becomes INVALID_INPUT naming `field`."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field, self.message = field, message


def _depth_exceeds(value: object, limit: int) -> bool:
    """True when `value` nests deeper than `limit` containers (iterative: no recursion on hostile input)."""
    stack = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        children: Iterable[object] = node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()
        if children and depth > limit:
            return True
        stack.extend((child, depth + 1) for child in children if isinstance(child, (dict, list)))
    return False


def _load_json(text: str, field: str) -> Any:
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _Bad(field, f"{field} is not valid JSON: {exc}") from None
    if _depth_exceeds(value, _MAX_JSON_DEPTH):
        raise _Bad(field, f"{field} nests deeper than {_MAX_JSON_DEPTH} levels")
    return value


def _unpercent(text: str) -> str:
    """Percent-decoding of a URI fragment (RFC 3986), UTF-8; undecodable text is returned unchanged."""
    if "%" not in text:
        return text
    try:
        return re.sub(r"(?:%[0-9A-Fa-f]{2})+", lambda m: bytes.fromhex(m.group().replace("%", "")).decode("utf-8"), text)
    except UnicodeDecodeError:
        return text


def _anchor(root: Any, name: str) -> Any:
    """The subschema that declares `$anchor`/`$dynamicAnchor` `name` or an `$id` of `#name`; None when there is none."""
    stack, seen = [root], 0
    while stack and seen < 200000:
        node = stack.pop()
        seen += 1
        if isinstance(node, dict):
            if name in (node.get("$anchor"), node.get("$dynamicAnchor")) or node.get("$id") == f"#{name}":
                return node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]{0,8}")  # RFC 6901 array index: ASCII digits, no leading zero


def _pointer(root: Any, ref: str) -> Any:
    """Resolve a local reference ("#", "#/a/b" as an RFC 6901 pointer in a percent-encoded fragment, or "#anchor");
    None when it does not resolve. An empty reference token is a real (empty) key, not a skipped segment."""
    if not ref.startswith("#"):
        return None
    fragment = _unpercent(ref[1:])
    if fragment == "":
        return root
    if not fragment.startswith("/"):
        return _anchor(root, fragment)
    node = root
    for part in fragment[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and _ARRAY_INDEX.fullmatch(part) and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
    return node


# --- JSON Schema compatibility ------------------------------------------------------------------------------------

_JSON_TYPES = {"null", "boolean", "integer", "number", "string", "array", "object"}
_UNANALYSED = ("allOf", "not", "if", "then", "else", "patternProperties", "dependentRequired", "dependencies",
               "dependentSchemas", "propertyNames", "contains", "minContains", "maxContains", "prefixItems",
               "additionalItems", "unevaluatedProperties", "unevaluatedItems", "$dynamicRef")
_ANNOTATIONS = ("title", "description", "examples", "$comment", "default", "deprecated", "readOnly", "writeOnly", "$schema",
                "$id", "$anchor", "$defs", "definitions")


def _bound(node: dict[str, Any], low: bool) -> tuple[Decimal, bool] | None:
    """Tightest numeric bound of a schema as (value, exclusive) or None. A Draft-4 `exclusiveMinimum: true` flag makes
    `minimum` exclusive (likewise for the maximum)."""
    inclusive, exclusive = ("minimum", "exclusiveMinimum") if low else ("maximum", "exclusiveMaximum")
    candidates: list[tuple[Decimal, bool]] = []
    if _is_num(node.get(inclusive)):
        candidates.append((_dec(node[inclusive]), node.get(exclusive) is True))
    if _is_num(node.get(exclusive)):
        candidates.append((_dec(node[exclusive]), True))
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c[0], c[1])) if low else min(candidates, key=lambda c: (c[0], not c[1]))


def _value_fits(value: Any, types: set[str]) -> bool:
    """Can the JSON `value` be valid under a schema whose declared type set is `types`?"""
    if value is None:
        return "null" in types
    if isinstance(value, bool):
        return "boolean" in types
    if isinstance(value, int):
        return bool(types & {"integer", "number"})
    if isinstance(value, float):
        return "number" in types or ("integer" in types and value.is_integer())
    if isinstance(value, str):
        return "string" in types
    return "array" in types if isinstance(value, list) else "object" in types


def _position(items: Any, extra: Any, index: int) -> Any:
    """The schema of array position `index`: a tuple entry, the schema past the tuple, or the one `items` schema."""
    if not isinstance(items, list):
        return items
    return items[index] if index < len(items) else extra


_DRAFT_URI = re.compile(r"https?://json-schema\.org/(draft-0[3467]|draft/(?:2019-09|2020-12))/schema#?\Z")


def _draft_identity(declared: Any) -> Any:
    """The draft a `$schema` value names. Spellings of one draft (with or without the trailing `#`, http or https) give the same
    identity; any other value is its own identity, so unrecognised or different drafts stay apart."""
    match = _DRAFT_URI.match(declared) if isinstance(declared, str) else None
    return match.group(1) if match else declared


class _JsonCompare:
    """Does every instance valid under `sub` stay valid under `sup`? Sound for the supported keyword set: where it cannot
    decide (a keyword in `_UNANALYSED` that differs, a oneOf whose alternatives it cannot prove exclusive, an unresolvable
    pattern) it reports a major finding rather than a pass. Keywords of `sub` that are ignored only make it stricter.
    Siblings of a `$ref` apply in addition to its target on both sides in Draft 2019-09 and later; drafts 3 to 7 ignore them (see _draft)."""

    def __init__(self, base: Any, head: Any) -> None:
        self.roots = {"base": base, "head": head}
        self.nodes = 0
        self.stack: set[tuple[int, int]] = set()
        self.unanalysed: set[tuple[str, str]] = set()
        self.cache: dict[tuple[object, ...], Any] = {}  # derived schemas keep one identity, so the cycle check recognises them

    @staticmethod
    def _field(side: str) -> str:
        return f"{side}_schema"

    def _conjuncts(self, schema: Any, side: str, depth: int = 0) -> list[Any]:
        """The schemas whose conjunction `schema` denotes. A `$ref` contributes its target, and its sibling keywords apply
        in addition (Draft 2019-09 and later; drafts 3 to 7 ignore them, and an unrecognised draft refuses them). Booleans may appear in the list."""
        if depth > _MAX_REF_HOPS:
            raise _Bad(self._field(side), f"$ref chain exceeds {_MAX_REF_HOPS} hops: too long or cyclic")
        if isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
            target = _pointer(self.roots[side], schema["$ref"])
            if target is None:
                raise _Bad(self._field(side), f"$ref {_safe(schema['$ref'], 60)!r} does not resolve inside the document")
            if self._draft(side) == "legacy":
                return self._conjuncts(target, side, depth + 1)
            siblings = self._siblings(schema)
            if siblings and self._draft(side) in ("unknown", "undeclared"):
                raise _Bad(self._field(side), "the draft is not declared or not recognised, so the keywords beside a $ref cannot be applied "
                                              "with certainty: declare a $schema of draft 3 to 7 or 2019-09 or later")
            return [*self._conjuncts(target, side, depth + 1), *([siblings] if siblings else [])]
        if not isinstance(schema, (dict, bool)):
            raise _Bad(self._field(side), "every schema must be an object or a boolean")
        return [schema]

    def _siblings(self, schema: dict[str, Any]) -> dict[str, Any]:
        """The non-annotation keywords beside a `$ref`, cached per node so that the cycle check sees a stable identity."""
        key = ("siblings", id(schema))
        if key not in self.cache:
            self.cache[key] = ({k: v for k, v in schema.items() if k != "$ref" and k not in _ANNOTATIONS}, schema)
        siblings: dict[str, Any] = self.cache[key][0]
        return siblings

    def _draft(self, side: str) -> str:
        """The draft the root `$schema` declares: "legacy" (draft 3 to 7, where a $ref ignores its siblings), "modern" (2019-09 or
        2020-12, where they apply), "undeclared" (no `$schema`: a validator's default draft decides, so the siblings are not known to
        apply) or "unknown" (a `$schema` that is not a recognised draft)."""
        root = self.roots[side]
        declared = root.get("$schema") if isinstance(root, dict) else None
        if declared is None:
            return "undeclared"
        if not isinstance(declared, str):
            return "unknown"
        if any(f"draft-0{number}" in declared for number in "3467"):
            return "legacy"
        return "modern" if "draft/2019-09" in declared or "draft/2020-12" in declared else "unknown"

    def _merged(self, parts: list[Any]) -> Any:
        """A schema at least as weak as the conjunction of `parts`, so a proof for it proves the conjunction. None when
        if/then/else come from two parts, which cannot be merged keyword by keyword. Cached per list of parts."""
        key = ("merged", tuple(id(part) for part in parts))
        if key not in self.cache:
            self.cache[key] = (self._merge(parts), parts)
        return self.cache[key][0]

    @staticmethod
    def _merge(parts: list[Any]) -> dict[str, Any] | None:
        if sum(1 for part in parts if isinstance(part, dict) and {"if", "then", "else"} & part.keys()) > 1:
            return None
        merged: dict[str, Any] = {}
        for part in parts:
            for key, value in part.items():
                if key not in merged:
                    merged[key] = value
                elif key in ("properties", "patternProperties") and isinstance(merged[key], dict) and isinstance(value, dict):
                    merged[key] = {**merged[key], **value}  # a name keeps the constraint of the side that declares it
                elif key == "required" and isinstance(merged[key], list) and isinstance(value, list):
                    merged[key] = merged[key] + [name for name in value if name not in merged[key]]
                elif key == "additionalProperties":
                    merged[key] = False if merged[key] is False or value is False else value
                else:
                    merged[key] = value  # a keyword alone is implied by the conjunction it came from
        return merged

    def _flat(self, schema: Any, side: str) -> Any:
        """A schema at least as weak as `schema`, for the disjointness test; True when no proof is possible."""
        parts = [part for part in self._conjuncts(schema, side) if part is not True]
        if any(part is False for part in parts):
            return False
        if not parts:
            return True
        if len(parts) == 1:
            return parts[0]
        merged = self._merged(parts)
        return True if merged is None else merged

    def _types(self, node: dict[str, Any], side: str) -> set[str] | None:
        declared = node.get("type")
        if declared is None:
            return None
        names = [declared] if isinstance(declared, str) else declared
        if not isinstance(names, list) or not names or any(not isinstance(n, str) or n not in _JSON_TYPES for n in names):
            raise _Bad(self._field(side), f"unsupported JSON Schema type {_safe(declared, 40)!r}")
        return set(names)

    def _declared(self, side: str) -> object:
        """The draft identity of the root `$schema` of `side` (see _draft_identity), or None when it declares none."""
        root = self.roots[side]
        return _draft_identity(root.get("$schema")) if isinstance(root, dict) else None

    def _same(self, sub: dict[str, Any], ss: str, sup: dict[str, Any], ps: str) -> bool:
        """Both subtrees are textually identical, carry no `$ref` (which may resolve differently on each side), and are read under
        the same declared draft: identical text means different things under draft 4 and draft 7 (`const`, a boolean
        `exclusiveMinimum`), so the shortcut is taken only when both roots declare the same `$schema`."""
        text = _canon(sub)
        return text == _canon(sup) and "$ref" not in text and self._declared(ss) == self._declared(ps)

    def compare(self, sub: Any, sub_side: str, sup: Any, sup_side: str, path: str, out: list[tuple[str, str]]) -> None:
        """Every instance valid under `sub` is valid under `sup`; findings cover the parts that are not provably valid. Both
        sides are conjunctions (see _conjuncts). The source is accepted against one target part when one of its conjuncts
        alone is, or when their merged superset is; otherwise the findings of the merged superset are reported."""
        self.nodes += 1
        if self.nodes > _NODE_BUDGET:
            raise _Bad(self._field(sub_side), "schema comparison exceeds the node budget; split the schema")
        sub_all = self._conjuncts(sub, sub_side)
        if any(part is False for part in sub_all):
            return  # the source accepts nothing
        sub_parts = [part for part in sub_all if part is not True]
        for part in self._conjuncts(sup, sup_side):
            if part is True:
                continue
            if part is False:
                out.append(("blocker", f"{path}: no longer accepted (the target schema is `false`)"))
                return
            merged: dict[str, Any] | None = None
            candidates: list[Any]
            if not sub_parts:
                candidates = [{}]
            elif len(sub_parts) == 1:
                candidates = sub_parts
            else:
                merged = self._merged(sub_parts)
                candidates = sub_parts if merged is None else [*sub_parts, merged]
            trials: list[list[tuple[str, str]]] = []
            accepted = False
            for candidate in candidates:
                trial: list[tuple[str, str]] = []
                key = (id(candidate), id(part))
                if key not in self.stack:  # a pair met again is a $ref cycle, already being decided further up
                    self.stack.add(key)
                    if len(self.stack) > _MAX_DEPTH:
                        raise _Bad(self._field(sub_side), f"schema nesting exceeds {_MAX_DEPTH} levels after resolving $ref")
                    try:
                        self._body(candidate, sub_side, part, sup_side, path, trial)
                    finally:
                        self.stack.discard(key)
                if not trial:
                    accepted = True
                    break
                trials.append(trial)
            if accepted:
                continue
            if merged is None and len(sub_parts) > 1:
                out.append(("major", f"{path}: the $ref siblings combine if/then/else, which cannot be merged: "
                                     "compatibility is not proven"))
            else:
                out.extend(trials[-1])

    def _body(self, sub: dict[str, Any], ss: str, sup: dict[str, Any], ps: str, path: str, out: list[tuple[str, str]]) -> None:
        for keyword in _UNANALYSED:
            if keyword in sup:
                text = _canon(sup[keyword])
                if keyword not in sub or "$ref" in text or _canon(sub[keyword]) != text:
                    out.append(("major", f"{path}: keyword {keyword!r} differs between the schemas and cannot be analysed: "
                                         "compatibility is not proven"))
                else:
                    self.unanalysed.add((path, keyword))
            elif keyword in sub:
                self.unanalysed.add((path, keyword))
        sub_alts = next((sub[k] for k in ("anyOf", "oneOf") if isinstance(sub.get(k), list)), None)
        if sub_alts is not None:
            if self._same(sub, ss, sup, ps):
                return
            # the source is the union of its alternatives, and each one keeps the source keywords beside anyOf/oneOf
            beside = self._beside(sub)
            for index, alt in enumerate(sub_alts):
                node = self._with_siblings(beside, alt, ss, f"{path}<alt{index}>", out)
                if node is not None:
                    self.compare(node, ss, sup, ps, f"{path}<alt{index}>", out)
            return
        for key in ("anyOf", "oneOf"):
            if isinstance(sup.get(key), list):
                self._alternatives(sub, ss, sup[key], ps, key, path, out)
        st, pt = self._types(sub, ss), self._types(sup, ps)
        if pt is not None:
            widened = pt | ({"integer"} if "number" in pt else set())
            if st is None:
                out.append(("blocker", f"{path}: source accepts any type but target requires {sorted(pt)}"))
            elif not st <= widened:
                out.append(("blocker", f"{path}: type {sorted(st)} is not accepted by target type {sorted(pt)}"))
        can: Callable[..., bool] = lambda *names: st is None or bool(st & set(names))  # noqa: E731 - tiny local predicate
        self._enum(sub, ss, sup, ps, path, out)
        if can("number", "integer"):
            self._range(sub, sup, path, out)
            self._multiple(sub, sup, st, path, out)
        if can("string"):
            self._length(sub, sup, "minLength", "maxLength", "string length", path, out)
            if "pattern" in sup and sub.get("pattern") != sup["pattern"]:
                out.append(("major", f"{path}: pattern changed; old values cannot be proven to match the target pattern"))
            if "format" in sup and sub.get("format") != sup["format"]:
                out.append(("minor", f"{path}: format changed to {_safe(sup['format'], 30)!r} (annotation unless the validator "
                                     "asserts formats)"))
        if can("array"):
            self._array(sub, ss, sup, ps, path, out)
        if can("object"):
            self._object(sub, ss, sup, ps, path, out)

    def _beside(self, node: dict[str, Any]) -> dict[str, Any]:
        """The keywords of `node` other than its anyOf/oneOf, cached per node so that the cycle check sees a stable identity."""
        key = ("beside", id(node))
        if key not in self.cache:
            self.cache[key] = ({k: v for k, v in node.items() if k not in ("anyOf", "oneOf")}, node)
        beside_cached: dict[str, Any] = self.cache[key][0]
        return beside_cached

    def _with_siblings(self, beside: dict[str, Any], alt: Any, side: str, path: str, out: list[tuple[str, str]]) -> Any:
        """The source alternative `alt` with the source keywords beside its anyOf/oneOf: one schema at least as weak as their
        conjunction, so a proof for it proves the source alternative. None when the alternative accepts nothing, or when the
        conjunction cannot be merged (the finding then says so)."""
        if beside and self._disjoint(beside, side, alt, side):
            return None  # no instance has both the source keywords beside anyOf/oneOf and this alternative: it accepts nothing
        parts = [part for part in self._conjuncts(alt, side) if part is not True]
        if any(part is False for part in parts):
            return None
        parts = [part for part in (beside, *parts) if part]
        if not parts:
            return {}
        if len(parts) == 1:
            return parts[0]
        merged = self._merged(parts)
        if merged is None:
            out.append(("major", f"{path}: the keywords beside anyOf/oneOf and this alternative combine if/then/else, which cannot be "
                                 "merged: compatibility is not proven"))
        return merged

    def _alternatives(self, sub: Any, ss: str, alts: list[Any], ps: str, key: str, path: str, out: list[tuple[str, str]]) -> None:
        """`sub` against the anyOf/oneOf of the target: some alternative must accept it, and for oneOf exactly one."""
        covering: list[int] = []
        for index, alt in enumerate(alts):
            trial: list[tuple[str, str]] = []
            self.compare(sub, ss, alt, ps, path, trial)
            if not trial:
                covering.append(index)
        if not covering:
            out.append(("major", f"{path}: not accepted by any alternative of the target anyOf/oneOf"))
            return
        if key != "oneOf" or len(alts) < 2:
            return
        if len(covering) > 1:
            out.append(("major", f"{path}: matches several alternatives of the target oneOf, which accepts exactly one"))
            return
        chosen = covering[0]
        for index, alt in enumerate(alts):
            if index != chosen and not (self._disjoint(sub, ss, alt, ps) or self._disjoint(alts[chosen], ps, alt, ps)):
                out.append(("major", f"{path}: the target oneOf alternatives are not provably mutually exclusive, so a value "
                                     "valid before can match two of them"))
                return

    def _disjoint(self, first: Any, fs: str, second: Any, ss: str, depth: int = 0) -> bool:
        """True only when no instance can satisfy both schemas: disjoint type sets, disjoint enum/const values, a value
        set that fits no type of the other, or a required property whose two schemas are disjoint (a discriminator)."""
        if depth > 6:
            return False
        first, second = self._flat(first, fs), self._flat(second, ss)
        if first is False or second is False:
            return True
        if first is True or second is True:
            return False
        ta, tb = self._types(first, fs), self._types(second, ss)
        if ta is not None and tb is not None:
            widen: Callable[[set[str]], set[str]] = lambda t: t | ({"integer"} if "number" in t else set())  # noqa: E731
            if not widen(ta) & widen(tb):
                return True
        va, vb = self._values(first, fs), self._values(second, ss)
        if va is not None and vb is not None:
            return not {_canon(v) for v in va} & {_canon(v) for v in vb}
        if va is not None and tb is not None and not any(_value_fits(v, tb) for v in va):
            return True
        if vb is not None and ta is not None and not any(_value_fits(v, ta) for v in vb):
            return True
        props_a, props_b = first.get("properties"), second.get("properties")
        req_a, req_b = first.get("required"), second.get("required")
        if isinstance(props_a, dict) and isinstance(props_b, dict) and isinstance(req_a, list) and isinstance(req_b, list):
            shared = {n for n in req_a if isinstance(n, str)} & {n for n in req_b if isinstance(n, str)}
            for name in sorted(shared):
                if name in props_a and name in props_b and self._disjoint(props_a[name], fs, props_b[name], ss, depth + 1):
                    return True
        return False

    def _const_applies(self, side: str) -> bool:
        """`const` is a keyword from draft 6 on: drafts 3 and 4 ignore it, so a const there proves nothing about the instances."""
        root = self.roots[side]
        declared = root.get("$schema") if isinstance(root, dict) else None
        return not (isinstance(declared, str) and any(f"draft-0{number}" in declared for number in "34"))

    def _values(self, node: dict[str, Any], side: str) -> list[Any] | None:
        """The finite value set of a schema (`enum` and `const` intersected when both are present), or None. A `const` counts
        only under a draft that has the keyword."""
        enum = node.get("enum") if isinstance(node.get("enum"), list) else None
        if "const" in node and self._const_applies(side):
            if enum is None:
                return [node["const"]]
            return [node["const"]] if _canon(node["const"]) in {_canon(v) for v in enum} else []
        return enum

    def _enum(self, sub: dict[str, Any], ss: str, sup: dict[str, Any], ps: str, path: str, out: list[tuple[str, str]]) -> None:
        target = self._values(sup, ps)
        if target is None:
            return
        source = self._values(sub, ss)
        if source is None:
            out.append(("major", f"{path}: target restricts to an enum but the source accepts any value"))
            return
        allowed = {_canon(v) for v in target}
        missing = sorted({_canon(v) for v in source} - allowed)
        if missing:
            shown = ", ".join(_safe(v, 30) for v in missing[:3])
            out.append(("major", f"{path}: value(s) {shown} accepted by the source are not in the target enum"))

    def _range(self, sub: dict[str, Any], sup: dict[str, Any], path: str, out: list[tuple[str, str]]) -> None:
        for low, label in ((True, "lower"), (False, "upper")):
            wanted = _bound(sup, low)
            if wanted is None:
                continue
            have = _bound(sub, low)
            if have is None:
                ok = False
            elif low:
                ok = have[0] > wanted[0] or (have[0] == wanted[0] and (have[1] or not wanted[1]))
            else:
                ok = have[0] < wanted[0] or (have[0] == wanted[0] and (have[1] or not wanted[1]))
            if not ok:
                out.append(("major", f"{path}: numeric {label} bound {'removed' if have is None else 'loosened'} "
                                     f"(target {'>' if low and wanted[1] else '>=' if low else '<' if wanted[1] else '<='} {_n(wanted[0])})"))

    @staticmethod
    def _multiple(sub: dict[str, Any], sup: dict[str, Any], st: set[str] | None, path: str, out: list[tuple[str, str]]) -> None:
        """`multipleOf`: every multiple of the source step must be a multiple of the target step."""
        wanted = sup.get("multipleOf")
        if not _is_num(wanted) or wanted <= 0:
            return
        step = sub.get("multipleOf")
        unit = _dec(step) if _is_num(step) and step > 0 else (Decimal(1) if st == {"integer"} else None)
        try:
            proven = unit is not None and unit % _dec(wanted) == 0
        except ArithmeticError:
            proven = False
        if not proven:
            out.append(("major", f"{path}: multipleOf {_n(wanted)} is not guaranteed by the source"))

    def _length(self, sub: dict[str, Any], sup: dict[str, Any], low_key: str, high_key: str, label: str, path: str, out: list[tuple[str, str]]) -> None:
        if _is_num(sup.get(low_key)) and sup[low_key] > 0 and not (_is_num(sub.get(low_key)) and sub[low_key] >= sup[low_key]):
            out.append(("major", f"{path}: {low_key} is below the target minimum {_n(sup[low_key])} ({label})"))
        if _is_num(sup.get(high_key)) and not (_is_num(sub.get(high_key)) and sub[high_key] <= sup[high_key]):
            out.append(("major", f"{path}: {high_key} is above the target maximum {_n(sup[high_key])} ({label})"))

    def _array(self, sub: dict[str, Any], ss: str, sup: dict[str, Any], ps: str, path: str, out: list[tuple[str, str]]) -> None:
        self._length(sub, sup, "minItems", "maxItems", "array length", path, out)
        if sup.get("uniqueItems") is True and sub.get("uniqueItems") is not True:
            out.append(("major", f"{path}: target requires uniqueItems but the source does not"))
        sub_items, sup_items = sub.get("items", True), sup.get("items", True)
        if isinstance(sub_items, list) or isinstance(sup_items, list):
            # tuple form: position by position, then the schema past the tuples (additionalItems, or items in single form)
            sub_extra, sup_extra = sub.get("additionalItems", True), sup.get("additionalItems", True)
            width = max(len(items) for items in (sub_items, sup_items) if isinstance(items, list))
            # the source's maxItems bounds the lengths its instances can have: a position past that length never occurs
            longest = sub.get("maxItems") if _is_num(sub.get("maxItems")) and sub["maxItems"] >= 0 else None
            for index in range(width):
                if longest is None or index + 1 <= longest:
                    self.compare(_position(sub_items, sub_extra, index), ss, _position(sup_items, sup_extra, index), ps,
                                 f"{path}[{index}]", out)
            if longest is None or width + 1 <= longest:
                self.compare(_position(sub_items, sub_extra, width), ss, _position(sup_items, sup_extra, width), ps,
                             f"{path}[]", out)
            return
        self.compare(sub_items, ss, sup_items, ps, f"{path}[]", out)

    def _object(self, sub: dict[str, Any], ss: str, sup: dict[str, Any], ps: str, path: str, out: list[tuple[str, str]]) -> None:
        sub_props: dict[str, Any] = sub["properties"] if isinstance(sub.get("properties"), dict) else {}
        sup_props: dict[str, Any] = sup["properties"] if isinstance(sup.get("properties"), dict) else {}
        sub_ap, sup_ap = sub.get("additionalProperties", True), sup.get("additionalProperties", True)
        sub_req = {r for r in sub.get("required", []) if isinstance(r, str)} if isinstance(sub.get("required"), list) else set()
        sup_req = {r for r in sup.get("required", []) if isinstance(r, str)} if isinstance(sup.get("required"), list) else set()
        self._length(sub, sup, "minProperties", "maxProperties", "property count", path, out)
        for name in sorted(sup_req - sub_req):
            out.append(("blocker", f"{path}.{_safe(name, 40)}: required by the target but optional or undeclared in the source"))
        for name in sorted(set(sub_props) | set(sup_props)):
            here = f"{path}.{_safe(name, 40)}"
            sub_side = sub_props.get(name, sub_ap)
            sup_side = sup_props.get(name, sup_ap)
            if sub_side is False:
                continue
            if sup_side is False:
                out.append(("blocker", f"{here}: accepted by the source but rejected by the closed target schema"))
                continue
            if name not in sub_props and sub_ap is True:
                if self._constrains(sup_side, ps):
                    out.append(("blocker", f"{here}: the source is an open content model and accepts any value here, "
                                           "the target constrains it"))
                continue
            self.compare(sub_side, ss, sup_side, ps, here, out)
        if sup_ap is not True and sub_ap is not False:
            if sup_ap is False:
                out.append(("blocker", f"{path}: the source accepts undeclared properties but the target is closed "
                                       "(additionalProperties false)"))
            elif sub_ap is True:
                out.append(("major", f"{path}: the source accepts any undeclared property, the target constrains them"))
            else:
                self.compare(sub_ap, ss, sup_ap, ps, f"{path}.*", out)
        if "patternProperties" in sub and sup_ap is not True and _canon(sub["patternProperties"]) != _canon(sup.get("patternProperties")):
            out.append(("major", f"{path}: the source accepts properties by pattern that the closed or constrained target does not "
                                 "provably accept"))

    def _constrains(self, schema: Any, side: str) -> bool:
        return any(part is False or (isinstance(part, dict) and any(k not in _ANNOTATIONS for k in part))
                   for part in self._conjuncts(schema, side))


def _json_schema_compat(base: Any, head: Any) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    compare = _JsonCompare(base, head)
    backward: list[tuple[str, str]] = []
    forward: list[tuple[str, str]] = []
    compare.compare(base, "base", head, "head", "$", backward)  # data written with base must pass head
    compare.compare(head, "head", base, "base", "$", forward)  # data written with head must pass base
    extra = [("minor", f"{path}: keyword {keyword!r} is not analysed; verify this part manually")
             for path, keyword in sorted(compare.unanalysed)]
    return backward, forward, extra


# --- Avro compatibility -------------------------------------------------------------------------------------------

_AVRO_PRIMITIVES = {"null", "boolean", "int", "long", "float", "double", "bytes", "string"}
_AVRO_PROMOTIONS = {("int", "long"), ("int", "float"), ("int", "double"), ("long", "float"), ("long", "double"),
                    ("float", "double"), ("string", "bytes"), ("bytes", "string")}


def _avro_qualify(name: str, namespace: str) -> str:
    return name if "." in name or not namespace else f"{namespace}.{name}"


def _avro_logical(schema: dict[str, Any]) -> tuple[str, tuple[Any, Any] | None]:
    """(logicalType, decimal parameters or None) of a primitive or fixed schema; a decimal's scale defaults to 0."""
    logical = schema.get("logicalType")
    if not isinstance(logical, str):
        return "", None
    return (logical, (schema.get("precision"), schema.get("scale", 0))) if logical == "decimal" else (logical, None)


def _avro_aliases(node: dict[str, Any], field: str, label: str) -> list[str]:
    """The string aliases of a record, enum, fixed or field; a non-list value is INVALID_INPUT, never silently dropped."""
    raw = node.get("aliases", [])
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in raw):
        raise _Bad(field, f"Avro aliases of {label} must be a list of strings")
    return raw


def _avro_normalize(schema: Any, namespace: str, names: dict[str, dict[str, Any]], field: str) -> dict[str, Any]:
    """Schema JSON -> graph of nodes {k: kind, ...}; named types are shared objects, so recursion is a real cycle."""
    if isinstance(schema, str):
        if schema in _AVRO_PRIMITIVES:
            return {"k": schema, "lt": "", "dec": None}
        for candidate in (_avro_qualify(schema, namespace), schema):
            if candidate in names:
                return names[candidate]
        raise _Bad(field, f"unknown Avro type reference {_safe(schema, 60)!r}")
    if isinstance(schema, list):
        if not schema:
            raise _Bad(field, "an Avro union needs at least one branch")
        return {"k": "union", "b": [_avro_normalize(b, namespace, names, field) for b in schema]}
    if not isinstance(schema, dict):
        raise _Bad(field, "an Avro schema must be a string, an object or a union array")
    kind = schema.get("type")
    if kind in ("record", "error", "enum", "fixed"):
        raw = schema.get("name")
        if not isinstance(raw, str) or not raw:
            raise _Bad(field, f"Avro {kind} needs a non-empty name")
        full = raw if "." in raw else _avro_qualify(raw, schema["namespace"] if isinstance(schema.get("namespace"), str) else namespace)
        own_ns = full.rpartition(".")[0]
        aliases = [_avro_qualify(a, own_ns) for a in _avro_aliases(schema, field, _safe(full, 60))]
        node: dict[str, Any] = {"k": "record" if kind in ("record", "error") else kind, "n": full, "aliases": aliases, "lt": "", "dec": None}
        names[full] = node
        if node["k"] == "record":
            fields = schema.get("fields")
            if not isinstance(fields, list):
                raise _Bad(field, f"Avro record {_safe(full, 60)} needs a fields array")
            node["f"] = []
            for entry in fields:
                if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or "type" not in entry:
                    raise _Bad(field, f"every field of record {_safe(full, 60)} needs a name and a type")
                node["f"].append({"name": entry["name"], "t": _avro_normalize(entry["type"], own_ns, names, field),
                                  "default": "default" in entry,
                                  "aliases": _avro_aliases(entry, field, "field " + _safe(entry.get("name"), 40))})
        elif node["k"] == "enum":
            symbols = schema.get("symbols")
            if not isinstance(symbols, list) or not symbols or any(not isinstance(s, str) for s in symbols):
                raise _Bad(field, f"Avro enum {_safe(full, 60)} needs a non-empty symbols array of strings")
            node["sym"], node["default"] = symbols, "default" in schema
        else:
            if not isinstance(schema.get("size"), int) or isinstance(schema.get("size"), bool):
                raise _Bad(field, f"Avro fixed {_safe(full, 60)} needs an integer size")
            node["size"] = schema["size"]
            node["lt"], node["dec"] = _avro_logical(schema)
        return node
    if kind == "array":
        return {"k": "array", "i": _avro_normalize(schema.get("items"), namespace, names, field), "lt": "", "dec": None}
    if kind == "map":
        return {"k": "map", "v": _avro_normalize(schema.get("values"), namespace, names, field), "lt": "", "dec": None}
    if isinstance(kind, str) and kind in _AVRO_PRIMITIVES:
        logical, decimal = _avro_logical(schema)
        return {"k": kind, "lt": logical, "dec": decimal}
    if isinstance(kind, (dict, list)) or (isinstance(kind, str) and kind in names):
        return _avro_normalize(kind, namespace, names, field)
    raise _Bad(field, f"unsupported Avro type {_safe(kind, 40)!r}")


class _AvroCompare:
    """Can data written with `writer` be read with `reader` (Avro schema resolution)?"""

    def __init__(self) -> None:
        self.stack: set[tuple[int, int]] = set()
        self.nodes = 0

    def match(self, w: dict[str, Any], r: dict[str, Any], path: str, out: list[tuple[str, str]]) -> None:
        self.nodes += 1
        if self.nodes > _NODE_BUDGET:
            raise _Bad("base_schema", "schema comparison exceeds the node budget; split the schema")
        key = (id(w), id(r))
        if key in self.stack:
            return
        self.stack.add(key)
        if len(self.stack) > _MAX_DEPTH:
            raise _Bad("base_schema", f"schema nesting exceeds {_MAX_DEPTH} levels")
        try:
            self._match(w, r, path, out)
        finally:
            self.stack.discard(key)

    @staticmethod
    def _label(node: dict[str, Any]) -> str:
        return _safe(node.get("n") or node["k"], 60)

    def _match(self, w: dict[str, Any], r: dict[str, Any], path: str, out: list[tuple[str, str]]) -> None:
        if w["k"] == "union":
            for index, branch in enumerate(w["b"]):
                self.match(branch, r, f"{path}<{index}:{self._label(branch)}>", out)
            return
        if r["k"] == "union":
            for branch in r["b"]:
                trial: list[tuple[str, str]] = []
                self.match(w, branch, path, trial)
                if not trial:
                    return
            out.append(("blocker", f"{path}: no branch of the reader union accepts {self._label(w)}"))
            return
        wk, rk = w["k"], r["k"]
        if wk in _AVRO_PRIMITIVES and rk in _AVRO_PRIMITIVES:
            if wk != rk and (wk, rk) not in _AVRO_PROMOTIONS:
                out.append(("blocker", f"{path}: {wk} cannot be read as {rk}"))
            else:
                self._logical(w, r, path, out)
            return
        if wk != rk:
            out.append(("blocker", f"{path}: {self._label(w)} ({wk}) cannot be read as {self._label(r)} ({rk})"))
            return
        if wk in ("record", "enum", "fixed"):
            short: Callable[[str], str] = lambda n: n.rpartition(".")[2]  # noqa: E731
            if short(w["n"]) != short(r["n"]) and w["n"] not in r["aliases"] and short(w["n"]) not in [short(a) for a in r["aliases"]]:
                out.append(("blocker", f"{path}: {wk} name {_safe(w['n'], 60)} does not match reader name {_safe(r['n'], 60)}"))
                return
        if wk == "record":
            by_name = {f["name"]: f for f in w["f"]}
            for rf in sorted(r["f"], key=lambda f: f["name"]):
                source = next((by_name[n] for n in [rf["name"], *rf["aliases"]] if n in by_name), None)
                here = f"{path}.{_safe(rf['name'], 40)}"
                if source is None:
                    if not rf["default"]:
                        out.append(("blocker", f"{here}: reader field has no default and the writer does not provide it"))
                else:
                    self.match(source["t"], rf["t"], here, out)
        elif wk == "enum":
            unknown = sorted(set(w["sym"]) - set(r["sym"]))
            if unknown and not r["default"]:
                out.append(("blocker", f"{path}: enum symbol(s) {', '.join(_safe(s, 30) for s in unknown[:3])} unknown to the reader, "
                                       "which has no default"))
        elif wk == "fixed":
            if w["size"] != r["size"]:
                out.append(("blocker", f"{path}: fixed size {w['size']} differs from reader size {r['size']}"))
            else:
                self._logical(w, r, path, out)
        elif wk == "array":
            self.match(w["i"], r["i"], f"{path}[]", out)
        elif wk == "map":
            self.match(w["v"], r["v"], f"{path}{{}}", out)

    @staticmethod
    def _logical(w: dict[str, Any], r: dict[str, Any], path: str, out: list[tuple[str, str]]) -> None:
        """A changed logical type, or a changed decimal precision or scale, reinterprets the same bytes."""
        if w["lt"] != r["lt"]:
            out.append(("major", f"{path}: logicalType changed from {_safe(w['lt'], 30) or 'none'} to {_safe(r['lt'], 30) or 'none'}"))
        elif w["dec"] != r["dec"]:
            shown: Callable[[Any], str] = lambda d: f"({_safe(d[0], 12)},{_safe(d[1], 12)})"  # noqa: E731
            out.append(("major", f"{path}: decimal precision/scale changed from {shown(w['dec'])} to {shown(r['dec'])}"))


def _avro_schema_compat(base: Any, head: Any) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
    base_node = _avro_normalize(base, "", {}, "base_schema")
    head_node = _avro_normalize(head, "", {}, "head_schema")
    backward: list[tuple[str, str]] = []
    forward: list[tuple[str, str]] = []
    _AvroCompare().match(base_node, head_node, "$", backward)  # writer base, reader head
    _AvroCompare().match(head_node, base_node, "$", forward)  # writer head, reader base
    return backward, forward, []


def schema_compat(payload: dict[str, Any]) -> dict[str, Any]:
    """Compatibility of two event schemas. backward: consumers on `head` read data written with `base`; forward:
    consumers still on `base` read data written with `head`; full: both. Verdict covers the requested mode only."""

    def work() -> dict[str, Any]:
        fmt, mode = payload["format"], payload["mode"]
        try:
            base = _load_json(payload["base_schema"], "base_schema")
            head = _load_json(payload["head_schema"], "head_schema")
        except _Bad as exc:
            return _invalid(exc.field, exc.message)
        try:
            if fmt == "avro":
                backward, forward, extra = _avro_schema_compat(base, head)
            else:
                backward, forward, extra = _json_schema_compat(base, head)
        except _Bad as exc:
            return _invalid(exc.field, exc.message)
        raw = list(extra)
        if mode in ("backward", "full"):
            raw += [(sev, f"[backward] {text}") for sev, text in backward]
        if mode in ("forward", "full"):
            raw += [(sev, f"[forward] {text}") for sev, text in forward]
        breaking: Callable[[list[tuple[str, str]]], bool] = lambda items: not any(s in ("blocker", "major") for s, _ in items)  # noqa: E731
        backward_ok, forward_ok = breaking(backward), breaking(forward)
        compatible = {"backward": backward_ok, "forward": forward_ok, "full": backward_ok and forward_ok}[mode]
        verdict, findings = _wrap(raw, "SC")
        return {"state": "SUCCESS", "format": fmt, "mode": mode, "backward_compatible": backward_ok,
                "forward_compatible": forward_ok, "compatible": compatible,
                "breaking_changes": min(1000, sum(1 for s, _ in raw if s in ("blocker", "major"))),
                "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- CloudEvents 1.0 ----------------------------------------------------------------------------------------------

_CE_REQUIRED = ("id", "source", "specversion", "type")
_CE_OPTIONAL = ("datacontenttype", "dataschema", "subject", "time")
_CE_NAME = re.compile(r"[a-z0-9]+\Z")
_CE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})\Z", re.ASCII)
_CE_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:\S+\Z")
_CE_MEDIA = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+\-]*/[A-Za-z0-9][A-Za-z0-9!#$&^_.+\-]*"
    r"(\s*;\s*[^;=\s]+=(\"[^\"]*\"|[^;\s\"]+))*\Z")


def _rfc3339(text: str) -> bool:
    match = _CE_TIME.match(text)
    if not match:
        return False
    year, month, day, hour, minute, second = (int(g) for g in match.groups()[:6])
    zone = match.group(8)
    try:
        datetime(year or 4, month, day, hour, minute, min(second, 59))  # year 0000 is valid RFC 3339; 4 is leap like 0
    except ValueError:
        return False
    if second > 60 or (zone not in ("Z", "z") and (int(zone[1:3]) > 23 or int(zone[4:6]) > 59)):
        return False
    return True


def cloudevents_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the context attributes of one CloudEvent against the 1.0 specification (names, required set, value forms)."""

    def work() -> dict[str, Any]:
        attributes, has_data = payload["attributes"], payload["has_data"]
        values: dict[str, str] = {}
        label: dict[str, str] = {}
        raw: list[tuple[str, str]] = []
        for entry in attributes:
            name = entry["name"]
            if name in ("data", "data_base64"):
                return _invalid("attributes", f"{name!r} is the event payload, not a context attribute: use has_data")
            label.setdefault(name, repr(_safe(name, 64)))
            if name in values:
                raw.append(("blocker", f"attribute {label[name]} appears more than once"))
            values[name] = entry["value"]
        for name in sorted(values):
            if not _CE_NAME.match(name):
                raw.append(("blocker", f"attribute name {label[name]} must use only lower-case a-z and digits 0-9"))
            elif len(name) > 20:
                raw.append(("minor", f"attribute name {label[name]} is longer than the recommended 20 characters"))
        for name in _CE_REQUIRED:
            if name not in values:
                raw.append(("blocker", f"required attribute {name!r} is missing"))
            elif values[name] == "":
                raw.append(("blocker", f"required attribute {name!r} must not be empty"))
        if values.get("specversion", "1.0") not in ("", "1.0"):
            raw.append(("blocker", f"specversion is {_safe(values['specversion'], 20)!r}; this check validates CloudEvents 1.0"))
        source = values.get("source")
        if source and re.search(r"\s", source):
            raw.append(("major", "source must be a URI-reference without whitespace"))
        etype = values.get("type")
        if etype and "." not in etype:
            raw.append(("minor", f"type {_safe(etype, 60)!r} should be prefixed with a reverse-DNS name (for example com.example.order.created)"))
        if "subject" in values and values["subject"] == "":
            raw.append(("major", "subject must not be empty when present"))
        if "time" in values and not _rfc3339(values["time"]):
            raw.append(("major", f"time {_safe(values['time'], 40)!r} is not an RFC 3339 timestamp"))
        if "dataschema" in values and not _CE_SCHEME.match(values["dataschema"]):
            raw.append(("major", f"dataschema {_safe(values['dataschema'], 60)!r} must be an absolute URI"))
        if "datacontenttype" in values:
            if not _CE_MEDIA.match(values["datacontenttype"]):
                raw.append(("major", f"datacontenttype {_safe(values['datacontenttype'], 60)!r} is not a valid RFC 2046 media type"))
            elif not has_data:
                raw.append(("minor", "datacontenttype is set but the event carries no data"))
        if has_data and "datacontenttype" not in values:
            raw.append(("info", "data without datacontenttype: consumers must assume application/json"))
        extensions = sorted(_safe(n, 64) for n in values if n not in _CE_REQUIRED and n not in _CE_OPTIONAL)
        verdict, findings = _wrap(raw, "CE")
        return {"state": "SUCCESS", "valid": verdict != "fail",
                "required_present": sum(1 for n in _CE_REQUIRED if values.get(n)),
                "optional_present": sum(1 for n in _CE_OPTIONAL if n in values),
                "extension_attributes": extensions, "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- AsyncAPI lint ------------------------------------------------------------------------------------------------

_ASYNCAPI_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.\-]+)?\Z", re.ASCII)


def _asyncapi_refs(document: Any, raw: list[tuple[str, str]]) -> None:
    """Report every local $ref that does not resolve and every external $ref (not followed)."""
    stack = [("#", document)]
    seen = 0
    while stack:
        where, node = stack.pop()
        seen += 1
        if seen > 200000:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    if not value.startswith("#"):
                        raw.append(("info", f"{_safe(where, 160)}: external $ref {_safe(value, 60)!r} is not resolved by this lint"))
                    elif _pointer(document, value) is None:
                        raw.append(("major", f"{_safe(where, 160)}: $ref {_safe(value, 60)!r} does not resolve"))
                else:
                    stack.append((f"{where}/{key}", value))
        elif isinstance(node, list):
            stack.extend((f"{where}/{i}", v) for i, v in enumerate(node))


# Budgets of one AsyncAPI message walk (see _AsyncApiWalk). Each is a fail-closed stop: the walk ends, the document is reported
# fail with the stop reason, and the script records the document as not analysed.
ASYNCAPI_WALK_BUDGET = 200000  # steps: one per message visit, one per memoised walk compared, one per replay (replays are cheap)
# Copy ceiling, a documented resource bound: a replay copies the findings of a shared message, and a document whose shared pools copy
# more than 500,000 findings is reported incomplete with the stop reason ("...findings copied from shared messages"). It is a time
# bound on this host, set against the handler's 10 s timeout, and a document over it fails closed with that same stop reason. It is a
# deliberate parity difference, not a non-difference: HEAD lints such documents without the bound. Measured on this host: 999 channels
# each referencing a 500-leaf pool (500,000 copies) lint in 1.87 s with HEAD's verdict, messages and findings; 1,000 channels (500,500
# copies) are refused with the stop reason in 2.09 s; the 182 KB document (1,700 channels, a 600-leaf pool, about 1.02 million copies)
# is refused, where HEAD gives warn, 600 messages and 300 findings; the 3.8 KB shape (built as: messages T14 = {contentType x} and
# Tk = oneOf[$ref T(k+1), $ref T(k+1)] for k = 13..0; a channel message that is 45 oneOf wrappers over a bare $ref T0, which is 46
# levels counting the bare reference: 3,853 bytes and 786,430 copies measured; read literally as 46 wrappers over $ref T0 it is 3,904
# bytes and 802,814 copies) is refused, where HEAD gives warn, 1 message and 300 findings; 600 channels with a
# 500-leaf pool (300,500 copies) and 500 channels with a 500-leaf pool
# (250,500 copies) lint with HEAD's output; cyclic n=8 completes (449,513 copies), and n=9 and n=12 stop at this ceiling with the same
# stop reason. A count-based replay would not reproduce HEAD's set-based dedupe of identical rendered findings (two sites whose rendered
# texts coincide, such as names cut to 60 characters, names containing '/', or equal-length redactions), so it must not replace this
# replay without accepting that difference.
ASYNCAPI_EMIT_BUDGET = 500000
ASYNCAPI_MAX_CHAIN = 700  # nested $ref levels on one walk; each level is one Python frame, under the interpreter's limit of 1000
ASYNCAPI_WALK_STOPPED = "AsyncAPI message walk stopped"  # the prefix of every stop finding and reason
ASYNCAPI_ORIGIN_MAX = 24  # the origin in a stop reason is cut to this: with the file label it keeps the sentence inside 300 characters


class _AsyncApiWalk:
    """The state of the message walk of one AsyncAPI document. `raw`, `keys` and `reach` are where the walk writes now: its
    findings, its counted message keys and the $ref targets it reached. A fresh walk of a $ref target writes to its own capture,
    which becomes a memo entry and is replayed under every path that reaches the target. `reason` is set when a stop ends the walk."""

    __slots__ = ("document", "default_ct", "origin", "raw", "keys", "reach", "entries", "steps", "emitted", "exhausted", "reason")

    def __init__(self, document: Any, default_ct: bool, raw: list[tuple[str, str]], keys: set[tuple[str, str]]) -> None:
        self.document, self.default_ct, self.origin = document, default_ct, ""
        self.raw, self.keys = raw, keys
        self.reach: set[int] = set()
        self.entries: dict[int, list[tuple[Any, ...]]] = {}
        self.steps = self.emitted = 0
        self.exhausted, self.reason = False, ""

    def stop(self, reason: str) -> None:
        if not self.exhausted:
            self.exhausted, self.reason = True, reason

    def charge(self, steps: int) -> bool:
        """Charge `steps` to the step budget. False, with the walk stopped, once the budget is spent."""
        self.steps += steps
        if self.steps > ASYNCAPI_WALK_BUDGET:
            self.stop(f"{ASYNCAPI_WALK_STOPPED} after {ASYNCAPI_WALK_BUDGET} steps: the message references exceed what this lint "
                      "walks, the document is not fully analysed")
            return False
        return True


_SELF = ("self", "")  # the message a $ref reached: counted under the $ref string of whichever walk reaches it


def _asyncapi_message(message: Any, where: str, chain: tuple[int, ...], walk: _AsyncApiWalk) -> None:
    """A message, a $ref to one, or a oneOf of messages, walked from `where`; findings carry `where` as their path prefix. Each
    distinct message is one counted key: a $ref is ("ref", its absolute $ref string), an inline message is ("path", its path).
    `chain` holds the ids of the $ref targets on the current walk: meeting one again is a cycle, reported at its own path and not
    followed. A $ref target is walked once per chain context; later reaches replay the memo entry under their own path. The body is
    inline, so one $ref level costs one Python frame."""
    if walk.exhausted or not walk.charge(1):
        return
    fresh: tuple[Any, ...] | None = None
    own: tuple[str, str] | None = None
    if isinstance(message, dict) and isinstance(message.get("$ref"), str):
        ref = message["$ref"]
        target = _pointer(walk.document, ref)
        if target is None:
            return  # reported by the global $ref walk
        target_id = id(target)
        walk.reach.add(target_id)
        if target_id in chain:
            walk.raw.append(("major", f"{where}: message $ref is cyclic: a oneOf refers back to itself"))
            return
        entry = _memo_entry(walk, target_id, chain)
        if walk.exhausted:
            return
        if entry is not None:
            _replay(walk, entry, where, ref)
            return
        if len(chain) >= ASYNCAPI_MAX_CHAIN:
            walk.stop(f"{ASYNCAPI_WALK_STOPPED} under {_safe(walk.origin, ASYNCAPI_ORIGIN_MAX)}: a $ref chain is deeper than "
                      f"{ASYNCAPI_MAX_CHAIN} levels, "
                      "the document is not fully analysed")
            return
        fresh = (where, chain, ref, target_id, walk.raw, walk.keys, walk.reach)
        walk.raw, walk.keys, walk.reach = [], set(), set()
        message, where, chain, own = target, "", (*chain, target_id), _SELF
    if not isinstance(message, dict):
        walk.raw.append(("major", f"{where}: message must be an object"))
    elif isinstance(message.get("oneOf"), list):
        for index, alt in enumerate(message["oneOf"]):
            _asyncapi_message(alt, f"{where}/oneOf/{index}", chain, walk)
    else:
        walk.keys.add(own if own is not None else ("path", where))
        if "payload" not in message:
            walk.raw.append(("minor", f"{where}: message has no payload schema, so consumers have no contract"))
        if not message.get("contentType") and not walk.default_ct:
            walk.raw.append(("minor", f"{where}: no contentType and no defaultContentType"))
    if fresh is not None:
        outer_where, outer_chain, ref, target_id, raw, keys, reach = fresh
        entry = (frozenset(walk.reach), frozenset(outer_chain).intersection(walk.reach), tuple(walk.raw), frozenset(walk.keys))
        walk.raw, walk.keys, walk.reach = raw, keys, reach
        if not walk.exhausted:  # a walk the budget cut short is never replayed
            walk.entries.setdefault(target_id, []).append(entry)
        _replay(walk, entry, outer_where, ref)


def _memo_entry(walk: _AsyncApiWalk, target_id: int, chain: tuple[int, ...]) -> tuple[Any, ...] | None:
    """The memoised walk of the target valid under this chain, or None. A walk is valid when the chain holds exactly the targets it
    walked under, among the targets that walk reached."""
    chain_set = set(chain)
    for entry in walk.entries.get(target_id, ()):
        if not walk.charge(1):
            return None
        if chain_set.intersection(entry[0]) == entry[1]:
            return entry
    return None


def _replay(walk: _AsyncApiWalk, entry: tuple[Any, ...], where: str, ref: str) -> None:
    """Replay a memoised walk under `where`: its findings, its counted keys and its reached targets, as they are found there. A
    stopped walk copies no more findings, but the messages a replay reaches are still counted, so the message count stays the count
    analysed up to the stop."""
    reached, _walked_under, relative, keys = entry
    if not walk.exhausted and walk.charge(1):  # a replay costs one step, however many findings it copies
        walk.emitted += len(relative)
        if walk.emitted > ASYNCAPI_EMIT_BUDGET:
            walk.stop(f"{ASYNCAPI_WALK_STOPPED} after {ASYNCAPI_EMIT_BUDGET} findings copied from shared messages: the document is "
                      "not fully analysed")
        else:
            walk.reach.update(reached)
            walk.raw.extend([(sev, where + text) for sev, text in relative])
    for kind, value in keys:
        if kind == "self":
            walk.keys.add(("ref", ref))
        elif kind == "path":
            walk.keys.add(("path", where + value))
        else:
            walk.keys.add((kind, value))


def _walk_top(walk: _AsyncApiWalk, message: Any, where: str) -> None:
    """One top-level message of the document. The interpreter's recursion limit ends the walk as a stop, never as an exception."""
    walk.origin = where
    try:
        _asyncapi_message(message, where, (), walk)
    except RecursionError:
        walk.stop(f"{ASYNCAPI_WALK_STOPPED} under {_safe(where, ASYNCAPI_ORIGIN_MAX)}: the walk exceeded the interpreter's recursion limit, "
                  "the document is not fully analysed")


def asyncapi_lint(payload: dict[str, Any]) -> dict[str, Any]:
    """Structural lint of an AsyncAPI 2.x or 3.x document given as JSON text: required fields, operations, messages, refs."""

    def work() -> dict[str, Any]:
        try:
            document = _load_json(payload["document"], "document")
        except _Bad as exc:
            return _invalid(exc.field, exc.message)
        if not isinstance(document, dict):
            return _invalid("document", "the AsyncAPI document must be a JSON object")
        raw: list[tuple[str, str]] = []
        version = document.get("asyncapi")
        match = _ASYNCAPI_VERSION.match(version) if isinstance(version, str) and len(version) <= 40 else None
        if not match:
            return _invalid("document", "the document needs an `asyncapi` version string such as 3.0.0")
        major = int(match.group(1))
        info = document.get("info")
        for key in ("title", "version"):
            if not isinstance(info, dict) or not isinstance(info.get(key), str) or not info[key]:
                raw.append(("blocker", f"info.{key} is required"))
        channels = document.get("channels")
        default_ct = isinstance(document.get("defaultContentType"), str) and bool(document["defaultContentType"])
        operations_seen = 0
        messages: set[tuple[str, str]] = set()
        walk = _AsyncApiWalk(document, default_ct, raw, messages)
        if major not in (2, 3):
            raw.append(("blocker", f"AsyncAPI {_safe(version, 40)} is not supported by this lint (2.x and 3.x)"))
            channels = None
        elif major == 2:
            if not isinstance(channels, dict):
                raw.append(("blocker", "channels is required and must be an object in AsyncAPI 2.x"))
                channels = {}
            elif not channels:
                raw.append(("minor", "channels is empty: the document describes no channel"))
            operation_ids: dict[str, str] = {}
            for name in sorted(channels):
                channel, cn = channels[name], _safe(name, 60)
                if not isinstance(channel, dict):
                    raw.append(("major", f"channels/{cn}: channel must be an object"))
                    continue
                declared = channel.get("parameters")
                if declared is not None and not isinstance(declared, dict):
                    raw.append(("major", f"channels/{cn}: parameters must be an object, so no parameter of {cn} is declared"))
                    declared = {}
                for param in re.findall(r"\{([^{}]+)\}", name):
                    if param not in (declared or {}):
                        raw.append(("major", f"channels/{cn}: parameter {{{_safe(param, 40)}}} is not declared under parameters"))
                ops = [k for k in ("publish", "subscribe") if isinstance(channel.get(k), dict)]
                if not ops and "$ref" not in channel:
                    raw.append(("minor", f"channels/{cn}: no publish or subscribe operation"))
                for kind in ops:
                    operations_seen += 1
                    operation = channel[kind]
                    where = f"channels/{cn}/{kind}"
                    op_id = operation.get("operationId")
                    if not isinstance(op_id, str) or not op_id:
                        raw.append(("minor", f"{where}: operationId is missing"))
                    elif op_id in operation_ids:
                        raw.append(("major", f"{where}: operationId {_safe(op_id, 60)!r} duplicates {operation_ids[op_id]}"))
                    else:
                        operation_ids[op_id] = where
                    if "message" not in operation:
                        raw.append(("minor", f"{where}: operation has no message (the AsyncAPI 2.x schema does not require one, "
                                             "but consumers get no contract)"))
                    else:
                        _walk_top(walk, operation["message"], f"{where}/message")
            servers: Any = document.get("servers")
            for name in sorted(servers) if isinstance(servers, dict) else []:
                server = servers[name]
                for key in ("url", "protocol"):
                    if not isinstance(server, dict) or not server.get(key):
                        if not (isinstance(server, dict) and "$ref" in server):
                            raw.append(("blocker", f"servers/{_safe(name, 60)}: {key} is required"))
        else:
            channels = channels if isinstance(channels, dict) else {}
            for name in sorted(channels):
                channel, cn = channels[name], _safe(name, 60)
                if isinstance(channel, dict) and "$ref" in channel:
                    continue
                msgs = channel.get("messages") if isinstance(channel, dict) else None
                if not isinstance(msgs, dict) or not msgs:
                    raw.append(("minor", f"channels/{cn}: no messages declared"))
                    continue
                for msg_name in sorted(msgs):
                    _walk_top(walk, msgs[msg_name], f"channels/{cn}/messages/{_safe(msg_name, 60)}")
            operations: Any = document.get("operations")
            for name in sorted(operations) if isinstance(operations, dict) else []:
                operations_seen += 1
                operation, where = operations[name], f"operations/{_safe(name, 60)}"
                if not isinstance(operation, dict):
                    raw.append(("major", f"{where}: operation must be an object"))
                    continue
                if operation.get("action") not in ("send", "receive"):
                    raw.append(("blocker", f"{where}: action must be send or receive"))
                ref = operation.get("channel")
                if not isinstance(ref, dict) or not isinstance(ref.get("$ref"), str):
                    raw.append(("blocker", f"{where}: channel must be a $ref to #/channels/..."))
                elif not ref["$ref"].startswith("#/channels/"):
                    raw.append(("major", f"{where}: channel $ref {_safe(ref['$ref'], 60)!r} must point at #/channels/..."))
            servers = document.get("servers")
            for name in sorted(servers) if isinstance(servers, dict) else []:
                server = servers[name]
                if isinstance(server, dict) and "$ref" not in server:
                    for key in ("host", "protocol"):
                        if not server.get(key):
                            raw.append(("blocker", f"servers/{_safe(name, 60)}: {key} is required"))
        if walk.exhausted:  # the stop is pinned: truncation never cuts it, and the message count is said to be partial
            pinned = [("major", f"{walk.reason}; the message count and the findings list are both partial")]
        else:
            pinned = []
        _asyncapi_refs(document, raw)
        verdict, findings = _wrap(raw, "AA", pinned)
        return {"state": "SUCCESS", "asyncapi_version": _safe(version, 40),
                "channels": len(channels) if isinstance(channels, dict) else 0,
                "operations": operations_seen, "messages": len(messages), "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- partition plan -----------------------------------------------------------------------------------------------

_MAX_PARTITIONS = 10**12  # the output schema maximum for every partition count


def partition_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Partitions needed for a peak rate: the largest of the throughput bound, the consumer-capacity bound and the bound
    that keeps the hottest key's partition within one partition's capacity (min of consumer rate and write budget)."""

    def work() -> dict[str, Any]:
        peak, size = _dec(payload["peak_msgs_per_s"]), _dec(payload["avg_msg_bytes"])
        cap, consumer = _dec(payload["partition_max_mib_per_s"]), _dec(payload["consumer_max_msgs_per_s"])
        target = peak * (1 + _dec(payload["headroom_pct"]) / 100)
        mib_per_s = target * size / _MIB
        by_throughput, by_consumers = max(1, _ceil(mib_per_s / cap)), max(1, _ceil(target / consumer))
        hot = _dec(payload["hot_key_share_pct"]) / 100
        capacity = min(consumer, cap * _MIB / size)  # msgs/s one partition can take
        hot_total, spread = target * hot, target * (1 - hot)
        slack = capacity - hot_total
        if spread == 0:
            by_skew = 1 if slack >= 0 else -1
        else:
            by_skew = _ceil(spread / slack) if slack > 0 else -1
        required = max(by_throughput, by_consumers, by_skew)
        if required > _MAX_PARTITIONS:
            driver = max((by_skew, "hot_key_share_pct"), (by_throughput, "partition_max_mib_per_s"),
                         (by_consumers, "consumer_max_msgs_per_s"))[1]
            return _invalid(driver, f"the inputs imply more than {_MAX_PARTITIONS} partitions: check the units "
                                    "(avg_msg_bytes is bytes, partition_max_mib_per_s is MiB/s)")
        current = payload.get("current_partitions")
        evaluated = current if current is not None else required
        hot_rate = hot_total + spread / evaluated
        skew = hot * evaluated + (1 - hot)
        raw: list[tuple[str, str]] = []
        if by_skew < 0:
            raw.append(("major", f"the hottest key alone carries {_n(hot_total)} msg/s, at or above one partition's capacity of "
                                 f"{_n(capacity)} msg/s: no partition count fixes it, split the key or re-key"))
        elif hot_rate > capacity:
            raw.append(("major", f"the hottest partition would carry {_n(hot_rate)} msg/s, above one partition's capacity of "
                                 f"{_n(capacity)} msg/s ({by_skew} partitions keep it within capacity)"))
        if current is not None and current < max(by_throughput, by_consumers):
            raw.append(("major", f"{current} partitions is below the {max(by_throughput, by_consumers)} needed for a uniform load "
                                 f"({by_throughput} by throughput, {by_consumers} by consumer capacity)"))
        if current is not None and current > required:
            raw.append(("info", f"{current} partitions exceeds the {required} needed; partitions cannot normally be reduced later"))
        if current is not None and current < required:
            raw.append(("info", "increasing the partition count remaps keys (hash mod N): per-key ordering breaks across the change"))
        if skew > SKEW_WARN:
            raw.append(("minor", f"key skew: the hottest partition carries {_n(_dec(round(float(skew), 2)))}x the mean load"))
        verdict, findings = _wrap(raw, "PP")
        return {"state": "SUCCESS", "required_partitions": required, "partitions_by_throughput": by_throughput,
                "partitions_by_consumers": by_consumers, "partitions_by_skew": by_skew, "evaluated_partitions": evaluated,
                "target_msgs_per_s": _r(target, 2), "throughput_mib_per_s": _r(mib_per_s),
                "hot_partition_msgs_per_s": _r(hot_rate, 2), "skew_ratio": _r(skew),
                "max_consumer_parallelism": evaluated, "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- consumer lag -------------------------------------------------------------------------------------------------


def lag_assessment(payload: dict[str, Any]) -> dict[str, Any]:
    """Drain time, trend and SLO breach of a consumer group backlog; consumers needed to clear it within the SLO."""

    def work() -> dict[str, Any]:
        lag, arrival, slo = _dec(payload["lag_msgs"]), _dec(payload["arrival_msgs_per_s"]), _dec(payload["slo_s"])
        per, count = _dec(payload["per_consumer_msgs_per_s"]), payload["consumer_count"]
        partitions = payload.get("partition_count")
        effective = min(count, partitions) if partitions is not None else count
        rate = per * effective
        net = rate - arrival
        delay = lag / rate
        if net < 0:
            trend = "growing"
        elif lag == 0:
            trend = "steady"
        elif net == 0:
            trend = "stalled"
        else:
            trend = "draining"
        drain = lag / net if trend == "draining" else Decimal(-1)
        breach = delay > slo
        if not breach:
            recovery = Decimal(0)
        elif trend == "draining":
            recovery = (lag - slo * rate) / net
        else:
            recovery = Decimal(-1)
        needed_rate = arrival + lag / slo
        needed = max(1, _ceil(needed_rate / per))
        needs_partitions = (breach or trend in ("growing", "stalled")) and partitions is not None and needed > partitions
        raw: list[tuple[str, str]] = []
        if trend == "growing":
            raw.append(("blocker", f"consumers process {_n(rate)} msg/s but {_n(arrival)} msg/s arrive: lag grows without bound"))
        if trend == "stalled":
            raw.append(("major", "consumption equals arrival: the existing backlog never drains"))
        if breach:
            raw.append(("major", f"estimated delay {_n(_dec(round(float(delay), 2)))} s exceeds the {_n(slo)} s SLO"))
        if partitions is not None and count > partitions:
            raw.append(("minor", f"{count - partitions} consumer(s) are idle: only {partitions} partitions can be consumed in parallel"))
        if needs_partitions:
            raw.append(("major", f"clearing the backlog within the SLO needs {needed} consumers but only {partitions} partitions exist"))
        verdict, findings = _wrap(raw, "LG")
        return {"state": "SUCCESS", "effective_consumers": effective, "consume_rate_msgs_per_s": _r(rate, 2),
                "net_drain_msgs_per_s": _r(net, 2), "estimated_delay_s": _r(delay, 2), "drain_time_s": _r(drain, 2),
                "recovery_time_s": _r(recovery, 2), "trend": trend, "slo_breach": breach, "consumers_needed": needed,
                "needs_more_partitions": needs_partitions, "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- retry schedule -----------------------------------------------------------------------------------------------


def retry_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    """Exponential backoff schedule (none/full/equal jitter) checked against the visibility timeout and the retry budget."""

    def work() -> dict[str, Any]:
        base, mult, cap = _dec(payload["base_delay_s"]), _dec(payload["multiplier"]), _dec(payload["max_delay_s"])
        if cap < base:
            return _invalid("max_delay_s", "max_delay_s must be at least base_delay_s")
        attempts, jitter, mode = payload["max_attempts"], payload["jitter"], payload["retry_mode"]
        processing, visibility, budget = _dec(payload["processing_timeout_s"]), _dec(payload["visibility_timeout_s"]), _dec(payload["budget_s"])
        schedule: list[dict[str, Any]] = []
        bounds: list[tuple[Decimal, Decimal]] = []
        delay = base
        for retry in range(1, attempts):
            high = min(delay, cap)
            low = {"none": high, "full": Decimal(0), "equal": high / 2}[jitter]
            bounds.append((low, high))
            schedule.append({"retry": retry, "delay_min_s": _r(low, 3), "delay_max_s": _r(high, 3)})
            delay = high * mult
        total_min = sum((low for low, _ in bounds), Decimal(0))
        total_max = sum((high for _, high in bounds), Decimal(0))
        worst = total_max + processing * attempts
        raw: list[tuple[str, str]] = []
        fits_visibility = True
        if processing > visibility:
            fits_visibility = False
            raw.append(("blocker", f"processing may take {_n(processing)} s but the visibility timeout is {_n(visibility)} s: "
                                   "the message is redelivered while still being processed"))
        if mode == "in-process":
            if worst > visibility:
                fits_visibility = False
                raw.append(("blocker", f"in-process retries take up to {_n(worst)} s, beyond the {_n(visibility)} s visibility "
                                       "timeout: the message becomes visible again mid-retry"))
        else:
            if visibility > SQS_MAX_VISIBILITY_S:
                fits_visibility = False
                raw.append(("major", f"visibility timeout {_n(visibility)} s exceeds the {SQS_MAX_VISIBILITY_S} s SQS maximum"))
            longest = max((_dec(s["delay_max_s"]) for s in schedule), default=Decimal(0))
            if longest + processing > SQS_MAX_VISIBILITY_S:
                fits_visibility = False
                raw.append(("major", f"the longest backoff {_n(longest)} s plus processing exceeds the {SQS_MAX_VISIBILITY_S} s "
                                     "maximum message visibility timeout"))
        fits_budget = worst <= budget
        if not fits_budget:
            raw.append(("major", f"worst case {_n(worst)} s exceeds the {_n(budget)} s retry budget"))
        if jitter == "none" and attempts > 1:
            raw.append(("minor", "no jitter: consumers that failed together retry together (thundering herd)"))
        if mult == 1 and attempts > 1:
            raw.append(("info", "constant delay: the schedule does not back off"))
        verdict, findings = _wrap(raw, "RS")
        return {"state": "SUCCESS", "attempts": attempts, "retries": attempts - 1, "schedule": schedule,
                "total_delay_min_s": _r(total_min, 3), "total_delay_max_s": _r(total_max, 3),
                "worst_case_total_s": _r(worst, 3), "fits_visibility": fits_visibility, "fits_budget": fits_budget,
                "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- DLQ coverage -------------------------------------------------------------------------------------------------


def dlq_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Every consumer needs a distinct DLQ, a max receive count, a redrive path, an alarm and longer DLQ retention."""

    def work() -> dict[str, Any]:
        consumers = payload["consumers"]
        names = [c["name"] for c in consumers]
        if len(set(names)) != len(names):
            return _invalid("consumers", "consumer names must be unique")
        shared: dict[str, list[str]] = {}
        for c in consumers:
            if c["dlq"]:
                shared.setdefault(c["dlq"], []).append(c["name"])
        raw: list[tuple[str, str]] = []
        uncovered: list[str] = []
        for c in sorted(consumers, key=lambda item: item["name"]):
            name, dlq, covered = _safe(c["name"], 60), _safe(c["dlq"], 60), True
            if not c["dlq"]:
                raw.append(("blocker", f"{name}: no dead-letter queue configured"))
                uncovered.append(_safe(c["name"], 80))
                continue
            if c["dlq"] == c["queue"]:
                raw.append(("blocker", f"{name}: the DLQ is the source queue itself"))
                covered = False
            if c["max_receive_count"] < 1:
                raw.append(("blocker", f"{name}: no max receive count, so messages never move to the DLQ"))
                covered = False
            elif c["max_receive_count"] > SQS_MAX_RECEIVE_COUNT:
                raw.append(("major", f"{name}: max receive count {c['max_receive_count']} exceeds the SQS limit of {SQS_MAX_RECEIVE_COUNT}, "
                                     "so the redrive policy is rejected and no message reaches the DLQ"))
                covered = False
            elif c["max_receive_count"] == 1:
                raw.append(("minor", f"{name}: max receive count 1 sends a message to the DLQ after a single transient failure"))
            elif c["max_receive_count"] > HIGH_RECEIVE_COUNT:
                raw.append(("minor", f"{name}: max receive count {c['max_receive_count']} keeps a poison message in flight "
                                     f"for more than {HIGH_RECEIVE_COUNT} attempts"))
            if c["fifo"] != c["dlq_fifo"]:
                raw.append(("blocker", f"{name}: queue and DLQ must both be FIFO or both standard"))
                covered = False
            if not c["redrive_runbook"]:
                raw.append(("major", f"{name}: no redrive path or runbook for messages in {dlq}"))
            if not c["dlq_alarm"]:
                raw.append(("minor", f"{name}: no alarm on DLQ depth for {dlq}"))
            if c["dlq_retention_days"] <= c["source_retention_days"]:
                raw.append(("major", f"{name}: DLQ retention {_n(c['dlq_retention_days'])} d must exceed source retention "
                                     f"{_n(c['source_retention_days'])} d (retention counts from the original enqueue time)"))
            if not covered:
                uncovered.append(_safe(c["name"], 80))
        for dlq in sorted(shared):
            if len(shared[dlq]) > 1:
                members = ", ".join(sorted(_safe(n, 60) for n in shared[dlq]))
                raw.append(("info", f"DLQ {_safe(dlq, 60)} is shared by {members}: poison messages of different consumers mix"))
        verdict, findings = _wrap(raw, "DL")
        return {"state": "SUCCESS", "consumers": len(consumers), "covered": len(consumers) - len(uncovered),
                "uncovered_consumers": sorted(uncovered), "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- idempotency --------------------------------------------------------------------------------------------------


def idempotency_window(payload: dict[str, Any]) -> dict[str, Any]:
    """Does the dedup window outlive the longest time a duplicate can still arrive, and is the key strategy sound?"""

    def work() -> dict[str, Any]:
        window = _dec(payload["dedup_window_s"])
        required = max(_dec(payload["retry_window_s"]), _dec(payload["redelivery_window_s"]), _dec(payload["replay_window_s"]))
        natural, key = payload["naturally_idempotent"], payload["key_source"]
        headroom = window - required
        raw: list[tuple[str, str]] = []
        if key == "none":
            raw.append(("info", "no idempotency key: safe only because the effect is naturally idempotent") if natural
                       else ("blocker", "no idempotency key: a redelivered message repeats its side effect"))
        elif key == "broker-message-id":
            raw.append(("major", "the broker message id changes when the producer retries a send and on replay: it is not a stable idempotency key"))
        elif key == "payload-hash":
            raw.append(("minor", "a payload hash collapses distinct but identical events (for example two equal commands)"))
        if key != "none":
            if window < required:
                raw.append(("minor" if natural else "blocker",
                            f"the dedup window of {_n(window)} s is shorter than the {_n(required)} s during which a duplicate can arrive"))
            if not payload["atomic_claim"] and not natural:
                raw.append(("major", "key check and effect are not one atomic claim (conditional write or unique constraint): "
                                     "concurrent redeliveries can both pass"))
        covered = bool(natural) or (key not in ("none", "broker-message-id") and window >= required and payload["atomic_claim"])
        verdict, findings = _wrap(raw, "ID")
        return {"state": "SUCCESS", "required_window_s": _r(required, 3), "headroom_s": _r(headroom, 3),
                "covered": covered, "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- ordering -----------------------------------------------------------------------------------------------------

# broker -> (ordering unit, keyed). unit "" means no ordering guarantee.
_ORDERING = {
    "kafka": ("partition", True), "kinesis": ("shard", True), "sqs-fifo": ("message group", True),
    "sns-fifo": ("message group", True), "pubsub": ("ordering key", True), "rabbitmq": ("queue", False),
    "sqs-standard": ("", False), "sns-standard": ("", False), "eventbridge": ("", False),
}


def ordering_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Does the broker's ordering unit, the routing key and the consumer/retry behaviour preserve per-entity order?"""

    def work() -> dict[str, Any]:
        broker = payload["broker"]
        unit, keyed = _ORDERING[broker]
        entity, route = set(payload["entity_key_fields"]), payload["partition_key_fields"]
        needed = payload["order_required"]
        if needed and unit and keyed and not entity:
            return _invalid("entity_key_fields", "name the fields that identify one entity: per-entity order cannot be judged without them")
        breaks: list[tuple[str, str]] = []  # what breaks order; blockers and majors when order is required, notes otherwise
        notes: list[tuple[str, str]] = []
        if unit and keyed:
            if not route:
                breaks.append(("blocker", f"no {unit} key: events of one entity spread over several {unit}s"))
            elif entity:
                extra = sorted(set(route) - entity)
                if extra:
                    breaks.append(("blocker", f"routing key field(s) {', '.join(_safe(n, 40) for n in extra)} are not part of the "
                                              f"entity key: events of one entity can land in different {unit}s"))
                elif set(route) < entity:
                    notes.append(("info", "the routing key is coarser than the entity key: order holds, but several entities share a "
                                          f"{unit} (watch skew)"))
        if unit and payload["handler_concurrency_per_unit"] > 1:
            breaks.append(("major", f"{payload['handler_concurrency_per_unit']} handlers work on one {unit} concurrently: "
                                    "processing order is not preserved"))
        if unit and payload["retry_mode"] == "retry-queue":
            breaks.append(("major", "failed messages are re-queued behind later ones (retry topic/queue): order is broken on every retry"))
        if broker == "kafka" and payload["producer_in_flight"] > 1 and not payload["idempotent_producer"]:
            breaks.append(("major", "more than one in-flight request without idempotent producer: a retried batch can overtake a later one"))
        if broker == "kafka" and payload["producer_in_flight"] > 5 and payload["idempotent_producer"]:
            breaks.append(("major", "idempotent producers preserve order only up to 5 in-flight requests"))
        if payload["partitions_will_change"]:
            if broker == "kafka":
                breaks.append(("major", "the partition count changes: keys are remapped, so ordering is not guaranteed across the change"))
            elif broker == "kinesis":
                breaks.append(("major", "resharding re-routes keys to child shards: read each parent shard to its end before its "
                                        "children, or order breaks"))
            elif unit:
                notes.append(("info", f"partitions_will_change does not apply to {broker}: its ordering unit ({unit}) is not a fixed "
                                      "partition set"))
        entity_unknown = bool(unit) and keyed and not entity
        if entity_unknown:
            notes.append(("info", "entity_key_fields is empty: per-entity order cannot be judged, so ordered is false"))
        raw: list[tuple[str, str]] = list(notes)
        if needed and not unit:
            raw.append(("blocker", "eventbridge (classic event bus) gives no ordering guarantee; the Custom Event Bus orders per event "
                                   "group for FIFO subscribers, otherwise use sqs-fifo, kinesis or kafka" if broker == "eventbridge"
                                   else f"{broker} gives no ordering guarantee"))
        if needed:
            raw += breaks
        else:
            raw += [("info", f"order is not required here; for information: {text}") for _, text in breaks]
        verdict, findings = _wrap(raw, "OR")
        ordered = bool(unit) and not entity_unknown and not any(sev in ("blocker", "major") for sev, _ in breaks)
        return {"state": "SUCCESS", "ordering_unit": unit, "ordered": ordered, "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- function sizing ----------------------------------------------------------------------------------------------


def function_sizing(payload: dict[str, Any]) -> dict[str, Any]:
    """Concurrency (Little's law on p95), timeout margin, downstream connection limit, cold start and queue trigger fit."""

    def work() -> dict[str, Any]:
        platform, memory, timeout = payload["platform"], payload["memory_mb"], payload["timeout_s"]
        if platform == "aws-lambda":
            if not LAMBDA_MEMORY_MB[0] <= memory <= LAMBDA_MEMORY_MB[1]:
                return _invalid("memory_mb", f"AWS Lambda memory is {LAMBDA_MEMORY_MB[0]}..{LAMBDA_MEMORY_MB[1]} MB")
            if timeout > LAMBDA_MAX_TIMEOUT_S:
                return _invalid("timeout_s", f"AWS Lambda timeout is at most {LAMBDA_MAX_TIMEOUT_S} s")
        p95, p99 = _dec(payload["p95_duration_ms"]), _dec(payload["p99_duration_ms"])
        if p99 < p95:
            return _invalid("p99_duration_ms", "p99_duration_ms must be at least p95_duration_ms")
        limit, per_instance = payload.get("downstream_max_connections"), payload.get("connections_per_instance")
        if (limit is None) != (per_instance is None):
            return _invalid("connections_per_instance", "downstream_max_connections and connections_per_instance go together")
        invocations = _dec(payload["peak_events_per_s"]) / payload["batch_size"]
        needed = max(1, _ceil(invocations * (1 + _dec(payload["headroom_pct"]) / 100) * p95 / 1000))
        safe = max(0, _floor(Decimal(limit) / Decimal(per_instance))) if limit is not None and per_instance is not None else -1
        reserved = payload.get("reserved_concurrency")
        raw: list[tuple[str, str]] = []
        timeout_ms = Decimal(timeout) * 1000
        if p99 >= timeout_ms:
            raw.append(("blocker", f"p99 duration {_n(p99)} ms reaches the {timeout} s timeout: at least 1% of invocations are cut off"))
        elif p99 > timeout_ms * TIMEOUT_MARGIN:
            raw.append(("minor", f"p99 duration {_n(p99)} ms is above 80% of the {timeout} s timeout: little safety margin"))
        if reserved is not None and reserved < needed:
            raw.append(("major", f"reserved concurrency {reserved} is below the {needed} needed at peak: events are throttled"))
        if limit is not None:
            if reserved is not None and reserved > safe:
                raw.append(("major", f"reserved concurrency {reserved} x {per_instance} connections exceeds the downstream limit of "
                                     f"{limit}; at most {safe} instances are safe"))
            if safe < needed:
                raw.append(("major", f"the downstream limit allows {safe} instances but peak needs {needed}: use a pooler or proxy, or buffer"))
        cold = _dec(payload["cold_start_ms"])
        provisioned = 0
        if cold > _dec(payload["cold_start_budget_ms"]):
            provisioned = needed
            raw.append(("major", f"cold start {_n(cold)} ms exceeds the {_n(payload['cold_start_budget_ms'])} ms budget: "
                                 f"provision {needed} warm instance(s) or shrink the package"))
        visibility = payload.get("source_visibility_timeout_s")
        if visibility is not None:
            if platform == "aws-lambda" and visibility < LAMBDA_SQS_VISIBILITY_FACTOR * timeout:
                raw.append(("major", f"source queue visibility timeout {visibility} s is below {LAMBDA_SQS_VISIBILITY_FACTOR}x the "
                                     f"{timeout} s function timeout ({LAMBDA_SQS_VISIBILITY_FACTOR * timeout} s)"))
            elif platform != "aws-lambda" and visibility < timeout:
                raw.append(("blocker", f"source visibility timeout {visibility} s is below the {timeout} s function timeout"))
        verdict, findings = _wrap(raw, "FS")
        vcpu = memory / LAMBDA_MB_PER_VCPU if platform == "aws-lambda" else Decimal(0)
        return {"state": "SUCCESS", "concurrency_needed": needed, "max_safe_concurrency": safe,
                "provisioned_concurrency_suggested": provisioned, "vcpu_equivalent": _r(vcpu, 2),
                "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- autoscaler ---------------------------------------------------------------------------------------------------


def scaler_sanity(payload: dict[str, Any]) -> dict[str, Any]:
    """Bounds, partitions, reaction time, cooldown and shutdown grace of a KEDA/HPA-style event-driven scaler. Reaction
    time is the house approximation polling interval + replica startup; KEDA's `cooldownPeriod` only delays the scale to
    zero (from the last active trigger), so it is judged only when min_replicas is 0."""

    def work() -> dict[str, Any]:
        low, high = payload["min_replicas"], payload["max_replicas"]
        polling, startup = _dec(payload["polling_interval_s"]), _dec(payload["replica_startup_s"])
        partitions = payload.get("partitions")
        needed = _ceil(_dec(payload["peak_backlog"]) / _dec(payload["target_backlog_per_replica"]))
        effective = min(high, partitions) if partitions is not None else high
        reaction = polling + startup
        raw: list[tuple[str, str]] = []
        if low > high:
            raw.append(("blocker", f"min_replicas {low} is above max_replicas {high}"))
        elif low == high:
            raw.append(("minor", f"min equals max ({low}): the scaler cannot scale"))
        if needed > effective:
            cause = f"only {partitions} partitions can be consumed in parallel" if partitions is not None and partitions < high \
                else f"max_replicas is {high}"
            raw.append(("major", f"peak backlog needs {needed} replicas but the effective ceiling is {effective} ({cause}): lag will grow"))
        if reaction > _dec(payload["max_reaction_s"]):
            raw.append(("major", f"reaction time {_n(reaction)} s (polling {_n(polling)} s + startup {_n(startup)} s) exceeds the "
                                 f"{_n(payload['max_reaction_s'])} s target"))
        if low == 0:
            raw.append(("info", f"scale to zero: the first event waits up to {_n(reaction)} s for a replica"))
        if low == 0 and payload["cooldown_period_s"] < payload["polling_interval_s"]:
            raw.append(("minor", "the cooldown is shorter than the polling interval: scale to zero is only evaluated once per poll, so "
                                 "the effective quiet period before it is the polling interval"))
        if payload["termination_grace_period_s"] < payload["max_processing_s"]:
            raw.append(("major", f"termination grace {payload['termination_grace_period_s']} s is shorter than the longest message "
                                 f"processing {payload['max_processing_s']} s: scale-down kills in-flight work"))
        verdict, findings = _wrap(raw, "AS")
        return {"state": "SUCCESS", "needed_replicas": needed, "effective_max_replicas": effective,
                "reaction_time_s": _r(reaction, 3), "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- saga ---------------------------------------------------------------------------------------------------------


def saga_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    """Compensable steps need idempotent compensations, one pivot, retriable steps after it, and an outbox for events."""

    def work() -> dict[str, Any]:
        steps = payload["steps"]
        names = [s["name"] for s in steps]
        if len(set(names)) != len(names):
            return _invalid("steps", "step names must be unique")
        pivots = [i for i, s in enumerate(steps) if s["kind"] == "pivot"]
        raw: list[tuple[str, str]] = []
        uncompensated: list[str] = []
        if len(pivots) > 1:
            raw.append(("major", f"{len(pivots)} pivot steps ({', '.join(_safe(steps[i]['name'], 40) for i in pivots)}): "
                                 "a saga has one commit point"))
        first_pivot = pivots[0] if pivots else len(steps)
        compensation: Callable[[Any], str] = lambda step: step["compensation"].strip()  # noqa: E731 - blank text is no compensating action
        for index, step in enumerate(steps):
            name, kind = _safe(step["name"], 60), step["kind"]
            if kind == "compensable":
                if not compensation(step):
                    raw.append(("blocker", f"{name}: compensable step has no compensating action"))
                    uncompensated.append(_safe(step["name"], 80))
                elif not step["compensation_idempotent"]:
                    raw.append(("major", f"{name}: compensation {_safe(compensation(step), 60)} is not idempotent; it can be retried or "
                                         "redelivered"))
                if not step["idempotent"]:
                    raw.append(("minor", f"{name}: compensable step is not idempotent: a redelivered command repeats its effect"))
                if index > first_pivot:
                    raw.append(("major", f"{name}: compensable step after the pivot cannot be rolled back once the pivot committed"))
            elif kind == "pivot":
                if not step["idempotent"]:
                    raw.append(("major", f"{name}: the pivot is not idempotent: a redelivered command can commit twice"))
            elif kind == "retriable":
                if not step["idempotent"]:
                    raw.append(("major", f"{name}: retriable step is retried until it succeeds and must be idempotent"))
                if any(later["kind"] in ("compensable", "pivot") for later in steps[index + 1:]):
                    raw.append(("major", f"{name}: retriable step before a later compensable or pivot step is not undone if that step fails"))
        if not payload["outbox"]:
            raw.append(("major", "no transactional outbox: the state change and the event publish are a dual write that can diverge"))
        compensable = [s for s in steps if s["kind"] == "compensable"]
        order = [_safe(compensation(s), 120) for s in reversed(steps[:first_pivot]) if s["kind"] == "compensable" and compensation(s)]
        verdict, findings = _wrap(raw, "SG")
        return {"state": "SUCCESS", "steps": len(steps), "compensable_steps": len(compensable),
                "compensated_steps": sum(1 for s in compensable if compensation(s)),
                "uncompensated_steps": sorted(uncompensated), "pivot": _safe(steps[pivots[0]]["name"], 80) if pivots else "",
                "compensation_order": order, "verdict": verdict, "findings": findings}

    return _respond(payload, work)


# --- retention and replay -----------------------------------------------------------------------------------------


def retention_replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Can the retained log be replayed from `replay_from_age_s`, how long does catch-up take, and is it restartable?"""

    def work() -> dict[str, Any]:
        retention, age = _dec(payload["retention_s"]), _dec(payload["replay_from_age_s"])
        events, rate, live = _dec(payload["replay_events"]), _dec(payload["replay_rate_per_s"]), _dec(payload["live_rate_per_s"])
        compacted, prod = payload["compacted"], payload["environment"] == "prod"
        raw: list[tuple[str, str]] = []
        net = rate - live
        duration = events / net if net > 0 else Decimal(-1)
        if duration > _MAX_FINITE_S:
            return _invalid("replay_events", "replay_events at replay_rate_per_s takes longer than a finite duration can represent")
        if events > 0 and net <= 0:
            raw.append(("blocker", f"replay rate {_n(rate)} evt/s does not exceed the live rate {_n(live)} evt/s: replay never catches up"))
        elif events == 0:
            duration = Decimal(0)
        restartable = False
        headroom = retention - age
        if compacted:
            raw.append(("info", "compacted log: only the latest record per key is retained, so replay rebuilds state, not history"))
            restartable = duration >= 0
        else:
            if age > retention:
                raw.append(("blocker", f"replay starts {_n(age)} s back but only {_n(retention)} s is retained: older events are gone"))
            elif duration >= 0:
                restartable = age + duration <= retention
                if not restartable:
                    raw.append(("major", f"replay needs {_n(_dec(round(float(duration), 1)))} s from a point {_n(age)} s old: "
                                         f"the start offset expires after {_n(retention)} s, so a failed replay cannot restart"))
        if not payload["consumers_idempotent"]:
            raw.append(("blocker", "consumers are not idempotent: a replay repeats their side effects"))
        if prod:
            raw.append(("info", "replay or backfill in production is autonomy level L3: explicit approval is required"))
        verdict, findings = _wrap(raw, "RR")
        return {"state": "SUCCESS", "replay_duration_s": _r(duration, 2), "restartable": restartable,
                "retention_headroom_s": _r(headroom, 2), "requires_l3_approval": prod,
                "verdict": verdict, "findings": findings}

    return _respond(payload, work)
