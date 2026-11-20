"""A05 — GSD quality-gate read API (API-GsdQualityGate 1.0.0).

Contract: ``contracts/gsd-quality-gate.yaml`` (A03, sha256
0c9a841f…4e6197b6966466fab63249329de0ccafac16bc955b6b4ba70).
Six read-only operations, implemented here as framework-free handlers
plus a tiny WSGI adapter (stdlib only; ADR-0005: no new dependencies).

Every handler is read-only and derives its answer from the repository
state under one root directory (by default the checkout that owns this
file).  Handlers never invent verdicts:

* ``GET /quality/gates/{gateId}`` — one of ``lint-scan``,
  ``sweep-report``, ``ci-gate``.  A recorded verdict file
  ``docs/gates/<gateId>.json`` wins when present; otherwise the gate
  is reported ``pending`` (no verdict has been recorded yet).
* ``GET /quality/parity/{module}`` — the Rust core is admitted only
  with parity evidence (ADR-0001).  A ``tests/parity/records/<module>/parity.json``
  record wins when present; otherwise the module is ``blocked``.
* ``GET /quality/sweep/{sweepId}`` — counts tracked ``swarm/*.py``
  sources against those with a tests/parity counterpart.
* ``GET /quality/wip/inventory`` — untracked quality tests in the
  dirty tree (ADR-0003), from ``git status --porcelain``.
* ``GET /quality/ci/{prNumber}`` — ``all_green`` is true only when a
  recorded verdict ``docs/gate-verdicts/pr-<n>.json`` says so.
* ``GET /quality/engine`` — always ``python`` (AC-6/AC-16); the
  optional ``approval_record`` names a flip-approval file when one
  exists.  This module never flips an engine.

The oracle never changes: ``x-engine-default: python``.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "Router",
    "router",
    "handle",
    "application",
    "default_root",
    "main",
]

GATES = ("lint-scan", "sweep-report", "ci-gate")

Handler = Callable[..., tuple[int, dict[str, Any]]]

# Untracked quality-test patterns in the dirty tree (ADR-0003).
_WIP_PATTERNS = (
    re.compile(r"^tests/test_quality_[^/]+\.py$"),
    re.compile(r"^tests/test_quality_manifests\.py$"),
    re.compile(r"^crates/[^/]+/tests/quality_[^/]+\.rs$"),
    re.compile(r"^web/quality-[^/]+\.test\.mjs$"),
    re.compile(r"^omp/test/quality-[^/]+\.test\.ts$"),
)


class Router:
    """Minimal ``@router.get(path)`` registry, one per HTTP method."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Handler]] = []

    def _add(self, method: str, path: str) -> Callable[[Handler], Handler]:
        def decorator(func: Handler) -> Handler:
            self.routes.append((method, path, func))
            return func

        return decorator

    def get(self, path: str) -> Callable[[Handler], Handler]:
        return self._add("GET", path)

    def match(self, method: str, path: str) -> tuple[Handler | None, dict[str, str]]:
        """Return (handler, params) for the first matching route, else (None, {})."""
        for route_method, template, handler in self.routes:
            if route_method != method.upper():
                continue
            params = _match_template(template, path)
            if params is not None:
                return handler, params
        return None, {}

    def path_params(self, template: str) -> list[str]:
        return re.findall(r"\{(\w+)\}", template)


def _match_template(template: str, path: str) -> dict[str, str] | None:
    template_parts = [p for p in template.split("/") if p]
    path_parts = [p for p in path.split("/") if p]
    if len(template_parts) != len(path_parts):
        return None
    params: dict[str, str] = {}
    for want, got in zip(template_parts, path_parts):
        m = re.fullmatch(r"\{(\w+)\}", want)
        if m:
            params[m.group(1)] = got
        elif want != got:
            return None
    return params


router = Router()


def default_root() -> Path:
    """The checkout that owns this package."""
    return Path(__file__).resolve().parent.parent


# --- derived repo state -------------------------------------------------

def _git_porcelain(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return ""
    return out.stdout or ""


def _tracked_py(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "swarm/*.py"],
            capture_output=True,
            text=True,
            check=False,
        )
        names = [line for line in (out.stdout or "").splitlines() if line]
        if names:
            return names
    except (OSError, ValueError):
        pass
    return sorted(str(p.relative_to(root)) for p in (root / "swarm").glob("*.py"))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _is_quality_test(path: str) -> bool:
    return any(pat.match(path) for pat in _WIP_PATTERNS)


def _problem(status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
    return status, {"code": code, "message": message}


# --- handlers -----------------------------------------------------------

@router.get("/quality/gates/{gateId}")
def get_quality_gate(root: Path, gateId: str) -> tuple[int, dict[str, Any]]:
    """AC-11/AC-12/AC-15 gate verdict; pending when nothing is recorded."""
    if gateId not in GATES:
        return _problem(404, "unknown_gate", f"no quality gate named {gateId!r}")
    recorded = _read_json(root / "docs" / "gates" / f"{gateId}.json")
    if isinstance(recorded, dict) and recorded.get("gate_id") == gateId:
        verdict = {
            "gate_id": gateId,
            "status": recorded.get("status", "pending"),
            "high_severity_findings": int(recorded.get("high_severity_findings", 0)),
        }
        for key in ("credential_findings", "secret_findings"):
            if key in recorded:
                verdict[key] = int(recorded[key])
        if verdict["status"] in ("passed", "failed", "pending"):
            return 200, verdict
    return 200, {"gate_id": gateId, "status": "pending", "high_severity_findings": 0,
                 "credential_findings": 0, "secret_findings": 0}


@router.get("/quality/parity/{module}")
def get_parity_status(root: Path, module: str) -> tuple[int, dict[str, Any]]:
    """AC-5 parity vs the Python oracle; blocked without evidence."""
    record_dir = root / "tests" / "parity" / "records" / module
    test_file = root / "tests" / "parity" / f"test_{module}_parity.py"
    if not record_dir.is_dir() and not test_file.is_file():
        return _problem(404, "unknown_module", f"no parity module named {module!r}")
    recorded = _read_json(record_dir / "parity.json") if record_dir.is_dir() else None
    if isinstance(recorded, dict):
        return 200, {
            "module": module,
            "oracle": "python",
            "divergence_cases": int(recorded.get("divergence_cases", 0)),
            "verdict": recorded.get("verdict", "blocked"),
        }
    return 200, {"module": module, "oracle": "python", "divergence_cases": 0, "verdict": "blocked"}


@router.get("/quality/sweep/{sweepId}")
def get_sweep_status(root: Path, sweepId: str) -> tuple[int, dict[str, Any]]:
    """AC-11/AC-12: fraction of tracked swarm sources with test counterpart."""
    sources = _tracked_py(root)
    tests_dir = root / "tests"
    parity_dir = tests_dir / "parity"
    with_verdicts = 0
    for rel in sources:
        stem = Path(rel).stem
        candidates = (
            tests_dir / f"test_{stem}.py",
            tests_dir / f"test_quality_{stem}.py",
            parity_dir / f"test_{stem}_parity.py",
        )
        # a <stem> module counts when any test file names it or a parity test exists for it
        if any(c.is_file() for c in candidates) or any(parity_dir.glob(f"test_*{stem}*.py")):
            with_verdicts += 1
    return 200, {
        "sweep_id": sweepId,
        "paths_with_verdicts": with_verdicts,
        "paths_total": len(sources),
    }


@router.get("/quality/wip/inventory")
def get_wip_inventory(root: Path) -> tuple[int, dict[str, Any]]:
    """AC-1/ADR-0003: inventory untracked quality tests in the dirty tree."""
    files: list[dict[str, Any]] = []
    for line in _git_porcelain(root).splitlines():
        if not line.startswith("?? "):
            continue
        path = line[3:].strip()
        if _is_quality_test(path):
            files.append({"path": path, "verdict": "untracked", "recovered": False})
    files.sort(key=lambda item: item["path"])
    return 200, {"files": files}


@router.get("/quality/ci/{prNumber}")
def get_ci_verdict(root: Path, prNumber: str) -> tuple[int, dict[str, Any]]:
    """AC-15: all-green only from a recorded verdict (fail closed)."""
    if not prNumber.isdigit():
        return _problem(404, "unknown_pr", f"no CI verdict for PR {prNumber!r}")
    number = int(prNumber)
    recorded = _read_json(root / "docs" / "gate-verdicts" / f"pr-{number}.json")
    if isinstance(recorded, dict) and recorded.get("pr_number") == number:
        verdict: dict[str, Any] = {"pr_number": number, "all_green": recorded.get("all_green") is True}
        if "credential_findings" in recorded:
            verdict["credential_findings"] = int(recorded["credential_findings"])
        return 200, verdict
    return 200, {"pr_number": number, "all_green": False}


@router.get("/quality/engine")
def get_engine_default(root: Path) -> tuple[int, dict[str, Any]]:
    """AC-6/AC-16: the default engine is python; never flips here."""
    approval = root / "docs" / "approvals" / "engine-flip.md"
    return 200, {
        "engine": "python",
        "approval_record": str(approval.relative_to(root)) if approval.is_file() else None,
    }


# --- dispatch + WSGI --------------------------------------------------

def handle(method: str, path: str, root: Path | None = None) -> tuple[int, dict[str, Any]]:
    """Route one request to its handler; 404 Problem when nothing matches."""
    base = root if root is not None else default_root()
    handler, params = router.match(method, path.split("?")[0])
    if handler is None:
        return _problem(404, "not_found", f"no route for {method.upper()} {path}")
    return handler(base, **params)


def application(environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
    """WSGI entry point (``wsgiref.simple_server`` compatible)."""
    method = environ.get("REQUEST_METHOD", "GET")
    path = environ.get("PATH_INFO", "/")
    status, payload = handle(method, path)
    body = json.dumps(payload).encode("utf-8")
    reason = {200: "OK", 404: "Not Found"}.get(status, "Error")
    start_response(f"{status} {reason}", [("Content-Type", "application/json"),
                                          ("Content-Length", str(len(body)))])
    return [body]


def main() -> None:
    from wsgiref.simple_server import make_server

    print("quality-gate API on http://127.0.0.1:8080/v1")
    make_server("127.0.0.1", 8080, application).serve_forever()


if __name__ == "__main__":
    main()
