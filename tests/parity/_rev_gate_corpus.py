"""Cells for the host `rev_gate` route (phase 14.5, unit U3.6d).

Each cell builds a small tree (a plain directory, or a git repository with fixed identities), runs the real
`scripts/rev_gate.py --json` on Python, and runs the same argv through the `gsd_engine` binary with the selector's request
shape. `served` is the design: a served cell must be answered by the binary (exit 0 or 1); a declined cell must come back
as `E-DEP` with `write_state` `not_started`, which is the selector's trigger to run the Python body. Compared fields are the
verdict, runs, findings (identity, severity, kind, summary, evidence, location), stats, the envelope payload and the summary.
Signatures, ids and clock fields are masked.

Secret-shaped text is assembled at run time from fragments, so no committed line matches a token pattern.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "rev_gate.py"
SIGNING_KEY = "ab" * 32
# Envelope clock and random fields. Finding ids are compared: they are the RF-n numbering.
MASKED = {"ts", "issued_at", "sig", "trace_id", "msg_id"}
# Masked only when the caller did not pass --correlation-id (the envelope then carries a random uuid).
MASKED_WITHOUT_CORRELATION = {"correlation_id"}
REQUEST_ENV = ("SWARM_DIR", "SWARM_TASK_ID", "SWARM_CORRELATION_ID", "SWARM_AGENT_SESSION", "SWARM_DRYRUN_FAIL")
GIT_ENV = {
    "GIT_AUTHOR_NAME": "rev", "GIT_AUTHOR_EMAIL": "rev@example.invalid",
    "GIT_COMMITTER_NAME": "rev", "GIT_COMMITTER_EMAIL": "rev@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z",
    "GIT_CONFIG_NOSYSTEM": "1",
}
AKIA = "AKIA" + "ABCDEFGHIJKLMNOP"
GH_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
HIGH_VALUE = "".join(["Zk3m", "Q9xL", "2vB7", "nR4t", "Y8wE"])
JWT = "eyJ" + "abcdefghij" + ".eyJ" + "abcdefghij" + ".abcdefghijk"
SLACK = "xox" + "b-" + "1234567890-abc"
PEM = "-----BEGIN " + "RSA " + "PRIVATE KEY-----"


@dataclass(frozen=True)
class Case:
    name: str
    served: bool
    build: Callable[[Path], tuple[Path, list[str]]]
    json: bool = True
    signed: bool = True
    note: str = ""


def _write(root: Path, files: dict[str, str | bytes]) -> None:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8", newline="")


def _git(root: Path, *args: str) -> None:
    env = {**os.environ, **GIT_ENV, "HOME": str(root.parent / "home")}
    subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)


def _repo(base: Path, first: dict[str, str | bytes], second: dict[str, str | bytes] | None = None,
          delete: tuple[str, ...] = ()) -> Path:
    root = base / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _write(root, first)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    if second is not None or delete:
        if second:
            _write(root, second)
        for rel in delete:
            (root / rel).unlink()
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "two")
    return root


def _plain(base: Path, files: dict[str, str | bytes], args: list[str] | None = None) -> tuple[Path, list[str]]:
    root = base / "tree"
    root.mkdir(parents=True)
    _write(root, files)
    return root, list(args or [])


def _lines(count: int, prefix: str = "x") -> str:
    return "".join(f"{prefix}{i} = {i}\n" for i in range(count))


def _whole_tree_mixed(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {
        "src/app.py": "import os\n# TODO: remove\nprint('hi')\nx = 1\n",
        "tests/test_app.py": "def test_x():\n    assert True\n",
        "README.md": "# hi\n",
    })


def _whole_tree_every_check(base: Path) -> tuple[Path, list[str]]:
    js = "\n".join([
        "console.log(value);",
        "// TODO fix later",
        "// if (value) {",
        "// for (;;) {",
        "// return value;",
        "debugger;",
        f"const aws = '{AKIA}';",
        f"const password = '{HIGH_VALUE}';",
        "bad\x01char",
    ]) + "\n"
    py = "\n".join([
        "# import os",
        "# from x import y",
        "# def f():",
        "# return 1",
        "pdb.set_trace()",
        "breakpoint()",
        f"token = '{JWT}'",
        f"key = '{SLACK}'",
        f"gh = '{GH_TOKEN}'",
        f"header = '{PEM}'",
        "api_key = 'placeholder_value_1234567890'",
        f"secret_key: \"{HIGH_VALUE}\"",
        "low = 'aaaaaaaaaaaaaaaaaaaa'",
    ]) + "\n"
    return _plain(base, {
        "src/a.js": js,
        "src/b.py": py,
        "tests/a.spec.js": "it('x', () => {})\n",
    })


def _size_over_max(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"big.txt": _lines(12)}, ["--max-lines", "5"])


def _size_default_threshold(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"big.txt": _lines(801)})


def _binary_whole_tree(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"blob.bin": b"\xff\xfe\x00bin", "ok.txt": "fine\n"})


def _line_breaks(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {
        "crlf.py": b"x = 1\r\n# TODO a\r\nlone\rcr\r# TODO b\n# TODO c\x0bd\x0ce\x1cf\x1dg\x1eh\x1fi\n",
        "tests/test_crlf.py": "x = 1\n",
    })


def _coverage_without_tests(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"src/only.py": "x = 1\n"})


def _root_under_build(base: Path) -> tuple[Path, list[str]]:
    root = base / "build" / "repo"
    root.mkdir(parents=True)
    _write(root, {"src/a.py": "# TODO x\n", "tests/test_a.py": "x = 1\n"})
    return root, []


def _symlink_declined(base: Path) -> tuple[Path, list[str]]:
    root, args = _plain(base, {"src/a.py": "x = 1\n"})
    (root / "src" / "link.py").symlink_to(root / "src" / "a.py")
    return root, args


def _self_named_declined(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"scripts/rev_gate.py": "print('x')\n"})


def _non_ascii_declined(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"src/u.py": "x = 'caf\u00e9'\n"})


def _bidi_declined(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"src/bidi.py": "x = 1  # \u202e evil\n"})


def _bom_declined(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"src/bom.py": "\ufeffx = 1\n"})


def _nel_declined(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"src/nel.py": "# TODO a\x85b\n"})


def _dry_run_declined(base: Path) -> tuple[Path, list[str]]:
    return _plain(base, {"src/a.py": "x = 1\n"}, ["--dry-run"])


def _missing_root_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    return root, ["--root", str(base / "nope")]


def _unsafe_task_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    return root, ["--task-id", "bad id"]


def _diff_rules(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"src/keep.py": "x = 1\n"},
                 {"src/keep.py": "x = 1\nprint('x')\n# TODO new\n", "tests/test_keep.py": "y = 2\n"})
    return root, ["--diff-base", "HEAD~1"]


def _diff_invalid_base(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"src/keep.py": "x = 1\n"}, {"src/keep.py": "x = 1\n# TODO z\n"})
    return root, ["--diff-base", "no-such-ref"]


def _diff_single_commit(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"src/only.py": "x = 1\n# TODO once\n"})
    return root, []


def _diff_double_plus(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"src/c.py": "counter = 0\n"}, {"src/c.py": "counter = 0\n++ counter\n"})
    return root, []


def _diff_double_minus(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"db/q.sql": "-- old sql comment\nselect 1;\n"},
                 {"db/q.sql": "select 1;\n", "src/z.py": "z = 1\n"})
    return root, []


def _diff_binary(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"a.txt": "one\n", "img.bin": b"\x00\x01\x02"},
                 {"a.txt": "one\ntwo\n", "img.bin": b"\x00\x01\x03\x04"})
    return root, []


def _diff_deleted(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"src/gone.py": "x = 1\n", "src/keep.py": "y = 2\n"},
                 {"src/keep.py": "y = 3\n"}, delete=("src/gone.py",))
    return root, []


def _diff_untracked_skipped(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"src/keep.py": "x = 1\n", ".gitignore": "build/\n"},
                 {"src/keep.py": "x = 2\n"})
    _write(root, {"notes.txt": "loose\n", "build/x.py": "# TODO hidden\n", "dist/y.js": "// TODO dist\n"})
    return root, []


def _diff_quoted_name(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"seed.txt": "s\n"}, {"caf\u00e9.txt": "menu\n", "seed.txt": "s2\n"})
    return root, []


def _diff_name_with_space(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"seed.txt": "s\n"}, {"my file.py": "x = 1\n# TODO spaced\n", "seed.txt": "s2\n"})
    return root, []


def _no_signing_key(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    return root, []


def _diff_crlf(base: Path) -> tuple[Path, list[str]]:
    root = _repo(base, {"seed.txt": "s\n"}, {"src/crlf.py": b"x = 1\r\n# TODO here\r\nlone\rcr\r\n",
                                             "seed.txt": "s2\n"})
    return root, []


def _diff_dead_code_across_hunks(base: Path) -> tuple[Path, list[str]]:
    first = _lines(10)
    second = first.splitlines(keepends=True)
    second.insert(2, "# import os\n")
    second.insert(6, "# return 1\n")
    second.insert(10, "# if x:\n")
    root = _repo(base, {"src/d.py": first}, {"src/d.py": "".join(second)})
    return root, []


def _findings_file(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    (root / "findings.json").write_text(json.dumps({"findings": [
        {"severity": "minor", "summary": "naming", "kind": "style", "location": "src/a.py:1"},
        {"id": "X-1", "severity": "info", "summary": "ok"},
    ]}), encoding="utf-8")
    return root, ["--findings-file", str(root / "findings.json")]


def _findings_major(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    (root / "findings.json").write_text(json.dumps([
        {"severity": "major", "summary": "wrong branch", "evidence": "line 4", "owner_suggestion": "A05"},
    ]), encoding="utf-8")
    return root, ["--findings-file", str(root / "findings.json")]


def _findings_nonascii(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    (root / "findings.json").write_text(json.dumps([
        {"severity": "minor", "summary": "na\u00efve name", "ac_ref": "AC-2"},
    ]), encoding="utf-8")
    return root, ["--findings-file", str(root / "findings.json")]


def _findings_duplicate_keys(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    (root / "findings.json").write_text(
        '{"findings": [], "findings": [{"summary": "last wins", "severity": "minor"}]}', encoding="utf-8")
    return root, ["--findings-file", str(root / "findings.json")]


def _findings_missing_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    return root, ["--findings-file", str(root / "absent.json")]


def _findings_bad_severity_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    (root / "findings.json").write_text(json.dumps([{"severity": "urgent", "summary": "s"}]), encoding="utf-8")
    return root, ["--findings-file", str(root / "findings.json")]


def _findings_non_object_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    (root / "findings.json").write_text(json.dumps(["just text"]), encoding="utf-8")
    return root, ["--findings-file", str(root / "findings.json")]


def _findings_big_int_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    (root / "findings.json").write_text('[{"summary": 12345678901234567890123, "severity": "minor"}]', encoding="utf-8")
    return root, ["--findings-file", str(root / "findings.json")]


def _per_target_served(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    (root / "per.json").write_text(json.dumps({
        "T-missing": [{"summary": "only mine", "severity": "major"}],
        "T-other": [{"summary": "info", "severity": "info"}],
    }), encoding="utf-8")
    return root, ["--per-target-findings", str(root / "per.json")]


def _per_target_empty_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    (root / "per.json").write_text("{}", encoding="utf-8")
    return root, ["--per-target-findings", str(root / "per.json")]


def _per_target_non_list_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    (root / "per.json").write_text(json.dumps({"T-1": "nope"}), encoding="utf-8")
    return root, ["--per-target-findings", str(root / "per.json")]


def _per_target_with_task_no_store_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    (root / "per.json").write_text(json.dumps({"T-1": [{"summary": "s", "severity": "minor"}]}), encoding="utf-8")
    return root, ["--task-id", "T-1", "--per-target-findings", str(root / "per.json")]


_SEED = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from swarm.taskstore import TaskStore
store = TaskStore(Path(sys.argv[2]))
for spec in json.loads(sys.argv[3]):
    store.create(task_id=spec["task_id"], correlation_id=spec["correlation_id"], capability="review",
                 title=spec.get("title", ""), notes=spec.get("notes"))
    if spec.get("state"):
        store.update(spec["task_id"], state=spec["state"])
"""


def _seed(root: Path, specs: list[dict]) -> None:
    """Creates `root/.swarm/tasks.db` through the real `TaskStore` and the given task rows."""
    (root / ".swarm").mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith("SWARM_")}
    subprocess.run([sys.executable, "-c", _SEED, str(REPO), str(root / ".swarm" / "tasks.db"), json.dumps(specs)],
                   check=True, capture_output=True, env=env)


def _gate_specs() -> list[dict]:
    targets = ["T-a", "T-b"]
    return [
        {"task_id": "T-a", "correlation_id": "C-1", "state": "VALIDATED"},
        {"task_id": "T-b", "correlation_id": "C-1", "state": "VALIDATED"},
        {"task_id": "T-gate", "correlation_id": "C-1", "state": "IN_PROGRESS",
         "notes": {"gate": "review", "gate_for": targets}},
    ]


def _gate_task_records(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    _seed(root, _gate_specs())
    return root, ["--task-id", "T-gate", "--correlation-id", "C-1"]


def _gate_task_per_target(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    _seed(root, _gate_specs())
    (root / "per.json").write_text(json.dumps({"T-a": [{"summary": "only a", "severity": "major"}],
                                              "T-b": []}), encoding="utf-8")
    return root, ["--task-id", "T-gate", "--correlation-id", "C-1",
                  "--per-target-findings", str(root / "per.json")]


def _gate_task_unknown_target_declined(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n"})
    _seed(root, _gate_specs())
    (root / "per.json").write_text(json.dumps({"T-zzz": [{"summary": "stray", "severity": "minor"}]}),
                                   encoding="utf-8")
    return root, ["--task-id", "T-gate", "--correlation-id", "C-1",
                  "--per-target-findings", str(root / "per.json")]


def _task_id_with_empty_store(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    _seed(root, [])
    return root, ["--task-id", "T-1", "--correlation-id", "C-1"]


def _task_id_without_store(base: Path) -> tuple[Path, list[str]]:
    root, _ = _plain(base, {"src/a.py": "x = 1\n", "tests/test_a.py": "x = 1\n"})
    return root, ["--task-id", "T-1", "--correlation-id", "C-1"]


CASES: tuple[Case, ...] = (
    Case("whole_tree_rules_mixed", True, _whole_tree_mixed),
    Case("whole_tree_every_check_kind", True, _whole_tree_every_check),
    Case("size_over_max_lines", True, _size_over_max),
    Case("size_default_threshold_801", True, _size_default_threshold),
    Case("binary_file_whole_tree", True, _binary_whole_tree),
    Case("line_breaks_crlf_and_separators", True, _line_breaks),
    Case("coverage_without_tests", True, _coverage_without_tests),
    Case("root_under_build_dir_is_empty", True, _root_under_build),
    Case("symlink_in_tree", False, _symlink_declined),
    Case("self_named_rev_gate_py", False, _self_named_declined),
    Case("non_ascii_content", False, _non_ascii_declined),
    Case("bidi_control_character", False, _bidi_declined),
    Case("byte_order_mark", False, _bom_declined),
    Case("nel_separator", False, _nel_declined),
    Case("dry_run", False, _dry_run_declined),
    Case("text_mode_without_json", False, _whole_tree_mixed, json=False),
    Case("root_not_a_directory", False, _missing_root_declined),
    Case("unsafe_task_id", False, _unsafe_task_declined),
    Case("diff_head_minus_one_rules", True, _diff_rules),
    Case("diff_invalid_base_falls_back", True, _diff_invalid_base),
    Case("diff_single_commit_no_base", True, _diff_single_commit),
    Case("diff_added_line_double_plus", True, _diff_double_plus),
    Case("diff_deleted_line_double_minus", True, _diff_double_minus),
    Case("diff_binary_change", True, _diff_binary),
    Case("diff_deleted_file", True, _diff_deleted),
    Case("diff_untracked_and_ignored", True, _diff_untracked_skipped),
    Case("diff_quoted_non_ascii_name", True, _diff_quoted_name),
    Case("diff_crlf_added_lines", True, _diff_crlf),
    Case("diff_name_with_space", True, _diff_name_with_space),
    Case("diff_dead_code_across_hunks", True, _diff_dead_code_across_hunks),
    Case("findings_file_with_findings_key", True, _findings_file),
    Case("findings_file_major_blocks", True, _findings_major),
    Case("findings_file_non_ascii_text", True, _findings_nonascii),
    Case("findings_file_duplicate_keys", True, _findings_duplicate_keys),
    Case("findings_file_missing", False, _findings_missing_declined),
    Case("findings_file_bad_severity", False, _findings_bad_severity_declined),
    Case("findings_file_non_object_item", False, _findings_non_object_declined),
    Case("findings_file_integer_beyond_i64", False, _findings_big_int_declined),
    Case("per_target_without_task", True, _per_target_served),
    Case("per_target_empty_object", False, _per_target_empty_declined),
    Case("per_target_value_not_list", False, _per_target_non_list_declined),
    Case("per_target_with_task_and_no_store", False, _per_target_with_task_no_store_declined),
    Case("no_signing_key_advisory", True, _no_signing_key, signed=False),
    Case("task_id_with_empty_store", True, _task_id_with_empty_store),
    Case("gate_task_records_targets", True, _gate_task_records),
    Case("gate_task_per_target_known_targets", True, _gate_task_per_target),
    Case("gate_task_per_target_unknown_target", False, _gate_task_unknown_target_declined),
    Case("task_id_without_store_dir", False, _task_id_without_store),
)


def base_env(home: Path) -> dict[str, str]:
    """The environment both legs run in: signing key set, no state file, a private HOME for git."""
    env = {key: value for key, value in os.environ.items()
           if key not in {"SWARM_ED25519_KEY", "SWARM_AGENT_SESSION", "SWARM_TASK_ID", "SWARM_CORRELATION_ID",
                          "SWARM_DRYRUN_FAIL", "GSD_ENGINE", "SWARM_PURE_PYTHON", "SWARM_DIR", "GSD_ENGINE_BIN",
                          "GIT_DIR", "GIT_INDEX_FILE", "XDG_CONFIG_HOME"}}
    env.update(GIT_ENV)
    env.update({
        "SWARM_SIGNING_KEY": SIGNING_KEY,
        "SWARM_GSD_STATE_FILE": "",
        "SWARM_CLAUDE_STATE_DIR": str(home / "claude"),
        "HOME": str(home),
        "GSD_RUST_BACKEND": "host",
    })
    return env


def materialize(case: Case, base: Path) -> tuple[Path, list[str], dict[str, str]]:
    """Builds the case under `base` and returns (root, argv, env). The argv carries `--json` unless the case is text mode."""
    base.mkdir(parents=True, exist_ok=True)
    root, args = case.build(base)
    home = base / "home"
    home.mkdir(exist_ok=True)
    env = base_env(home)
    env["SWARM_DIR"] = str(root / ".swarm")
    if not case.signed:
        env.pop("SWARM_SIGNING_KEY", None)
    argv = (["--json"] if case.json else []) + args
    return root, argv, env


def run_python(root: Path, argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *argv], cwd=root, env=env, capture_output=True, text=True,
                          timeout=120)


def run_binary(binary: str, root: Path, argv: list[str], env: dict[str, str]) -> tuple[int, dict]:
    """One call through the binary with the selector's request shape. Returns (exit code, reply object)."""
    request = {
        "agent": "A09",
        "script": "rev_gate",
        "argv": argv,
        "root": str(root),
        "env": {key: env[key] for key in REQUEST_ENV if env.get(key)},
    }
    done = subprocess.run([binary], input=json.dumps(request).encode("utf-8"), capture_output=True, env=env,
                          timeout=120)
    return done.returncode, json.loads(done.stdout.decode("utf-8"))


def _mask(value, drop: set[str]):
    if isinstance(value, dict):
        return {key: _mask(item, drop) for key, item in value.items() if key not in drop}
    if isinstance(value, list):
        return [_mask(item, drop) for item in value]
    return value


def comparable(body: dict, argv: list[str]) -> dict:
    """The body with the envelope's random and clock fields removed; a caller-given correlation id is kept."""
    drop = set(MASKED)
    if "--correlation-id" not in argv:
        drop |= MASKED_WITHOUT_CORRELATION
    return _mask(body, drop)


def declined(code: int, reply: dict) -> bool:
    error = reply.get("error") or {}
    details = error.get("details") or {}
    return code == 2 and error.get("code") == "E-DEP" and details.get("write_state") == "not_started"


def answered(code: int, reply: dict) -> bool:
    return code in (0, 1) and reply.get("script") == "rev_gate" and reply.get("agent") == "A09"
