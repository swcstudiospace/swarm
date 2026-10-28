"""Policy for the GitHub workflows (Phase 14.2): every workflow runs on the Omega CI runner pool with a read-only
token, a bounded job time and no swallowed failures, except the one declared GitHub-hosted exception (D-P22).

A small checker works on the parsed YAML (and the raw text for the exception marker). The real workflows must pass
it, and every rule is proven to bite: a mutated deep copy of the real document must fail with that rule's violation.
"""

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

POOL = ["self-hosted", "linux", "x64", "omega-ci"]
HOSTED = "ubuntu-latest"
EXCEPTION_MARKER = "GitHub-hosted exception (D-P22)"
GUARD = "github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository"
MAX_TIMEOUT_MINUTES = 120
HOST_FACTS = "scripts/ci_host_facts.sh"
REQUIRED_CI_JOBS = ["test", "rust", "clippy", "parity-rust", "browser", "supply-chain"]
BROWSER_SKIP = "vars.BROWSER_GATE != 'skip'"
SCRIPT_REF = re.compile(r"(?<![\w./-])scripts/[\w./-]*\w")

Document = dict[str, Any]


def check_workflow(path: Path, text: str, document: Document) -> list[str]:
    """Return one short violation per broken rule, each naming the workflow, the job (if any) and the rule."""
    name = path.name
    violations: list[str] = []
    jobs: dict[str, Any] = document.get("jobs") or {}

    if document.get("permissions") != {"contents": "read"}:
        violations.append(f"{name}: permissions: top-level permissions must equal {{contents: read}}")

    hosted_exception = name == "wheel.yml" and EXCEPTION_MARKER in text and all(job.get("runs-on") == HOSTED for job in jobs.values())

    for job_name, job in jobs.items():
        where = f"{name}: job {job_name}"
        steps: list[dict[str, Any]] = job.get("steps") or []

        timeout = job.get("timeout-minutes")
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= MAX_TIMEOUT_MINUTES:
            violations.append(f"{where}: timeout: timeout-minutes must be an integer from 1 to {MAX_TIMEOUT_MINUTES}, got {timeout!r}")
        if "continue-on-error" in job or any("continue-on-error" in step for step in steps):
            violations.append(f"{where}: continue-on-error: failures must not be swallowed")

        exempt = hosted_exception and job.get("runs-on") == HOSTED
        if not exempt and job.get("runs-on") != POOL:
            reason = f"runs-on must equal {POOL}, got {job.get('runs-on')!r}"
            if job.get("runs-on") == HOSTED:
                reason += f" ({HOSTED} needs wheel.yml, the marker {EXCEPTION_MARKER!r} and every job of the file on it)"
            violations.append(f"{where}: runs-on: {reason}")
        if exempt:
            continue

        if GUARD not in str(job.get("if", "")):
            violations.append(f"{where}: guard: the job's if must contain the fork guard")
        first = str(steps[0].get("uses", "")) if steps else ""
        second = str(steps[1].get("run", "")) if len(steps) > 1 else ""
        if not first.startswith("actions/checkout") or HOST_FACTS not in second:
            violations.append(f"{where}: pool-steps: step 0 must be actions/checkout and step 1 must run {HOST_FACTS}")

    if name == "ci.yml":
        missing = [job for job in REQUIRED_CI_JOBS if job not in jobs]
        if missing:
            violations.append(f"{name}: required-jobs: missing jobs {missing}")
        if "browser" in jobs and BROWSER_SKIP not in str(jobs["browser"].get("if", "")):
            violations.append(f"{name}: job browser: browser-gate: the if must contain {BROWSER_SKIP}")

    for job_name, job in jobs.items():
        for step in job.get("steps") or []:
            run = step.get("run")
            for token in sorted(set(SCRIPT_REF.findall(run if isinstance(run, str) else ""))):
                if not (ROOT / token).is_file():
                    violations.append(f"{name}: job {job_name}: script-ref: {token} does not exist")
    return violations


def _load(path: Path) -> tuple[str, Document]:
    text = path.read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


@pytest.fixture(scope="module")
def real() -> dict[str, tuple[str, Document]]:
    return {path.name: _load(path) for path in sorted(WORKFLOWS.glob("*.yml"))}


def test_the_expected_workflows_exist(real: dict[str, tuple[str, Document]]) -> None:
    assert {"ci.yml", "wheel.yml"} <= set(real)


@pytest.mark.parametrize("filename", sorted(path.name for path in WORKFLOWS.glob("*.yml")))
def test_real_workflow_passes_the_policy(filename: str, real: dict[str, tuple[str, Document]]) -> None:
    text, document = real[filename]
    assert check_workflow(WORKFLOWS / filename, text, document) == []


@dataclass(frozen=True)
class Mutation:
    """One way to break the policy: a change to a deep copy of a real workflow, and the rule that must report it."""

    label: str
    source: str
    rule: str
    mutate: Callable[[Document], None] | None = None
    strip_marker: bool = False
    rename_to: str | None = None


def _swap_first_two_steps(document: Document) -> None:
    steps = document["jobs"]["rust"]["steps"]
    steps[0], steps[1] = steps[1], steps[0]


def _drop_host_facts(document: Document) -> None:
    steps = document["jobs"]["clippy"]["steps"]
    document["jobs"]["clippy"]["steps"] = [step for step in steps if HOST_FACTS not in str(step.get("run", ""))]


def _stale_script_reference(document: Document) -> None:
    document["jobs"]["rust"]["steps"][3]["run"] = "scripts/does_not_exist.sh"


MUTATIONS = [
    Mutation("guard dropped from one job", "ci.yml", "guard", lambda d: d["jobs"]["clippy"].pop("if")),
    Mutation("guard replaced by an unrelated condition", "ci.yml", "guard", lambda d: d["jobs"]["rust"].update({"if": "always()"})),
    Mutation("pool job moved to ubuntu-latest", "ci.yml", "runs-on", lambda d: d["jobs"]["test"].update({"runs-on": HOSTED})),
    Mutation("pool label list shortened", "ci.yml", "runs-on", lambda d: d["jobs"]["test"].update({"runs-on": POOL[:3]})),
    Mutation("one job of wheel.yml leaves ubuntu-latest", "wheel.yml", "runs-on", lambda d: d["jobs"]["wheel"].update({"runs-on": POOL})),
    Mutation("wheel.yml marker removed from the text", "wheel.yml", "runs-on", strip_marker=True),
    Mutation("ubuntu-latest workflow not named wheel.yml", "wheel.yml", "runs-on", rename_to="other.yml"),
    Mutation(
        "continue-on-error on a step",
        "ci.yml",
        "continue-on-error",
        lambda d: d["jobs"]["rust"]["steps"][3].update({"continue-on-error": True}),
    ),
    Mutation("continue-on-error on a job", "ci.yml", "continue-on-error", lambda d: d["jobs"]["rust"].update({"continue-on-error": False})),
    Mutation("timeout-minutes 121", "ci.yml", "timeout", lambda d: d["jobs"]["test"].update({"timeout-minutes": 121})),
    Mutation("timeout-minutes removed", "ci.yml", "timeout", lambda d: d["jobs"]["supply-chain"].pop("timeout-minutes")),
    Mutation("timeout-minutes not an integer", "wheel.yml", "timeout", lambda d: d["jobs"]["wheel"].update({"timeout-minutes": "30"})),
    Mutation("host-facts step removed", "ci.yml", "pool-steps", _drop_host_facts),
    Mutation("steps 0 and 1 swapped", "ci.yml", "pool-steps", _swap_first_two_steps),
    Mutation("stale scripts/ reference", "ci.yml", "script-ref", _stale_script_reference),
    Mutation(
        "stale scripts/ reference in wheel.yml",
        "wheel.yml",
        "script-ref",
        lambda d: d["jobs"]["wheel"]["steps"][5].update({"run": "scripts/gone.sh dist"}),
    ),
    Mutation("contents: read permission removed", "ci.yml", "permissions", lambda d: d["permissions"].pop("contents")),
    Mutation("write permission", "wheel.yml", "permissions", lambda d: d.update({"permissions": {"contents": "write"}})),
    Mutation("required job renamed", "ci.yml", "required-jobs", lambda d: d["jobs"].update({"lint": d["jobs"].pop("clippy")})),
    Mutation("browser skip switch removed", "ci.yml", "browser-gate", lambda d: d["jobs"]["browser"].update({"if": GUARD})),
]


@pytest.mark.parametrize("mutation", MUTATIONS, ids=[mutation.label for mutation in MUTATIONS])
def test_each_rule_reports_a_broken_workflow(mutation: Mutation, real: dict[str, tuple[str, Document]]) -> None:
    text, original = real[mutation.source]
    assert check_workflow(WORKFLOWS / mutation.source, text, original) == []

    document = copy.deepcopy(original)
    if mutation.mutate is not None:
        mutation.mutate(document)
    mutated_text = text.replace(EXCEPTION_MARKER, "") if mutation.strip_marker else text
    assert document != original or mutated_text != text or mutation.rename_to is not None

    violations = check_workflow(WORKFLOWS / (mutation.rename_to or mutation.source), mutated_text, document)
    assert any(f": {mutation.rule}:" in violation for violation in violations), violations


def test_the_violation_names_workflow_and_job() -> None:
    text, document = _load(WORKFLOWS / "ci.yml")
    del document["jobs"]["rust"]["timeout-minutes"]
    violations = check_workflow(WORKFLOWS / "ci.yml", text, document)
    assert violations == ["ci.yml: job rust: timeout: timeout-minutes must be an integer from 1 to 120, got None"]
