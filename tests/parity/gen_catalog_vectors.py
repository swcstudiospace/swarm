#!/usr/bin/env python3
"""Vectors for the pure Rust `swarm_tool catalog` (`crates/gsd-route/src/catalog.rs`), generated from the REAL `scripts/swarm_tool.py`.

Every case runs `AgentScript("shared", "swarm_tool", ...).main(argv)`, the real entry point of `swarm_tool.py` (argv parsing, `run()`, the
catalog, the printed reply), with the run log stubbed (it is a side effect that never changes the bytes printed). A case records:

* `argv`: what the script was called with, the `catalog` command and its options;
* `registry` and `skills` (only for a synthetic case; the cases of the real corpus use the top-level ones): the slice the host builds
  (`registry_slice`: the dict agents with `id` and `slug`, and the dict tool entries of `tool_registry.iter_entries` with the five keys
  the catalog reads) and the rows `skill_registry.load_skills` returned, reduced to name, description, owner directory and tools;
* `expect`: what Python printed (`stdout`, `null` when the reply carries a traceback with host paths) and its exit status;
* `rust`: `reply` (the Rust function must print exactly `expect`) or `not_started` (it must decline; `why` names the decline class, which
  the module documentation of `catalog.rs` lists).

The real corpus is `agents.json` and `prompts/skills` of this checkout: every one of the 30 agents as caller, by id and by slug, crossed with
every kind, with queries (nothing, one word, several words, mixed case, whitespace, empty), in JSON and text mode; the dry run; the refusals
(unknown, missing and padded callers, task and correlation ids). The synthetic corpus is built in a temporary directory and read through the
real loaders: tier, timeout, purpose and name shapes, falsy and odd `permitted_callers`, shared and duplicate names, a null purpose
that a query reaches as the text `None`, the Kelvin sign, skills of other owners, skills with parse problems, and every decline class.

Units under `units`: `argv` (Python's own parser on each argument list, and the one the Rust parser must accept or decline) and
`lower_contains` (`word in text.lower()` for the Unicode lower-casing the query match relies on).

Determinism: the document and the skill loaders are read once (the cases of the real corpus would otherwise parse 2.6 MB each time; the fresh
test reads three real callers uncached). Nothing in the file is secret, a host path or a time.

`python3 tests/parity/gen_catalog_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SWARM_PURE_PYTHON", "1")
os.environ.setdefault("SWARM_GSD_STATE_FILE", "")
for _name in ("SWARM_AGENTS_FILE", "SWARM_TASK_ID", "SWARM_CORRELATION_ID"):
    os.environ.pop(_name, None)

from swarm import script_base, skill_registry, tool_registry  # noqa: E402

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "catalog.json"
SCHEMA = "gsd-route.catalog-vectors.v1"
MAX_REPLY_BYTES = 1 << 20
TOOL_KEYS = ("name", "purpose", "side_effect_tier", "timeout_s", "permitted_callers")
KINDS = ("tools", "skills", "all")
REAL_QUERIES = ("route", "review gate", "zzzz-no-such-word")
SAMPLE_AGENTS = ("A01", "A05", "A13", "A30")
SAMPLE_QUERIES = ("ROUTE Norm", "Contract", "", "   ", "a\tb", "route\x1fnorm", "e", "none", "5", "spec", "a")

Row = dict[str, Any]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


swarm_tool = _load("_swarm_tool_for_vectors", REPO / "scripts" / "swarm_tool.py")
SCRIPT = script_base.AgentScript("shared", "swarm_tool", swarm_tool.run, description=swarm_tool.__doc__, add_args=swarm_tool.add_args)


def registry_slice(data: dict[str, Any]) -> Row:
    """The slice of an `agents.json` document the host hands the Rust catalog (the contract in `catalog.rs`)."""
    agents = [{key: a[key] for key in ("id", "slug") if key in a} for a in data.get("agents") or [] if isinstance(a, dict)]
    tools = [{key: t[key] for key in TOOL_KEYS if key in t} for _container, t in tool_registry.iter_entries(data) if isinstance(t, dict)]
    return {"agents": agents, "tools": tools}


def skill_rows(root: Path | None = None) -> list[Row]:
    """Every skill source as `load_skills` returns it, reduced to what the catalog prints."""
    return [{"name": s.name, "description": s.description, "owner": s.owner_dir, "tools": list(s.tools)} for s in skill_registry.load_skills(root)]


def run_script(argv: list[str]) -> tuple[int, str]:
    """The exit status and stdout of the real script; an argparse rejection is its `SystemExit` code."""
    out = io.StringIO()
    with mock.patch.object(script_base, "emit", lambda *args, **kwargs: {}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        try:
            code = SCRIPT.main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, out.getvalue()


@contextlib.contextmanager
def real_sources(uncached: bool = False) -> Any:
    """The repository's own registry and skills, read once unless `uncached`."""
    document = tool_registry.load_document(REPO / "agents.json")
    skills = skill_registry.load_skills(REPO)
    patches = [mock.patch.object(tool_registry, "AGENTS_FILE", REPO / "agents.json"), mock.patch.object(skill_registry, "ROOT", REPO)]
    if not uncached:
        patches += [mock.patch.object(tool_registry, "load_document", lambda path=None: document),
                    mock.patch.object(skill_registry, "load_skills", lambda root=None: skills)]
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield document


def case(name: str, argv: list[str], *, rust: str = "reply", why: str | None = None, inline: tuple[Row, list[Row]] | None = None) -> Row:
    """Runs `argv` against the real script, in the sources currently patched in."""
    code, stdout = run_script(argv)
    row: Row = {"name": name, "argv": argv}
    if inline is not None:
        row["registry"], row["skills"] = inline
    assert rust != "reply" or (stdout and '"trace"' not in stdout), f"{name}: Python printed no reply Rust could reproduce (argv rejected, or a traceback)"
    row["rust"], row["why"] = rust, why
    row["expect"] = {"exit": code, "stdout": None if '"trace"' in stdout else stdout}
    return row


def real_cases() -> list[Row]:
    cases: list[Row] = []
    with real_sources() as data:
        agents = [(a["id"], a["slug"]) for a in data["agents"]]
        for ident, _slug in agents:
            for kind in KINDS:
                cases.append(case(f"real/{ident}/{kind}", ["catalog", "--caller", ident, "--kind", kind, "--json"]))
            cases.append(case(f"real/{ident}/text", ["--caller", ident]))
            for query in REAL_QUERIES:
                cases.append(case(f"real/{ident}/query/{query}", ["catalog", "--caller", ident, "--query", query, "--json"]))
        for ident, slug in agents:
            cases.append(case(f"real/{ident}/by-slug", ["catalog", "--caller", slug, "--query", "zzzz-no-such-word", "--json"]))
        for ident in SAMPLE_AGENTS:
            for query in SAMPLE_QUERIES:
                for kind in KINDS:
                    argv = ["catalog", "--caller", ident, "--kind", kind, "--query", query, "--json"]
                    cases.append(case(f"real/{ident}/sample/{kind}/{query!r}", argv))
        cases += [
            case("real/dry-run/bare", ["catalog", "--dry-run", "--json"]),
            case("real/dry-run/bare-text", ["catalog", "--dry-run"]),
            case("real/dry-run/default-command", ["--dry-run", "--json"]),
            case("real/dry-run/caller", ["catalog", "--caller", "A05", "--dry-run", "--json"]),
            case("real/dry-run/caller-text", ["catalog", "--caller", "A05", "--dry-run", "--kind", "tools"]),
            case("real/dry-run/unknown", ["catalog", "--caller", "A99", "--dry-run", "--json"]),
            case("real/order/flags-first", ["--json", "--caller=A05", "--kind=skills", "catalog", "--query=route"]),
            case("real/order/repeated-flag", ["catalog", "--caller", "A01", "--caller", "A05", "--json", "--kind", "tools", "--kind", "skills"]),
            case("real/order/root-dropped", ["catalog", "--caller", "A05", "--root", ".", "--json", "--kind", "tools"]),
        ]
        for label, caller in (("missing", None), ("empty", ""), ("blank", "   "), ("tabs", "\t\n"), ("padded", " A05 "), ("padded-slug", "\ta05-backend\n"),
                              ("nbsp", "\u00a0A05\u00a0"), ("fs", "\x1fA05\x1c"), ("lower-id", "a05"), ("upper-slug", "A05-BACKEND"), ("unknown", "A99"),
                              ("shared", "shared"), ("word", "nobody"), ("single-quote", "it's"), ("double-quote", 'say "hi"'), ("both-quotes", "it's \"x\""),
                              ("backslash", "a\\b"), ("controls", "a\tb\nc\x01\x7fd"), ("long", "x" * 300), ("none-text", "None"), ("digits", "5")):
            for mode in ("json", "text"):
                argv = ["catalog"] + (["--caller", caller] if caller is not None else []) + (["--json"] if mode == "json" else [])
                cases.append(case(f"real/caller/{label}/{mode}", argv))
        for label, caller in (("cafe", "caf\u00e9"), ("fullwidth", "\uff21\uff10\uff15"), ("emoji", "\U0001f600")):
            cases.append(case(f"real/caller/{label}", ["catalog", "--caller", caller, "--json"], rust="not_started", why="non-ascii-repr"))
        for label, flag, value in (("task-empty", "--task-id", ""), ("task-dots", "--task-id", "a..b"), ("task-slash", "--task-id", "../x"),
                                   ("task-long", "--task-id", "a" * 129), ("task-max", "--task-id", "a" * 128),
                                   ("task-unicode", "--task-id", "caf\u00e9"), ("task-space", "--task-id", "a b"), ("task-ok", "--task-id", "T-1.a_b"),
                                   ("corr-empty", "--correlation-id", ""), ("corr-bad", "--correlation-id", "x/y"), ("corr-ok", "--correlation-id", "c1"),
                                   ("corr-dots", "--correlation-id", "..")):
            for mode in ("json", "text"):
                argv = ["catalog", "--caller", "A05", "--kind", "tools", "--query", "norm", flag, value] + (["--json"] if mode == "json" else [])
                cases.append(case(f"real/id/{label}/{mode}", argv))
        cases.append(case("real/id/task-dash-inline", ["catalog", "--caller", "A05", "--task-id=-x", "--json"]))
        cases.append(case("real/id/both-bad", ["catalog", "--caller", "A05", "--task-id", "a b", "--correlation-id", "x/y", "--json"]))
        cases.append(case("real/id/dry-run", ["catalog", "--dry-run", "--task-id", "a b", "--json"]))
        for query in ("caf\u00e9", "\u00e9", "\u212a", "\u0130", "\U0001f600", "ROUTE \u00e9"):
            cases.append(case(f"real/query/non-ascii/{query!r}", ["catalog", "--caller", "A05", "--query", query, "--json"], rust="not_started",
                              why="non-ascii-query"))
            cases.append(case(f"real/query/non-ascii-unknown/{query!r}", ["catalog", "--caller", "A99", "--query", query, "--json"]))
    return cases


def skill_md(name: str, description: str, owner: str, tools: str = "[]") -> str:
    return f"---\nname: {name}\ndescription: {description}\nowner: {owner}\ntools: {tools}\n---\n## When to Use\n\nx\n"


def synthetic(name: str, doc: dict[str, Any], skills: dict[str, dict[str, str]], calls: list[list[str]], *, rust: str = "reply",
              why: str | None = None) -> list[Row]:
    """Runs `calls` against a temporary registry and skill tree, through the real loaders."""
    root = Path(tempfile.mkdtemp(prefix="catalog-vectors-"))
    try:
        (root / "agents.json").write_text(json.dumps(doc), encoding="utf-8")
        for owner, files in skills.items():
            (root / "prompts" / "skills" / owner).mkdir(parents=True)
            for stem, text in files.items():
                (root / "prompts" / "skills" / owner / f"{stem}.md").write_text(text, encoding="utf-8")
        with mock.patch.object(tool_registry, "AGENTS_FILE", root / "agents.json"), mock.patch.object(skill_registry, "ROOT", root):
            inline = (registry_slice(tool_registry.load_document(root / "agents.json")), skill_rows(root))
            return [case(f"synthetic/{name}/{index}", argv, rust=rust, why=why, inline=inline) for index, argv in enumerate(calls)]
    finally:
        shutil.rmtree(root)


def tool(name: object, callers: object, **fields: object) -> Row:
    return {"name": name, "purpose": f"purpose of {name}", "permitted_callers": callers, **fields}


def doc_of(agents: list[Row], shared: list[Row] | None = None) -> dict[str, Any]:
    return {"agents": agents, "shared_tools": shared if shared is not None else []}


def agent(ident: str, slug: str, *tools: object, **extra: object) -> Row:
    return {"id": ident, "slug": slug, **extra, "registered_tools": list(tools)}


def synthetic_cases() -> list[Row]:
    every = [["catalog", "--caller", "A01", "--json", "--kind", kind] for kind in KINDS]
    queried = lambda *words: [["catalog", "--caller", "A01", "--json", "--query", word] for word in words]  # noqa: E731
    cases: list[Row] = []
    cases += synthetic("tiers", doc_of([agent("A01", "a01-x", tool("t_read", ["A01"], timeout_s=5), tool("t_write", ["A01"], side_effect_tier="WRITE", timeout_s=7),
                                              tool("t_bad_tier", ["A01"], side_effect_tier=5, timeout_s=None), tool("t_null_tier", ["A01"], side_effect_tier=None),
                                              tool("t_float", ["A01"], timeout_s=1.5), tool("t_big", ["A01"], timeout_s=1e22),
                                              tool("t_str_timeout", ["A01"], timeout_s="30"), tool("t_neg", ["A01"], timeout_s=-0.0))]),
                       {}, every + [["catalog", "--caller", "A01"]])
    cases += synthetic("sorting", doc_of([agent("A01", "a01-x", tool("b", ["A01"]), tool("a", ["A01"]), tool("B", ["A01"]), tool("_z", ["A01"]), tool("a", ["A01"], timeout_s=1),
                                              tool("\u00e9clair", ["A01"]), tool("z\u2603", ["A01"]), tool("a1", ["A01"]), tool("a-", ["A01"]))],
                                   shared=[tool("shared_b", ["A01", "A02"]), tool("A", ["A01"])]),
                       {}, every)
    cases += synthetic("callers", doc_of([agent("A01", "a01-x",
                                                tool("c_list", ["A02", "A01", "A03"]), tool("c_none", None),
                                                tool("c_empty", []), tool("c_false", False), tool("c_zero", 0), tool("c_zero_float", 0.0), tool("c_blank", ""),
                                                tool("c_empty_obj", {}), tool("c_other", ["A02"]), tool("c_mixed", [1, None, True, ["A01"], {"A01": 1}, "a01-x"]),
                                                tool("c_exact", ["A01"]), tool("c_case", ["a01"]), tool("c_space", [" A01"]), {"name": "c_no_callers"}),
                                          agent("A02", "a02-y", tool("d_own", ["A02"]), tool("d_both", ["A01", "A02"]))]),
                       {}, every + [["catalog", "--caller", "a02-y", "--json"]])
    cases += synthetic("entries", {"agents": [agent("A01", "a01-x", tool("e_ok", ["A01"]), "text", 5, None, ["A01"], tool("e_after", ["A01"])), "not an agent", None, 7,
                                              {"id": "A02", "slug": "a02-y", "registered_tools": "nope"}, {"id": "A03", "slug": "a03-z", "registered_tools": {"A01": 1}},
                                              {"id": "A04", "slug": "a04-w"}],
                                   "shared_tools": [tool("e_shared", ["A01"]), "str", 3]},
                       {}, every + [["catalog", "--caller", "A99", "--json"], ["catalog", "--caller", "A04", "--json"], ["catalog", "--json"]])
    cases += synthetic("shared-only", {"agents": [{"id": "A01", "slug": "a01-x"}], "shared_tools": [tool("s1", ["A01"])]}, {}, every)
    cases += synthetic("no-shared-key", {"agents": [{"id": "A01", "slug": "a01-x", "registered_tools": [tool("n1", ["A01"])]}]}, {}, every)
    cases += synthetic("no-agents", {"agents": [], "shared_tools": []}, {}, every + [["catalog", "--caller", "A01", "--json"], ["catalog", "--caller", "", "--json"]])
    cases += synthetic("duplicate-agents", {"agents": [{"id": "A01", "slug": "a01-x", "registered_tools": [tool("first", ["A01"])]},
                                                       {"id": "A01", "slug": "a01-other", "registered_tools": [tool("second", ["A01"])]},
                                                       {"id": "A09", "slug": "A01", "registered_tools": [tool("third", ["A09"])]}]}, {},
                       every + [["catalog", "--caller", "a01-other", "--json"], ["catalog", "--caller", "A09", "--json"]])
    cases += synthetic("text", doc_of([agent("A01", "a01-x",
                                             {"name": "quote\"s", "purpose": "tab\there \u00e9 \u2192 \U0001f600 \u007f \u2028 nl\nx \\ back", "permitted_callers": ["A01"]},
                                             {"name": "plain", "purpose": "", "permitted_callers": ["A01"]},
                                             {"name": "nullp", "purpose": None, "permitted_callers": ["A01"]},
                                             {"name": "nop", "permitted_callers": ["A01"]},
                                             {"name": "objp", "purpose": {"k": [1, 2.5, None, True], "e": {}, "l": []}, "permitted_callers": ["A01"]},
                                             {"name": "boolp", "purpose": True, "permitted_callers": ["A01"]},
                                             {"name": "intp", "purpose": 5, "permitted_callers": ["A01"]},
                                             {"name": "floatp", "purpose": 2.5, "permitted_callers": ["A01"]},
                                             {"name": "listp", "purpose": ["a"], "permitted_callers": ["A01"]},
                                             {"name": "k", "purpose": "plain \u212a letter", "permitted_callers": ["A01"]})]),
                       {}, every)
    cases += synthetic("query-texts", doc_of([agent("A01", "a01-x",
                                                    {"name": "q_null", "purpose": None, "permitted_callers": ["A01"]},
                                                    {"name": "q_true", "purpose": True, "permitted_callers": ["A01"]},
                                                    {"name": "q_int", "purpose": 42, "permitted_callers": ["A01"]},
                                                    {"name": "q_kelvin", "purpose": "\u212a only", "permitted_callers": ["A01"]},
                                                    {"name": "q_dotted", "purpose": "\u0130stanbul", "permitted_callers": ["A01"]},
                                                    {"name": "q_sigma", "purpose": "\u039f\u0394\u039f\u03a3 \u03a3\u03a4", "permitted_callers": ["A01"]},
                                                    {"name": "q_sharp", "purpose": "Stra\u00dfe STRASSE", "permitted_callers": ["A01"]},
                                                    {"name": "Q_UPPER", "purpose": "MiXeD Case Words", "permitted_callers": ["A01"]},
                                                    {"name": "q_join", "purpose": "tail", "permitted_callers": ["A01"]},
                                                    {"name": "q_other", "purpose": "unrelated", "permitted_callers": ["A02"]})]),
                       {}, queried("none", "NONE", "true", "42", "k", "i", "kelvin", "stanbul", "isTANBUL", "mixed words", "words mixed", "q_join tail",
                                   "q_join  tail", "join tail", "joinx", "unrelated", "q_ other", "\t", "\u000b"))
    cases += synthetic("decline/query-non-ascii", doc_of([agent("A01", "a01-x", tool("t", ["A01"]))]), {},
                       queried("\u0085", "a\u00a0b", "a\u2003b", "\u212a", "\u0130"), rust="not_started", why="non-ascii-query")
    cases += synthetic("skills", doc_of([agent("A01", "a01-x", tool("t1", ["A01"]))]),
                       {"a01-x": {"zeta": skill_md("a01-x--zeta", "Use when zeta", "a01-x", "[t1, 'be_x', \"q\"]"),
                                  "alpha": skill_md("a01-x--alpha", "Alpha \u00e9 \u2192 text, with \"quotes\"", "a01-x"),
                                  "dup": skill_md("same-name", "own copy", "a01-x", "[a]"),
                                  "broken": "no frontmatter at all\n",
                                  "partial": "---\nname: only-name\n---\nbody\n",
                                  "badtools": skill_md("a01-x--badtools", "tools without brackets", "a01-x", "x, y"),
                                  "empty": "---\nname: \ndescription: \nowner: \ntools: []\n---\n"},
                        "a02-y": {"other": skill_md("a02-y--other", "belongs to another agent", "a02-y")},
                        "shared": {"b": skill_md("swarm-b", "shared b", "shared", "[t1]"), "a": skill_md("swarm-a", "Shared a", "shared"),
                                   "dup": skill_md("same-name", "shared copy", "shared"), "zz": skill_md("a01-x--alpha", "same name as an own skill", "shared")},
                        "orphan": {"x": skill_md("orphan--x", "unowned", "orphan")}},
                       every + queried("alpha", "SHARED", "copy", "zeta", "quotes", "other", "x", "belongs", "only-name", "") + [["catalog", "--caller", "a02-y", "--json"],
                                                                                                                               ["catalog", "--caller", "a01-x"]])
    cases += synthetic("skills-no-owner-files", doc_of([agent("A01", "a01-x")]), {}, every)
    cases += synthetic("skills-only-shared", doc_of([agent("A01", "a01-x")]), {"shared": {"s": skill_md("swarm-s", "only shared", "shared")}}, every)
    cases += synthetic("shared-slug", doc_of([{"id": "A01", "slug": "shared"}]), {"shared": {"s": skill_md("swarm-s", "twice listed", "shared")}}, every)
    cases += synthetic("non-ascii-ids", doc_of([{"id": "A\u00e9", "slug": "a-\u00e9", "registered_tools": [tool("t", ["A\u00e9"])]}]),
                       {"a-\u00e9": {"s": skill_md("a-e--s", "own", "a-\u00e9")}},
                       every + [["catalog", "--caller", "a-\u00e9", "--json"], ["catalog", "--caller", "A99", "--json"], ["catalog", "--caller", "A99"]])
    # declines
    cases += synthetic("decline/id-not-text", doc_of([{"id": 7, "slug": "seven", "registered_tools": [tool("t", [7])]}, {"id": "A02", "slug": "a02-y"}]), {},
                       [["catalog", "--caller", "seven", "--json"]], rust="not_started", why="agent-shape")
    cases += synthetic("id-not-text-other-agent", doc_of([{"id": 7, "slug": "seven", "registered_tools": [tool("t", [7])]}, {"id": "A02", "slug": "a02-y"}]), {},
                       [["catalog", "--caller", "A02", "--json"]])
    cases += synthetic("decline/id-not-text-unknown", doc_of([{"id": {"x": 1}, "slug": "obj"}, {"id": "A02", "slug": "a02-y"}]), {},
                       [["catalog", "--caller", "A99", "--json"]], rust="not_started", why="agent-shape")
    cases += synthetic("scalar-ids-unknown", doc_of([{"id": None, "slug": "none"}, {"slug": "no-id"}, {"id": 5, "slug": "five"}, {"id": True, "slug": "true"}, {"id": "A02", "slug": "a02-y"}]),
                       {}, [["catalog", "--caller", "A99", "--json"]])
    cases += synthetic("decline/slug-not-text", doc_of([{"id": "A01", "slug": None, "registered_tools": [tool("t", ["A01"])]}]), {},
                       [["catalog", "--caller", "A01", "--json"]], rust="not_started", why="agent-shape")
    cases += synthetic("decline/name-not-text", doc_of([agent("A01", "a01-x", {"name": 5, "purpose": "p", "permitted_callers": ["A01"]})]), {},
                       [["catalog", "--caller", "A01", "--json", "--kind", "tools"], ["catalog", "--caller", "A01", "--json", "--query", "zzz"]],
                       rust="not_started", why="tool-shape")
    cases += synthetic("decline/name-missing", doc_of([agent("A01", "a01-x", {"purpose": "p", "permitted_callers": ["A01"]})]), {},
                       [["catalog", "--caller", "A01", "--json", "--kind", "tools"]], rust="not_started", why="tool-shape")
    cases += synthetic("name-not-text-unreached", doc_of([agent("A01", "a01-x", {"name": 5, "purpose": "p", "permitted_callers": ["A02"]}, tool("ok", ["A01"]))]), {},
                       [["catalog", "--caller", "A01", "--json", "--kind", "tools"], ["catalog", "--caller", "A01", "--json", "--kind", "skills"]])
    cases += synthetic("decline/callers-text", doc_of([agent("A01", "a01-x", tool("t", "A01"))]), {},
                       [["catalog", "--caller", "A01", "--json", "--kind", "tools"]], rust="not_started", why="tool-shape")
    cases += synthetic("decline/callers-object", doc_of([agent("A01", "a01-x", tool("t", {"A01": 1}))]), {},
                       [["catalog", "--caller", "A01", "--json"]], rust="not_started", why="tool-shape")
    cases += synthetic("decline/callers-number", doc_of([agent("A01", "a01-x", tool("t", 3))]), {},
                       [["catalog", "--caller", "A01", "--json"]], rust="not_started", why="tool-shape")
    cases += synthetic("callers-bad-unreached", doc_of([agent("A01", "a01-x", tool("t", "A01"))]), {},
                       [["catalog", "--caller", "A01", "--json", "--kind", "skills"]])
    cases += synthetic("decline/purpose-object-query", doc_of([agent("A01", "a01-x", {"name": "t", "purpose": {"k": "v"}, "permitted_callers": ["A01"]})]), {},
                       [["catalog", "--caller", "A01", "--json", "--query", "k"]], rust="not_started", why="str-of-value")
    cases += synthetic("decline/purpose-float-query", doc_of([agent("A01", "a01-x", {"name": "t", "purpose": 2.5, "permitted_callers": ["A01"]})]), {},
                       [["catalog", "--caller", "A01", "--json", "--query", "2.5"]], rust="not_started", why="str-of-value")
    return cases


def argv_units() -> list[Row]:
    """Python's parse of each argument list, and whether the Rust parser must accept it (`subset`)."""
    parser = SCRIPT.parser()
    accepted = [
        [], ["catalog"], ["--json"], ["catalog", "--json", "--dry-run"], ["--caller", "A05"], ["--caller=A05"], ["--caller", ""], ["--caller="],
        ["--kind", "tools"], ["--kind=skills", "catalog"], ["--kind", "all", "--kind", "tools"], ["--query", "a b"], ["--query", ""], ["--query=-x"],
        ["--task-id", "T1", "--correlation-id", "C1"], ["--task-id=T1", "--correlation-id=C1"], ["--task-id", ""], ["--root", "/x", "catalog"],
        ["--root=/x"], ["catalog", "--caller", "A01", "--caller", "A02"], ["--dry-run", "catalog", "--json"], ["--json", "--json"],
        ["--caller", "A05", "--query", "route norm", "--kind", "tools", "--json"], ["--query", "caf\u00e9"], ["--caller", "a05-backend"],
    ]
    declined = [
        ["call"], ["call", "--name", "x"], ["catalog", "call"], ["catalog", "catalog"], ["nope"], [""], ["--kind", "bad"], ["--kind=BAD"], ["--kind"],
        ["--caller"], ["--query"], ["--cal", "A05"], ["--ca=A05"], ["--jso"], ["--json=1"], ["--dry-run=1"], ["--unknown"], ["--unknown", "x"], ["-h"], ["--help"],
        ["-x"], ["-"], ["--", "catalog"], ["--caller", "--json"], ["--task-id", "-x"], ["--query", "-5"], ["--query", "-x"], ["--name", "n"], ["--input", "{}"], ["--plan-mode"],
        ["--caller", "A05", "--kind"], ["--task-id"], ["--correlation-id"], ["--root"], ["catalog", "extra"],
    ]
    rows: list[Row] = []
    for argv in accepted + declined:
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            try:
                ns = parser.parse_args(argv)
            except SystemExit as exc:
                rows.append({"argv": argv, "python": {"exit": int(exc.code or 0)}, "args": None})
                continue
        args = {"json": ns.json, "dry_run": ns.dry_run, "caller": ns.caller, "kind": ns.kind, "query": ns.query, "task_id": ns.task_id,
                "correlation_id": ns.correlation_id}
        rows.append({"argv": argv, "python": {"command": ns.command, "name": ns.name, "input": ns.input, "plan_mode": ns.plan_mode}, "args": args})
    return rows


def argv_subset(row: Row) -> bool:
    """The Rust parser must accept exactly the lists Python reads as `catalog` that use none of the options it leaves to Python."""
    options = {token.split("=")[0] for token in row["argv"] if token.startswith("--")}
    if row["args"] is None or row["python"]["command"] != "catalog" or options & {"--name", "--input", "--plan-mode"}:
        return False
    argv = row["argv"]
    return "--" not in argv and not any(a.startswith("-") and not a.startswith("--") for a in argv) and not any(
        argv[i].startswith("--") and "=" not in argv[i] and argv[i] not in ("--json", "--dry-run") and argv[i + 1].startswith("-") for i in range(len(argv) - 1))


def units() -> Row:
    texts = ["Route NORM", "none", "None", "\u212a", "\u0130stanbul", "I\u0307", "\u039f\u0394\u039f\u03a3", "\u03c3 \u03c2", "Stra\u00dfe", "STRASSE", "\u01c5",
             "tab\there", "caf\u00e9 \u2192 x", "\u00c5ngstr\u00f6m", "\u1e9e", "A\u0308", "K", "k", "i", "I", "x y z", "", "\U0001f600", "\u0131", "\u2126"]
    words = ["route", "norm", "none", "k", "i", "x", "y", "z", "a", "tab", "ss", "ngstr", "stanbul", "tail", "e", "s", "o"]
    contains = [{"first": t, "second": "tail", "word": w, "contains": w in " ".join((t, "tail")).lower()} for t in texts for w in words]
    argv = argv_units()
    for row in argv:
        row["subset"] = argv_subset(row)
    return {"argv": argv, "lower_contains": contains}


def build_cases() -> list[Row]:
    cases = real_cases() + synthetic_cases()
    names = [c["name"] for c in cases]
    repeated = sorted({n for n in names if names.count(n) > 1})
    assert not repeated, f"duplicate case names: {repeated}"
    return cases


def render() -> str:
    """The committed file text: constants, the real slice, the units, then one case per line."""
    with real_sources() as data:
        head = {"schema": SCHEMA, "constants": {"max_reply_bytes": MAX_REPLY_BYTES, "kinds": list(KINDS), "agent": "shared", "script": "swarm_tool"},
                "registry": registry_slice(data), "skills": skill_rows(REPO), "units": units()}
    lines = ",\n".join("  " + json.dumps(c) for c in build_cases())
    return json.dumps(head)[:-1] + ',\n "cases": [\n' + lines + "\n ]\n}\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = VECTOR_PATH.read_text(encoding="utf-8") if VECTOR_PATH.exists() else ""
        print("up to date" if current == text else "STALE: rerun without --check")
        return 0 if current == text else 1
    VECTOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTOR_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {VECTOR_PATH.relative_to(REPO)} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
