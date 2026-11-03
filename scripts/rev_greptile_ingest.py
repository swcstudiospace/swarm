#!/usr/bin/env python3
"""A09 — turn the open Greptile review threads of a pull request into advisory per-target findings.

Greptile's inline comments open with a severity badge (``<img alt="P1" ...>``) and a bold title. This reads the review
threads (a ``--threads`` JSON export, or ``--pr OWNER/REPO#N`` through one read-only ``gh api graphql`` query) and
produces the ``{target task id: [finding, ...]}`` object that ``rev_gate.py --per-target-findings`` consumes
(swarm/verdicts.py). The findings are input to A09's review; ``rev_gate.py`` still decides the verdict. This script
never writes to GitHub, never touches the Task Store, never signs or records a verdict and never moves a task toward
approval.

Severity: P0 blocker, P1 major, P2 minor, P3 and lower info. A Greptile comment with no badge counts as major (fail
closed). Resolved threads, outdated threads and threads whose first comment is not Greptile's are dropped and counted,
in that order. Only a thread's first comment is read; replies never change a finding.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from bisect import bisect_left
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from swarm.errors import ErrorCode, SwarmError  # noqa: E402
from swarm.gates import SEVERITIES  # noqa: E402
from swarm.script_base import AgentScript, check_task_id  # noqa: E402

DEFAULT_AUTHORS = ("greptile-apps", "greptile-apps[bot]")
SUMMARY_MAX = 200
EVIDENCE_MAX = 1000
MAX_BODY = 20_000
GH_TIMEOUT_S = 60
UNBADGED = "no severity badge; treated as major"
_BY_PRIORITY = ("blocker", "major", "minor")  # P0, P1, P2; P3 and lower are info

# The badge is the first element of a Greptile comment: <a href="#"><img alt="P1" src=...></a> **Title**
_BADGE = re.compile(r"""\s*(?:<a\b[^>]*>\s*)?<img\b[^>]*?\balt\s*=\s*["']P(\d+)["']""", re.I)
_DETAILS = re.compile(r"<details\b.*?(?:</details\s*>|\Z)", re.I | re.S)
_META_LINES = re.compile(r"^[ \t]*\*\*(?:Knowledge Base|Sources?) Used:?\*\*:?[^\n]*(?:\n[ \t]*[-*][^\n]*)*", re.I | re.M)
# Known HTML only: placeholders such as `scripts/<script>.py` or <seat-prefix> are text and must survive. picture and
# source carry the trailing "Fix in ..." buttons; their anchors hold long percent-encoded prompt URLs.
_HTML = re.compile(r"</?(?:a|img|picture|source|br|hr|p|b|i|em|strong|code|pre|sub|sup|summary|div|span|ul|ol|li|h[1-6]|"
                   r"blockquote|table|thead|tbody|tr|td|th)\b[^>]*>", re.I)
# Link text may hold escaped brackets and nested labels: [\[n6\] title](url), [the [build] report](url). The label
# is at most 300 tokens (one character, or a backslash plus the next). A scan from every "[" cost up to 300 tokens,
# 300 times the work on a body of brackets (30,000 of them took about 5.8 s); _label_closers finds all labels in one pass.
# The URL is not capped, and parentheses in it nest:
# a 2,000-character cap left the tail of a long artifact URL in the evidence and cut off the explanation after it.
_LABEL_TOKENS = 300
# A backtick fence whose opening line contains a backtick is inline code (```a ` <b>```), not a block. Treating it as
# a fence saved every following line, including the <details> prompt, as code. Tilde fences may name a backtick language.
_FENCE_TICK = re.compile(r"[ \t]*(`{3,})([^`]*)$")
_FENCE_TILDE = re.compile(r"[ \t]*(~{3,})")
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")  # one line; a run of backticks closes with the same run
_PLACEHOLDER = re.compile(r"\x00(\d+)\x00")
_TITLE = re.compile(r"\*\*(.+?)\*\*", re.S)
_PR = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([0-9]+)")
_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")

# One read-only query; never a mutation. first:1 comment is enough: only a thread's first comment is read.
_QUERY = ("query($owner:String!,$repo:String!,$number:Int!){repository(owner:$owner,name:$repo){pullRequest(number:$number)"
          "{reviewThreads(first:100){pageInfo{hasNextPage} nodes{id isResolved isOutdated path line originalLine "
          "comments(first:1){nodes{databaseId author{login} body createdAt}}}}}}}")


class _Thread(NamedTuple):
    resolved: bool
    outdated: bool
    login: str | None
    body: str
    path: str | None
    line: int | None


def _fail(message: str, code: ErrorCode = ErrorCode.E_INPUT) -> SwarmError:
    return SwarmError(code, message)


def _int_or_none(node: dict, key: str, where: str) -> int | None:
    value = node.get(key)
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        raise _fail(f"{where}: {key} must be a number")
    return value


def _read_thread(node: object, index: int) -> _Thread:
    where = f"thread {index}"
    if not isinstance(node, dict):
        raise _fail(f"{where} is not an object")
    for key in ("isResolved", "isOutdated"):
        if key in node and not isinstance(node[key], bool):
            raise _fail(f"{where}: {key} must be true or false")
    path = node.get("path")
    if path is not None and not isinstance(path, str):
        raise _fail(f"{where}: path must be text")
    line = _int_or_none(node, "line", where)
    if line is None:
        line = _int_or_none(node, "originalLine", where)
    comments = node.get("comments")
    if not isinstance(comments, dict) or not isinstance(comments.get("nodes"), list):
        raise _fail(f"{where}: comments.nodes must be a list")
    login, body = None, ""
    if comments["nodes"]:
        first = comments["nodes"][0]
        if not isinstance(first, dict) or not isinstance(first.get("body"), str):
            raise _fail(f"{where}: the first comment needs a text body")
        author = first.get("author")
        if author is not None and not isinstance(author, dict):
            raise _fail(f"{where}: the first comment's author must be an object")
        login, body = None if author is None else author.get("login"), first["body"]
        if login is not None and not isinstance(login, str):
            raise _fail(f"{where}: the first comment's author login must be text")
    return _Thread(bool(node.get("isResolved")), bool(node.get("isOutdated")), login, body, path, line)


def _thread_nodes(data: object) -> list:
    """The thread nodes of a GraphQL response object, or of a bare list of nodes; anything else is E-INPUT."""
    if isinstance(data, list):
        return data
    shape = ("expected a GraphQL response with data.repository.pullRequest.reviewThreads.nodes, "
             "or a bare list of review-thread nodes")
    if not isinstance(data, dict):
        raise _fail(shape)
    if data.get("errors"):  # partial data plus errors (a failed author lookup leaves author null) is not a complete export
        raise _fail("the GraphQL response contains errors: export a complete, successful response; "
                    "a partial one could hide review threads")
    try:
        threads = data["data"]["repository"]["pullRequest"]["reviewThreads"]
    except (KeyError, TypeError):
        raise _fail(shape) from None
    if not isinstance(threads, dict) or not isinstance(threads.get("nodes"), list):
        raise _fail(shape)
    page = threads.get("pageInfo")
    if isinstance(page, dict) and page.get("hasNextPage") is True:
        raise _fail("the review threads are incomplete (pageInfo.hasNextPage is true): export every page; "
                    "dropping the rest would hide findings")
    return threads["nodes"]


def _load_file(path: str) -> object:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _fail(f"cannot read {path}: {getattr(exc, 'strerror', None) or exc}") from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise _fail(f"{path} is not JSON: {exc}") from exc


def _fetch(pr: str) -> object:
    match = _PR.fullmatch(pr)
    if not match:
        raise _fail(f"--pr must look like OWNER/REPO#NUMBER, got {pr!r}")
    gh = shutil.which("gh")
    if gh is None:
        raise _fail("gh is not installed or not on PATH; export the threads and use --threads", ErrorCode.E_DEP)
    owner, repo, number = match.groups()
    cmd = [gh, "api", "graphql", "-f", f"query={_QUERY}", "-f", f"owner={owner}", "-f", f"repo={repo}", "-F", f"number={number}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise _fail(f"gh did not answer within {GH_TIMEOUT_S} s", ErrorCode.E_DEP) from None
    except OSError as exc:
        raise _fail(f"cannot run gh: {exc.strerror or exc}", ErrorCode.E_DEP) from exc
    if proc.returncode != 0:
        tail = _TOKEN.sub("***", (proc.stderr or proc.stdout or "").strip()[-300:])
        raise _fail(f"gh api graphql failed (exit {proc.returncode}): {tail}", ErrorCode.E_DEP)
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise _fail(f"gh returned output that is not JSON: {exc}") from exc


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _unescape_label(label: str) -> str:
    """`\\[` and `\\]` in a link label are literal brackets. One pass, and only those two escapes."""
    parts: list[str] = []
    i = start = 0
    n = len(label)
    while i < n:
        if label[i] == "\\" and i + 1 < n and label[i + 1] in "[]":
            if start < i:
                parts.append(label[start:i])
            parts.append(label[i + 1])
            i += 2
            start = i
        else:
            i += 1
    if start == 0:
        return label
    if start < n:
        parts.append(label[start:n])
    return "".join(parts)


def _label_closers(text: str) -> dict[int, int]:
    """Each `[` that opens a link label _strip_links accepts, mapped to the position of the `]` that closes it.

    A label is accepted when the scan from its `[` reaches its closing `]`: the first unmatched `]` after it, at most
    _LABEL_TOKENS tokens before it, no unescaped newline, a non-empty label, and `(` right after the `]`. A token is one
    character, or a backslash plus the next character. The token after any `[` begins where a scan from that `[` begins,
    so one tokenization serves every `[`. Matching `]` comes from one backward pass with a stack, so the cost is linear
    in the length of the text however many brackets it holds."""
    if "[" not in text:
        return {}
    n = len(text)
    starts: list[int] = []
    p = 0
    while p < n:
        starts.append(p)
        p += 2 if text[p] == "\\" and p + 1 < n else 1
    m = len(starts)
    top = [-1] * (m + 1)  # token t: the nearest unmatched "]" at or after t, or -1
    newline = [m] * (m + 1)  # token t: the first unescaped newline at or after t, or m
    pending: list[int] = []
    for t in range(m - 1, -1, -1):
        ch = text[starts[t]]
        if ch == "]":
            pending.append(t)
        elif ch == "[" and pending:
            pending.pop()
        top[t] = pending[-1] if pending else -1
        newline[t] = t if ch == "\n" else newline[t + 1]
    closers: dict[int, int] = {}
    for b, ch in enumerate(text):
        if ch != "[" or b + 1 >= n:
            continue
        t = bisect_left(starts, b + 1)  # b + 1 always starts a token: a non-backslash character ends one
        c = top[t]
        if c < 0 or c == t or c - t > _LABEL_TOKENS or newline[t] < c:
            continue
        pos = starts[c]
        if pos + 1 < n and text[pos + 1] == "(":
            closers[b] = pos
    return closers


def _strip_links(text: str) -> str:
    """Markdown links become their label. The URL is not read again.

    A label opens at `[` and closes at its matching `]`; _label_closers holds the rules: nested brackets balance, `\\[`
    and `\\]` are literal, at most 300 tokens, no newline. Parenthesis depth starts at 1 after `](`; an unescaped `(`
    raises it and an unescaped `)` lowers it, and there is no length cap. A newline means the link is not closed, so
    that line is copied once from the `[`. When the depth hits 0, only the label is emitted and the scan continues after
    the closing `)`. A `[` that opens no link is copied and the scan continues after it. `\\[` and `\\]` in the label are
    written back without the backslash."""
    closers = _label_closers(text)
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        bracket = text.find("[", i)
        if bracket < 0:
            out.append(text[i:])
            break
        out.append(text[i:bracket])
        j = closers.get(bracket)
        if j is None:
            out.append("[")
            i = bracket + 1
            continue
        k, paren = j + 2, 1
        while k < n and paren > 0:
            ch = text[k]
            if ch == "\\" and k + 1 < n and text[k + 1] != "\n":  # \) and \( are literal; a newline still ends the URL
                k += 2
                continue
            if ch == "\n":
                break
            if ch == "(":
                paren += 1
            elif ch == ")":
                paren -= 1
            k += 1
        if paren != 0:
            end = k if k < n else n  # k is the newline, or the end: copy this line once, do not scan it again
            out.append(text[bracket:end])
            i = end
            continue
        out.append(_unescape_label(text[bracket + 1:j]))
        i = k  # k already stepped past the closing ")"
    return "".join(out)


def _protect_code(text: str) -> tuple[str, list[str]]:
    """`text` with every fenced block (``` or ~~~, an unterminated one to the end) and every inline code span replaced by
    a \\x00N\\x00 placeholder, and the originals. Greptile quotes code in its explanations; markup removal must not touch
    it (a backticked <details> would swallow the rest of the comment)."""
    saved: list[str] = []

    def keep(chunk: str) -> str:
        saved.append(chunk)
        return f"\x00{len(saved) - 1}\x00"

    out: list[str] = []
    fence, block = "", []
    for line in text.split("\n"):
        bare = line.strip(" \t")
        if not fence:
            opening = _FENCE_TICK.match(line) or _FENCE_TILDE.match(line)
            if opening:
                fence, block = opening.group(1), [line]
            else:
                out.append(line)
            continue
        block.append(line)
        if bare and set(bare) == {fence[0]} and len(bare) >= len(fence):  # the closing fence: the same character, as long
            out.append(keep("\n".join(block)))
            fence, block = "", []
    if fence:
        out.append(keep("\n".join(block)))
    return _INLINE_CODE.sub(lambda m: keep(m.group(0)), "\n".join(out)), saved


def _clean(body: str) -> str:
    """The comment text without the badge, details blocks, Knowledge Base/Source lines, HTML, link targets and control
    or zero-width/bidi characters. Code (fenced or inline) is kept verbatim."""
    text = "".join(ch for ch in body if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf"))
    text, saved = _protect_code(text)  # after the filter: no real \x00 is left to collide with a placeholder
    text = _DETAILS.sub("", text)
    text = _META_LINES.sub("", text)
    text = _HTML.sub("", text)
    text = _strip_links(text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return _PLACEHOLDER.sub(lambda m: saved[int(m.group(1))], text).strip()


def _finding(number: int, thread: _Thread) -> dict:
    badge = _BADGE.match(thread.body)
    if badge:
        priority = int(badge.group(1))
        severity = _BY_PRIORITY[priority] if priority < len(_BY_PRIORITY) else "info"
    else:
        severity = "major"
    cleaned = _clean(thread.body[:MAX_BODY])  # only the head can reach the 1000-char evidence, and the work stays bounded
    title = _TITLE.match(cleaned)
    if title:
        heading, rest = title.group(1), cleaned[title.end():]
    else:
        heading, _, rest = cleaned.partition("\n")
    summary = _clip(re.sub(r"\s+", " ", heading).strip() or "Greptile comment without text", SUMMARY_MAX)
    evidence = rest.strip()
    if not badge:
        evidence = UNBADGED + (f"\n{evidence}" if evidence else "")
    location = None if thread.path is None else thread.path if thread.line is None else f"{thread.path}:{thread.line}"
    return {"id": f"GP-{number:03d}", "severity": severity, "kind": "greptile", "summary": summary,
            "evidence": _clip(evidence, EVIDENCE_MAX), "ac_ref": None, "owner_suggestion": "A05", "location": location}


def _load_map(path: str, targets: list[str]) -> dict[str, list[str]]:
    data = _load_file(path)
    if not isinstance(data, dict) or not all(isinstance(g, list) and all(isinstance(x, str) for x in g) for g in data.values()):
        raise _fail(f"{path}: expected an object {{target task id: [glob, ...]}}")
    unknown = sorted(set(data) - set(targets))
    if unknown:
        raise _fail(f"{path} names ids that are not --target values: {', '.join(unknown)}")
    return data


def _attribute(items: list[tuple[dict, str | None]], targets: list[str], mapping: dict[str, list[str]] | None) -> dict[str, list[dict]]:
    """Each finding goes to the targets whose globs match its path; with no map, no path or no match it goes to every
    target (fail closed: a finding is never silently lost)."""
    out: dict[str, list[dict]] = {t: [] for t in targets}
    for finding, path in items:
        hit = [t for t in targets if mapping and path is not None
               and any(fnmatch.fnmatchcase(path, glob) for glob in mapping.get(t, ()))]
        for target in hit or targets:
            out[target].append(dict(finding))
    return out


def _write_atomic(path: str, payload: dict) -> None:
    dest = Path(path)
    tmp_name = None
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
        os.replace(tmp_name, dest)
    except OSError as exc:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)
        raise _fail(f"cannot write --out {path}: {exc.strerror or exc}") from exc


def _counts(threads: int, findings: list[dict], resolved: int, outdated: int, other: int) -> dict:
    by_severity = {s: 0 for s in SEVERITIES}
    for f in findings:
        by_severity[f["severity"]] += 1
    return {"threads": threads, "findings": len(findings), "by_severity": by_severity,
            "dropped": {"resolved": resolved, "outdated": outdated, "not_greptile": other}}


def run(args, ctx) -> dict:
    if ctx.dry_run:
        return {"status": "ok", "advisory": True, "source": "dry-run", "per_target_findings": {},
                "counts": _counts(0, [], 0, 0, 0), "dry_run": True, "summary": "dry-run: no threads read, nothing written"}
    if bool(args.threads) == bool(args.pr):
        raise _fail("give exactly one of --threads FILE or --pr OWNER/REPO#N")
    targets = list(dict.fromkeys(check_task_id(t, "--target") for t in args.target or []))
    if not targets:
        raise _fail("at least one --target TASK_ID is required")
    authors = args.author or list(DEFAULT_AUTHORS)
    if not all(a.strip() for a in authors):
        raise _fail("--author must not be empty")
    mapping = _load_map(args.map, targets) if args.map else None

    nodes = _thread_nodes(_load_file(args.threads) if args.threads else _fetch(args.pr))
    resolved = outdated = other = 0
    kept: list[_Thread] = []
    for index, node in enumerate(nodes):
        thread = _read_thread(node, index)
        if thread.resolved:
            resolved += 1
        elif thread.outdated:
            outdated += 1
        elif thread.login not in authors:
            other += 1
        else:
            kept.append(thread)

    findings = [_finding(n, t) for n, t in enumerate(kept, 1)]
    per_target = _attribute([(f, t.path) for f, t in zip(findings, kept)], targets, mapping)
    if args.out:
        _write_atomic(args.out, per_target)
    counts = _counts(len(nodes), findings, resolved, outdated, other)
    return {"status": "ok", "advisory": True, "source": "file" if args.threads else "pr", "per_target_findings": per_target,
            "counts": counts,
            "summary": f"{len(findings)} advisory finding(s) from {len(nodes)} thread(s) for {len(targets)} target(s); "
                       f"dropped {resolved} resolved, {outdated} outdated, {other} not from Greptile"}


def add_args(p):
    p.add_argument("--threads", help="Greptile review threads: a GraphQL response object or a bare list of thread nodes (JSON)")
    p.add_argument("--pr", help="OWNER/REPO#N: read the threads with one read-only `gh api graphql` query instead of --threads")
    p.add_argument("--target", action="append", help="gate_for target task id that receives findings (repeatable, at least one)")
    p.add_argument("--map", help="JSON {target task id: [glob, ...]}: attribute a finding to the targets whose globs match its "
                                 "path; a finding with no match or no path goes to every target")
    p.add_argument("--author", action="append", help="accepted Greptile login (repeatable; default greptile-apps and greptile-apps[bot])")
    p.add_argument("--out", help="also write the per-target findings object here (atomic), ready for rev_gate.py --per-target-findings")


if __name__ == "__main__":
    sys.exit(AgentScript("A09", "rev_greptile_ingest", run, description=__doc__, add_args=add_args).main())
