"""A07 DATA lane: pure checks over migration SQL, foreign-key edges, field sets, versions, and row counts.

Mission: give the Data Engineer repeatable evidence for migration safety (destructive DDL,
NOT NULL additions), naming, foreign-key ordering, PII tagging, contract compatibility,
row-count reconciliation, migration ordering, and up/down pairing. All tools are READ tier.

Tools:
  data_destructive_ddl_scan     classify destructive DDL statements in SQL text
  data_not_null_default_check   ADD COLUMN NOT NULL or PRIMARY KEY without DEFAULT
  data_index_name_check         CREATE INDEX names against prefix_table_suffix
  data_fk_cycle_check           cycle in supplied foreign-key edges
  data_pii_tag_coverage         share of PII columns carrying an allowed tag
  data_contract_field_compat    breaking vs additive changes between two field sets
  data_row_count_reconcile      before + delta against observed after
  data_migration_version_order  unique and strictly increasing version ids
  data_reversible_pair_check    up/down and Flyway V/U file pairing by name
  data_lock_risk_scan           DDL that takes blocking locks (index build, validating FK/CHECK, SET NOT NULL)
  data_explain_regression_check shadow plan cost/time vs baseline against the percentage budget

Most tools split SQL on ';' after comment removal, so ';', '--' or keywords inside string literals can
mislead them. data_lock_risk_scan uses a real lexer instead (comments, '..', E'..', "..", $tag$..$tag$) and also scans
the statements inside DO bodies and EXECUTE strings; bodies of CREATE FUNCTION/PROCEDURE are not run by the migration and
are not scanned. Its lock_timeout model: SET [SESSION] and SELECT set_config(.., false) last until RESET, SET .. TO 0 or
DEFAULT, or ROLLBACK of the enclosing BEGIN; SET LOCAL and set_config(.., true) last until COMMIT/ROLLBACK/END (outside an
explicit BEGIN they count as covering the migration runner's own transaction).
"""
from __future__ import annotations

import re
from fractions import Fraction
from typing import Any, NamedTuple, cast

from swarm.tools.support import respond

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _statements(sql: str) -> list[str]:
    cleaned = _LINE_COMMENT.sub(" ", _BLOCK_COMMENT.sub(" ", sql))
    return [" ".join(part.split()) for part in cleaned.split(";") if part.strip()]


def _no_statements() -> dict[str, Any]:
    return {"state": "INVALID_INPUT", "field": "sql", "message": "no SQL statements after removing comments"}


def _top_level_parts(body: str) -> list[str]:
    """Split on commas that sit outside parentheses."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    parts.append("".join(current).strip())
    return [part for part in parts if part]


# --- data_destructive_ddl_scan -------------------------------------------------------

_DESTRUCTIVE = (
    ("DROP_TABLE", re.compile(r"^DROP\s+TABLE\b")),
    ("DROP_SCHEMA", re.compile(r"^DROP\s+SCHEMA\b")),
    ("DROP_DATABASE", re.compile(r"^DROP\s+DATABASE\b")),
    ("TRUNCATE", re.compile(r"^TRUNCATE\b")),
    (
        "DROP_COLUMN",
        re.compile(
            r"^ALTER\s+TABLE\b.*\bDROP\s+(?:COLUMN\s+)?(?:IF\s+EXISTS\s+)?"
            r"(?!(?:CONSTRAINT|DEFAULT|NOT\s+NULL|IDENTITY|EXPRESSION|PRIMARY|FOREIGN|UNIQUE|CHECK|INDEX|EXCLUDE)\b)[\w\"`\[]"
        ),
    ),
    (
        "COLUMN_TYPE_CHANGE",
        re.compile(r"^ALTER\s+TABLE\b.*\bALTER\s+(?:COLUMN\s+)?\S+\s+(?:SET\s+DATA\s+)?TYPE\b"),
    ),
)


def _destructive_kinds(statement: str) -> list[str]:
    upper = statement.upper()
    return [kind for kind, pattern in _DESTRUCTIVE if pattern.search(upper)]


def data_destructive_ddl_scan(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        statements = _statements(payload["sql"])
        if not statements:
            return _no_statements()
        kinds: set[str] = set()
        flagged = 0
        for statement in statements:
            found = _destructive_kinds(statement)
            if found:
                flagged += 1
                kinds.update(found)
        return {
            "state": "SUCCESS",
            "destructive": flagged > 0,
            "statement_count": len(statements),
            "destructive_count": flagged,
            "kinds": sorted(kinds),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_not_null_default_check -----------------------------------------------------

_ALTER_TABLE = re.compile(r'^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([\w."]+)\s+(.*)$', re.I)
_ADD_COLUMN = re.compile(r'^ADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([\w"]+)\s+(.*)$', re.I)
_NOT_COLUMN_WORDS = frozenset({"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "EXCLUDE", "INDEX", "KEY"})
_NEEDS_VALUE = re.compile(r"\bNOT\s+NULL\b|\bPRIMARY\s+KEY\b", re.I)
_HAS_DEFAULT = re.compile(r"\bDEFAULT\b", re.I)


def data_not_null_default_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        statements = _statements(payload["sql"])
        if not statements:
            return _no_statements()
        checked = 0
        unsafe: list[str] = []
        for statement in statements:
            alter = _ALTER_TABLE.match(statement)
            if not alter:
                continue
            table = alter.group(1).replace('"', "")
            for part in _top_level_parts(alter.group(2)):
                add = _ADD_COLUMN.match(part)
                if not add:
                    continue
                column = add.group(1).replace('"', "")
                if column.upper() in _NOT_COLUMN_WORDS:
                    continue
                checked += 1
                definition = add.group(2)
                if _NEEDS_VALUE.search(definition) and not _HAS_DEFAULT.search(definition):
                    unsafe.append(f"{table}.{column}")
        return {
            "state": "SUCCESS",
            "checked_columns": checked,
            "unsafe_columns": unsafe,
            "unsafe_count": len(unsafe),
            "safe": not unsafe,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_index_name_check -----------------------------------------------------------

_CREATE_INDEX = re.compile(
    r'^CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?([\w"]+)\s+ON\s+(?:ONLY\s+)?([\w."]+)',
    re.I,
)
_ANY_INDEX = re.compile(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\b", re.I)
_NOT_INDEX_NAMES = frozenset({"CONCURRENTLY", "IF", "ON", "ONLY"})
_PREFIX = re.compile(r"[a-z][a-z0-9]{0,15}")
_SNAKE = re.compile(r"[a-z0-9_]+")
_MAX_IDENTIFIER = 63


def data_index_name_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        prefix = payload["prefix"]
        if not _PREFIX.fullmatch(prefix):
            return {
                "state": "INVALID_INPUT",
                "field": "prefix",
                "message": "prefix must start with a lowercase letter and hold at most 16 lowercase letters or digits",
            }
        statements = _statements(payload["sql"])
        if not statements:
            return _no_statements()
        indexes = [statement for statement in statements if _ANY_INDEX.match(statement)]
        violations: list[str] = []
        for statement in indexes:
            named = _CREATE_INDEX.match(statement)
            if not named or named.group(1).upper() in _NOT_INDEX_NAMES:
                violations.append("unnamed index: a name is required")
                continue
            name = named.group(1).replace('"', "")
            table = named.group(2).replace('"', "").rsplit(".", 1)[-1]
            reasons: list[str] = []
            if not _SNAKE.fullmatch(name):
                reasons.append("must be lowercase letters, digits and underscores")
            if len(name) > _MAX_IDENTIFIER:
                reasons.append(f"longer than {_MAX_IDENTIFIER} characters")
            stem = f"{prefix}_{table}_"
            if not name.startswith(stem) or len(name) == len(stem):
                reasons.append(f"must start with {stem}")
            if reasons:
                violations.append(f"{name}: {'; '.join(reasons)}")
        return {
            "state": "SUCCESS",
            "index_count": len(indexes),
            "violations": violations,
            "violation_count": len(violations),
            "conforming": not violations,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_fk_cycle_check -------------------------------------------------------------


def _find_cycle(graph: dict[str, set[str]]) -> list[str]:
    """Depth-first search over sorted nodes. Returns one cycle with its start repeated at the end, or []."""
    unseen, active, done = 0, 1, 2
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = active
        path.append(node)
        for nxt in sorted(graph[node]):
            seen = state.get(nxt, unseen)
            if seen == active:
                return path[path.index(nxt):] + [nxt]
            if seen == unseen:
                found = visit(nxt)
                if found:
                    return found
        path.pop()
        state[node] = done
        return []

    for node in sorted(graph):
        if state.get(node, unseen) == unseen:
            found = visit(node)
            if found:
                return found
    return []


def data_fk_cycle_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        edges = payload["edges"]
        allow_self = payload.get("allow_self_reference", False)
        nodes = {edge["from"] for edge in edges} | {edge["to"] for edge in edges}
        graph: dict[str, set[str]] = {node: set() for node in nodes}
        for edge in edges:
            if edge["from"] != edge["to"]:
                graph[edge["from"]].add(edge["to"])
        self_refs = sorted({edge["from"] for edge in edges if edge["from"] == edge["to"]})
        if self_refs and not allow_self:
            cycle = [self_refs[0], self_refs[0]]
        else:
            cycle = _find_cycle(graph)
        return {
            "state": "SUCCESS",
            "edge_count": len(edges),
            "table_count": len(nodes),
            "has_cycle": bool(cycle),
            "cycle": cycle,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_pii_tag_coverage -----------------------------------------------------------


def data_pii_tag_coverage(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        columns = payload["columns"]
        names = [column["name"] for column in columns]
        if len(set(names)) != len(names):
            return {"state": "INVALID_INPUT", "field": "columns", "message": "column names must be unique"}
        allowed = set(payload["allowed_tags"])
        pii = [column for column in columns if column["pii"]]
        untagged = sorted(column["name"] for column in pii if column["tag"].strip() not in allowed)
        tagged = len(pii) - len(untagged)
        coverage = round(tagged / len(pii), 4) if pii else 1.0
        return {
            "state": "SUCCESS",
            "column_count": len(columns),
            "pii_columns": len(pii),
            "tagged_pii_columns": tagged,
            "untagged": untagged,
            "coverage": coverage,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_contract_field_compat ------------------------------------------------------


def data_contract_field_compat(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        indexes: dict[str, dict[str, dict[str, Any]]] = {}
        for label in ("before", "after"):
            index = {field["name"]: field for field in payload[label]}
            if len(index) != len(payload[label]):
                return {"state": "INVALID_INPUT", "field": label, "message": "field names must be unique"}
            indexes[label] = index
        old, new = indexes["before"], indexes["after"]
        breaking: list[str] = []
        additive: list[str] = []
        for name in sorted(old.keys() | new.keys()):
            if name not in new:
                breaking.append(f"{name}: removed")
            elif name not in old:
                if new[name]["required"]:
                    breaking.append(f"{name}: new required field")
                else:
                    additive.append(f"{name}: new optional field")
            else:
                was, now = old[name], new[name]
                if was["type"] != now["type"]:
                    breaking.append(f"{name}: type {was['type']} -> {now['type']}")
                if not was["required"] and now["required"]:
                    breaking.append(f"{name}: became required")
                elif was["required"] and not now["required"]:
                    additive.append(f"{name}: relaxed to optional")
        return {
            "state": "SUCCESS",
            "compatible": not breaking,
            "breaking": breaking,
            "additive": additive,
            "breaking_count": len(breaking),
            "additive_count": len(additive),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_row_count_reconcile --------------------------------------------------------


def data_row_count_reconcile(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        before, after, delta = payload["before"], payload["after"], payload["delta"]
        expected = before + delta
        if expected < 0:
            return {"state": "INVALID_INPUT", "field": "delta", "message": "delta would make the row count negative"}
        if before > 0 and after == 0 and expected == 0:
            return {
                "state": "INVALID_INPUT",
                "field": "after",
                "message": (
                    "before > 0, after = 0 and delta = -before always reconcile, and a dropped table reads the same as an emptied one; "
                    "count with a query that errors when the table is absent, and state a partial loss or confirm the drop from the DDL scan"
                ),
            }
        drift = after - expected
        return {
            "state": "SUCCESS",
            "expected_after": expected,
            "observed_change": after - before,
            "drift": drift,
            "reconciled": drift == 0,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_migration_version_order ----------------------------------------------------

_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


_VersionKey = tuple[tuple[int, int, str], ...]


def _version_key(version: str) -> _VersionKey:
    """Natural order: digit runs compare as integers, other runs as text."""
    return tuple(
        (0, int(token), "") if token.isdigit() else (1, 0, token)
        for token in re.findall(r"\d+|\D+", version)
    )


def data_migration_version_order(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        versions = payload["versions"]
        for version in versions:
            if not _VERSION.fullmatch(version):
                return {
                    "state": "INVALID_INPUT",
                    "field": "versions",
                    "message": f"invalid version id {version!r}; use letters, digits, '.', '_' or '-'",
                }
        duplicates: list[str] = []
        out_of_order: list[str] = []
        seen: set[_VersionKey] = set()
        highest: _VersionKey | None = None
        for version in versions:
            key = _version_key(version)
            if key in seen:
                duplicates.append(version)
            elif highest is not None and key < highest:
                out_of_order.append(version)
            seen.add(key)
            highest = key if highest is None else max(highest, key)
        return {
            "state": "SUCCESS",
            "count": len(versions),
            "ordered": not duplicates and not out_of_order,
            "duplicates": duplicates,
            "out_of_order": out_of_order,
            "latest": max(versions, key=_version_key),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_reversible_pair_check ------------------------------------------------------

_UP_DOWN = re.compile(r"(.+)\.(up|down)\.sql")
_FLYWAY = re.compile(r"([VU])([0-9][^_]*)__(.+)\.sql")
_MIGRATION_LIKE = re.compile(r"\.(?:sql|py|js|ts|mjs|cjs|rb|prisma|xml|ya?ml)$", re.I)


def _unchecked_migration(filename: str) -> bool:
    """A file the pairing cannot judge but that may be a migration (alembic .py, prisma .sql, ...); Flyway R__ has no reverse."""
    base = filename.rsplit("/", 1)[-1]
    return not base.startswith("R__") and _MIGRATION_LIKE.search(base) is not None


def _pair_key(filename: str) -> tuple[str, str, str] | None:
    """Return (direction, family, display) for a recognised migration file, else None."""
    name = filename.rsplit("/", 1)[-1]
    up_down = _UP_DOWN.fullmatch(name)
    if up_down:
        direction = "forward" if up_down.group(2) == "up" else "reverse"
        return direction, "up-down", up_down.group(1)
    flyway = _FLYWAY.fullmatch(name)
    if flyway:
        direction = "forward" if flyway.group(1) == "V" else "reverse"
        return direction, "flyway", f"V{flyway.group(2)}__{flyway.group(3)}"
    return None


def data_reversible_pair_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        filenames = payload["filenames"]
        forward: set[tuple[str, str]] = set()
        reverse: set[tuple[str, str]] = set()
        unrecognized: list[str] = []
        for filename in filenames:
            parsed = _pair_key(filename)
            if parsed is None:
                unrecognized.append(filename)
                continue
            direction, family, display = parsed
            (forward if direction == "forward" else reverse).add((family, display))
        return {
            "state": "SUCCESS",
            "file_count": len(filenames),
            "recognized_count": len(forward) + len(reverse),
            "paired": sorted(key[1] for key in forward & reverse),
            "missing_reverse": sorted(key[1] for key in forward - reverse),
            "orphan_reverse": sorted(key[1] for key in reverse - forward),
            "unrecognized": sorted(unrecognized),
            "ok": forward == reverse and not any(_unchecked_migration(name) for name in unrecognized),
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_lock_risk_scan -------------------------------------------------------------

_CREATE_ANY_INDEX = re.compile(r"^CREATE\s+(?:UNIQUE\s+)?INDEX\b(?P<rest>.*)$", re.I)
_CONCURRENTLY = re.compile(r"^\s*CONCURRENTLY\b", re.I)
_IDENT = r'(?:"[^"]+"|\w+)'
_ADD_CONSTRAINT = re.compile(
    rf"^ADD\s+(?:CONSTRAINT\s+{_IDENT}\s+)?(FOREIGN\s+KEY|CHECK|UNIQUE|PRIMARY\s+KEY)\b(?P<rest>.*)$", re.I
)
_NOT_VALID = re.compile(r"\bNOT\s+VALID\b", re.I)
_USING_INDEX = re.compile(r"\bUSING\s+INDEX\b", re.I)
_SET_NOT_NULL = re.compile(rf"^ALTER\s+(?:COLUMN\s+)?{_IDENT}\s+SET\s+NOT\s+NULL\b", re.I)
_COLUMN_UNIQUE = re.compile(r"\bUNIQUE\b|\bPRIMARY\s+KEY\b", re.I)
_COLUMN_REFERENCES = re.compile(r"\bREFERENCES\b", re.I)

_MAX_BODY_DEPTH = 4
_SQL_TOKEN = re.compile(
    r"""--[^\n]*
    |/\*
    |(?<![\w$])[Ee]'(?:[^'\\]|\\.|'')*(?:'|\Z)
    |'(?:[^']|'')*(?:'|\Z)
    |"(?:[^"]|"")*(?:"|\Z)
    |(?<![\w$])\$(?:[A-Za-z_\u0080-\uffff][\w\u0080-\uffff]*)?\$
    |;
    |\s+
    |(?:[^\s;'"$/\-]|-(?!-)|/(?!\*))+
    |.""",
    re.X | re.S,
)
_BLOCK_COMMENT_EDGE = re.compile(r"/\*|\*/")


class _Stmt(NamedTuple):
    """One statement: `masked` has literal and quoted-identifier contents replaced by '_' (same length as `raw`)."""

    masked: str
    raw: str
    literals: tuple[str, ...]


def _split_sql(sql: str) -> list[_Stmt]:
    """Split on top-level ';' like Postgres: comments (nested), '..', E'..', "..", and $tag$..$tag$ never split or match keywords."""
    statements: list[_Stmt] = []
    masked: list[str] = []
    raw: list[str] = []
    literals: list[str] = []
    last_space = True

    def flush() -> None:
        nonlocal masked, raw, literals, last_space
        text = "".join(masked)
        lead = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        if end > lead:
            statements.append(_Stmt(text[lead:end], "".join(raw)[lead:end], tuple(literals)))
        masked, raw, literals, last_space = [], [], [], True

    def put(mask: str, original: str) -> None:
        nonlocal last_space
        masked.append(mask)
        raw.append(original)
        last_space = False

    def space() -> None:
        nonlocal last_space
        if not last_space:
            masked.append(" ")
            raw.append(" ")
            last_space = True

    position, size = 0, len(sql)
    while position < size:
        token = _SQL_TOKEN.match(sql, position)
        assert token is not None  # _SQL_TOKEN ends in '.', which matches any character under re.S
        text = token.group(0)
        position = token.end()
        head = text[0]
        if text == ";":
            flush()
        elif text == "/*":
            depth = 1
            while depth and position < size:
                edge = _BLOCK_COMMENT_EDGE.search(sql, position)
                if edge is None:
                    position = size
                    break
                depth += 1 if edge.group(0) == "/*" else -1
                position = edge.end()
            space()
        elif text.startswith("--") or text.isspace():
            space()
        elif head == '"':
            put('"' + "_" * (len(text) - 2) + '"' if len(text) > 2 else text, text)
        elif head in "'Ee" and "'" in text[:2]:
            escaped = head != "'"
            start = 2 if escaped else 1
            inner = text[start:-1] if text.endswith("'") and len(text) > start else text[start:]
            literals.append(inner if escaped else inner.replace("''", "'"))
            put(text[:start] + "_" * (len(text) - start - 1) + "'", text)
        elif head == "$" and len(text) >= 2 and text.endswith("$"):
            end = sql.find(text, position)
            body_end = size if end < 0 else end
            literals.append(sql[position:body_end])
            closing = "" if end < 0 else text
            whole = text + sql[position:body_end] + closing
            position = size if end < 0 else end + len(text)
            put("'" + "_" * (len(whole) - 2) + "'", whole)
        else:
            put(text, text)
    flush()
    return statements


_DO_STATEMENT = re.compile(r"^DO\b", re.I)
_EXECUTE_STATEMENT = re.compile(r"^EXECUTE\b", re.I)
_FORMAT_SPECIFIER = re.compile(r"%[%ILs]")
_PLPGSQL_PREFIXES = (
    re.compile(r"(?:BEGIN|DECLARE|ELSE|LOOP|THEN)\b\s*", re.I),
    re.compile(r"(?:IF|ELSIF|ELSEIF|WHEN|FOR|WHILE|EXCEPTION\s+WHEN)\b.*?\b(?:THEN|LOOP)\b\s*", re.I),
)


def _strip_plpgsql_prefix(statement: _Stmt) -> _Stmt:
    """Drop block keywords before the real statement of a DO body: BEGIN, IF .. THEN, ELSE, LOOP, EXCEPTION WHEN .. THEN."""
    offset = 0
    changed = True
    while changed:
        changed = False
        for prefix in _PLPGSQL_PREFIXES:
            match = prefix.match(statement.masked, offset)
            if match and match.end() > offset:
                offset = match.end()
                changed = True
    return statement if offset == 0 else _Stmt(statement.masked[offset:], statement.raw[offset:], statement.literals)


def _expand(index: int, statement: _Stmt, inner: bool, depth: int, out: list[tuple[int, _Stmt, bool]]) -> None:
    """Append `statement` and, for a DO body or an EXECUTE inside one, the statements it runs, all under the outer index."""
    if inner:
        statement = _strip_plpgsql_prefix(statement)
    out.append((index, statement, inner))
    if depth >= _MAX_BODY_DEPTH or not statement.literals:
        return
    if not inner and _DO_STATEMENT.match(statement.masked):
        bodies = [max(statement.literals, key=len)]
    elif inner and _EXECUTE_STATEMENT.match(statement.masked):
        bodies = [_FORMAT_SPECIFIER.sub("x", literal) for literal in statement.literals]
    else:
        return
    for body in bodies:
        for nested in _split_sql(body):
            _expand(index, nested, True, depth + 1, out)


_SET_LOCK_TIMEOUT = re.compile(r"^SET\s+(?:(SESSION|LOCAL)\s+)?lock_timeout\b\s*(?:=|\bTO\b)\s*(.*)$", re.I | re.S)
_RESET_LOCK_TIMEOUT = re.compile(r"^(?:RESET\s+(?:lock_timeout|ALL)|DISCARD\s+ALL)\b", re.I)
_SET_CONFIG = re.compile(
    r"^SELECT\s+set_config\s*\(\s*'lock_timeout'\s*,\s*'([^']*)'\s*,\s*(true|false)\s*\)", re.I
)
_TRANSACTION_BEGIN = re.compile(r"^(?:BEGIN|START\s+TRANSACTION)\b", re.I)
_TRANSACTION_END = re.compile(r"^(?:COMMIT|END|ROLLBACK|ABORT)\b", re.I)
_ROLLBACK = re.compile(r"^(?:ROLLBACK|ABORT)\b", re.I)
_SAVEPOINT_ROLLBACK = re.compile(r"^ROLLBACK\s+(?:WORK\s+|TRANSACTION\s+)?TO\b", re.I)
_TIMEOUT_VALUE = re.compile(r"(\d+(?:\.\d*)?|\.\d+)\s*(?:us|ms|s|min|h|d)?", re.I)


def _timeout_value_on(value: str) -> bool | None:
    """True when a lock_timeout value enables a timeout, False for 0 or DEFAULT, None when unparseable."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1]
    elif value.upper() == "DEFAULT":
        return False
    match = _TIMEOUT_VALUE.fullmatch(value.strip())
    if match is None:
        return None
    return Fraction(match.group(1)) > 0


class _TimeoutState:
    """lock_timeout as Postgres would hold it while the script runs: session value, SET LOCAL value, and transaction undo."""

    def __init__(self) -> None:
        self.session = False
        self.local: bool | None = None
        self.in_transaction = False
        self.saved_session = False
        self.ever_enabled = False

    @property
    def effective(self) -> bool:
        return self.session if self.local is None else self.local

    def _assign(self, enabled: bool, local: bool) -> None:
        if local:
            self.local = enabled
        else:
            self.session = enabled
        self.ever_enabled = self.ever_enabled or enabled

    def apply(self, statement: _Stmt, inner: bool) -> None:
        raw = statement.raw
        setter = _SET_LOCK_TIMEOUT.match(raw)
        if setter:
            enabled = _timeout_value_on(setter.group(2))
            if enabled is not None:
                self._assign(enabled, (setter.group(1) or "").upper() == "LOCAL")
            return
        config = _SET_CONFIG.match(raw)
        if config:
            enabled = _timeout_value_on("'" + config.group(1) + "'")
            if enabled is not None:
                self._assign(enabled, config.group(2).lower() == "true")
            return
        if _RESET_LOCK_TIMEOUT.match(raw):
            self.session, self.local = False, None
            return
        if inner:
            return
        if _TRANSACTION_BEGIN.match(raw):
            if not self.in_transaction:
                self.in_transaction, self.saved_session, self.local = True, self.session, None
        elif _TRANSACTION_END.match(raw) and not _SAVEPOINT_ROLLBACK.match(raw):
            if _ROLLBACK.match(raw) and self.in_transaction:
                self.session = self.saved_session
            self.in_transaction, self.local = False, None


def _lock_risks(statement: str) -> list[str]:
    """Blocking-lock kinds in one masked statement, each at most once, in a stable order."""
    kinds: list[str] = []
    index = _CREATE_ANY_INDEX.match(statement)
    if index:
        if not _CONCURRENTLY.match(index.group("rest")):
            kinds.append("create-index-blocking")
        return kinds
    alter = _ALTER_TABLE.match(statement)
    if not alter:
        return kinds
    for part in _top_level_parts(alter.group(2)):
        constraint = _ADD_CONSTRAINT.match(part)
        if constraint:
            family = " ".join(constraint.group(1).upper().split())
            rest = constraint.group("rest")
            if family == "FOREIGN KEY" and not _NOT_VALID.search(rest):
                kinds.append("add-foreign-key-validating")
            elif family == "CHECK" and not _NOT_VALID.search(rest):
                kinds.append("add-check-validating")
            elif family in ("UNIQUE", "PRIMARY KEY") and not _USING_INDEX.search(rest):
                kinds.append("add-unique-index-build")
            continue
        column = _ADD_COLUMN.match(part)
        if column and column.group(1).replace('"', "").upper() not in _NOT_COLUMN_WORDS:
            definition = column.group(2)
            if _COLUMN_UNIQUE.search(definition):
                kinds.append("add-unique-index-build")
            if _COLUMN_REFERENCES.search(definition) and not _NOT_VALID.search(definition):
                kinds.append("add-foreign-key-validating")
        elif _SET_NOT_NULL.match(part):
            kinds.append("set-not-null-scan")
    return sorted(set(kinds), key=kinds.index)


def data_lock_risk_scan(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        top_level = _split_sql(payload["sql"])
        if not top_level:
            return _no_statements()
        flat: list[tuple[int, _Stmt, bool]] = []
        for position, statement in enumerate(top_level, 1):
            _expand(position, statement, False, 0, flat)
        risks: list[dict[str, Any]] = []
        state = _TimeoutState()
        unprotected = False
        for position, statement, inner in flat:
            state.apply(statement, inner)
            for kind in _lock_risks(statement.masked):
                risks.append({"kind": kind, "statement_index": position})
                unprotected = unprotected or not state.effective
        in_sql = not unprotected if risks else state.ever_enabled
        return {
            "state": "SUCCESS",
            "statement_count": len(top_level),
            "risks": risks,
            "risk_count": len(risks),
            "lock_timeout_required": bool(risks),
            "lock_timeout_declared": in_sql or payload.get("lock_timeout_s", 0) > 0,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict


# --- data_explain_regression_check ---------------------------------------------------

_DEFAULT_THRESHOLD_PCT = 20


def _exact(value: object) -> Fraction:
    """Exact rational of a JSON number, so 20 % sits exactly on the boundary."""
    return Fraction(str(value))


def data_explain_regression_check(payload: dict[str, Any]) -> dict[str, Any]:
    def work() -> dict[str, Any]:
        queries = payload["queries"]
        if not queries:
            return {"state": "INVALID_INPUT", "field": "queries", "message": "queries must not be empty"}
        threshold = _exact(payload.get("threshold_pct", _DEFAULT_THRESHOLD_PCT))
        seen: set[str] = set()
        regressions: list[dict[str, Any]] = []
        worst: Fraction | None = None
        for position, query in enumerate(queries):
            qid = query["id"]
            if qid in seen:
                return {"state": "INVALID_INPUT", "field": f"queries[{position}].id", "message": f"duplicate query id {qid!r}"}
            seen.add(qid)
            baseline = _exact(query["baseline"])
            if baseline <= 0:
                return {
                    "state": "INVALID_INPUT",
                    "field": f"queries[{position}].baseline",
                    "message": "baseline must be greater than 0 to compute a percentage change",
                }
            try:
                pct = (_exact(query["shadow"]) - baseline) * 100 / baseline
                pct_value = round(float(pct), 2)
            except (OverflowError, ValueError):
                return {
                    "state": "INVALID_INPUT",
                    "field": f"queries[{position}].shadow",
                    "message": "shadow is out of range for a percentage change",
                }
            worst = pct if worst is None or pct > worst else worst
            if pct > threshold:
                regressions.append({"id": qid, "pct": pct_value})
        assert worst is not None  # queries is non-empty, so the loop above set worst
        return {
            "state": "SUCCESS",
            "checked": len(queries),
            "regressions": regressions,
            "max_pct": round(float(worst), 2),
            "within_budget": not regressions,
        }

    return cast(dict[str, Any], respond(payload, work))  # respond() is untyped in support.py; work() returns a result dict
