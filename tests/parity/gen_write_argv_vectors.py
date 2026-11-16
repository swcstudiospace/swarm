#!/usr/bin/env python3
"""Generates `crates/gsd-route/tests/vectors/transition_args.json`: the argv grammar of the host `--transition` route (Phase 14.5, U3.2a).

The oracle is the REAL argparse parser of `scripts/orch_status.py` (`orch_status.SCRIPT.parser()`, built by `AgentScript.parser`,
`allow_abbrev=False`). For every argv of a corpus the file records what that parser did: the parsed namespace fields, or the error
(exit code and message) argparse printed, or a help request. Nothing is predicted: every `argparse` entry below was produced by the
parser at generation time.

Next to it each case carries `rust_serves` and `rust_fields`: what `reference_served` says the Rust route serves. That function is the
Python statement of the grammar documented in `crates/gsd-route/src/transition_args.rs` and the source of the selector's
`rust_serves_write` set; the Rust parser must agree with it on every case, and `tests/test_write_argv_mirror.py` holds it against
argparse: whatever it serves, argparse parsed to the same values, and an argv argparse rejects is never served. A case that argparse
accepts and the route does not model is named `unmodelled-...`; the generator stops when such a case is served or rejected by argparse.

`SWARM_TASK_ID` and `SWARM_CORRELATION_ID` are removed from the environment while the parser is built (they are argparse defaults), so
the file does not depend on the machine.

`python3 tests/parity/gen_write_argv_vectors.py` rewrites the committed file; `--check` compares without writing.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
os.environ.setdefault("SWARM_PURE_PYTHON", "1")

VECTOR_PATH = REPO / "crates" / "gsd-route" / "tests" / "vectors" / "transition_args.json"
SCHEMA = "gsd-route.transition-args-vectors.v1"
SCRIPT_PATH = REPO / "scripts" / "orch_status.py"
ENV_DEFAULTS = ("SWARM_TASK_ID", "SWARM_CORRELATION_ID")

# The namespace fields the route reads (`transition`, `reason`, `root`, `dry_run`, `task_id`, `correlation_id`, `json`) and the ones it
# requires to stay at their defaults (`ingest`, `advisory`, `history`).
FIELDS = ("transition", "reason", "root", "dry_run", "task_id", "correlation_id", "json", "ingest", "advisory", "history")
VALUE_OPTIONS = {"--reason": "reason", "--root": "root", "--repo": "root", "--task-id": "task_id", "--correlation-id": "correlation_id"}

# Values for the value-option product: plain, empty, spaced, dashed in each of argparse's ways, unicode, and the neighbours of the grammar.
VALUES = ("x", "", "a b", "-x", "- x", "-5", "-", "--", "--json", "é ü", "a=b", "T-1", "bad..id", "x" * 129)


def load_script() -> ModuleType:
    """`scripts/orch_status.py` as a module (its `SCRIPT` owns the parser)."""
    spec = importlib.util.spec_from_file_location("orch_status_for_argv_vectors", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def argparse_result(script: ModuleType, argv: list[str]) -> dict[str, Any]:
    """What the real parser does with `argv`: the namespace fields, or the exit code and message of its refusal."""
    with mock.patch.dict(os.environ):
        for name in ENV_DEFAULTS:
            os.environ.pop(name, None)
        parser = script.SCRIPT.parser()
    err, out = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        lines = [line for line in err.getvalue().splitlines() if "error: " in line]
        message = lines[-1].split("error: ", 1)[1] if lines else ("help" if out.getvalue() else "")
        return {"ok": False, "exit": code, "error": message}
    values = vars(args)
    return {"ok": True, "fields": {name: values[name] for name in FIELDS}}


def _plain(value: str) -> bool:
    return not value.startswith("-")


def reference_served(argv: list[str]) -> dict[str, Any] | None:
    """The grammar the Rust route serves, as the namespace fields it would read; `None` for anything outside it."""
    fields: dict[str, Any] = {"transition": None, "reason": "", "root": ".", "dry_run": False, "task_id": None, "correlation_id": None,
                              "json": False, "ingest": None, "advisory": False, "history": None}
    seen: set[str] = set()
    index = 0
    while index < len(argv):
        token = argv[index]
        name, inline = token, None
        if "=" in token and token.split("=", 1)[0].startswith("--"):
            name, inline = token.split("=", 1)
        if name in ("--json", "--dry-run"):
            if inline is not None:
                return None
            fields["json" if name == "--json" else "dry_run"] = True
        elif name == "--transition":
            pair = argv[index + 1:index + 3]
            if inline is not None or fields["transition"] is not None or len(pair) != 2 or not all(part and _plain(part) for part in pair):
                return None
            fields["transition"] = pair
            index += 2
        elif name in VALUE_OPTIONS:
            if VALUE_OPTIONS[name] in seen:
                return None
            seen.add(VALUE_OPTIONS[name])
            if inline is None:
                index += 1
                if index >= len(argv) or not _plain(argv[index]):
                    return None
                inline = argv[index]
            fields[VALUE_OPTIONS[name]] = inline
        else:
            return None
        index += 1
    return fields if fields["json"] and fields["transition"] is not None else None


def _permutations() -> list[tuple[str, list[str]]]:
    cases = []
    groups = {
        "base": [["--transition", "T-1", "DONE"], ["--json"], ["--reason", "r"], ["--dry-run"]],
        "ids": [["--transition", "T-1", "DONE"], ["--json"], ["--root=/r"], ["--task-id", "T-9"]],
    }
    for label, parts in groups.items():
        for number, order in enumerate(itertools.permutations(parts)):
            cases.append((f"order-{label}-{number:02d}", [token for part in order for token in part]))
    return cases


def _value_product() -> list[tuple[str, list[str]]]:
    cases = []
    base = ["--transition", "T-1", "DONE", "--json"]
    for option in VALUE_OPTIONS:
        short = option[2:]
        for number, value in enumerate(VALUES):
            cases.append((f"value-{short}-sep-{number:02d}", [*base, option, value]))
            cases.append((f"value-{short}-inline-{number:02d}", [*base, f"{option}={value}"]))
            cases.append((f"value-{short}-before-{number:02d}", [option, value, *base]))
    return cases


def _named() -> list[tuple[str, list[str]]]:
    t = ["--transition", "T-1", "DONE"]
    named: list[tuple[str, list[str]]] = [
        ("minimal", [*t, "--json"]),
        ("json-first", ["--json", *t]),
        ("empty-argv", []),
        ("json-only", ["--json"]),
        ("no-json", t),
        ("no-json-with-reason", [*t, "--reason", "r"]),
        ("json-twice", [*t, "--json", "--json"]),
        ("dry-run", [*t, "--json", "--dry-run"]),
        ("dry-run-twice", [*t, "--json", "--dry-run", "--dry-run"]),
        ("dry-run-without-json", [*t, "--dry-run"]),
        ("dry-run-inline-value", [*t, "--json", "--dry-run=1"]),
        ("dry-run-inline-empty", [*t, "--json", "--dry-run="]),
        ("json-inline-value", [*t, "--json=true"]),
        ("json-inline-empty", [*t, "--json="]),
        # transition arity and shape
        ("transition-none", ["--transition", "--json"]),
        ("transition-last", ["--json", "--transition"]),
        ("transition-one", ["--transition", "T-1", "--json"]),
        ("transition-one-last", ["--json", "--transition", "T-1"]),
        ("transition-three", ["--transition", "T-1", "DONE", "EXTRA", "--json"]),
        ("transition-inline", ["--transition=T-1", "DONE", "--json"]),
        ("transition-inline-both", ["--transition=T-1 DONE", "--json"]),
        ("transition-flag-between", ["--transition", "T-1", "--json", "DONE"]),
        ("transition-twice", [*t, "--transition", "T-2", "CLAIMED", "--json"]),
        ("transition-twice-second-short", [*t, "--transition", "T-2", "--json"]),
        ("transition-empty-pair", ["--transition", "", "", "--json"]),
        ("transition-empty-state", ["--transition", "T-1", "", "--json"]),
        ("transition-dashed-state", ["--transition", "T-1", "-DONE", "--json"]),
        ("transition-dashed-task", ["--transition", "-T-1", "DONE", "--json"]),
        ("transition-spaced-dash", ["--transition", "T-1", "- DONE", "--json"]),
        ("transition-negative-number", ["--transition", "T-1", "-5", "--json"]),
        ("transition-unicode", ["--transition", "T-é", "DÖNE", "--json"]),
        ("transition-bad-state-name", ["--transition", "T-1", "NOT_A_STATE", "--json"]),
        ("transition-lowercase-state", ["--transition", "T-1", "done", "--json"]),
        ("transition-bad-id", ["--transition", "bad..id", "DONE", "--json"]),
        ("transition-long-id", ["--transition", "T" * 200, "DONE", "--json"]),
        ("transition-ends-with-separator", [*t, "--json", "--"]),
        ("transition-after-separator", ["--json", "--", *t]),
        ("transition-separator-in-args", ["--transition", "--", "DONE", "--json"]),
        # abbreviation traps (allow_abbrev=False)
        ("abbrev-trans", ["--trans", "T-1", "DONE", "--json"]),
        ("abbrev-tran", ["--tran", "T-1", "DONE", "--json"]),
        ("abbrev-tr", ["--tr", "T-1", "DONE", "--json"]),
        ("abbrev-t", ["--t", "T-1", "DONE", "--json"]),
        ("abbrev-transition-extra-letter", ["--transitions", "T-1", "DONE", "--json"]),
        ("abbrev-trans-only", ["--trans", "--json"]),
        ("abbrev-trans-inline", ["--trans=T-1", "DONE", "--json"]),
        ("abbrev-json", [*t, "--js"]),
        ("abbrev-jso", [*t, "--jso"]),
        ("abbrev-dry", [*t, "--json", "--dry"]),
        ("abbrev-dry-run-no-hyphen", [*t, "--json", "--dryrun"]),
        ("abbrev-reas", [*t, "--json", "--reas", "x"]),
        ("abbrev-rea-inline", [*t, "--json", "--rea=x"]),
        ("abbrev-ro", [*t, "--json", "--ro", "/x"]),
        ("abbrev-rep", [*t, "--json", "--rep", "/x"]),
        ("abbrev-task", [*t, "--json", "--task", "T-9"]),
        ("abbrev-corr", [*t, "--json", "--corr", "C-1"]),
        ("abbrev-correlation", [*t, "--json", "--correlation", "C-1"]),
        ("abbrev-ing", [*t, "--json", "--ing", "p"]),
        ("abbrev-hist", [*t, "--json", "--hist", "T-1"]),
        ("abbrev-adv", [*t, "--json", "--adv"]),
        ("case-upper-json", [*t, "--JSON"]),
        ("case-upper-transition", ["--TRANSITION", "T-1", "DONE", "--json"]),
        ("case-title-reason", [*t, "--json", "--Reason", "x"]),
        # ids
        ("task-id-valid", [*t, "--json", "--task-id", "T-9"]),
        ("task-id-inline", [*t, "--json", "--task-id=T-9"]),
        ("task-id-empty", [*t, "--json", "--task-id", ""]),
        ("task-id-inline-empty", [*t, "--json", "--task-id="]),
        ("task-id-dotdot", [*t, "--json", "--task-id", "bad..id"]),
        ("task-id-leading-dash-inline", [*t, "--json", "--task-id=-bad"]),
        ("task-id-leading-dot", [*t, "--json", "--task-id", ".hidden"]),
        ("task-id-slash", [*t, "--json", "--task-id", "a/b"]),
        ("task-id-long", [*t, "--json", "--task-id", "a" * 129]),
        ("task-id-unicode", [*t, "--json", "--task-id", "é"]),
        ("task-id-missing", [*t, "--json", "--task-id"]),
        ("task-id-twice", [*t, "--json", "--task-id", "T-1", "--task-id", "T-2"]),
        ("correlation-id-valid", [*t, "--json", "--correlation-id", "C-1"]),
        ("correlation-id-inline", [*t, "--json", "--correlation-id=C-1"]),
        ("correlation-id-empty", [*t, "--json", "--correlation-id", ""]),
        ("correlation-id-invalid", [*t, "--json", "--correlation-id", "a b"]),
        ("correlation-id-missing", [*t, "--json", "--correlation-id"]),
        ("both-ids", [*t, "--json", "--task-id", "T-9", "--correlation-id", "C-1"]),
        # reason
        ("reason-plain", [*t, "--json", "--reason", "because"]),
        ("reason-before-transition", ["--reason", "because", *t, "--json"]),
        ("reason-between-transition-args", ["--transition", "T-1", "--reason", "r", "DONE", "--json"]),
        ("reason-missing-last", [*t, "--json", "--reason"]),
        ("reason-then-flag", [*t, "--reason", "--json"]),
        ("reason-then-dry-run", [*t, "--json", "--reason", "--dry-run"]),
        ("reason-twice", [*t, "--json", "--reason", "a", "--reason", "b"]),
        ("reason-sep-then-inline", [*t, "--json", "--reason", "a", "--reason=b"]),
        ("reason-inline-then-sep", [*t, "--json", "--reason=a", "--reason", "b"]),
        ("reason-multiline", [*t, "--json", "--reason", "line one\nline two"]),
        ("reason-quote", [*t, "--json", "--reason", 'say "hi" \\ done']),
        ("reason-emoji", [*t, "--json", "--reason", "ok \U0001f600"]),
        ("reason-two-words-unquoted", [*t, "--json", "--reason", "two", "words"]),
        # root and repo
        ("root-sep", [*t, "--json", "--root", "/x"]),
        ("root-inline", [*t, "--json", "--root=/x"]),
        ("repo-sep", [*t, "--json", "--repo", "/x"]),
        ("repo-inline", [*t, "--json", "--repo=/x"]),
        ("root-missing", [*t, "--json", "--root"]),
        ("repo-missing", [*t, "--json", "--repo"]),
        ("root-dot", [*t, "--json", "--root", "."]),
        ("root-relative", [*t, "--json", "--root", "sub/dir"]),
        ("root-then-repo", [*t, "--json", "--root", "/a", "--repo", "/b"]),
        ("repo-then-root", [*t, "--json", "--repo", "/b", "--root", "/a"]),
        ("root-twice", [*t, "--json", "--root", "/a", "--root=/b"]),
        ("root-and-repo-inline", [*t, "--json", "--root=/a", "--repo=/b"]),
        ("root-with-equals", [*t, "--json", "--root", "/a=b"]),
        ("root-inline-with-equals", [*t, "--json", "--root=/a=b"]),
        ("root-two-values", [*t, "--json", "--root", "/a", "/b"]),
        # everything together
        ("all-options", ["--root", "/r", "--transition", "T-1", "DONE", "--reason", "why", "--json", "--dry-run", "--task-id", "T-9",
                         "--correlation-id", "C-1"]),
        ("all-options-inline", ["--root=/r", "--transition", "T-1", "DONE", "--reason=why", "--json", "--dry-run", "--task-id=T-9",
                                "--correlation-id=C-1"]),
        # extra positionals and unknown flags
        ("extra-positional", [*t, "--json", "extra"]),
        ("extra-positional-first", ["extra", *t, "--json"]),
        ("bare-pair", ["T-1", "DONE", "--json"]),
        ("unknown-flag", [*t, "--json", "--frobnicate"]),
        ("unknown-flag-with-value", [*t, "--json", "--frobnicate", "x"]),
        ("unknown-flag-inline", [*t, "--json", "--frobnicate=x"]),
        ("no-json-flag", [*t, "--no-json"]),
        ("single-dash-j", [*t, "-j"]),
        ("single-dash-json", [*t, "-json"]),
        ("single-dash-combined", [*t, "-jd"]),
        ("help", [*t, "--json", "--help"]),
        ("help-short", ["-h"]),
        ("help-alone", ["--help"]),
        ("help-before", ["--help", *t, "--json"]),
        ("json-trailing-space", [*t, "--json "]),
        ("json-leading-space", [*t, " --json"]),
        ("json-nbsp", [*t, "--json\u00a0"]),
        ("empty-token", [*t, "--json", ""]),
        ("empty-token-first", ["", *t, "--json"]),
        ("equals-only", [*t, "--json", "="]),
        ("double-equals", [*t, "--json", "--reason==x"]),
        ("dashes-only", [*t, "--json", "--="]),
        ("input-option", [*t, "--json", "--input", "{}"]),
        # the options of the other orch_status routes, with a transition
        ("unmodelled-ingest", [*t, "--json", "--ingest", "result.json"]),
        ("unmodelled-ingest-inline", [*t, "--json", "--ingest=result.json"]),
        ("unmodelled-ingest-advisory", [*t, "--json", "--ingest", "result.json", "--advisory"]),
        ("unmodelled-advisory", [*t, "--json", "--advisory"]),
        ("unmodelled-history", [*t, "--json", "--history", "T-1"]),
        ("unmodelled-history-inline", [*t, "--json", "--history=T-1"]),
        ("unmodelled-history-first", ["--history", "T-1", *t, "--json"]),
        ("unmodelled-history-and-ingest", [*t, "--json", "--history", "T-1", "--ingest", "r.json"]),
        ("unmodelled-all-three", [*t, "--json", "--ingest", "r.json", "--advisory", "--history", "T-1"]),
        ("ingest-without-transition", ["--json", "--ingest", "result.json"]),
        ("history-without-transition", ["--json", "--history", "T-1"]),
        ("read-listing", ["--json"]),
        ("read-listing-correlation", ["--json", "--correlation-id", "C-1"]),
        # values that argparse reads as something else
        ("negative-number-reason", [*t, "--json", "--reason", "-5"]),
        ("negative-float-reason", [*t, "--json", "--reason", "-1.5"]),
        ("dash-only-reason", [*t, "--json", "--reason", "-"]),
        ("dash-space-reason", [*t, "--json", "--reason", "-a b"]),
        ("dash-reason-inline", [*t, "--json", "--reason=-a"]),
        ("flag-name-as-reason-inline", [*t, "--json", "--reason=--json"]),
        ("flag-name-as-root", [*t, "--json", "--root", "--dry-run"]),
        ("reason-eats-nothing-before-json", [*t, "--reason", "", "--json"]),
        ("root-empty-sep", [*t, "--json", "--root", ""]),
        ("repo-empty-inline", [*t, "--json", "--repo="]),
    ]
    named.extend(_permutations())
    named.extend(_value_product())
    return named


def build_cases() -> list[dict[str, Any]]:
    """Every corpus case with argparse's verdict and the reference grammar's."""
    script = load_script()
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, argv in _named():
        assert name not in seen, f"duplicate case name {name}"
        seen.add(name)
        parsed = argparse_result(script, argv)
        served = reference_served(argv)
        if name.startswith("unmodelled-"):
            assert parsed["ok"] and served is None, f"{name} must parse under argparse and stay unserved"
        cases.append({"name": name, "argv": argv, "argparse": parsed, "rust_serves": served is not None, "rust_fields": served})
    return cases


def render() -> str:
    """The committed file text: one case per line (stable and reviewable in a diff)."""
    lines = ",\n".join("  " + json.dumps(case, sort_keys=True, ensure_ascii=True) for case in build_cases())
    head = json.dumps({"schema": SCHEMA, "fields": list(FIELDS)}, sort_keys=True)
    return head[:-1] + ',\n "cases": [\n' + lines + "\n ]\n}\n"


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
