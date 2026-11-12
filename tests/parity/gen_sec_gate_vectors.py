"""Corpus of `sec_gate` parity cases (phase 14.5, unit U3.6e).

Every secret-shaped value is generated in the run from a seeded RNG, in the shapes the scanner matches. None is a
credential: the values are random strings. Trees, shims and inputs are written into a scratch directory by
`materialize`, and nothing secret-like is committed.

Each case says whether the host route must serve it (`served`). The served cells are the grammar the route proves
identical to Python; the declined cells are the ones it must hand to Python with `E-DEP` and `write_state =
not_started`. Run `python3 tests/parity/gen_sec_gate_vectors.py --list` to print the corpus, or
`--manifest <file>` to write it as JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import stat
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Union

SLEEP = shutil.which("sleep") or "/bin/sleep"  # an absolute path: audit shims run with PATH set to the shim dir alone

UPPER_DIGITS = string.ascii_uppercase + string.digits
ALNUM = string.ascii_letters + string.digits
URL_SAFE = ALNUM + "_-"
HEX = "0123456789abcdef"

Entry = Union[str, bytes, tuple]  # text, raw bytes, or ("symlink", target)


@dataclass
class Case:
    name: str
    served: bool
    argv: list[str]  # `{root}` and `{base}` are substituted per run; --json is part of the argv
    files: dict[str, Entry] = field(default_factory=dict)  # paths inside the scanned tree
    outside: dict[str, str] = field(default_factory=dict)  # paths under the case base (allow lists, findings files)
    shims: dict[str, str] = field(default_factory=dict)  # PATH shims: name -> shell body (after the shebang)
    root_rel: str = "repo"  # the scanned tree, relative to the case base
    note: str = ""


def _synth(rng: random.Random, alphabet: str, size: int) -> str:
    return "".join(rng.choice(alphabet) for _ in range(size))


def _secret_lines(rng: random.Random) -> str:
    aws = "AKIA" + _synth(rng, UPPER_DIGITS, 16)
    gh = "ghp_" + _synth(rng, ALNUM, 36)
    slack = "xoxb-" + _synth(rng, ALNUM, 12)
    stripe = "sk_live_" + _synth(rng, ALNUM, 24)
    google = "AIza" + _synth(rng, URL_SAFE, 35)
    jwt = "eyJ" + _synth(rng, ALNUM, 12) + ".eyJ" + _synth(rng, ALNUM, 12) + "." + _synth(rng, URL_SAFE, 12)
    aws_secret = _synth(rng, ALNUM + "/+=", 40)
    password = _synth(rng, ALNUM, 20)
    secret_key = _synth(rng, HEX, 20)
    return "\n".join([
        f'key = "{aws}"',
        f'token = "{gh}"',
        f'SLACK = "{slack}"',
        f"stripe = '{stripe}'",
        f'maps = "{google}"',
        '-----BEGIN RSA PRIVATE KEY-----',
        f'bearer = "{jwt}"',
        f'aws_secret_access_key = "{aws_secret}"',
        f'password = "{password}"',
        f'secret_key = "{secret_key}"',
        "# a comment still carries a leaked key: " + aws,
        "",
    ])


_PLACEHOLDER_LINES = "\n".join([
    'token = "EXAMPLE_TOKEN_DO_NOT_USE_0123456789"',
    'password = "changeme-changeme-changeme"',
    'api_key = "your-api-key-goes-here-123456"',
    'secret = "<redacted-value-placeholder>"',
    "",
])

_LOW_ENTROPY_LINES = "\n".join([
    'password = "aaaaaaaaaaaaaaaaaaaa"',
    'passwd = "abcdefghijklmnopqrstuvwxyz"',
    'api_key = "ab12ab12ab12ab12ab12"',
    "",
])

_SAST_PY = "\n".join([
    "eval(user_input)",
    "exec(command_text)",
    'eval("1 + 1")',
    'subprocess.run(f"ls {directory}", shell=True)',
    'os.system("ls " + directory)',
    "pickle.loads(blob)",
    "yaml.load(stream)",
    "yaml.load(stream, Loader=SafeLoader)",
    'cursor.execute(f"SELECT * FROM t WHERE id={identifier}")',
    'cursor.execute("SELECT * FROM t WHERE id=%s", (identifier,))',
    "query = \"select * from t where x = '\" + value",
    'subprocess.run(["ls", directory], shell=False)',
    "# eval(commented_out)",
    "",
])

_SAST_JS = "\n".join([
    "eval(code);",
    "const f = new Function('return 1');",
    "el.innerHTML = html;",
    "const node = <div dangerouslySetInnerHTML={markup} />;",
    "db.query(`select * from t where id = ${identifier}`);",
    "// el.innerHTML = commented;",
    "",
])

_IAC_TF = "\n".join([
    'cidr_blocks = ["0.0.0.0/0"]',
    "privileged = true",
    "hostNetwork: true",
    "# privileged = true",
    "",
])

_IAC_YAML = "\n".join([
    "securityContext:",
    "  allowPrivilegeEscalation: true",
    "  runAsUser: 0",
    "  hostPID: true",
    "  # hostIPC: true",
    "",
])


def _pad_to(text: str, size: int) -> bytes:
    body = text.encode("ascii")
    return body + b"x" * (size - len(body))


def _shim_json(body: str, status: int = 0) -> str:
    # Shims run with PATH set to the shim directory alone, so they use shell builtins only.
    return f"printf '%s\\n' '{body}'\nexit {status}"


def build_cases(rng: random.Random, base_marker: str = "") -> list[Case]:
    """The corpus. Secret values come from `rng`, so a seeded run is reproducible."""
    secrets = _secret_lines(rng)
    cases: list[Case] = []
    add = cases.append

    add(Case("empty_tree", True, ["--json"], note="no files, no manifests"))
    add(Case(
        "secrets_and_placeholders", True, ["--json"],
        files={"src/app.py": secrets + _PLACEHOLDER_LINES + _LOW_ENTROPY_LINES,
               "config/settings.env": secrets},
    ))
    add(Case(
        "sast_rules", True, ["--json"],
        files={"pkg/run.py": _SAST_PY, "web/app.js": _SAST_JS, "web/view.ts": _SAST_JS,
               "svc/main.go": "exec.Command(x)\n", "README.md": "eval(not_scanned)\n"},
    ))
    add(Case(
        "iac_rules", True, ["--json"],
        files={"infra/main.tf": _IAC_TF, "k8s/pod.yaml": _IAC_YAML, "k8s/other.yml": _IAC_YAML,
               "docs/notes.txt": _IAC_TF},
    ))
    add(Case(
        "line_separators_and_crlf", True, ["--json"],
        files={"src/crlf.py": "x = 1\r\nkey = \"AKIAABCDEFGHIJKLMNOP\"\r\neval(a)\r\n",
               "src/lone_cr.py": "a = 1\rkey = \"AKIAZYXWVUTSRQPONMLK\"\r",
               "src/vt.py": "a = 1\x0bkey = \"AKIA1234567890ABCDEF\"\x0cdone\x1cz = eval(q)\x1d\x1e",
               "src/unit_sep.py": "password\x1f=\x1f\"Zq3rT8vL2mN5pX7kB1wD\"\n"},
    ))
    add(Case(
        "invalid_utf8_is_skipped", True, ["--json"],
        files={"src/bad.py": b"\xff\xfe key = \"AKIAABCDEFGHIJKLMNOP\"\n",
               "src/good.py": "key = \"AKIAQWERTYUIOPASDFGH\"\n"},
    ))
    add(Case(
        "non_ascii_text_declines", False, ["--json"],
        files={"src/dash.py": "# a dash \u2014 here\nkey = \"AKIAABCDEFGHIJKLMNOP\"\n"},
        note="valid non-ASCII UTF-8 in a scanned file: Python's Unicode tables decide",
    ))
    add(Case(
        "non_ascii_file_name_serves", True, ["--json"],
        files={"src/na\u00efve.py": "key = \"AKIAABCDEFGHIJKLMNOP\"\n"},
        note="a non-ASCII name is only a location, not scanned text",
    ))
    add(Case(
        "size_limit_2mb", True, ["--json"],
        files={"src/big.py": _pad_to('key = "AKIAABCDEFGHIJKLMNOP"\n', 2_000_001),
               "src/edge.py": _pad_to('key = "AKIAZZZZZZZZZZZZZZZZ"\n', 2_000_000)},
        note="2_000_001 bytes is skipped, 2_000_000 bytes is scanned",
    ))
    add(Case(
        "walk_order_nested", True, ["--json"],
        files={
            "a/one.py": 'key = "AKIAAAAAAAAAAAAAAAAA"\n',
            "a/b/two.py": 'key = "AKIABBBBBBBBBBBBBBBB"\n',
            "a/b/c/three.py": 'key = "AKIACCCCCCCCCCCCCCCC"\n',
            "a/d/four.py": 'key = "AKIADDDDDDDDDDDDDDDD"\n',
            "z/five.py": 'key = "AKIAEEEEEEEEEEEEEEEE"\n',
            "m/n/six.py": 'key = "AKIAFFFFFFFFFFFFFFFF"\n',
            "top.py": 'key = "AKIAGGGGGGGGGGGGGGGG"\n',
            "m/seven.py": 'key = "AKIAHHHHHHHHHHHHHHHH"\n',
        },
        note="emission order is the rglob order of CPython 3.12",
    ))
    add(Case(
        "skipped_directories", True, ["--json"],
        files={"node_modules/dep/x.py": secrets, ".git/hook.py": secrets,
               "build/out.py": secrets, "dist/out.py": secrets, "__pycache__/c.py": secrets,
               ".venv/v.py": secrets, "venv/v.py": secrets, ".swarm/s.py": secrets,
               "src/kept.py": 'key = "AKIAKEPTKEPTKEPTKEPT"\n'},
    ))
    add(Case(
        "root_under_build_directory", True, ["--json"], root_rel="build/repo",
        files={"src/app.py": secrets},
        note="iter_files tests absolute parts: nothing is scanned under a build ancestor",
    ))
    add(Case(
        "symlinks", True, ["--json"],
        files={"src/real.py": 'key = "AKIASYMLINKSYMLINKS1"\n',
               "src/link.py": ("symlink", "real.py"),
               "src/linkdir": ("symlink", "../src"),
               "src/dangling.py": ("symlink", "missing.py")},
    ))
    add(Case(
        "self_named_file_declines", False, ["--json"],
        files={"scripts/sec_gate.py": "# not the script\nkey = \"AKIAABCDEFGHIJKLMNOP\"\n"},
        note="a file named sec_gate.py may be the script: Python answers",
    ))
    add(Case(
        "manifests_no_tools_skipped", True, ["--json"],
        files={"requirements.txt": "requests==2.0\n", "package.json": "{}\n",
               "Cargo.lock": "x\n", "go.mod": "module x\n", "src/app.py": "x = 1\n"},
        note="PATH holds no audit tool: each audit is skipped:tool-missing",
    ))
    add(Case(
        "strict_missing_tools", True, ["--json", "--strict"],
        files={"requirements.txt": "requests==2.0\n", "package.json": "{}\n", "src/app.py": "x = 1\n"},
    ))
    add(Case(
        "pip_audit_clean", True, ["--json"],
        files={"requirements.txt": "requests==2.0\n", "src/app.py": "x = 1\n"},
        shims={"pip-audit": _shim_json('{"dependencies": [{"name": "requests", "vulns": []}]}')},
    ))
    add(Case(
        "pip_audit_kev_blocker", True, ["--json"],
        files={"requirements.txt": "requests==2.0\n"},
        shims={"pip-audit": _shim_json(
            '{"dependencies": [{"name": "requests", "vulns": [{"id": "PYSEC-1", "aliases": ["CVE-1"], "kev": true}]}]}', 1)},
    ))
    add(Case(
        "pip_audit_high_and_exploited_word", True, ["--json"],
        files={"requirements.txt": "requests==2.0\n", "pyproject.toml": "[project]\n"},
        shims={"pip-audit": _shim_json(
            '{"dependencies": [{"name": "a", "vulns": [{"id": "PYSEC-2", "details": "actively Exploited"}, {"id": "PYSEC-3"}]}]}', 1)},
    ))
    add(Case(
        "pip_audit_list_top_level", True, ["--json"],
        files={"requirements-dev.txt": "pytest\n"},
        shims={"pip-audit": _shim_json('[{"name": "a", "vulns": [{"id": "X-1"}]}]', 1)},
    ))
    add(Case(
        "pip_audit_bad_shape_declines", False, ["--json"],
        files={"requirements.txt": "requests==2.0\n"},
        shims={"pip-audit": _shim_json('{"dependencies": 5}', 0)},
        note="Python raises TypeError (exit 2): the route declines",
    ))
    add(Case(
        "pip_audit_nan_declines", False, ["--json"],
        files={"requirements.txt": "requests==2.0\n"},
        shims={"pip-audit": _shim_json('{"dependencies": [], "x": NaN}', 0)},
        note="Python's json accepts NaN; the route's parser does not prove it",
    ))
    add(Case(
        "npm_audit_moderate", True, ["--json"],
        files={"package.json": "{}\n", "package-lock.json": "{}\n"},
        shims={"npm": _shim_json('{"metadata": {"vulnerabilities": {"info": 0, "low": 1, "moderate": 2, "high": 0, "critical": 0}}}', 1)},
    ))
    add(Case(
        "npm_audit_critical", True, ["--json"],
        files={"package.json": "{}\n"},
        shims={"npm": _shim_json('{"metadata": {"vulnerabilities": {"critical": 1, "high": 2, "moderate": 0, "low": 0}}}', 1)},
    ))
    add(Case(
        "npm_audit_non_json_failure", True, ["--json"],
        files={"package.json": "{}\n"},
        shims={"npm": "echo 'network down'\nexit 3"},
        note="no JSON: the return code alone makes high = 1",
    ))
    add(Case(
        "npm_audit_non_ascii_declines", False, ["--json"],
        files={"package.json": "{}\n"},
        shims={"npm": _shim_json('{"metadata": {"vulnerabilities": {}}, "note": "\u00e9"}', 0)},
    ))
    add(Case(
        "cargo_audit_count", True, ["--json"],
        files={"Cargo.lock": "version = 3\n"},
        shims={"cargo-audit": "exit 0", "cargo": _shim_json('{"vulnerabilities": {"count": 2, "found": true}}', 1)},
    ))
    add(Case(
        "govulncheck_findings", True, ["--json"],
        files={"go.mod": "module x\n"},
        shims={"govulncheck": 'echo \'{"finding": {"osv": "GO-1"}, "trace": [{"module": "m"}]}\'\necho \'{"config": {}}\'\n'
                              'echo \'{"finding": {"osv": "GO-2"}, "trace": []}\'\nexit 0'},
    ))
    add(Case(
        "audit_timeout_fails_closed", True, ["--json", "--timeout", "1"],
        files={"package.json": "{}\n"},
        shims={"npm": f"exec {SLEEP} 30"},
        note="TimeoutExpired text is served byte for byte",
    ))
    add(Case(
        "allow_list_suppresses_and_expires", True, ["--json", "--allow-list", "{base}/allow.json"],
        files={"src/a.py": 'key = "AKIAABCDEFGHIJKLMNOP"\n', "src/b.py": 'key = "AKIAQRSTUVWXYZ123456"\n'},
        outside={"allow.json": json.dumps({"allow": [
            {"id": "{id_a}", "justification": "fixture key", "expires": "2999-12-31"},
            {"id": "{id_b}", "justification": "old waiver", "expires": "2000-01-01"},
        ]})},
    ))
    add(Case(
        "allow_list_without_justification_declines", False, ["--json", "--allow-list", "{base}/allow.json"],
        files={"src/a.py": 'key = "AKIAABCDEFGHIJKLMNOP"\n'},
        outside={"allow.json": json.dumps([{"id": "{id_a}"}])},
    ))
    add(Case(
        "allow_list_bad_expiry_declines", False, ["--json", "--allow-list", "{base}/allow.json"],
        files={"src/a.py": 'key = "AKIAABCDEFGHIJKLMNOP"\n'},
        outside={"allow.json": json.dumps([{"id": "{id_a}", "justification": "x", "expires": "2030-02-30"}])},
    ))
    add(Case(
        "allow_list_empty_path_is_none", True, ["--json", "--allow-list", ""],
        files={"src/a.py": 'key = "AKIAABCDEFGHIJKLMNOP"\n'},
    ))
    add(Case(
        "per_target_one_target_blocking_declines", False, ["--json", "--per-target-findings", "{base}/findings.json"],
        files={"src/a.py": "x = 1\n"},
        outside={"findings.json": json.dumps({"T-target-1": [{"severity": "major", "summary": "agent says"}]})},
        note="a passing script verdict with a major agent finding: exit 1 needs status fail in the engine contract",
    ))
    add(Case(
        "per_target_one_target", True, ["--json", "--per-target-findings", "{base}/findings.json"],
        files={"src/a.py": "x = 1\n"},
        outside={"findings.json": json.dumps({"T-target-1": [{"severity": "minor", "summary": "agent says"}]})},
    ))
    add(Case(
        "per_target_two_targets_declines", False, ["--json", "--per-target-findings", "{base}/findings.json"],
        files={"src/a.py": "x = 1\n"},
        outside={"findings.json": json.dumps({"T-a": [], "T-b": []})},
    ))
    add(Case(
        "per_target_with_task_id_declines", False,
        ["--json", "--task-id", "T-7", "--per-target-findings", "{base}/findings.json"],
        files={"src/a.py": "x = 1\n"},
        outside={"findings.json": json.dumps({"T-7": [{"severity": "minor"}]})},
    ))
    add(Case(
        "correlation_id_only", True, ["--json", "--correlation-id", "C-100"],
        files={"src/a.py": 'key = "AKIAABCDEFGHIJKLMNOP"\n'},
    ))
    add(Case(
        "task_id_without_store_declines", False, ["--json", "--task-id", "T-7", "--correlation-id", "C-7"],
        files={"src/a.py": 'key = "AKIAABCDEFGHIJKLMNOP"\n'},
        note="the store path: with no .swarm directory the taskwriter refuses (not_started) and Python creates it",
    ))
    add(Case(
        "dry_run_declines", False, ["--json", "--dry-run"],
        files={"src/a.py": "x = 1\n"},
    ))
    add(Case(
        "text_mode_declines", False, [],
        files={"src/a.py": "x = 1\n"},
        note="no --json: Python's text output is not the route's",
    ))
    add(Case(
        "zero_timeout_declines", False, ["--json", "--timeout", "0"],
        files={"src/a.py": "x = 1\n"},
    ))
    add(Case(
        "placeholder_in_long_line", True, ["--json"],
        files={"src/long.py": "x = '" + "y" * 200 + "' + 'AKIAABCDEFGHIJKLMNOP'\n"},
        note="evidence is the stripped line cut to 160 characters",
    ))
    return cases


def case_by_name(name: str) -> Case:
    for case in build_cases(random.Random(20261030)):
        if case.name == name:
            return case
    raise KeyError(name)


def finding_id(kind: str, rule: str, loc: str) -> str:
    """Python's `fid` (sec_gate.py:63-64), used to name allow-list ids in the corpus."""
    return "SEC-" + hashlib.sha1("|".join((kind, rule, loc)).encode()).hexdigest()[:8]


def materialize(case: Case, base: Path, rng_seed: int = 20261030) -> dict[str, Path]:
    """Writes the case into `base`: the tree, the shims, the outside inputs. Returns the paths the runner needs."""
    root = base / case.root_rel
    root.mkdir(parents=True, exist_ok=True)
    for rel, entry in case.files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(entry, tuple) and entry[0] == "symlink":
            if target.is_symlink() or target.exists():
                target.unlink()
            target.symlink_to(entry[1])
        elif isinstance(entry, bytes):
            target.write_bytes(entry)
        else:
            target.write_text(entry, encoding="utf-8", newline="")
    shims = base / "shims"
    shims.mkdir(parents=True, exist_ok=True)
    for name, body in case.shims.items():
        shim = shims / name
        shim.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    for rel, text in case.outside.items():
        (base / rel).write_text(_fill_ids(text, root, case), encoding="utf-8")
    return {"root": root, "shims": shims, "base": base}


def _fill_ids(text: str, root: Path, case: Case) -> str:
    """Allow-list ids name the real findings of the tree, computed with Python's `fid`."""
    if "{id_a}" not in text and "{id_b}" not in text:
        return text
    return text.replace("{id_a}", finding_id("secrets", "aws-access-key", "src/a.py:1")).replace(
        "{id_b}", finding_id("secrets", "aws-access-key", "src/b.py:1"))


def argv_for(case: Case, paths: dict[str, Path]) -> list[str]:
    out = [item.replace("{base}", str(paths["base"])) for item in case.argv]
    return out + ["--root", str(paths["root"])]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the case names and whether the route serves them")
    parser.add_argument("--manifest", type=Path, help="write the corpus manifest (names, argv, served) as JSON")
    args = parser.parse_args()
    cases = build_cases(random.Random(20261030))
    if args.manifest:
        rows = [{"name": c.name, "served": c.served, "argv": c.argv, "note": c.note} for c in cases]
        args.manifest.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    if args.list or not args.manifest:
        for case in cases:
            print(f"{'served ' if case.served else 'declined'}  {case.name}")
        print(f"{sum(c.served for c in cases)} served, {sum(not c.served for c in cases)} declined")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
