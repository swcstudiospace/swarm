"""Parity of `qa_gate` on Python and on the host engine (phase 14.5, unit U3.6b).

No-runner and genuine npm/Node consumers exercise literal selection and aggregate
evidence. The Rust leg calls the host binary directly, so the Python selector
cannot silently replay it. Unset `GSD_ENGINE_BIN` skips. No fixture installs dependencies.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import _scrubbed_base_env

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "qa_gate.py"


def _binary() -> str:
    value = os.environ.get("GSD_ENGINE_BIN", "")
    if not value:
        pytest.skip("GSD_ENGINE_BIN is not set")
    if not os.access(value, os.X_OK):
        pytest.fail(f"GSD_ENGINE_BIN is not executable: {value}")
    return value


def _run(
    root: Path, engine: str, binary: str, extra: list[str], path: str, *, force_color: bool = False,
) -> subprocess.CompletedProcess[str]:
    env = _scrubbed_base_env()
    env["SWARM_DIR"] = str(root / ".swarm")
    env["SWARM_GSD_STATE_FILE"] = ""
    env["SWARM_CLAUDE_STATE_DIR"] = str(root / "claude")
    env["PATH"] = path
    if force_color:
        env["FORCE_COLOR"] = "1"
    argv = ["--json", "--root", str(root), "--risk-class", "low", "--timeout", "10", *extra]
    if engine == "rust":
        # Match the existing direct-host parity convention. In particular, do not
        # let conftest's SWARM_PURE_PYTHON setting silently turn this into Python.
        env["GSD_RUST_BACKEND"] = "host"
        request = {
            "agent": "A08", "script": "qa_gate", "argv": argv, "root": str(root),
            "env": {key: env[key] for key in (
                "SWARM_DIR", "SWARM_GSD_STATE_FILE", "SWARM_CLAUDE_STATE_DIR",
                "SWARM_AGENT_SESSION",
            ) if key in env},
        }
        return subprocess.run(
            [binary], input=json.dumps(request), capture_output=True, text=True,
            env=env, cwd=ROOT, timeout=60,
        )
    env["GSD_ENGINE"] = "python"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=60,
    )


def _comparable(body: dict) -> dict:
    return {
        "status": body.get("status"),
        "verdict": body.get("verdict"),
        "risk_class": body.get("risk_class"),
        "runs": body.get("runs"),
        "findings": [
            {key: item.get(key) for key in ("id", "severity", "kind")}
            for item in body.get("findings") or []
        ],
        "executed": [
            {key: item.get(key) for key in ("runner", "returncode", "passed", "failed", "evidence_complete")}
            for item in body.get("executed") or []
        ],
    }


def test_no_runner_matches_both_engines(tmp_path: Path) -> None:
    binary = _binary()
    bodies = []
    for engine in ("python", "rust"):
        root = tmp_path / engine
        root.mkdir()
        done = _run(root, engine, binary, [], os.environ.get("PATH", ""))
        body = json.loads(done.stdout)
        assert done.returncode == 0, (done.stdout, done.stderr)
        assert body["status"] == "ok" and body["verdict"] == "pass"
        assert body["executed"] == []
        assert body["runs"]["unit"] == "skipped:infra"
        assert len(body["findings"]) == 1
        assert body["findings"][0]["id"] == "QF-000"
        assert body["findings"][0]["severity"] == "minor"
        assert body["findings"][0]["kind"] == "coverage"
        if engine == "rust":
            assert body["script"] == "qa_gate" and body["agent"] == "A08"
        bodies.append(body)
    assert _comparable(bodies[0]) == _comparable(bodies[1])


_NODE_CASES = [
    # Independent oracle: case, passed, failed, process returncode, completeness.
    ("tap-pass", 1, 0, 0, True),
    ("spec-pass", 1, 0, 0, True),
    ("spec-colored", 1, 0, 0, True),
    ("tap-fail", 0, 1, 1, True),
    ("spec-fail", 0, 1, 1, True),
    ("tap-mixed", 1, 1, 1, True),
    ("spec-mixed", 1, 1, 1, True),
    ("tap-nested", 2, 0, 0, True),
    ("spec-nested", 2, 0, 0, True),
    ("reporter-before", 1, 0, 0, True),
    ("reporter-separated", 1, 0, 0, True),
    ("zero", 0, 0, 0, True),
    ("skip", 0, 0, 0, True),
    ("todo", 0, 0, 0, True),
    ("silent-preload", 0, 0, 0, False),
    ("syntax", 0, 1, 1, True),
    ("runtime", 0, 1, 1, True),
    ("printed-pass-nonzero", 1, 0, 7, True),
    ("realm-real-tsc", 1, 0, 0, True),
    ("realm-real-tsc-fail", 0, 1, 1, True),
    ("realm-compiler-short-circuit", 0, 0, 1, False),
]

_NODE_HEADER = (
    "const { test } = require('node:test');\n"
    "const assert = require('node:assert/strict');\n"
    "require('node:fs').appendFileSync('node-marker', 'traversed\\n');\n"
)


def _node_fixture(root: Path, case: str) -> None:
    """Real npm/Node consumers, with actual TypeScript for compiler-chain cases."""
    root.mkdir()
    source = "test('passing', () => assert.equal(1, 1));\n"
    if case.endswith("-fail"):
        source = "test('failing', () => assert.equal(1, 2));\n"
    elif case.endswith("-mixed"):
        source += "test('failing', () => assert.equal(1, 2));\n"
    elif case.endswith("-nested"):
        source = "test('outer', async t => { await t.test('inner', () => {}); });\n"
    elif case == "skip":
        source = "test.skip('skipped', () => assert.fail('must not run'));\n"
    elif case == "todo":
        source = "test.todo('not implemented');\n"
    elif case == "runtime":
        source = "throw new Error('intentional runtime failure');\n"
    elif case == "syntax":
        source = "const broken = ;\n"
    script = "node --test --test-reporter=tap fixture.test.cjs"
    if case.startswith("spec-"):
        script = "node --test --test-reporter=spec fixture.test.cjs"
    elif case == "reporter-before":
        script = "node --test-reporter spec --test fixture.test.cjs"
    elif case == "reporter-separated":
        script = "node --test --test-reporter tap fixture.test.cjs"
    if case == "zero":
        script = "node --test --test-reporter=tap"
    elif not case.startswith("realm-"):
        (root / "fixture.test.cjs").write_text(_NODE_HEADER + source, encoding="utf-8")
    scripts = {"test": script}
    if case == "silent-preload":
        # Honest output-free consumer: preload mutes writes, not the tests.
        (root / "silent.cjs").write_text(
            "process.stdout.write = process.stderr.write = () => true;\n", encoding="utf-8",
        )
        (root / ".npmrc").write_text("node-options=--require=./silent.cjs\n", encoding="utf-8")
    if case == "printed-pass-nonzero":
        (root / "posttest.cjs").write_text("process.exitCode = 7;\n", encoding="utf-8")
        scripts["posttest"] = "node posttest.cjs"
    if case.startswith("realm-"):
        # The exact production command, including quoted recursive glob.
        scripts["test"] = 'tsc -p tsconfig.json && node --test "dist/test/**/*.test.js"'
        src = root / "src" / "test"
        src.mkdir(parents=True)
        (root / "src" / "node-builtins.d.ts").write_text(
            'declare module "node:test" {\n'
            '  export function test(name: string, body: () => void): void;\n'
            '}\n'
            'declare module "node:assert/strict" {\n'
            '  export function equal(actual: unknown, expected: unknown): void;\n'
            '}\n'
            'declare module "node:fs" {\n'
            '  export function appendFileSync(path: string, data: string): void;\n'
            '}\n',
            encoding="utf-8",
        )
        typed_source = (
            'import { test } from "node:test";\n'
            'import { equal } from "node:assert/strict";\n'
            'import { appendFileSync } from "node:fs";\n'
            'appendFileSync("node-marker", "traversed\\n");\n'
        )
        if case == "realm-compiler-short-circuit":
            typed_source += 'const value: number = "type error";\n'
            typed_source += 'test("unreachable", () => equal(value, 1));\n'
        else:
            expected = 2 if case == "realm-real-tsc-fail" else 1
            typed_source += f'test("typed assertion", () => equal(1, {expected}));\n'
        (src / "realm.test.ts").write_text(typed_source, encoding="utf-8")
        (root / "tsconfig.json").write_text(json.dumps({
            "compilerOptions": {
                "target": "ES2022", "module": "NodeNext", "moduleResolution": "NodeNext",
                "strict": True, "noEmitOnError": True, "types": [],
                "rootDir": "src", "outDir": "dist",
            },
            "include": ["src/**/*.ts"],
        }), encoding="utf-8")
    (root / "package.json").write_text(json.dumps({"private": True, "scripts": scripts}), encoding="utf-8")


def _assert_node_outcome(
    done: subprocess.CompletedProcess[str], body: dict, case: str,
    passed: int, failed: int, returncode: int, complete: bool,
) -> None:
    """Assert the external oracle on each engine before comparing engines."""
    ok = returncode == 0 and complete and passed > 0 and failed == 0
    assert done.returncode == (0 if ok else 1), (case, done.stdout, done.stderr)
    assert body["status"] == ("ok" if ok else "fail"), (case, body)
    assert body["verdict"] == ("pass" if ok else "fail"), (case, body)
    assert body["runs"] == {
        "unit": "pass" if ok else "fail", "integration": "skipped:not-selected",
        "e2e": "skipped:not-selected", "perf": "skipped:not-selected",
    }
    assert len(body["executed"]) == 1, (case, body)
    run = body["executed"][0]
    assert run["runner"] == "npm test"
    assert (run["returncode"], run["passed"], run["failed"]) == (returncode, passed, failed), (case, run)
    assert run["evidence_complete"] is complete, (case, run)
    if ok:
        assert body["findings"] == []
    else:
        assert len(body["findings"]) == 1, (case, body)
        finding = body["findings"][0]
        assert (finding["severity"], finding["kind"]) == ("major", "functional")
    if case == "spec-colored":
        assert "\x1b[" in run["tail"], "normalization must not change raw evidence"
    if case == "silent-preload":
        assert run["tail"] == "", "the real runner was deliberately output-free"


@pytest.mark.parametrize(
    "case,passed,failed,returncode,complete", _NODE_CASES, ids=[row[0] for row in _NODE_CASES],
)
def test_genuine_npm_node_consumers_match_both_engines(
    tmp_path: Path, case: str, passed: int, failed: int, returncode: int, complete: bool,
) -> None:
    binary = _binary()
    tools = ("npm", "node", "tsc") if case.startswith("realm-") else ("npm", "node")
    for tool in tools:
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    bodies = []
    for engine in ("python", "rust"):
        root = tmp_path / engine
        _node_fixture(root, case)
        done = _run(
            root, engine, binary, [], os.environ.get("PATH", ""),
            force_color=case == "spec-colored",
        )
        body = json.loads(done.stdout)
        _assert_node_outcome(done, body, case, passed, failed, returncode, complete)
        if engine == "rust":
            assert body["script"] == "qa_gate" and body["agent"] == "A08", "host must answer, not decline"
        marker = root / "node-marker"
        should_traverse = case not in ("zero", "syntax", "realm-compiler-short-circuit")
        assert marker.exists() is should_traverse, (case, engine, body)
        if should_traverse:
            assert marker.read_text(encoding="utf-8") == "traversed\n", "no hidden replay"
        if case.startswith("realm-"):
            emitted = root / "dist" / "test" / "realm.test.js"
            assert emitted.exists() is (case != "realm-compiler-short-circuit"), (case, engine)
            if case == "realm-compiler-short-circuit":
                assert "TS2322" in body["executed"][0]["tail"], "real compiler must reject the type mismatch"
        bodies.append(body)
    assert _comparable(bodies[0]) == _comparable(bodies[1]), case


