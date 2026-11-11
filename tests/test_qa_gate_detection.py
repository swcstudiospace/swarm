"""Quality runner detection follows repository evidence, not executable availability.

pytest uses the current interpreter; .NET remains required when dotnet is absent.
The TypeScript twin stays a pass-through to the canonical Python entrypoint.

Literal npm/node:test selection and real, dependency-free consumer regressions.
Consumer tests create temporary packages and invoke installed npm/Node (never
npm install/ci/npx). Gate subprocesses use conftest's isolated state and signing
fixtures. Classification and aggregate samples are pure parser checks, not consumer runs.
"""
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import _scrubbed_base_env, run_script

ROOT = Path(__file__).resolve().parent.parent


def _load_qa_gate():
    path = ROOT / "scripts" / "qa_gate.py"
    spec = importlib.util.spec_from_file_location("qa_gate_detection_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package(root, script, files=None):
    root.mkdir(parents=True, exist_ok=True)
    (root / "package.json").write_text(json.dumps({
        "name": "qa-compat-consumer", "private": True, "type": "module", "scripts": {"test": script},
    }))
    for name, content in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def _consumer_gate(root, *, env=None, bun=False, risk="low", extra=()):
    argv = ["--root", str(root), "--risk-class", risk, "--json", *extra]
    if bun:
        binary = shutil.which("bun")
        if binary is None:
            pytest.skip("optional real Bun passthrough consumer requires installed bun")
        child_env = _scrubbed_base_env()
        child_env.update(env or {})
        proc = subprocess.run([binary, str(ROOT / "scripts" / "ts" / "qa_gate.ts"), *argv],
                              cwd=ROOT, capture_output=True, text=True, env=child_env, timeout=120)
    else:
        proc = run_script("qa_gate.py", *argv, env=env)
    assert proc.returncode in (0, 1), proc.stdout + proc.stderr
    body = json.loads(proc.stdout)
    assert body["status"] == ("ok" if proc.returncode == 0 else "fail"), body
    return proc, body


def _node_run(body):
    assert len(body["executed"]) == 1, body
    run = body["executed"][0]
    assert run["runner"] == "npm test"
    return run


@pytest.fixture
def node_tools():
    # Tool absence is a test prerequisite, not permission to install anything.
    if shutil.which("npm") is None or shutil.which("node") is None:
        pytest.skip("real consumer requires installed npm and Node")


_MARKER = """import { writeFileSync } from 'node:fs';
writeFileSync('node-marker.json', JSON.stringify({inherited: process.env.QA_COMPAT_SENTINEL}));
"""
_PASS = _MARKER + """import test from 'node:test';
import assert from 'node:assert/strict';
test('real assertion', () => assert.equal(2 + 2, 4));
"""
_FAIL = _MARKER + """import test from 'node:test';
import assert from 'node:assert/strict';
test('real assertion failure', () => assert.equal(2 + 2, 5));
"""


def test_pytest_detected_when_importable_but_not_on_path(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    assert shutil.which("pytest") is None
    assert importlib.util.find_spec("pytest") is not None

    runners = [r for r in _load_qa_gate().detect_runners(root) if r["name"] == "pytest"]
    assert len(runners) == 1
    assert runners[0]["cmd"][:3] == [sys.executable, "-m", "pytest"]


def test_pytest_detected_and_runs_without_pytest_on_path(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sample.py").write_text("def test_ok():\n    assert True\n")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert shutil.which("pytest") is None
    assert importlib.util.find_spec("pytest") is not None
    proc, body = _consumer_gate(root)
    assert proc.returncode == 0, body
    assert body["executed"][0]["runner"] == "pytest"
    assert body["executed"][0]["passed"] == 1
    assert body["executed"][0]["returncode"] == 0


def test_ts_twin_documents_interpreter_detection():
    twin = (ROOT / "scripts" / "ts" / "qa_gate.ts").read_text(encoding="utf-8")
    source = (ROOT / "scripts" / "qa_gate.py").read_text(encoding="utf-8")
    assert 'passthrough("qa_gate")' in twin
    assert "find_spec" in twin
    assert "importlib.util.find_spec" in source
    assert 'which("pytest")' not in source
    assert "which('pytest')" not in source


@pytest.mark.parametrize("relative", ["App.sln", "App.slnx", "App.slnf", "src/App/App.csproj",
                                    "src/App/App.fsproj", "src/App/App.vbproj"])
def test_dotnet_detected_without_sdk(tmp_path, monkeypatch, relative):
    project = tmp_path / relative
    project.parent.mkdir(parents=True, exist_ok=True)
    project.write_text("")
    monkeypatch.setenv("PATH", "")
    assert shutil.which("dotnet") is None
    assert [r["name"] for r in _load_qa_gate().detect_runners(tmp_path)] == ["dotnet"]


@pytest.mark.parametrize("relative", ["bin/App.csproj", "obj/App.csproj", "node_modules/pkg/App.csproj",
                                    ".git/App.sln", ".swarm/App.sln"])
def test_generated_or_external_dotnet_projects_are_not_workloads(tmp_path, relative):
    project = tmp_path / relative
    project.parent.mkdir(parents=True)
    project.write_text("")
    assert not any(r["name"] == "dotnet" for r in _load_qa_gate().detect_runners(tmp_path))


def test_dotnet_coexists_with_other_workload_detection(tmp_path, monkeypatch):
    (tmp_path / "App.csproj").write_text("")
    (tmp_path / "pytest.ini").write_text("")
    monkeypatch.setenv("PATH", "")
    assert {r["name"] for r in _load_qa_gate().detect_runners(tmp_path)} == {"pytest", "dotnet"}


@pytest.mark.parametrize("script", [
    "node --test",
    'node --test "dist/test/**/*.test.js"',
    'tsc -p tsconfig.json && node --test "dist/test/**/*.test.js"',
    '/usr/bin/tsc -p tsconfig.json && node --test "dist/test/**/*.test.js"',
    'tsc -p tsconfig.json&&node --test "dist/test/**/*.test.js"',
    "node --test-reporter tap --test test/pass.test.js",
    "node --test --test-reporter spec test/pass.test.js",
    "node --test-reporter=spec --test test/pass.test.js",
    "node --test --test-reporter=tap test/pass.test.js",
    "'node' --test 'test/a b.test.js'",
    r"n\ode --test test/a\ b.test.js",
    "echo 'literal && $HOME ; | text' && node --test 'test/*.test.js'",
    r'echo "literal \$HOME" && node --test "test/*.test.js"',
])
def test_literal_script_selection(script):
    qa = _load_qa_gate()
    assert qa._is_node_test_script(script) is True
    if "**" in script:
        assert qa._literal_script_commands(script)[-1][-1] == "dist/test/**/*.test.js"


@pytest.mark.parametrize("script", [
    'echo "node --test"', "echo node --test", "jest node --test", "mocha --test node",
    "nodejs --test", "/usr/bin/node --test", "./node --test", "node test/--test.js",
    'node -e "node --test"', 'node --eval "node --test"', 'node -p "node --test"',
    'node --print "node --test"', "node --test-name-pattern node --test",
    "node --test --unknown", "node --test --test", "node --test --ci",
    "node --test-reporter=dot --test", "node --test-reporter --test",
    "node --test-reporter=tap --test-reporter=spec --test",
    "node --test --test-reporter tap --test-reporter tap",
    "node --test -- test/pass.test.js", "node --test test/pass.test.js --test-reporter=tap",
    "node test/pass.test.js --test", "node --test=1", "node --test-reporter='node --test'",
    "NODE_OPTIONS=--test node --test", "env node --test", "command node --test",
    "exec node --test", "sh -c 'node --test'", "echo x && env node --test",
    "/bin/sh -c 'echo compiler' && node --test",
    "/usr/bin/env tsc -p tsconfig.json && node --test",
    "./tools/sudo tsc && node --test",
    "false || node --test", "echo x; node --test", "node --test | cat", "node --test > out",
    "node --test 2>&1", "node --test &", "(node --test)", "node --test\n",
    "node --test\r", "node --test\0", "node \\\n--test", "node --test 'unterminated",
    "node --test \v", "node --test \f", "node --test \x7f", "node --test \x85",
    'node --test "unterminated', "node --test " + chr(92), "&& node --test", "node --test &&",
    "node --test && && echo x", "if true && node --test", "while true && node --test",
    "node --test $FILES", 'node --test "$FILES"', 'node --test "$(echo x)"',
    "node --test `echo x`", "node --test ~/test.js", "node --test test/*.js",
    "node --test test/?.js", "node --test test/[ab].js", "node --test test/{a,b}.js",
    "node --test # comment", "echo node --test && echo done", None, 12,
])
def test_selection_traps_keep_existing_ci_fallback(script):
    assert _load_qa_gate()._is_node_test_script(script) is False


@pytest.mark.parametrize("reporter,options", [
    ("tap", "--test --test-reporter=tap"),
    ("tap", "--test-reporter tap --test"),
    ("spec", "--test --test-reporter spec"),
    ("spec", "--test-reporter=spec --test"),
])
@pytest.mark.parametrize("case,source,passed,failed", [
    ("pass", _PASS, 1, 0),
    ("fail", _FAIL, 0, 1),
    ("mixed", _PASS + "test('second assertion fails', () => assert.equal(1, 0));\n", 1, 1),
])
def test_real_npm_reporter_counts_and_exit(node_tools, tmp_path, reporter, options, case, source, passed, failed):
    root = _package(tmp_path / "repo", f"node {options} test/case.test.js", {"test/case.test.js": source})
    proc, body = _consumer_gate(root, env={"QA_COMPAT_SENTINEL": "inherited-without-rewrite"})
    run = _node_run(body)
    assert (run["passed"], run["failed"], run["evidence_complete"]) == (passed, failed, True)
    assert (run["returncode"] == 0) == (failed == 0)
    assert (proc.returncode == 0) == (failed == 0)
    marker = json.loads((root / "node-marker.json").read_text())
    assert marker["inherited"] == "inherited-without-rewrite"
    if case == "pass":
        assert ("# pass " if reporter == "tap" else "ℹ pass ") in run["tail"]

    assert body["runs"]["unit"] == ("pass" if failed == 0 else "fail")


@pytest.mark.parametrize("reporter", ["tap", "spec"])
def test_real_nested_and_color_counts_are_not_added(node_tools, tmp_path, reporter):
    source = _MARKER + """import test from 'node:test';
import assert from 'node:assert/strict';
test('parent', async t => {
  await t.test('nested child', () => assert.ok(true));
});
"""
    root = _package(tmp_path / "repo", f"node --test --test-reporter={reporter} test/nested.test.js",
                    {"test/nested.test.js": source})
    proc, body = _consumer_gate(root, env={"FORCE_COLOR": "1"})
    run = _node_run(body)
    assert proc.returncode == 0, body
    assert (run["passed"], run["failed"], run["evidence_complete"]) == (2, 0, True)
    if reporter == "spec":
        assert "\x1b[" in run["tail"], "raw reporter color must be retained"


@pytest.mark.parametrize("reporter", ["tap", "spec"])
@pytest.mark.parametrize("source", [
    "import test from 'node:test'; test.skip('not run', () => {});\n",
    "import test from 'node:test'; test.todo('not implemented');\n",
])
def test_real_skip_and_todo_are_not_passing_evidence(node_tools, tmp_path, reporter, source):
    root = _package(tmp_path / "repo", f"node --test --test-reporter={reporter} test/skip.test.js",
                    {"test/skip.test.js": source})
    proc, body = _consumer_gate(root)
    run = _node_run(body)
    assert proc.returncode == 1
    assert (run["returncode"], run["passed"], run["failed"], run["evidence_complete"]) == (0, 0, 0, True)
    assert any(f["severity"] == "major" and f["kind"] == "functional" for f in body["findings"])


@pytest.mark.parametrize("reporter", ["tap", "spec"])
def test_real_empty_discovery_fails_closed(node_tools, tmp_path, reporter):
    root = _package(tmp_path / "repo", f"node --test --test-reporter={reporter}")
    proc, body = _consumer_gate(root)
    run = _node_run(body)
    assert proc.returncode == 1
    assert (run["returncode"], run["passed"], run["failed"], run["evidence_complete"]) == (0, 0, 0, True)
    assert any(f["severity"] == "major" and f["kind"] == "functional" for f in body["findings"])


def test_real_node_with_controlled_silent_preload_has_no_evidence(node_tools, tmp_path):
    # Honest controlled fixture: preload suppresses both real Node reporter streams.
    root = _package(tmp_path / "repo", "node --test --test-reporter=tap test/pass.test.js", {
        "test/pass.test.js": _PASS,
        "silent.cjs": "process.stdout.write = () => true; process.stderr.write = () => true;\n",
    })
    proc, body = _consumer_gate(root, env={"NODE_OPTIONS": "--require ./silent.cjs"})
    run = _node_run(body)
    assert (proc.returncode, run["returncode"]) == (1, 0)
    assert (root / "node-marker.json").exists(), "the real test ran even though its reporter was suppressed"
    assert (run["passed"], run["failed"], run["evidence_complete"], run["tail"]) == (0, 0, False, "")
    assert any(f["severity"] == "major" and f["kind"] == "functional" for f in body["findings"])


@pytest.mark.parametrize("source", ["this is not valid javascript @\n", "throw new Error('runtime failure');\n"])
def test_real_syntax_and_runtime_failures(node_tools, tmp_path, source):
    root = _package(tmp_path / "repo", "node --test --test-reporter=tap test/bad.test.js",
                    {"test/bad.test.js": source})
    proc, body = _consumer_gate(root)
    run = _node_run(body)
    assert proc.returncode == 1
    assert run["returncode"] != 0
    assert run["passed"] == 0 and run["failed"] > 0
    assert body["runs"]["unit"] == "fail"


@pytest.mark.parametrize("compiler_fails", [False, True])
def test_literal_realm_chain_with_installed_typescript(node_tools, tmp_path, compiler_fails):
    if shutil.which("tsc") is None:
        pytest.skip("real compiler consumer requires installed tsc on PATH; no compiler downloaded")
    # Only the Node APIs this tiny consumer uses are declared locally. Strict
    # checking remains enabled, without downloading a Node type package.
    declarations = """declare module 'node:test' {
  export default function test(name: string, fn: () => void): void;
}
declare module 'node:assert/strict' {
  const assert: { equal(actual: unknown, expected: unknown): void };
  export default assert;
}
declare module 'node:fs' {
  export function writeFileSync(path: string, data: string): void;
}
"""
    source = """import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync } from 'node:fs';
writeFileSync('node-marker.json', 'true');
const actual: number = 2 + 2;
test('compiled assertion', () => assert.equal(actual, 4));
"""
    if compiler_fails:
        source += "const broken: number = 'not a number';\n"
    root = _package(tmp_path / "repo", 'tsc -p tsconfig.json && node --test "dist/test/**/*.test.js"', {
        "tsconfig.json": json.dumps({"compilerOptions": {
            "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext",
            "strict": True, "types": [], "rootDir": "src", "outDir": "dist", "noEmitOnError": True,
        }, "include": ["src/**/*.ts"]}),
        "src/node-apis.d.ts": declarations,
        "src/test/nested/compiled.test.ts": source,
    })
    proc, body = _consumer_gate(root)
    run = _node_run(body)
    if compiler_fails:
        assert proc.returncode == 1 and run["returncode"] != 0
        assert (run["passed"], run["failed"], run["evidence_complete"]) == (0, 0, False)
        assert not (root / "node-marker.json").exists()
        assert not (root / "dist").exists()
    else:
        assert (proc.returncode, run["returncode"], run["passed"], run["failed"]) == (0, 0, 1, 0)
        assert run["evidence_complete"] is True
        assert (root / "node-marker.json").exists()
        assert (root / "dist/test/nested/compiled.test.js").exists()


@pytest.mark.parametrize("reporter", ["tap", "spec"])
def test_real_bun_twin_passthrough_consumer(node_tools, tmp_path, reporter):
    root = _package(tmp_path / "repo", f"node --test --test-reporter={reporter} test/pass.test.js",
                    {"test/pass.test.js": _PASS})
    proc, body = _consumer_gate(root, bun=True)
    run = _node_run(body)
    assert (proc.returncode, run["returncode"], run["passed"], run["failed"]) == (0, 0, 1, 0)
    assert run["evidence_complete"] is True
    assert (root / "node-marker.json").exists()


@pytest.mark.parametrize("stdout,stderr,expected", [
    ("# tests 1\n# suites 0\n# pass 1\n# fail 0\n# duration_ms 3.2\n", "", (1, 0, True)),
    ("ℹ tests 2\nℹ pass 1\nℹ fail 1\nℹ duration_ms 3.2\n", "", (1, 1, True)),
    ("# pass 0\n# fail 0\n", "", (0, 0, True)),
    ("ok 1 - assertion\n", "", (0, 0, False)),
    ("    # pass 30\n    # fail 9\nok 1\n# pass 1\n# fail 0\n", "", (1, 0, True)),
    ("\x1b[32mℹ pass 2\x1b[39m\n\x1b[32mℹ fail 0\x1b[39m\n", "", (2, 0, True)),
    ("\x1b]0;title\x07# pass 2\n# fail 0\n", "", (2, 0, True)),
    ("# pass 1\n# fail 0\n\n# pass 7\n", "", (7, 0, False)),
    ("# pass 1\n# fail 0\n\n# fail 3\n", "", (0, 3, False)),
    ("# pass 1\n# fail 0\n\n# tests 8\n", "", (0, 0, False)),
    ("# pass 1\n# fail 0\n# pass 4\n", "", (4, 0, False)),
    ("# tests 1\n# pass 1\n# tests 2\n# fail 0\n", "", (0, 0, False)),
    ("# pass 1\nℹ fail 0\n", "", (0, 0, False)),
    ("# pass 1\n# fail 0\nℹ pass 2\n", "", (2, 0, False)),
    ("# pass 1\n# fail 0\n\nℹ pass 3\nℹ fail 0\n", "", (3, 0, True)),
    ("# pass 2\n# fail 0\n\n# pass 3\n# fail 1\n", "", (3, 1, True)),
    ("# pass 1\nnoise\n# fail 0\n", "", (0, 0, False)),
    ("prefix # pass 99\n# fail 0\n", "", (0, 0, False)),
    ("# pass 1 trailing\n# fail 0\n", "", (0, 0, False)),
    ("# pass 1\n", "# fail 0\n", (0, 0, False)),
    ("# pass 1\n# fail 0\n", "# pass 2\n# fail 0\n", (2, 0, False)),
    ("compiler chatter\n", "ℹ pass 2\nℹ fail 0\n", (2, 0, True)),
    ("", "", (0, 0, False)),
])
def test_synthetic_terminal_aggregate_contract(stdout, stderr, expected):
    assert _load_qa_gate()._node_test_evidence(stdout, stderr) == expected


@pytest.mark.parametrize("sample,passed,failed", [
    ("Tests: 4 passed\n", 4, 0),
    ("2 passing\n", 2, 0),
    ("2 passing\n3 failing\n", 2, 3),
    ("Tests: 4 passed\n2 failed\n", 4, 2),
])
def test_legacy_jest_mocha_parser_samples(node_tools, tmp_path, sample, passed, failed):
    # Pure parsing samples, not a claim to have executed Jest or Mocha.
    root = _package(tmp_path / "repo", "jest")
    qa = _load_qa_gate()
    runner, = qa.detect_runners(root)
    assert (qa._count(runner["pass_re"], sample), qa._count(runner["fail_re"], sample)) == (passed, failed)


@pytest.mark.parametrize("risk,expected_exit,severity", [("low", 0, "minor"), ("medium", 1, "major")])
def test_no_runner_risk_behavior_is_unchanged(tmp_path, risk, expected_exit, severity):
    root = tmp_path / "empty"
    root.mkdir()
    proc, body = _consumer_gate(root, risk=risk)
    assert proc.returncode == expected_exit
    assert body["executed"] == []
    assert body["runs"]["unit"] == "skipped:infra"
    assert body["findings"][0]["severity"] == severity


@pytest.mark.parametrize("risk", ["medium", "high"])
def test_node_adapter_preserves_required_tiers(node_tools, tmp_path, risk):
    root = _package(tmp_path / "repo", "node --test --test-reporter=tap test/pass.test.js",
                    {"test/pass.test.js": _PASS})
    proc, body = _consumer_gate(root, risk=risk)
    assert body["runs"]["unit"] == body["runs"]["integration"] == "pass"
    assert _node_run(body)["passed"] == 1
    if risk == "high":
        assert proc.returncode == 1
        assert body["runs"]["e2e"] == body["runs"]["perf"] == "skipped:infra"
        assert {f["kind"] for f in body["findings"]} == {"e2e", "perf"}
    else:
        assert proc.returncode == 0


def test_dry_run_does_not_launch_node_consumer(node_tools, tmp_path):
    root = _package(tmp_path / "repo", "node --test --test-reporter=tap test/pass.test.js",
                    {"test/pass.test.js": _PASS})
    proc, body = _consumer_gate(root, extra=("--dry-run",))
    assert proc.returncode == 0 and body["dry_run"] is True
    assert not (root / "node-marker.json").exists()
    assert not body.get("executed")


def test_real_direct_default_node_reporter(node_tools, tmp_path):
    root = _package(tmp_path / "repo", "node --test test/pass.test.js", {"test/pass.test.js": _PASS})
    proc, body = _consumer_gate(root)
    run = _node_run(body)
    assert (proc.returncode, run["returncode"], run["passed"], run["failed"]) == (0, 0, 1, 0)
    assert run["evidence_complete"] is True
    assert (root / "node-marker.json").exists()


def test_real_npm_lifecycle_nonzero_overrides_passing_node_aggregate(node_tools, tmp_path):
    root = _package(tmp_path / "repo", "node --test --test-reporter=tap test/pass.test.js", {
        "test/pass.test.js": _PASS,
        "posttest.cjs": "require('node:fs').writeFileSync('posttest-marker', 'ran'); process.exit(7);\n",
    })
    package = json.loads((root / "package.json").read_text())
    package["scripts"]["posttest"] = "node posttest.cjs"
    (root / "package.json").write_text(json.dumps(package))
    proc, body = _consumer_gate(root)
    run = _node_run(body)
    assert (proc.returncode, run["returncode"], run["passed"], run["failed"]) == (1, 7, 1, 0)
    assert run["evidence_complete"] is True
    assert body["runs"]["unit"] == "fail"
    assert (root / "node-marker.json").exists()
    assert (root / "posttest-marker").exists()


def test_runner_timeout_is_an_internal_error_not_a_verdict(tmp_path):
    # P4-19: a runner that outruns --timeout propagates as E-INTERNAL; the gate records no verdict.
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_sleep.py").write_text("import time\n\n\ndef test_slow():\n    time.sleep(60)\n")
    started = time.monotonic()
    proc = run_script("qa_gate.py", "--root", str(root), "--risk-class", "low", "--json", "--timeout", "1")
    assert time.monotonic() - started < 50, "the runner was not stopped at its deadline"
    body = json.loads(proc.stdout)
    assert (proc.returncode, body["status"], body["error"]["code"]) == (2, "error", "E-INTERNAL")
    assert "verdict" not in body

