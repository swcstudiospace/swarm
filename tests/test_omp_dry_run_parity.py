"""G1 (Phase 6, RUN-01/SC1, D-04): the invocation `--dry-run` prints is the one the live runner spawns — argv, env
deltas and unsets, cwd and stdin — compared across two sibling stores with the same one-task plan."""
import json
import re
import shlex
from pathlib import Path

from conftest import ROOT, _scrubbed_base_env, omp_calls, run_script, stub_omp

KEY_VARS = ("SWARM_SIGNING_KEY", "SWARM_ED25519_KEY", "SWARM_ED25519_KEY_FILE", "SWARM_REQUIRE_KEY", "SWARM_ALLOW_INSECURE_DEV_KEY")
# T-06-19: the dry-run line also unsets the runner's task and correlation ids, matching the live child env.
# U3.4e: the child strip also drops the insecure-dev-key opt-in, so a controller that has it set cannot hand it to a session.
SESSION_UNSET = (*KEY_VARS, "SWARM_TASK_ID", "SWARM_CORRELATION_ID")
KEYS = {"SWARM_SIGNING_KEY": "runner-secret", "SWARM_ED25519_KEY": "11" * 32, "SWARM_REQUIRE_KEY": "1"}
RESULT = {"state": "IN_REVIEW", "outputs": [{"kind": "code.backend", "uri": "file://x", "version": "1", "digest": ""}],
          "metrics": {}, "summary_md": "ok"}
PLAN = {"tasks": [{"id": "one", "capability": "code.backend", "agent": "A05", "title": "one", "depends_on": [], "gates": []}]}
ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _store(base: Path) -> tuple[dict, Path]:
    """A planned one-task store under base/.swarm for repo base/work; the runner env (with the runner's keys)."""
    base.mkdir()
    env = {**_scrubbed_base_env(), "SWARM_DIR": str(base / ".swarm"), **KEYS,
           "SWARM_ED25519_KEY_FILE": str(base / "private-test-reference.hex")}
    work = base / "work"
    work.mkdir()
    plan = base / "plan.json"
    plan.write_text(json.dumps(PLAN))
    r = run_script("orch_plan.py", "--plan", str(plan), "--prefix", "T", "--repo", str(work), "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return env, work


def _parse(line: str) -> tuple[list[str], dict, list[str], str]:
    """`env -u K … K=V … argv… < stdin` → (unset keys, env deltas, argv, stdin path)."""
    tokens = shlex.split(line)
    assert tokens[0] == "env" and tokens[-2] == "<"
    i, unset, deltas = 1, [], {}
    while tokens[i] == "-u":
        unset.append(tokens[i + 1])
        i += 2
    while ASSIGN.match(tokens[i]):
        k, v = tokens[i].split("=", 1)
        deltas[k] = v
        i += 1
    return unset, deltas, tokens[i:-2], tokens[-1]


def test_dry_run_invocation_matches_live_omp_call(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    env_a, work_a = _store(a)
    env_b, work_b = _store(b)
    stub = stub_omp(tmp_path, RESULT)
    flags = ["--runtime", "omp", "--omp-bin", str(stub), "--once", "--json"]

    r = run_script("swarm_run.py", *flags, "--dry-run", "--repo", str(work_a), env=env_a)
    assert r.returncode == 0, r.stdout + r.stderr
    assert omp_calls(stub) == []  # dry-run spawns nothing
    (line,) = [ln.removeprefix("dry-run T-one [A05]: ") for ln in r.stderr.splitlines() if ln.startswith("dry-run T-one [A05]: ")]
    # the only difference between the stores is their root: a/ → b/
    unset, deltas, argv, stdin = _parse(line.replace(str(a), str(b)))

    r = run_script("swarm_run.py", *flags, "--repo", str(work_b), env=env_b)
    assert r.returncode == 0, r.stdout + r.stderr
    (call,) = omp_calls(stub)

    assert argv == call["argv"]
    assert {k: call["env"].get(k) for k in deltas} == deltas
    assert sorted(unset) == sorted(SESSION_UNSET)
    assert not set(unset) & set(call["env"])  # the runner had them all; the child has none
    assert env_b["SWARM_ED25519_KEY_FILE"] not in " ".join(call["argv"]) + call["stdin"]
    assert "--no-extensions" not in argv
    assert argv[argv.index("--approval-mode") + 1] == "write"
    assert Path(argv[argv.index("--cwd") + 1]).resolve() == Path(call["cwd"]).resolve() == work_b.resolve()
    assert Path(stdin) == b / ".swarm" / "assignments" / "T-one.a1.md"
    assert Path(stdin).read_text() == call["stdin"]
    assert argv[argv.index("-e") + 1] == str(ROOT / "omp")
